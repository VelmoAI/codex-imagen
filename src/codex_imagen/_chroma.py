"""codex_imagen._chroma — Pillow chroma-key pipeline (solid backdrop -> alpha).

Purpose
-------
The Codex OAuth bridge does NOT honor ``background: "transparent"`` — the
server-side validator rejects the request with HTTP 400 (verified by live
probes in ``probe_validate.py``). The only way to deliver a PNG with a real
alpha channel through that bridge is to:

1. Ask the model to render the subject on a solid, very-saturated backdrop
   (default: magenta ``#FF00FF`` — chosen because it almost never appears
   naturally in photographic or illustrative imagery and is far from human
   skin tones / common product colors).
2. Download the resulting opaque PNG.
3. Run :func:`keyout` here to replace the backdrop color with ``alpha = 0``.

This module is bridge-agnostic. It speaks Pillow (with an optional NumPy
fast path) and nothing else — it must not import the bridge, prompt
builder, or any other forge module.

Algorithm summary
-----------------
For each pixel we compute the Euclidean distance to the key color in RGB
space and translate it into an alpha value via a smooth ramp controlled by
``tolerance``. Pixels inside the inner radius are fully keyed
(``alpha = 0``); pixels outside the outer radius are fully opaque
(``alpha = 255``); pixels in between fade linearly. Optional "despill"
removes the residual key-color tint that survives on subject edges by
projecting each partial-alpha pixel onto the key-color vector and
subtracting that projection scaled by ``1 - alpha/255``. A small Gaussian
blur on the alpha channel feathers the cutout edge so it composites
cleanly on arbitrary backgrounds.

Performance
-----------
A 1024x1024 RGB image has ~1M pixels. Pure-Pillow ``ImageMath`` and band
arithmetic are workable, but NumPy vectorization is roughly an order of
magnitude faster. We try to import NumPy lazily and fall back to a
pure-Pillow path if it's not installed.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from pathlib import Path
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
#: string ("magenta") to the actual RGB triple without re-parsing.
KEY_PRESETS: dict[str, tuple[int, int, int]] = {
    "magenta": (255, 0, 255),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "cyan": (0, 255, 255),
    "yellow": (255, 255, 0),
}

#: Default key color. Magenta is chosen because it is rarely found in
#: natural imagery and is maximally distant from human skin tones.
DEFAULT_KEY_RGB: tuple[int, int, int] = (255, 0, 255)

#: Default tolerance (0-100). 40 works well for synthetic AI imagery on a
#: solid magenta backdrop — wide enough to absorb JPEG-ish artifacts but
#: tight enough not to nibble at subject edges.
DEFAULT_TOLERANCE: int = 40

#: Default Gaussian-blur radius applied to the alpha channel only.
DEFAULT_FEATHER_PX: int = 2


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChromaResult:
    """Outcome of a single :func:`keyout` pass.

    Attributes:
        src_path: Path to the source image that was read.
        dst_path: Path to the RGBA PNG that was written.
        key_rgb: The (R, G, B) key color that was matched against.
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
    """

    src_path: Path
    dst_path: Path
    key_rgb: tuple[int, int, int]
    tolerance: int
    despill_applied: bool
    feather_px: int
    pixels_total: int
    pixels_keyed_fully: int
    pixels_keyed_partial: int
    pixels_kept: int
    elapsed_ms: int


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
# Core entry point — file-to-file
# ---------------------------------------------------------------------------


def _validate_inputs(
    key_rgb: tuple[int, int, int],
    tolerance: int,
    feather_px: int,
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


def keyout(
    src_path: Path,
    dst_path: Path,
    *,
    key_rgb: tuple[int, int, int] = DEFAULT_KEY_RGB,
    tolerance: int = DEFAULT_TOLERANCE,
    despill: bool = True,
    feather_px: int = DEFAULT_FEATHER_PX,
) -> ChromaResult:
    """Replace the key color in ``src_path`` with transparency and save as PNG.

    Args:
        src_path: Input image path. Any format Pillow can decode.
        dst_path: Output PNG path. Parent directories are created if missing.
        key_rgb: The (R, G, B) backdrop color to key out.
        tolerance: 0-100. ``0`` only keys pixels exactly equal to ``key_rgb``.
            ``100`` keys everything (in practice the upper end is useless,
            but it's the natural max). The default ``40`` is well-tuned for
            magenta backdrops on synthetic AI imagery.
        despill: If ``True``, reduce residual key-color tint on subject edges
            by subtracting the key-color projection from partial-alpha pixels.
        feather_px: Gaussian-blur radius applied to the alpha channel only,
            in pixels. ``0`` disables feathering and yields a strictly
            binary cutout.

    Returns:
        A :class:`ChromaResult` with file paths and pixel statistics.

    Raises:
        ValueError: If ``tolerance`` is out of ``0-100`` or any ``key_rgb``
            component is out of ``0-255``, or ``feather_px`` is negative.
        FileNotFoundError: If ``src_path`` does not exist.
        OSError: If Pillow cannot decode the source image.
        ImportError: If Pillow is not installed.
    """

    _validate_inputs(key_rgb, tolerance, feather_px)

    src_path = Path(src_path)
    dst_path = Path(dst_path)
    if not src_path.exists():
        raise FileNotFoundError(f"chroma: source image not found: {src_path}")

    Image, ImageFilter = _import_pillow()

    t0 = time.perf_counter()

    # Drop any pre-existing alpha — we're computing a fresh one from RGB
    # similarity to the key color. Keeping the original alpha would create
    # ambiguity (was that pixel "subject" or "background"?).
    with Image.open(src_path) as raw:
        rgb_img = raw.convert("RGB")

    rgba, despill_applied = _compute_rgba(
        rgb_img,
        key_rgb=key_rgb,
        tolerance=tolerance,
        despill=despill,
        feather_px=feather_px,
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
        tolerance=tolerance,
        despill_applied=despill_applied,
        feather_px=feather_px,
        pixels_total=stats[0],
        pixels_keyed_fully=stats[1],
        pixels_keyed_partial=stats[2],
        pixels_kept=stats[3],
        elapsed_ms=elapsed_ms,
    )


def keyout_bytes(
    src_bytes: bytes,
    *,
    key_rgb: tuple[int, int, int] = DEFAULT_KEY_RGB,
    tolerance: int = DEFAULT_TOLERANCE,
    despill: bool = True,
    feather_px: int = DEFAULT_FEATHER_PX,
) -> bytes:
    """Run the chroma-key pipeline on in-memory bytes.

    Useful when the source PNG is already in memory (straight from the
    bridge) and a disk round-trip is wasteful.

    Args:
        src_bytes: Encoded image bytes (any Pillow-readable format).
        key_rgb: See :func:`keyout`.
        tolerance: See :func:`keyout`.
        despill: See :func:`keyout`.
        feather_px: See :func:`keyout`.

    Returns:
        Encoded PNG bytes with an RGBA alpha channel.

    Raises:
        ValueError: Same as :func:`keyout`.
        OSError: If Pillow cannot decode ``src_bytes``.
        ImportError: If Pillow is not installed.
    """

    _validate_inputs(key_rgb, tolerance, feather_px)

    Image, ImageFilter = _import_pillow()

    with Image.open(io.BytesIO(src_bytes)) as raw:
        rgb_img = raw.convert("RGB")

    rgba, _ = _compute_rgba(
        rgb_img,
        key_rgb=key_rgb,
        tolerance=tolerance,
        despill=despill,
        feather_px=feather_px,
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
    rgb_img: PILImage,
    *,
    key_rgb: tuple[int, int, int],
    tolerance: int,
    despill: bool,
    feather_px: int,
    Image,  # type: ignore[no-untyped-def]
    ImageFilter,  # type: ignore[no-untyped-def]
) -> tuple[PILImage, bool]:
    """Run the full chroma-key pipeline on a Pillow RGB image.

    Returns the resulting RGBA image plus a flag indicating whether any
    despill correction was actually applied (i.e. ``despill=True`` AND at
    least one partial-alpha pixel existed).
    """

    np = _try_import_numpy()
    if np is not None:
        return _compute_rgba_numpy(
            rgb_img,
            key_rgb=key_rgb,
            tolerance=tolerance,
            despill=despill,
            feather_px=feather_px,
            np=np,
            Image=Image,
            ImageFilter=ImageFilter,
        )
    return _compute_rgba_pillow(
        rgb_img,
        key_rgb=key_rgb,
        tolerance=tolerance,
        despill=despill,
        feather_px=feather_px,
        Image=Image,
        ImageFilter=ImageFilter,
    )


# ---------------------------------------------------------------------------
# NumPy fast path
# ---------------------------------------------------------------------------


def _compute_rgba_numpy(
    rgb_img: PILImage,
    *,
    key_rgb: tuple[int, int, int],
    tolerance: int,
    despill: bool,
    feather_px: int,
    np,  # type: ignore[no-untyped-def]
    Image,  # type: ignore[no-untyped-def]
    ImageFilter,  # type: ignore[no-untyped-def]
) -> tuple[PILImage, bool]:
    """NumPy-vectorized implementation of the chroma-key pipeline."""

    # Shape: (H, W, 3), dtype uint8
    arr = np.asarray(rgb_img, dtype=np.uint8)
    # Work in float32 for distance math; uint8 would overflow on diff^2.
    rgb_f = arr.astype(np.float32)
    key_arr = np.array(key_rgb, dtype=np.float32)

    # Per-pixel Euclidean distance to the key color in RGB space.
    diff = rgb_f - key_arr  # broadcasts (H, W, 3) - (3,)
    dist = np.sqrt(np.sum(diff * diff, axis=2))  # (H, W)

    inner, outer = _radii(tolerance)

    # Map distance -> alpha via linear ramp between inner and outer.
    #   dist <= inner  -> alpha = 0     (fully keyed)
    #   dist >= outer  -> alpha = 255   (fully kept)
    #   in between     -> linear
    if outer <= inner:
        # tolerance == 0: only an exact match keys out.
        alpha_f = np.where(dist <= 0.0, 0.0, 255.0)
    else:
        ramp = (dist - inner) / (outer - inner)
        ramp = np.clip(ramp, 0.0, 1.0)
        alpha_f = ramp * 255.0

    # Despill: subtract the key-color projection from partial-alpha pixels.
    # For each such pixel p with normalized key direction k_hat,
    #   projection = (p . k_hat) * k_hat
    # We subtract that projection scaled by (1 - alpha/255) so fully-opaque
    # pixels are untouched, fully-keyed pixels are about to be invisible
    # anyway, and the transition band gets de-tinted proportional to how
    # close it is to the backdrop.
    despill_applied = False
    out_rgb = rgb_f
    if despill:
        partial = (alpha_f > 0.0) & (alpha_f < 255.0)
        if np.any(partial):
            despill_applied = True
            key_norm_sq = float(np.sum(key_arr * key_arr))
            if key_norm_sq > 0.0:
                # Dot product of each pixel with the key vector. Shape: (H, W).
                dot = np.sum(rgb_f * key_arr, axis=2)
                # Projection magnitudes (scalar coefficient per pixel).
                coef = dot / key_norm_sq  # (H, W)
                # Strength of the despill effect: 0 for opaque, 1 for fully keyed.
                strength = (1.0 - alpha_f / 255.0)
                # Only touch partial-alpha pixels.
                strength = np.where(partial, strength, 0.0)
                # Subtract: out = rgb - (coef * strength)[..., None] * key
                subtract = (coef * strength)[..., None] * key_arr
                out_rgb = rgb_f - subtract

    # Feather: Gaussian-blur the alpha channel ONLY. We do this on the
    # final uint8 alpha via Pillow because its GaussianBlur matches what
    # callers expect from PIL.ImageFilter.
    out_rgb_u8 = np.clip(out_rgb, 0.0, 255.0).astype(np.uint8)
    alpha_u8 = np.clip(alpha_f, 0.0, 255.0).astype(np.uint8)

    rgb_pil = Image.fromarray(out_rgb_u8, mode="RGB")
    alpha_pil = Image.fromarray(alpha_u8, mode="L")
    if feather_px > 0:
        alpha_pil = alpha_pil.filter(ImageFilter.GaussianBlur(radius=feather_px))

    rgba = Image.merge("RGBA", (*rgb_pil.split(), alpha_pil))
    return rgba, despill_applied


# ---------------------------------------------------------------------------
# Pure-Pillow fallback path (no numpy)
# ---------------------------------------------------------------------------


def _compute_rgba_pillow(
    rgb_img: PILImage,
    *,
    key_rgb: tuple[int, int, int],
    tolerance: int,
    despill: bool,
    feather_px: int,
    Image,  # type: ignore[no-untyped-def]
    ImageFilter,  # type: ignore[no-untyped-def]
) -> tuple[PILImage, bool]:
    """Pure-Pillow implementation, used when NumPy is not importable."""

    width, height = rgb_img.size
    src_pixels = rgb_img.load()

    inner, outer = _radii(tolerance)
    kr, kg, kb = key_rgb
    key_norm_sq = float(kr * kr + kg * kg + kb * kb)

    # Allocate the output bands as flat bytearrays — faster than putpixel.
    n = width * height
    out_r = bytearray(n)
    out_g = bytearray(n)
    out_b = bytearray(n)
    out_a = bytearray(n)

    despill_applied = False
    band_zero_width = outer <= inner  # tolerance==0 special case

    for y in range(height):
        row_offset = y * width
        for x in range(width):
            r, g, b = src_pixels[x, y]
            dr = r - kr
            dg = g - kg
            db = b - kb
            dist = (dr * dr + dg * dg + db * db) ** 0.5

            if band_zero_width:
                alpha = 0 if dist <= 0.0 else 255
            elif dist <= inner:
                alpha = 0
            elif dist >= outer:
                alpha = 255
            else:
                alpha = int(round(((dist - inner) / (outer - inner)) * 255.0))
                if alpha < 0:
                    alpha = 0
                elif alpha > 255:
                    alpha = 255

            # Despill — only touch partial-alpha pixels.
            if despill and 0 < alpha < 255 and key_norm_sq > 0.0:
                despill_applied = True
                strength = 1.0 - (alpha / 255.0)
                # Scalar projection coefficient of (r,g,b) onto (kr,kg,kb).
                coef = (r * kr + g * kg + b * kb) / key_norm_sq
                sub_r = coef * strength * kr
                sub_g = coef * strength * kg
                sub_b = coef * strength * kb
                r = int(max(0.0, min(255.0, r - sub_r)))
                g = int(max(0.0, min(255.0, g - sub_g)))
                b = int(max(0.0, min(255.0, b - sub_b)))

            idx = row_offset + x
            out_r[idx] = r
            out_g[idx] = g
            out_b[idx] = b
            out_a[idx] = alpha

    r_band = Image.frombytes("L", (width, height), bytes(out_r))
    g_band = Image.frombytes("L", (width, height), bytes(out_g))
    b_band = Image.frombytes("L", (width, height), bytes(out_b))
    a_band = Image.frombytes("L", (width, height), bytes(out_a))

    if feather_px > 0:
        a_band = a_band.filter(ImageFilter.GaussianBlur(radius=feather_px))

    rgba = Image.merge("RGBA", (r_band, g_band, b_band, a_band))
    return rgba, despill_applied


# ---------------------------------------------------------------------------
# Stats helper
# ---------------------------------------------------------------------------


def _pixel_stats(rgba: PILImage) -> tuple[int, int, int, int]:
    """Return ``(total, alpha==0, 0<alpha<255, alpha==255)`` for the image."""

    alpha = rgba.split()[-1]  # final RGBA channel
    histogram = alpha.histogram()  # list of length 256
    fully_keyed = histogram[0]
    fully_kept = histogram[255]
    partial = sum(histogram[1:255])
    total = fully_keyed + partial + fully_kept
    return (total, fully_keyed, partial, fully_kept)
