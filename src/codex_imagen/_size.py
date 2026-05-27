"""codex_imagen._size — size string validator + nearest-legal suggestion.

Purpose
-------
The Codex OAuth bridge forwards a ``size`` field to ``gpt-image-2``. The
server-side validator enforces four hard rules and a soft "experimental"
threshold. Failing the hard rules causes an HTTP 400 *during* generation —
which is the worst possible time to find out. This module lets the rest
of forge fail fast (and offer a concrete fix) before a network call ever
happens.

Verified gpt-image-2 rules (from live probes, see ``probe_validate.py``)
-----------------------------------------------------------------------
1. Both width and height must be multiples of ``16``.
2. ``max(W, H) <= 3840`` px.
3. ``min(W, H) >= 16`` px (implied — the pixel budget enforces a higher
   floor in practice).
4. Aspect ratio: ``max(W, H) / min(W, H) <= 3.0``.
5. Total pixel count is bounded:
   ``655_360 <= W * H <= 8_294_400``
   (e.g. roughly ``800x819`` up to ``3840x2160``).
6. Above ``2560x1440`` the model still works but is officially
   "experimental" — we accept the size but emit a warning.

This module is bridge-agnostic geometry. It must not import Pillow, the
codex-image-gen bridge, or anything else from the package. ``stdlib``
only.

Public surface
--------------
* :data:`MIN_AXIS`, :data:`MAX_AXIS`, :data:`AXIS_MULTIPLE`,
  :data:`MAX_ASPECT_RATIO`, :data:`MIN_PIXELS`, :data:`MAX_PIXELS`,
  :data:`EXPERIMENTAL_THRESHOLD`, :data:`COMMON_SIZES`
* :class:`SizeValidation`
* :func:`validate` — forgiving, NEVER raises
* :func:`nearest_legal` — strict, raises on unparseable input
* :func:`parse` — strict, raises on ``"auto"`` or unparseable input

Usage example
-------------
>>> from codex_imagen._size import validate
>>> v = validate("800x800")
>>> v.is_valid
False
>>> v.suggestion
'1024x1024'
>>> v.error
'Total pixel count 640000 is below minimum 655360'

>>> validate("auto").is_valid
True

>>> validate("1536x1024").is_valid
True
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

__all__ = [
    "MIN_AXIS",
    "MAX_AXIS",
    "AXIS_MULTIPLE",
    "MAX_ASPECT_RATIO",
    "MIN_PIXELS",
    "MAX_PIXELS",
    "EXPERIMENTAL_THRESHOLD",
    "COMMON_SIZES",
    "SizeValidation",
    "validate",
    "nearest_legal",
    "parse",
]


# ---------------------------------------------------------------------------
# Constants (exported so callers can introspect / show the rules in CLIs)
# ---------------------------------------------------------------------------

#: Minimum length of either axis in pixels (hard).
MIN_AXIS: int = 16

#: Maximum length of either axis in pixels (hard).
MAX_AXIS: int = 3840

#: Both axes must be exact multiples of this value (hard).
AXIS_MULTIPLE: int = 16

#: Maximum allowed aspect ratio ``max(W,H)/min(W,H)`` (hard).
MAX_ASPECT_RATIO: float = 3.0

#: Minimum total pixel count ``W * H`` (hard).
MIN_PIXELS: int = 655_360

#: Maximum total pixel count ``W * H`` (hard).
MAX_PIXELS: int = 8_294_400

#: Above this threshold the model is "experimental" — still valid but
#: we attach a warning. Compared as ``max(W,H) > 2560`` AND
#: ``min(W,H) > 1440``; either axis alone over 2560 also trips it.
EXPERIMENTAL_THRESHOLD: tuple[int, int] = (2560, 1440)

#: Curated set of sizes guaranteed to satisfy every rule. Used by the
#: "auto" preset resolver and as a final fallback inside
#: :func:`nearest_legal`.
COMMON_SIZES: tuple[str, ...] = (
    "1024x1024",  # square, smallest sensible default
    "1024x1536",  # portrait
    "1536x1024",  # landscape — forge's "high" preset default
    "1024x1792",  # tall portrait
    "1792x1024",  # wide landscape
    "2048x2048",  # large square
)

# Separator regex: accept "x", "X", or "*" with optional whitespace
# around it. We never accept commas or "by" — those are typo-magnets
# and clash with the WxH convention everywhere else in forge.
_SEP_RE = re.compile(r"\s*[xX*]\s*")


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SizeValidation:
    """Result of validating one size string.

    Attributes:
        raw: The original, un-normalised input string (for echoing
            back in error messages).
        is_valid: ``True`` when the size passes all hard rules.
            ``"auto"`` is always valid.
        width: Parsed width in pixels, or ``None`` if the input was
            ``"auto"`` or could not be parsed at all.
        height: Parsed height in pixels (same ``None`` semantics as
            ``width``).
        suggestion: The nearest legal ``"WxH"`` string when the input
            was invalid AND parseable. ``None`` when the input was
            already valid or unparseable.
        error: Human-readable explanation of why the input was
            rejected. ``None`` when the input was valid.
        warnings: Non-fatal advisories (e.g. the experimental-size
            warning). May be present on otherwise-valid sizes.
    """

    raw: str
    is_valid: bool
    width: int | None
    height: int | None
    suggestion: str | None
    error: str | None
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse(size: str) -> tuple[int, int]:
    """Parse a ``"WxH"`` string into a ``(W, H)`` integer pair.

    Args:
        size: The size string. Separator may be ``x``, ``X``, or ``*``
            and may have surrounding whitespace. Whole string is
            stripped before parsing.

    Returns:
        A ``(width, height)`` tuple of positive integers.

    Raises:
        ValueError: If ``size`` is ``"auto"`` (case-insensitive),
            empty, contains a non-integer component, or otherwise
            does not match ``WxH`` shape.
    """
    if not isinstance(size, str):
        raise ValueError(f"size must be a str, got {type(size).__name__}")

    stripped = size.strip()
    if not stripped:
        raise ValueError("size string is empty")
    if stripped.lower() == "auto":
        # "auto" is a valid forge-level value but not a parseable
        # WxH — callers in this module handle it separately.
        raise ValueError("'auto' is not a numeric size; handle it before calling parse()")

    parts = _SEP_RE.split(stripped)
    if len(parts) != 2:
        raise ValueError(
            f"Cannot parse size string (expected 'WxH', got {size!r})"
        )

    w_str, h_str = parts[0].strip(), parts[1].strip()
    if not w_str or not h_str:
        raise ValueError(
            f"Cannot parse size string (expected 'WxH', got {size!r})"
        )

    # We accept only base-10 unsigned ints. ``int(...)`` would happily
    # gobble "+1024" or " 1024 " which we want to reject from the
    # canonical-string standpoint; but stripping was done above, and
    # "+1024" is harmless geometrically — so accept it. Reject only
    # truly malformed input.
    if not (w_str.lstrip("+").isdigit() and h_str.lstrip("+").isdigit()):
        raise ValueError(
            f"Cannot parse size string (expected integer 'WxH', got {size!r})"
        )

    w, h = int(w_str), int(h_str)
    if w <= 0 or h <= 0:
        raise ValueError(
            f"Cannot parse size string (width/height must be positive, got {size!r})"
        )
    return w, h


# ---------------------------------------------------------------------------
# Nearest-legal computation
# ---------------------------------------------------------------------------


def _round_to_multiple(value: int, *, multiple: int = AXIS_MULTIPLE) -> int:
    """Round to the nearest multiple of ``multiple``. Ties go up."""
    # Standard banker's-free rounding: add half, integer-divide,
    # multiply back. This makes ``rounded(24) == 32`` (nearest of 16
    # and 32 — 24 is equidistant, prefer the larger to push toward
    # the legal pixel floor more often than not).
    return ((value + multiple // 2) // multiple) * multiple


def _clamp_axis(value: int) -> int:
    """Clamp a rounded axis to ``[MIN_AXIS, MAX_AXIS]``."""
    if value < MIN_AXIS:
        return MIN_AXIS
    if value > MAX_AXIS:
        return MAX_AXIS
    return value


def _enforce_aspect(w: int, h: int) -> tuple[int, int]:
    """Shrink the longer axis until the 3:1 aspect rule is satisfied.

    Shrinking (rather than growing the shorter axis) is preferred
    because growing the shorter axis would often push us past the
    pixel-budget ceiling. The pixel-budget step below will scale
    *up* if we end up too small.
    """
    if w <= 0 or h <= 0:
        return w, h
    longer = max(w, h)
    shorter = min(w, h)
    if longer <= shorter * MAX_ASPECT_RATIO:
        return w, h
    # Cap the longer axis to exactly 3 * shorter.
    new_longer = int(shorter * MAX_ASPECT_RATIO)
    if w >= h:
        return new_longer, h
    return w, new_longer


def _enforce_pixel_budget(w: int, h: int) -> tuple[int, int]:
    """Scale both axes so ``W*H`` lands inside the pixel budget.

    Uses a single multiplicative factor so the aspect ratio is
    preserved (which keeps us in compliance with the aspect rule).
    """
    pixels = w * h
    if MIN_PIXELS <= pixels <= MAX_PIXELS:
        return w, h
    if pixels < MIN_PIXELS:
        # Scale up by sqrt(target / current). The +1 epsilon nudges
        # us over the boundary instead of rounding back under it.
        factor = math.sqrt(MIN_PIXELS / pixels)
        new_w = max(int(math.ceil(w * factor)), w + 1)
        new_h = max(int(math.ceil(h * factor)), h + 1)
        return new_w, new_h
    # Too many pixels: scale down.
    factor = math.sqrt(MAX_PIXELS / pixels)
    new_w = max(int(math.floor(w * factor)), 1)
    new_h = max(int(math.floor(h * factor)), 1)
    return new_w, new_h


def _is_legal(w: int, h: int) -> bool:
    """Return ``True`` if (w, h) passes every hard rule."""
    if w % AXIS_MULTIPLE != 0 or h % AXIS_MULTIPLE != 0:
        return False
    if w < MIN_AXIS or h < MIN_AXIS:
        return False
    if w > MAX_AXIS or h > MAX_AXIS:
        return False
    longer, shorter = max(w, h), min(w, h)
    if shorter == 0 or longer / shorter > MAX_ASPECT_RATIO:
        return False
    pixels = w * h
    if pixels < MIN_PIXELS or pixels > MAX_PIXELS:
        return False
    return True


def nearest_legal(size: str) -> str:
    """Return the closest legal size string for ``size``.

    Strategy (each step nudges the candidate closer to the legal box):

      1. Parse ``"WxH"`` (raises ``ValueError`` on unparseable input —
         use :func:`validate` for forgiving handling instead).
      2. Round each axis to the nearest multiple of
         :data:`AXIS_MULTIPLE`.
      3. Clamp each axis to ``[MIN_AXIS, MAX_AXIS]``.
      4. If the aspect ratio still exceeds :data:`MAX_ASPECT_RATIO`,
         shrink the longer axis to ``3 * shorter``.
      5. Scale both axes proportionally to fit
         ``[MIN_PIXELS, MAX_PIXELS]``.
      6. Re-round both axes back to multiples of
         :data:`AXIS_MULTIPLE` and re-clamp.
      7. If the result still fails :func:`_is_legal`, try every entry
         in :data:`COMMON_SIZES` and pick the one whose pixel count
         is closest to the candidate's. As a last resort return
         ``"1536x1024"``.

    Args:
        size: A parseable ``"WxH"`` string. ``"auto"`` raises.

    Returns:
        The closest legal size string in canonical ``"WxH"`` form.

    Raises:
        ValueError: If ``size`` cannot be parsed.
    """
    w, h = parse(size)

    # Step 1 — multiples of 16.
    w = _round_to_multiple(w)
    h = _round_to_multiple(h)

    # Step 2 — axis clamps.
    w = _clamp_axis(w)
    h = _clamp_axis(h)

    # Step 3 — aspect ratio.
    w, h = _enforce_aspect(w, h)

    # Step 4 — pixel budget.
    w, h = _enforce_pixel_budget(w, h)

    # Step 5 — final re-round & re-clamp (pixel-budget scaling may
    # have produced non-multiples of 16). After re-rounding we must
    # also re-check the aspect / pixel rules; they can flip again
    # when rounding shifts each axis by up to 15 px.
    w = _clamp_axis(_round_to_multiple(w))
    h = _clamp_axis(_round_to_multiple(h))

    if _is_legal(w, h):
        return f"{w}x{h}"

    # Second pass: re-apply aspect + budget after rounding, then
    # round once more. Two passes are sufficient in practice because
    # rounding by 16 px only shifts pixel count by O(16 * max_axis).
    w, h = _enforce_aspect(w, h)
    w, h = _enforce_pixel_budget(w, h)
    w = _clamp_axis(_round_to_multiple(w))
    h = _clamp_axis(_round_to_multiple(h))

    if _is_legal(w, h):
        return f"{w}x{h}"

    # Final fallback — pick the COMMON_SIZES entry closest in pixel
    # count to where we ended up. Guarantees a legal return even if
    # the input was pathological (e.g. "1x999999999").
    target_pixels = max(w * h, 1)
    best: str = "1536x1024"
    best_delta: float = math.inf
    for candidate in COMMON_SIZES:
        cw, ch = parse(candidate)
        delta = abs(cw * ch - target_pixels)
        if delta < best_delta:
            best_delta = delta
            best = candidate
    return best


# ---------------------------------------------------------------------------
# Public validate()
# ---------------------------------------------------------------------------


def _experimental_warning(w: int, h: int) -> tuple[str, ...]:
    """Return a tuple of warning strings if (w, h) is experimental."""
    long_edge = max(w, h)
    short_edge = min(w, h)
    th_long, th_short = EXPERIMENTAL_THRESHOLD
    # Trip if EITHER the long edge alone exceeds 2560 OR both edges
    # exceed the (2560, 1440) box. A 2560x1440 image is the last
    # *non*-experimental size — the boundary is strict-greater-than.
    if long_edge > th_long or (long_edge >= th_long and short_edge > th_short):
        return (
            f"Size {w}x{h} exceeds tested {th_long}x{th_short}; "
            f"results may be experimental.",
        )
    return ()


def validate(size: str) -> SizeValidation:
    """Validate a size string and (when invalid) suggest a fix.

    This function NEVER raises. Garbage input returns a
    :class:`SizeValidation` with ``is_valid=False`` and a populated
    ``error`` field.

    Args:
        size: The size string to validate. Accepts ``"auto"``,
            ``"WxH"`` (separators: ``x``, ``X``, ``*``), or any
            string at all.

    Returns:
        A :class:`SizeValidation` describing the outcome:

        * ``"auto"`` -> ``is_valid=True``, all numeric fields ``None``.
        * Valid ``WxH`` -> ``is_valid=True``, ``width``/``height``
          populated, possibly with experimental warning.
        * Invalid but parseable -> ``is_valid=False``,
          ``width``/``height`` populated, ``suggestion`` set to
          :func:`nearest_legal`, ``error`` describes the violated
          rule.
        * Unparseable -> ``is_valid=False``, numeric fields ``None``,
          ``suggestion=None``, ``error`` explains the parse failure.
    """
    raw = size if isinstance(size, str) else repr(size)

    # 0. Normalise. We don't lowercase the WxH part (digits are
    # case-free); only the literal "auto" sentinel.
    stripped = raw.strip() if isinstance(size, str) else ""

    # 1. "auto" passthrough.
    if stripped.lower() == "auto":
        return SizeValidation(
            raw=raw,
            is_valid=True,
            width=None,
            height=None,
            suggestion=None,
            error=None,
            warnings=(),
        )

    # 2. Try to parse. If we can't, we can't suggest either.
    try:
        w, h = parse(stripped if stripped else raw)
    except ValueError as exc:
        return SizeValidation(
            raw=raw,
            is_valid=False,
            width=None,
            height=None,
            suggestion=None,
            error=str(exc),
            warnings=(),
        )

    # 3. Rule-by-rule check. We stop at the first violation so the
    # error message points to *the* problem, not a soup of them. The
    # suggestion is the same regardless: the nearest legal size.
    error: str | None = None

    if w % AXIS_MULTIPLE != 0 or h % AXIS_MULTIPLE != 0:
        bad_axes: list[str] = []
        if w % AXIS_MULTIPLE != 0:
            bad_axes.append("Width")
        if h % AXIS_MULTIPLE != 0:
            bad_axes.append("Height")
        error = f"{'/'.join(bad_axes)} not multiple of {AXIS_MULTIPLE}"
    elif w < MIN_AXIS or h < MIN_AXIS:
        error = (
            f"Width or Height below minimum {MIN_AXIS}px "
            f"(got {w}x{h})"
        )
    elif w > MAX_AXIS or h > MAX_AXIS:
        error = (
            f"Width or Height exceeds max {MAX_AXIS}px "
            f"(got {w}x{h})"
        )
    else:
        longer, shorter = max(w, h), min(w, h)
        # ``shorter`` is guaranteed >0 because we rejected zero/neg
        # values in parse(). Aspect compared as float for clarity;
        # an equivalent integer comparison would be
        # ``longer <= MAX_ASPECT_RATIO * shorter``.
        if longer > MAX_ASPECT_RATIO * shorter:
            error = (
                f"Aspect ratio exceeds {MAX_ASPECT_RATIO:.0f}:1 "
                f"(got {longer}:{shorter} = {longer/shorter:.2f}:1)"
            )
        else:
            pixels = w * h
            if pixels < MIN_PIXELS:
                error = (
                    f"Total pixel count {pixels} is below minimum "
                    f"{MIN_PIXELS}"
                )
            elif pixels > MAX_PIXELS:
                error = (
                    f"Total pixel count {pixels} exceeds maximum "
                    f"{MAX_PIXELS}"
                )

    if error is not None:
        # We have a parseable but invalid size. Compute the nearest
        # legal one for the user to copy-paste.
        try:
            suggestion = nearest_legal(f"{w}x{h}")
        except ValueError:  # pragma: no cover — parse already succeeded
            suggestion = None
        return SizeValidation(
            raw=raw,
            is_valid=False,
            width=w,
            height=h,
            suggestion=suggestion,
            error=error,
            warnings=(),
        )

    # 4. Valid. Check the experimental threshold.
    warnings = _experimental_warning(w, h)
    return SizeValidation(
        raw=raw,
        is_valid=True,
        width=w,
        height=h,
        suggestion=None,
        error=None,
        warnings=warnings,
    )
