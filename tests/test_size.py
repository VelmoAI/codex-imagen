"""Unit tests for ``codex_imagen._size``.

The validator is pure stdlib geometry — no network, no Pillow, no
bridge. So the tests can hammer it with arbitrary inputs cheaply.

Test plan
---------
* The six rules each get a positive + negative test.
* Boundary cases (3840x1280 vs 3840x1279, pixel-budget edges).
* Experimental-threshold warning behaviour.
* Parser separator + whitespace handling.
* :func:`validate` smoke-test against garbage strings — must not raise.
"""

from __future__ import annotations

import random
import string

import pytest

from codex_imagen._size import (
    AXIS_MULTIPLE,
    COMMON_SIZES,
    EXPERIMENTAL_THRESHOLD,
    MAX_ASPECT_RATIO,
    MAX_AXIS,
    MAX_PIXELS,
    MIN_AXIS,
    MIN_PIXELS,
    SizeValidation,
    nearest_legal,
    parse,
    validate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_legal(w: int, h: int) -> bool:
    """Local replica of the legality check (kept in-test so the test
    file documents the rules independently of the module)."""
    if w % AXIS_MULTIPLE != 0 or h % AXIS_MULTIPLE != 0:
        return False
    if w < MIN_AXIS or h < MIN_AXIS:
        return False
    if w > MAX_AXIS or h > MAX_AXIS:
        return False
    longer, shorter = max(w, h), min(w, h)
    if longer / shorter > MAX_ASPECT_RATIO:
        return False
    pixels = w * h
    return MIN_PIXELS <= pixels <= MAX_PIXELS


# ---------------------------------------------------------------------------
# "auto" handling
# ---------------------------------------------------------------------------


def test_auto_is_valid() -> None:
    v = validate("auto")
    assert v.is_valid is True
    assert v.width is None
    assert v.height is None
    assert v.suggestion is None
    assert v.error is None
    assert v.warnings == ()


def test_auto_case_insensitive() -> None:
    for raw in ("AUTO", "Auto", " auto ", "AuTo"):
        assert validate(raw).is_valid is True, raw


# ---------------------------------------------------------------------------
# Positive cases — valid sizes
# ---------------------------------------------------------------------------


def test_valid_square_1024() -> None:
    v = validate("1024x1024")
    assert v.is_valid is True
    assert (v.width, v.height) == (1024, 1024)
    assert v.error is None
    assert v.warnings == ()


def test_valid_landscape_1536x1024() -> None:
    v = validate("1536x1024")
    assert v.is_valid is True
    assert (v.width, v.height) == (1536, 1024)


def test_valid_portrait_1024x1536() -> None:
    v = validate("1024x1536")
    assert v.is_valid is True
    assert (v.width, v.height) == (1024, 1536)


def test_valid_2048x2048() -> None:
    v = validate("2048x2048")
    assert v.is_valid is True
    assert v.warnings == ()  # 2048x2048 is below experimental threshold


def test_all_common_sizes_are_valid() -> None:
    for size in COMMON_SIZES:
        v = validate(size)
        assert v.is_valid, f"COMMON_SIZES entry {size!r} failed validation: {v.error}"


# ---------------------------------------------------------------------------
# Negative cases — invalid sizes
# ---------------------------------------------------------------------------


def test_invalid_unparseable_returns_error_no_suggestion() -> None:
    v = validate("not_a_size")
    assert v.is_valid is False
    assert v.width is None
    assert v.height is None
    assert v.suggestion is None
    assert v.error is not None and "Cannot parse" in v.error


def test_invalid_not_multiple_of_16_returns_suggestion() -> None:
    v = validate("1000x1000")  # 1000 % 16 = 8
    assert v.is_valid is False
    assert v.width == 1000 and v.height == 1000
    assert v.suggestion is not None
    assert "multiple of 16" in v.error.lower()
    # And the suggestion itself must actually be legal.
    sw, sh = parse(v.suggestion)
    assert _is_legal(sw, sh)


def test_invalid_too_large_returns_suggestion() -> None:
    v = validate("5000x3000")
    assert v.is_valid is False
    assert v.suggestion is not None
    sw, sh = parse(v.suggestion)
    assert _is_legal(sw, sh)
    assert sw <= MAX_AXIS and sh <= MAX_AXIS


def test_invalid_too_small_returns_suggestion() -> None:
    v = validate("128x128")  # multiple of 16, but well under min pixels
    assert v.is_valid is False
    assert v.suggestion is not None
    sw, sh = parse(v.suggestion)
    assert _is_legal(sw, sh)
    assert sw * sh >= MIN_PIXELS


def test_invalid_aspect_ratio_too_wide_returns_suggestion() -> None:
    # 3520x1024 -> aspect 3.4375 (multiple of 16, in pixel budget)
    v = validate("3520x1024")
    assert v.is_valid is False
    assert "aspect" in v.error.lower()
    assert v.suggestion is not None
    sw, sh = parse(v.suggestion)
    assert _is_legal(sw, sh)


def test_invalid_aspect_ratio_too_tall_returns_suggestion() -> None:
    # 1024x3520 -> portrait version of the above
    v = validate("1024x3520")
    assert v.is_valid is False
    assert "aspect" in v.error.lower()
    assert v.suggestion is not None
    sw, sh = parse(v.suggestion)
    assert _is_legal(sw, sh)


# ---------------------------------------------------------------------------
# Experimental warning
# ---------------------------------------------------------------------------


def test_experimental_size_is_valid_with_warning() -> None:
    # 2576x1456 — just above (2560, 1440), multiple of 16, in budget.
    v = validate("2576x1456")
    assert v.is_valid is True
    assert v.warnings != ()
    assert "experimental" in v.warnings[0].lower()


def test_2560x1440_is_not_experimental_warning() -> None:
    v = validate("2560x1440")
    assert v.is_valid is True
    assert v.warnings == ()


# ---------------------------------------------------------------------------
# Boundary cases
# ---------------------------------------------------------------------------


def test_3840x1280_at_aspect_3_to_1_is_valid() -> None:
    # Long edge max, exactly 3:1, exactly on max-axis boundary.
    v = validate("3840x1280")
    assert v.is_valid is True
    assert (v.width, v.height) == (3840, 1280)
    # Above 2560x1440 so it should carry the experimental warning.
    assert v.warnings != ()


def test_3840x1279_just_over_aspect_ratio_invalid() -> None:
    # 1279 isn't a multiple of 16, so the *first* failing rule the
    # validator reports is the multiple-of-16 rule. Suggestion must
    # still be legal regardless.
    v = validate("3840x1279")
    assert v.is_valid is False
    assert v.suggestion is not None
    sw, sh = parse(v.suggestion)
    assert _is_legal(sw, sh)


def test_3840x1264_just_over_aspect_ratio_invalid() -> None:
    # 1264 IS a multiple of 16, so this isolates the aspect rule.
    # 3840/1264 ≈ 3.038 — over the 3:1 limit.
    v = validate("3840x1264")
    assert v.is_valid is False
    assert "aspect" in v.error.lower()


def test_pixel_budget_min_boundary() -> None:
    # 800x819 = 655_200 (just under 655_360). 819 isn't a multiple
    # of 16; we don't care which rule fires first — only that this
    # is rejected and produces a legal suggestion.
    v = validate("800x819")
    assert v.is_valid is False
    assert v.suggestion is not None
    sw, sh = parse(v.suggestion)
    assert _is_legal(sw, sh)


def test_pixel_budget_max_boundary() -> None:
    # 3840x2160 = 8_294_400 — exactly the cap.
    v = validate("3840x2160")
    assert v.is_valid is True
    assert (v.width, v.height) == (3840, 2160)


def test_max_axis_boundary_3840() -> None:
    # Tall portrait at exactly the max axis on the long side.
    v = validate("1280x3840")
    assert v.is_valid is True


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_x_separator_lowercase() -> None:
    assert parse("1024x768") == (1024, 768)


def test_parse_X_separator_uppercase() -> None:
    assert parse("1024X768") == (1024, 768)


def test_parse_asterisk_separator() -> None:
    assert parse("1024*768") == (1024, 768)


def test_parse_with_whitespace() -> None:
    assert parse("  1024 x 768  ") == (1024, 768)


def test_parse_raises_on_auto() -> None:
    with pytest.raises(ValueError):
        parse("auto")


def test_parse_raises_on_empty() -> None:
    with pytest.raises(ValueError):
        parse("")


def test_parse_raises_on_non_integer() -> None:
    with pytest.raises(ValueError):
        parse("1024.5x768")


def test_parse_raises_on_negative() -> None:
    with pytest.raises(ValueError):
        parse("-1024x768")


def test_parse_raises_on_garbage() -> None:
    with pytest.raises(ValueError):
        parse("hello world")


def test_parse_raises_on_three_parts() -> None:
    with pytest.raises(ValueError):
        parse("1024x768x100")


# ---------------------------------------------------------------------------
# nearest_legal
# ---------------------------------------------------------------------------


def test_nearest_legal_clamps_too_large() -> None:
    result = nearest_legal("5000x3000")
    w, h = parse(result)
    assert w <= MAX_AXIS and h <= MAX_AXIS
    assert _is_legal(w, h)


def test_nearest_legal_rounds_to_multiple_of_16() -> None:
    result = nearest_legal("1025x1023")
    w, h = parse(result)
    assert w % AXIS_MULTIPLE == 0
    assert h % AXIS_MULTIPLE == 0
    assert _is_legal(w, h)


def test_nearest_legal_fixes_aspect_ratio() -> None:
    # 3840x1024 has aspect ratio 3.75 — too wide. Suggestion must
    # land at or under 3:1.
    result = nearest_legal("3840x1024")
    w, h = parse(result)
    assert max(w, h) / min(w, h) <= MAX_ASPECT_RATIO
    assert _is_legal(w, h)


def test_nearest_legal_grows_too_small() -> None:
    result = nearest_legal("128x128")
    w, h = parse(result)
    assert w * h >= MIN_PIXELS
    assert _is_legal(w, h)


def test_nearest_legal_already_valid_passes_through() -> None:
    # Already valid input: nearest_legal must return *a* legal
    # answer; it doesn't have to be identical but should be very
    # close. For a clean multiple-of-16 inside the budget we expect
    # the same value back.
    assert nearest_legal("1536x1024") == "1536x1024"
    assert nearest_legal("1024x1024") == "1024x1024"


def test_nearest_legal_raises_on_unparseable() -> None:
    with pytest.raises(ValueError):
        nearest_legal("not_a_size")


def test_nearest_legal_handles_pathological_input() -> None:
    # 1x1 — far below every threshold. The fallback must still
    # return a legal size.
    result = nearest_legal("1x1")
    w, h = parse(result)
    assert _is_legal(w, h)


def test_nearest_legal_handles_extreme_aspect() -> None:
    # 10000x100 — both axes out of range AND aspect 100:1.
    result = nearest_legal("10000x100")
    w, h = parse(result)
    assert _is_legal(w, h)


# ---------------------------------------------------------------------------
# Robustness smoke test
# ---------------------------------------------------------------------------


def test_validate_never_raises_on_garbage() -> None:
    """Throw a wide variety of bad inputs at validate(); none must raise."""
    rng = random.Random(42)
    garbage_inputs: list[str] = [
        "",
        " ",
        "\n",
        "auto auto",
        "1024",
        "x",
        "x1024",
        "1024x",
        "1024,768",
        "1024 by 768",
        "abc x def",
        "1024.0x768.0",
        "0x0",
        "-1x-1",
        "1e3x1e3",
        "🚀",
        "x" * 1000,
    ]
    # Plus a random-string pile.
    for _ in range(50):
        n = rng.randint(0, 30)
        garbage_inputs.append(
            "".join(rng.choices(string.printable, k=n))
        )

    for raw in garbage_inputs:
        try:
            result = validate(raw)
        except Exception as exc:  # pragma: no cover - the assert below fires first
            pytest.fail(f"validate({raw!r}) raised {exc!r}")
        assert isinstance(result, SizeValidation)
        # For each garbage input, either it's valid (e.g. lucky "auto"
        # collisions — practically impossible here) or it's invalid
        # with a populated error string.
        if not result.is_valid:
            assert result.error, f"missing error for {raw!r}"


def test_validate_returns_size_validation_type() -> None:
    """All return paths produce a SizeValidation instance."""
    assert isinstance(validate("auto"), SizeValidation)
    assert isinstance(validate("1024x1024"), SizeValidation)
    assert isinstance(validate("invalid"), SizeValidation)
    assert isinstance(validate("800x800"), SizeValidation)


def test_size_validation_is_frozen() -> None:
    """SizeValidation must be immutable so callers can pass it around safely."""
    v = validate("1024x1024")
    with pytest.raises(Exception):  # FrozenInstanceError subclasses Exception
        v.is_valid = False  # type: ignore[misc]


def test_experimental_threshold_constant_shape() -> None:
    """Defend against accidental edits to the constant's shape."""
    assert isinstance(EXPERIMENTAL_THRESHOLD, tuple)
    assert len(EXPERIMENTAL_THRESHOLD) == 2
    assert EXPERIMENTAL_THRESHOLD == (2560, 1440)
