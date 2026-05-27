"""codex_imagen._chroma — Pillow chroma-key pipeline (solid backdrop -> alpha).

Purpose
-------
The Codex OAuth bridge does NOT honor ``background: "transparent"`` — the
server-side validator rejects the request with HTTP 400 (verified by live
probes in ``probe_validate.py``). The only way to deliver a PNG with a real
alpha channel through that bridge is to:

1. Ask the model to render the subject on a solid, very-saturated backdrop
   (default: green ``#00FF00`` — chosen because it is the industry-standard
   chroma-key color and rarely appears in subjects like skin tones, products,
   or illustrative elements; switch to magenta ``#FF00FF`` for green subjects).
2. Download the resulting opaque PNG.
3. Run :func:`keyout` here to replace the backdrop color with ``alpha = 0``.

This module is bridge-agnostic. It speaks Pillow (with an optional NumPy
fast path) and nothing else — it must not import the bridge, prompt
builder, or any other imagen module.

Algorithm summary (upgraded)
-----------------------------
1. **Auto-key border sampling** (``auto_key`` parameter): sample the actual
   rendered key color from the image border (corners patch or full border
   band). This handles model drift where the rendered background color
   deviates from the exact hex requested.

2. **Dual-threshold soft matte**: pixels with channel-distance ≤
   ``transparent_threshold`` → alpha=0; ≥ ``opaque_threshold`` → alpha=255;
   in between → smoothstep curve. Defaults (12, 220) match the OpenAI
   imagegen skill's tested values.

3. **Key-channel dominance**: partial-alpha detection is augmented by
   checking whether the key channel(s) dominate — catches pixels that look
   key-colored even at moderate distance.

4. **Dominance-capping despill** (``despill_mode="dominance"``): for
   partial-alpha pixels, each spill channel is capped to ≤ max(non-spill
   channels) - 1. Physically correct — prevents spill channels from being
   brighter than the subject's actual lightness. The legacy full-projection
   subtraction is still accessible via ``despill_mode="projection"``.

5. **Alpha noise floor**: partial-alpha pixels below ``ALPHA_NOISE_FLOOR``
   (8) are clamped to zero to suppress near-transparent speckling.

Performance
-----------
A 1024x1024 RGB image has ~1M pixels. Pure-Pillow ``ImageMath`` and band
arithmetic are workable, but NumPy vectorization is roughly an order of
magnitude faster. We try to import NumPy lazily and fall back to a
pure-Pillow path if it's not installed.

Attribution
-----------
The dual-threshold soft matte, auto-key border sampling, dominance-capping
despill, and key-channel dominance heuristics are inspired by the algorithm
in OpenAI's native Codex ``imagegen`` skill
(``~/.codex/skills/.system/imagegen/scripts/remove_chroma_key.py``),
used under the Apache License 2.0. All code here is an independent
reimplementation. See NOTICE.md for the full attribution.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image as PILImage


# ---------------------------------------------------------------------------
# Constants / presets
# ---------------------------------------------------------------------------

# Maximum possible Euclidean distance between two RGB triples (sqrt(3) * 255).
# Used to map the 0-100 ``tolerance`` slider into actual pixel-distance radii.
_MAX_RGB_DIST: float = (3.0 ** 0.5) * 255.0  # ~441.673

#: Common chroma-key presets. Exposed so the CLI / API can map a friendly
#: string ("green") to the actual RGB triple without re-parsing.
KEY_PRESETS: dict[str, tuple[int, int, int]] = {
    "magenta": (255, 0, 255),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "cyan": (0, 255, 255),
    "yellow": (255, 255, 0),
}

#: Default key color. Green (#00FF00) is the industry-standard chroma-key
#: color and works well for most subjects (products, animals, people).
#: Switch to magenta (#FF00FF) only when the subject is green.
DEFAULT_KEY_RGB: tuple[int, int, int] = (0, 255, 0)

#: Default tolerance (0-100). 40 works well for synthetic AI imagery on a
#: solid backdrop — wide enough to absorb JPEG-ish artifacts but tight enough
#: not to nibble at subject edges.
DEFAULT_TOLERANCE: int = 40

#: Default Gaussian-blur radius applied to the alpha channel only.
DEFAULT_FEATHER_PX: int = 2

#: Dual-threshold soft matte: pixels at or below this distance are
#: fully transparent (alpha=0).
DEFAULT_TRANSPARENT_THRESHOLD: float = 12.0

#: Dual-threshold soft matte: pixels at or above this distance are
#: fully opaque (alpha=255).
DEFAULT_OPAQUE_THRESHOLD: float = 220.0

#: Alpha noise floor: partial-alpha pixels at or below this value are
#: clamped to 0, suppressing near-transparent speckling artifacts.
ALPHA_NOISE_FLOOR: int = 8

#: Dominance threshold: a pixel "looks key-colored" if the key channel(s)
#: dominate the non-key channels by at least this many units.
KEY_DOMINANCE_THRESHOLD: float = 16.0

#: Keywords that indicate a complex subject where chroma-key may produce
#: fringe artifacts (fur, hair, glass, smoke, liquids, translucent surfaces).
COMPLEX_SUBJECT_KEYWORDS: frozenset[str] = frozenset({
    "fur",
    "hair",
    "feather",
    "feathers",
    "smoke",
    "glass",
    "liquid",
    "translucent",
    "reflective",
    "crystal",
    "mist",
    "steam",
    "transparent",
})

COMPLEX_SUBJECT_WARNING = (
    "subject likely contains complex edges (fur/hair/glass/etc) — "
    "chroma-key fringe may be visible at semi-transparent edges. For "
    "perfect alpha, consider a model with native transparency support."
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChromaResult:
    """Outcome of a single :func:`keyout` pass.

    Attributes:
        src_path: Path to the source image that was read.
        dst_path: Path to the RGBA PNG that was written.
        key_rgb: The (R, G, B) key color that was matched against (may differ
            from the requested color if ``auto_key`` sampled the border).
        effective_key_rgb: The actual key color used (post border-sampling),
            always equal to key_rgb.
        tolerance: The 0-100 tolerance value used.
        despill_applied: Whether despill was applied (i.e. user requested it
            AND at least one partial-alpha pixel was present).
        feather_px: The Gaussian blur radius used on the alpha channel
            (0 means no feather).
        pixels_total: Total number of pixels in the image.
        pixels_keyed_fully: Pixels whose alpha is ``0`` after the pass.
        pixels_keyed_partial: Pixels whose alpha is strictly between ``0``
            and ``255`` after the pass.
        pixels_kept: Pixels whose alpha is ``255`` after the pass.
        elapsed_ms: Wall-clock milliseconds spent inside :func:`keyout`.
        warnings: Non-fatal diagnostic messages (e.g. complex-subject hint).
    """

    src_path: Path
    dst_path: Path
    key_rgb: tuple[int, int, int]
    effective_key_rgb: tuple[int, int, int]
    tolerance: int
    despill_applied: bool
    feather_px: int
    pixels_total: int
    pixels_keyed_fully: int
    pixels_keyed_partial: int
    pixels_kept: int
    elapsed_ms: int
    warnings: tuple[str, ...]


# ---------------------------------------------------------------------------
# Pillow import helper — Pillow is a hard dep, but tests want to be able to
# simulate "Pillow missing" to verify the error message.
# ---------------------------------------------------------------------------


def _import_pillow():  # type: ignore[no-untyped-def]
    """Import Pillow modules lazily and turn ImportError into an actionable one.

    Returns a tuple ``(Image, ImageFilter)``. We isolate the import in one
    helper so it can be monkeypatched in tests and so the error message is
    consistent across every public function.
    """

    try:
        from PIL import Image, ImageFilter
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ImportError(
            "Pillow is required for the chroma-key pipeline. "
            "Install it with: pip install Pillow"
        ) from exc
    return Image, ImageFilter


def _try_import_numpy():  # type: ignore[no-untyped-def]
    """Return the ``numpy`` module if installed, else ``None``.

    NumPy is optional — it just makes the per-pixel math an order of
    magnitude faster on 1024x1024+ images. If it's missing we silently fall
    back to a pure-Pillow path.
    """

    try:
        import numpy  # noqa: F401

        return numpy
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Color parsing
# ---------------------------------------------------------------------------


def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Parse a ``#RRGGBB`` (or shorthand ``#RGB``) hex string into ``(R, G, B)``.

    Args:
        hex_str: A 3-digit or 6-digit hex color string. The leading ``#``
            is optional. Parsing is case-insensitive.

    Returns:
        A 3-tuple of ints in ``[0, 255]``.

    Raises:
        ValueError: If the input is not a valid 3- or 6-digit hex color.
    """

    if not isinstance(hex_str, str):  # defensive — duck-typing would error confusingly
        raise ValueError(f"hex_to_rgb expects a string, got {type(hex_str).__name__}")

    s = hex_str.strip().lstrip("#")

    # Accept the CSS-style 3-digit shorthand by doubling each nibble.
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)

    if len(s) != 6:
        raise ValueError(
            f"hex_to_rgb: expected 3 or 6 hex digits (with optional '#'), "
            f"got {hex_str!r}"
        )

    try:
        value = int(s, 16)
    except ValueError as exc:
        raise ValueError(f"hex_to_rgb: {hex_str!r} is not a valid hex color") from exc

    r = (value >> 16) & 0xFF
    g = (value >> 8) & 0xFF
    b = value & 0xFF
    return (r, g, b)


# ---------------------------------------------------------------------------
# Chroma-key math helpers (ported algorithms)
# ---------------------------------------------------------------------------


def _channel_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    """Per-channel Chebyshev (max-channel) distance between two RGB triples."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def _clamp_channel(value: float) -> int:
    """Clamp a float to ``[0, 255]`` and round to int."""
    return max(0, min(255, int(round(value))))


def _smoothstep(t: float) -> float:
    """Hermite smoothstep curve: maps t in [0,1] to a smooth 0→1 ramp."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _spill_channels(key: tuple[int, int, int]) -> list[int]:
    """Return channel indices where the key color is 'high' (dominant).

    A channel qualifies as a spill channel if:
    - its value is ≥ max(key) - 16  (near-maximum for this key)
    - its value is ≥ 128            (actually bright)

    For ``#00FF00`` this returns ``[1]`` (green only).
    For ``#FF00FF`` this returns ``[0, 2]`` (red and blue).
    """
    key_max = max(key)
    if key_max < 128:
        return []
    return [idx for idx, v in enumerate(key) if v >= key_max - 16 and v >= 128]


def _key_channel_dominance(
    rgb: tuple[int, int, int],
    key: tuple[int, int, int],
) -> float:
    """How much the key's spill channels dominate the non-spill channels.

    Returns the difference: min(spill channel values) - max(non-spill values).
    A positive value means the key color dominates — pixel "looks key-colored".
    """
    spill = _spill_channels(key)
    if not spill:
        return 0.0
    non_spill = [i for i in range(3) if i not in spill]
    channels = [float(v) for v in rgb]
    key_strength = (
        min(channels[i] for i in spill) if len(spill) > 1 else channels[spill[0]]
    )
    non_key_strength = max((channels[i] for i in non_spill), default=0.0)
    return key_strength - non_key_strength


def _looks_key_colored(
    rgb: tuple[int, int, int],
    key: tuple[int, int, int],
    distance: int,
) -> bool:
    """Return True if the pixel looks like it carries key-color contamination.

    Two-tier heuristic:
    - distance ≤ 32: definitely key-colored.
    - otherwise: check key-channel dominance ≥ KEY_DOMINANCE_THRESHOLD.

    This catches partial-alpha pixels that look key-colored even when the
    Chebyshev distance is moderate (e.g. slightly desaturated key edge).
    """
    if distance <= 32:
        return True
    spill = _spill_channels(key)
    if not spill:
        return True
    return _key_channel_dominance(rgb, key) >= KEY_DOMINANCE_THRESHOLD


def _soft_alpha_dual(
    distance: int,
    transparent_threshold: float,
    opaque_threshold: float,
) -> int:
    """Dual-threshold alpha from a pixel's key-color distance.

    - distance ≤ transparent_threshold → alpha=0   (fully transparent)
    - distance ≥ opaque_threshold       → alpha=255 (fully opaque)
    - in between                        → smoothstep ramp
    """
    if distance <= transparent_threshold:
        return 0
    if distance >= opaque_threshold:
        return 255
    ratio = (float(distance) - transparent_threshold) / (
        opaque_threshold - transparent_threshold
    )
    return _clamp_channel(255.0 * _smoothstep(ratio))


def _dominance_alpha(
    rgb: tuple[int, int, int],
    key: tuple[int, int, int],
) -> int:
    """Alpha computed from key-channel dominance (clamped 0-255).

    When key channels strongly dominate non-key channels the pixel is
    transparentised further. This is combined with the distance-based alpha
    via ``min(distance_alpha, dominance_alpha)`` to catch pixels that have
    low key-distance but also low dominance (i.e., neutral-grey pixels that
    happen to be close to the key in Euclidean space).
    """
    spill = _spill_channels(key)
    if not spill:
        return 255
    channels = [float(v) for v in rgb]
    non_spill = [i for i in range(3) if i not in spill]
    key_strength = (
        min(channels[i] for i in spill) if len(spill) > 1 else channels[spill[0]]
    )
    non_key_strength = max((channels[i] for i in non_spill), default=0.0)
    dominance = key_strength - non_key_strength
    if dominance <= 0:
        return 255
    denominator = max(1.0, float(max(key)) - non_key_strength)
    alpha = 1.0 - min(1.0, dominance / denominator)
    return _clamp_channel(alpha * 255.0)


def _cleanup_spill_dominance(
    rgb: tuple[int, int, int],
    key: tuple[int, int, int],
    alpha: int = 255,
) -> tuple[int, int, int]:
    """Dominance-capping despill for partial-alpha pixels.

    For each spill channel (those carrying key-color contamination), cap its
    value to ≤ max(non-spill channels) - 1. This is physically correct:
    a non-key pixel's spill channel should never be brighter than the
    subject's lightness anchor (the non-spill channels).

    Fully-opaque pixels (alpha ≥ 252) are returned unchanged — they are pure
    subject pixels that don't need despill.

    Args:
        rgb: The pixel's (R, G, B) tuple.
        key: The chroma-key (R, G, B) triple.
        alpha: The computed alpha for this pixel (0-255).

    Returns:
        The despilled (R, G, B) tuple.
    """
    if alpha >= 252:
        return rgb

    spill = _spill_channels(key)
    if not spill:
        return rgb

    channels = [float(v) for v in rgb]
    non_spill = [i for i in range(3) if i not in spill]
    if non_spill:
        anchor = max(channels[i] for i in non_spill)
        cap = max(0.0, anchor - 1.0)
        for idx in spill:
            if channels[idx] > cap:
                channels[idx] = cap

    return (
        _clamp_channel(channels[0]),
        _clamp_channel(channels[1]),
        _clamp_channel(channels[2]),
    )


# ---------------------------------------------------------------------------
# Auto-key border sampling
# ---------------------------------------------------------------------------


def _sample_border_key(
    image: "PILImage",
    mode: str = "border",
) -> tuple[int, int, int]:
    """Sample the actual key color from the image border.

    Instead of trusting the requested key color, we read the actual pixels the
    model rendered at the edges and use their median as the effective key. This
    handles model drift where the rendered background deviates slightly from
    the exact ``#00FF00`` requested (e.g., renders as ``#00FE01``).

    Args:
        image: A Pillow image (RGB or RGBA).
        mode: ``"border"`` samples a thin band around all four edges.
              ``"corners"`` samples 12×12 patches at the four corners.

    Returns:
        The median (R, G, B) of the sampled border pixels.
    """
    width, height = image.size
    pixels = image.load()
    samples: list[tuple[int, int, int]] = []

    if mode == "corners":
        patch = max(1, min(width, height, 12))
        boxes = [
            (0, 0, patch, patch),
            (width - patch, 0, width, patch),
            (0, height - patch, patch, height),
            (width - patch, height - patch, width, height),
        ]
        for left, top, right, bottom in boxes:
            for y in range(top, bottom):
                for x in range(left, right):
                    px = pixels[x, y]
                    samples.append((px[0], px[1], px[2]))
    else:
        # Full border band (thin strip along all four edges).
        band = max(1, min(width, height, 6))
        step = max(1, min(width, height) // 256)
        for x in range(0, width, step):
            for y in range(band):
                px = pixels[x, y]
                samples.append((px[0], px[1], px[2]))
                px = pixels[x, height - 1 - y]
                samples.append((px[0], px[1], px[2]))
        for y in range(0, height, step):
            for x in range(band):
                px = pixels[x, y]
                samples.append((px[0], px[1], px[2]))
                px = pixels[width - 1 - x, y]
                samples.append((px[0], px[1], px[2]))

    if not samples:
        raise ValueError("Could not sample background key color from image border.")

    return (
        int(round(median(s[0] for s in samples))),
        int(round(median(s[1] for s in samples))),
        int(round(median(s[2] for s in samples))),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_inputs(
    key_rgb: tuple[int, int, int],
    tolerance: int,
    feather_px: int,
    edge_erode_px: int = 0,
) -> None:
    """Validate the public ``keyout`` parameters in one place."""

    if not (isinstance(key_rgb, tuple) and len(key_rgb) == 3):
        raise ValueError(f"key_rgb must be a 3-tuple, got {key_rgb!r}")
    for i, c in enumerate(key_rgb):
        if not isinstance(c, int) or not (0 <= c <= 255):
            raise ValueError(
                f"key_rgb component {i} = {c!r} is out of range 0-255"
            )
    if not isinstance(tolerance, int) or not (0 <= tolerance <= 100):
        raise ValueError(f"tolerance must be int in 0-100, got {tolerance!r}")
    if not isinstance(feather_px, int) or feather_px < 0:
        raise ValueError(
            f"feather_px must be a non-negative int, got {feather_px!r}"
        )
    if not isinstance(edge_erode_px, int) or edge_erode_px < 0:
        raise ValueError(
            f"edge_erode_px must be a non-negative int, got {edge_erode_px!r}"
        )


def check_complex_subject(prompt: str) -> str | None:
    """Return a warning string if the prompt mentions complex-edge subjects.

    Keywords like ``fur``, ``hair``, ``glass``, ``smoke``, ``liquid``,
    ``translucent``, etc. indicate subjects where chroma-key tends to produce
    fringe artifacts at semi-transparent edges. When detected, callers should
    surface this as a non-fatal warning.

    Args:
        prompt: The original user prompt string.

    Returns:
        A warning string if any complex-edge keywords are found, else ``None``.
    """
    lower = prompt.lower()
    # Use word-boundary-like matching: check for word-onset matches to avoid
    # partial matches (e.g. "hairy" for "hair").
    import re
    for kw in COMPLEX_SUBJECT_KEYWORDS:
        if re.search(r'\b' + re.escape(kw) + r'\b', lower):
            return COMPLEX_SUBJECT_WARNING
    return None


# ---------------------------------------------------------------------------
# Core entry point — file-to-file
# ---------------------------------------------------------------------------


def keyout(
    src_path: Path,
    dst_path: Path,
    *,
    key_rgb: tuple[int, int, int] = DEFAULT_KEY_RGB,
    tolerance: int = DEFAULT_TOLERANCE,
    despill: bool = True,
    despill_mode: str = "dominance",
    feather_px: int = DEFAULT_FEATHER_PX,
    edge_erode_px: int = 1,
    auto_key: str | None = "border",
    transparent_threshold: float = DEFAULT_TRANSPARENT_THRESHOLD,
    opaque_threshold: float = DEFAULT_OPAQUE_THRESHOLD,
) -> ChromaResult:
    """Replace the key color in ``src_path`` with transparency and save as PNG.

    Args:
        src_path: Input image path. Any format Pillow can decode.
        dst_path: Output PNG path. Parent directories are created if missing.
        key_rgb: The (R, G, B) backdrop color requested. When ``auto_key``
            is not None, this is used as the seed only — the actual key is
            sampled from the image border.
        tolerance: 0-100. Controls the inner/outer radius for the legacy
            distance-based hard threshold. When ``transparent_threshold`` and
            ``opaque_threshold`` are at their defaults the dual-threshold
            smoothstep algorithm is used instead, which ignores ``tolerance``
            for alpha computation (tolerance is still used for the
            ``despill`` partial-alpha gate in the Pillow fallback path).
        despill: If ``True``, reduce residual key-color tint on subject edges.
        despill_mode: ``"dominance"`` (default) — cap spill channels to
            ≤ max(non-spill channels) - 1, the physically correct formulation.
            ``"projection"`` — legacy full key-vector projection subtraction.
        feather_px: Gaussian-blur radius applied to the alpha channel only.
            ``0`` disables feathering.
        edge_erode_px: Pixels to erode from the alpha mask before feathering.
            Default ``1``. Set to ``0`` to disable.
        auto_key: ``"border"`` (default) — sample actual key from border band.
            ``"corners"`` — sample from corner patches. ``None`` — use
            ``key_rgb`` exactly as given (disable border sampling).
        transparent_threshold: Distance at or below which pixels are fully
            transparent. Default 12.
        opaque_threshold: Distance at or above which pixels are fully opaque.
            Default 220.

    Returns:
        A :class:`ChromaResult` with file paths and pixel statistics.

    Raises:
        ValueError: If ``tolerance`` is out of ``0-100`` or any ``key_rgb``
            component is out of ``0-255``, or ``feather_px`` / ``edge_erode_px``
            is negative.
        FileNotFoundError: If ``src_path`` does not exist.
        OSError: If Pillow cannot decode the source image.
        ImportError: If Pillow is not installed.
    """

    _validate_inputs(key_rgb, tolerance, feather_px, edge_erode_px)

    src_path = Path(src_path)
    dst_path = Path(dst_path)
    if not src_path.exists():
        raise FileNotFoundError(f"chroma: source image not found: {src_path}")

    Image, ImageFilter = _import_pillow()

    t0 = time.perf_counter()

    # Load image as RGB (drop any existing alpha — we compute a fresh one).
    with Image.open(src_path) as raw:
        rgb_img = raw.convert("RGB")

    # Determine the effective key: either as given, or sampled from the border.
    effective_key = key_rgb
    if auto_key is not None:
        rgba_for_sampling = rgb_img.convert("RGBA")
        effective_key = _sample_border_key(rgba_for_sampling, mode=auto_key)

    rgba, despill_applied = _compute_rgba(
        rgb_img,
        key_rgb=effective_key,
        tolerance=tolerance,
        despill=despill,
        despill_mode=despill_mode,
        feather_px=feather_px,
        edge_erode_px=edge_erode_px,
        transparent_threshold=transparent_threshold,
        opaque_threshold=opaque_threshold,
        Image=Image,
        ImageFilter=ImageFilter,
    )

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    rgba.save(dst_path, format="PNG")

    stats = _pixel_stats(rgba)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    return ChromaResult(
        src_path=src_path,
        dst_path=dst_path,
        key_rgb=key_rgb,
        effective_key_rgb=effective_key,
        tolerance=tolerance,
        despill_applied=despill_applied,
        feather_px=feather_px,
        pixels_total=stats[0],
        pixels_keyed_fully=stats[1],
        pixels_keyed_partial=stats[2],
        pixels_kept=stats[3],
        elapsed_ms=elapsed_ms,
        warnings=(),
    )


def keyout_bytes(
    src_bytes: bytes,
    *,
    key_rgb: tuple[int, int, int] = DEFAULT_KEY_RGB,
    tolerance: int = DEFAULT_TOLERANCE,
    despill: bool = True,
    despill_mode: str = "dominance",
    feather_px: int = DEFAULT_FEATHER_PX,
    edge_erode_px: int = 1,
    auto_key: str | None = "border",
    transparent_threshold: float = DEFAULT_TRANSPARENT_THRESHOLD,
    opaque_threshold: float = DEFAULT_OPAQUE_THRESHOLD,
) -> bytes:
    """Run the chroma-key pipeline on in-memory bytes.

    Useful when the source PNG is already in memory (straight from the
    bridge) and a disk round-trip is wasteful.

    Args:
        src_bytes: Encoded image bytes (any Pillow-readable format).
        key_rgb: See :func:`keyout`.
        tolerance: See :func:`keyout`.
        despill: See :func:`keyout`.
        despill_mode: See :func:`keyout`.
        feather_px: See :func:`keyout`.
        edge_erode_px: See :func:`keyout`.
        auto_key: See :func:`keyout`.
        transparent_threshold: See :func:`keyout`.
        opaque_threshold: See :func:`keyout`.

    Returns:
        Encoded PNG bytes with an RGBA alpha channel.

    Raises:
        ValueError: Same as :func:`keyout`.
        OSError: If Pillow cannot decode ``src_bytes``.
        ImportError: If Pillow is not installed.
    """

    _validate_inputs(key_rgb, tolerance, feather_px, edge_erode_px)

    Image, ImageFilter = _import_pillow()

    with Image.open(io.BytesIO(src_bytes)) as raw:
        rgb_img = raw.convert("RGB")

    effective_key = key_rgb
    if auto_key is not None:
        rgba_for_sampling = rgb_img.convert("RGBA")
        effective_key = _sample_border_key(rgba_for_sampling, mode=auto_key)

    rgba, _ = _compute_rgba(
        rgb_img,
        key_rgb=effective_key,
        tolerance=tolerance,
        despill=despill,
        despill_mode=despill_mode,
        feather_px=feather_px,
        edge_erode_px=edge_erode_px,
        transparent_threshold=transparent_threshold,
        opaque_threshold=opaque_threshold,
        Image=Image,
        ImageFilter=ImageFilter,
    )

    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Internal: the actual math, factored out so both file and bytes entry
# points share a single implementation.
# ---------------------------------------------------------------------------


def _radii(tolerance: int) -> tuple[float, float]:
    """Translate the 0-100 ``tolerance`` slider into inner/outer pixel radii.

    Tolerance is expressed in "fraction of the maximum possible RGB
    distance" — at ``tolerance=100`` the outer radius covers the entire
    color cube and everything becomes (at least partially) transparent.

    The inner radius is set to half the outer radius. This creates a
    soft transition band of width ``outer - inner = 0.5 * outer``, which
    is wide enough to gracefully handle JPEG-ish edge artifacts without
    eating into the subject. The special case ``tolerance == 0`` collapses
    the band to a single point so only exact matches get keyed out.
    """

    if tolerance == 0:
        return (0.0, 0.0)
    outer = (tolerance / 100.0) * _MAX_RGB_DIST
    inner = outer * 0.5
    return (inner, outer)


def _compute_rgba(
    rgb_img: "PILImage",
    *,
    key_rgb: tuple[int, int, int],
    tolerance: int,
    despill: bool,
    despill_mode: str = "dominance",
    feather_px: int,
    edge_erode_px: int = 0,
    transparent_threshold: float = DEFAULT_TRANSPARENT_THRESHOLD,
    opaque_threshold: float = DEFAULT_OPAQUE_THRESHOLD,
    Image,  # type: ignore[no-untyped-def]
    ImageFilter,  # type: ignore[no-untyped-def]
) -> tuple["PILImage", bool]:
    """Run the full chroma-key pipeline on a Pillow RGB image.

    Returns the resulting RGBA image plus a flag indicating whether any
    despill correction was actually applied (i.e. ``despill=True`` AND at
    least one partial-alpha pixel existed).
    """

    np = _try_import_numpy()
    if np is not None and despill_mode == "dominance":
        # NumPy path for dominance-capping despill (new default).
        return _compute_rgba_numpy_dominance(
            rgb_img,
            key_rgb=key_rgb,
            tolerance=tolerance,
            despill=despill,
            feather_px=feather_px,
            edge_erode_px=edge_erode_px,
            transparent_threshold=transparent_threshold,
            opaque_threshold=opaque_threshold,
            np=np,
            Image=Image,
            ImageFilter=ImageFilter,
        )
    if np is not None and despill_mode == "projection":
        return _compute_rgba_numpy_projection(
            rgb_img,
            key_rgb=key_rgb,
            tolerance=tolerance,
            despill=despill,
            feather_px=feather_px,
            edge_erode_px=edge_erode_px,
            np=np,
            Image=Image,
            ImageFilter=ImageFilter,
        )
    # Pure-Pillow path (no numpy).
    return _compute_rgba_pillow(
        rgb_img,
        key_rgb=key_rgb,
        tolerance=tolerance,
        despill=despill,
        despill_mode=despill_mode,
        feather_px=feather_px,
        edge_erode_px=edge_erode_px,
        transparent_threshold=transparent_threshold,
        opaque_threshold=opaque_threshold,
        Image=Image,
        ImageFilter=ImageFilter,
    )


# ---------------------------------------------------------------------------
# NumPy fast path — dominance-capping despill (new default)
# ---------------------------------------------------------------------------


def _compute_rgba_numpy_dominance(
    rgb_img: "PILImage",
    *,
    key_rgb: tuple[int, int, int],
    tolerance: int,
    despill: bool,
    feather_px: int,
    edge_erode_px: int = 0,
    transparent_threshold: float = DEFAULT_TRANSPARENT_THRESHOLD,
    opaque_threshold: float = DEFAULT_OPAQUE_THRESHOLD,
    np,  # type: ignore[no-untyped-def]
    Image,  # type: ignore[no-untyped-def]
    ImageFilter,  # type: ignore[no-untyped-def]
) -> tuple["PILImage", bool]:
    """NumPy-vectorized implementation using dual-threshold + dominance-capping despill."""

    arr = np.asarray(rgb_img, dtype=np.uint8)
    rgb_f = arr.astype(np.float32)
    key_arr = np.array(key_rgb, dtype=np.float32)

    # Per-pixel Chebyshev distance (max channel difference) — matches reference.
    diff = np.abs(rgb_f - key_arr)
    dist_cheby = np.max(diff, axis=2).astype(np.float32)  # (H, W)

    # Dual-threshold smoothstep alpha from Chebyshev distance.
    t_lo = float(transparent_threshold)
    t_hi = float(opaque_threshold)
    if t_hi <= t_lo:
        alpha_dist = np.where(dist_cheby <= t_lo, 0.0, 255.0)
    else:
        ratio = (dist_cheby - t_lo) / (t_hi - t_lo)
        ratio = np.clip(ratio, 0.0, 1.0)
        smooth = ratio * ratio * (3.0 - 2.0 * ratio)
        alpha_dist = smooth * 255.0

    # Key-channel dominance alpha.
    spill = _spill_channels(key_rgb)
    if spill:
        non_spill = [i for i in range(3) if i not in spill]
        key_max = float(max(key_rgb))
        channels = [rgb_f[:, :, i] for i in range(3)]
        if len(spill) > 1:
            key_strength = np.minimum(
                *[channels[i] for i in spill]
            ) if len(spill) == 2 else channels[spill[0]]
        else:
            key_strength = channels[spill[0]]
        if non_spill:
            non_key_strength = np.maximum(
                *[channels[i] for i in non_spill]
            ) if len(non_spill) == 2 else channels[non_spill[0]]
        else:
            non_key_strength = np.zeros_like(key_strength)
        dominance = key_strength - non_key_strength
        denominator = np.maximum(1.0, key_max - non_key_strength)
        dom_ratio = np.clip(dominance / denominator, 0.0, 1.0)
        alpha_dom = (1.0 - dom_ratio) * 255.0
        # Only apply dominance suppression where it *helps* (pixel looks key-colored).
        key_like_mask = (dist_cheby <= 32) | (
            (key_strength - non_key_strength) >= KEY_DOMINANCE_THRESHOLD
        )
        alpha_f = np.where(key_like_mask, np.minimum(alpha_dist, alpha_dom), alpha_dist)
    else:
        alpha_f = alpha_dist

    # Alpha noise floor: kill near-zero speckling.
    alpha_f = np.where((alpha_f > 0) & (alpha_f <= float(ALPHA_NOISE_FLOOR)), 0.0, alpha_f)
    alpha_f = np.clip(alpha_f, 0.0, 255.0)

    # Dominance-capping despill on partial-alpha pixels.
    despill_applied = False
    out_rgb = rgb_f.copy()
    if despill and spill:
        partial_mask = (alpha_f > 0.0) & (alpha_f < 255.0)
        if np.any(partial_mask):
            despill_applied = True
            non_spill = [i for i in range(3) if i not in spill]
            channels = [out_rgb[:, :, i].copy() for i in range(3)]
            if non_spill:
                if len(non_spill) >= 2:
                    anchor = np.maximum.reduce([channels[i] for i in non_spill])
                else:
                    anchor = channels[non_spill[0]]
                cap = np.maximum(0.0, anchor - 1.0)
                for idx in spill:
                    # Only cap where: partial alpha AND spill channel exceeds cap.
                    needs_cap = partial_mask & (channels[idx] > cap)
                    out_rgb[:, :, idx] = np.where(needs_cap, cap, channels[idx])

    out_rgb_u8 = np.clip(out_rgb, 0.0, 255.0).astype(np.uint8)
    alpha_u8 = alpha_f.astype(np.uint8)

    rgb_pil = Image.fromarray(out_rgb_u8, mode="RGB")
    alpha_pil = Image.fromarray(alpha_u8, mode="L")

    if edge_erode_px > 0:
        for _ in range(edge_erode_px):
            alpha_pil = alpha_pil.filter(ImageFilter.MinFilter(size=3))

    if feather_px > 0:
        alpha_pil = alpha_pil.filter(ImageFilter.GaussianBlur(radius=feather_px))

    rgba = Image.merge("RGBA", (*rgb_pil.split(), alpha_pil))
    return rgba, despill_applied


# ---------------------------------------------------------------------------
# NumPy fast path — legacy projection despill
# ---------------------------------------------------------------------------


def _compute_rgba_numpy_projection(
    rgb_img: "PILImage",
    *,
    key_rgb: tuple[int, int, int],
    tolerance: int,
    despill: bool,
    feather_px: int,
    edge_erode_px: int = 0,
    np,  # type: ignore[no-untyped-def]
    Image,  # type: ignore[no-untyped-def]
    ImageFilter,  # type: ignore[no-untyped-def]
) -> tuple["PILImage", bool]:
    """NumPy-vectorized implementation using legacy projection despill."""

    arr = np.asarray(rgb_img, dtype=np.uint8)
    rgb_f = arr.astype(np.float32)
    key_arr = np.array(key_rgb, dtype=np.float32)

    diff = rgb_f - key_arr
    dist = np.sqrt(np.sum(diff * diff, axis=2))  # Euclidean distance

    inner, outer = _radii(tolerance)

    if outer <= inner:
        alpha_f = np.where(dist <= 0.0, 0.0, 255.0)
    else:
        ramp = (dist - inner) / (outer - inner)
        ramp = np.clip(ramp, 0.0, 1.0)
        alpha_f = ramp * 255.0

    despill_applied = False
    out_rgb = rgb_f
    if despill:
        partial = alpha_f < 255.0
        if np.any(partial):
            despill_applied = True
            key_norm_sq = float(np.sum(key_arr * key_arr))
            if key_norm_sq > 0.0:
                dot = np.sum(rgb_f * key_arr, axis=2)
                coef = dot / key_norm_sq
                coef_masked = np.where(partial, coef, 0.0)
                subtract = coef_masked[..., None] * key_arr
                out_rgb = rgb_f - subtract

    out_rgb_u8 = np.clip(out_rgb, 0.0, 255.0).astype(np.uint8)
    alpha_u8 = np.clip(alpha_f, 0.0, 255.0).astype(np.uint8)

    rgb_pil = Image.fromarray(out_rgb_u8, mode="RGB")
    alpha_pil = Image.fromarray(alpha_u8, mode="L")

    if edge_erode_px > 0:
        for _ in range(edge_erode_px):
            alpha_pil = alpha_pil.filter(ImageFilter.MinFilter(size=3))

    if feather_px > 0:
        alpha_pil = alpha_pil.filter(ImageFilter.GaussianBlur(radius=feather_px))

    rgba = Image.merge("RGBA", (*rgb_pil.split(), alpha_pil))
    return rgba, despill_applied


# ---------------------------------------------------------------------------
# Pure-Pillow fallback path (no numpy)
# ---------------------------------------------------------------------------


def _compute_rgba_pillow(
    rgb_img: "PILImage",
    *,
    key_rgb: tuple[int, int, int],
    tolerance: int,
    despill: bool,
    despill_mode: str = "dominance",
    feather_px: int,
    edge_erode_px: int = 0,
    transparent_threshold: float = DEFAULT_TRANSPARENT_THRESHOLD,
    opaque_threshold: float = DEFAULT_OPAQUE_THRESHOLD,
    Image,  # type: ignore[no-untyped-def]
    ImageFilter,  # type: ignore[no-untyped-def]
) -> tuple["PILImage", bool]:
    """Pure-Pillow implementation, used when NumPy is not importable."""

    width, height = rgb_img.size
    src_pixels = rgb_img.load()

    kr, kg, kb = key_rgb
    key_norm_sq = float(kr * kr + kg * kg + kb * kb)  # for projection despill

    n = width * height
    out_r = bytearray(n)
    out_g = bytearray(n)
    out_b = bytearray(n)
    out_a = bytearray(n)

    despill_applied = False

    for y in range(height):
        row_offset = y * width
        for x in range(width):
            r, g, b = src_pixels[x, y]
            rgb = (r, g, b)

            # Chebyshev distance to key color.
            distance = _channel_distance(rgb, key_rgb)
            key_like = _looks_key_colored(rgb, key_rgb, distance)

            # Dual-threshold soft matte alpha (clamped by dominance for key-like pixels).
            if key_like:
                dist_alpha = _soft_alpha_dual(distance, transparent_threshold, opaque_threshold)
                dom_alpha = _dominance_alpha(rgb, key_rgb)
                alpha = min(dist_alpha, dom_alpha)
            else:
                alpha = _soft_alpha_dual(distance, transparent_threshold, opaque_threshold)

            # Alpha noise floor.
            if 0 < alpha <= ALPHA_NOISE_FLOOR:
                alpha = 0

            if alpha == 0:
                # Fully transparent — zero out RGB too (pre-multiplied convention).
                idx = row_offset + x
                out_r[idx] = 0
                out_g[idx] = 0
                out_b[idx] = 0
                out_a[idx] = 0
                continue

            # Despill partial-alpha pixels.
            if despill and key_like and alpha < 255:
                despill_applied = True
                if despill_mode == "dominance":
                    r, g, b = _cleanup_spill_dominance(rgb, key_rgb, alpha)
                else:
                    # Legacy projection despill.
                    if key_norm_sq > 0.0:
                        coef = (r * kr + g * kg + b * kb) / key_norm_sq
                        r = int(max(0.0, min(255.0, r - coef * kr)))
                        g = int(max(0.0, min(255.0, g - coef * kg)))
                        b = int(max(0.0, min(255.0, b - coef * kb)))

            idx = row_offset + x
            out_r[idx] = r
            out_g[idx] = g
            out_b[idx] = b
            out_a[idx] = alpha

    r_band = Image.frombytes("L", (width, height), bytes(out_r))
    g_band = Image.frombytes("L", (width, height), bytes(out_g))
    b_band = Image.frombytes("L", (width, height), bytes(out_b))
    a_band = Image.frombytes("L", (width, height), bytes(out_a))

    if edge_erode_px > 0:
        for _ in range(edge_erode_px):
            a_band = a_band.filter(ImageFilter.MinFilter(size=3))

    if feather_px > 0:
        a_band = a_band.filter(ImageFilter.GaussianBlur(radius=feather_px))

    rgba = Image.merge("RGBA", (r_band, g_band, b_band, a_band))
    return rgba, despill_applied


# ---------------------------------------------------------------------------
# Stats helper
# ---------------------------------------------------------------------------


def _pixel_stats(rgba: "PILImage") -> tuple[int, int, int, int]:
    """Return ``(total, alpha==0, 0<alpha<255, alpha==255)`` for the image."""

    alpha = rgba.split()[-1]  # final RGBA channel
    histogram = alpha.histogram()  # list of length 256
    fully_keyed = histogram[0]
    fully_kept = histogram[255]
    partial = sum(histogram[1:255])
    total = fully_keyed + partial + fully_kept
    return (total, fully_keyed, partial, fully_kept)
