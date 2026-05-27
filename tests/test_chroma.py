"""Tests for codex_imagen._chroma — chroma-key pipeline.

All test imagery is synthesized in-memory with Pillow, so the suite has no
on-disk fixtures and is fully hermetic. Both the NumPy fast path and the
pure-Pillow fallback are exercised — the fallback gets explicit coverage
via a monkeypatch that hides ``numpy`` from ``_compute_rgba``.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

# Make ``src/`` importable when running ``pytest`` from the repo root without
# the package being installed.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from codex_imagen import _chroma  # noqa: E402
from codex_imagen._chroma import (  # noqa: E402
    ChromaResult,
    DEFAULT_KEY_RGB,
    KEY_PRESETS,
    check_complex_subject,
    hex_to_rgb,
    keyout,
    keyout_bytes,
)


# ---------------------------------------------------------------------------
# Synthetic-image helpers
# ---------------------------------------------------------------------------


def _make_half_image(
    path: Path,
    *,
    left: tuple[int, int, int],
    right: tuple[int, int, int],
    size: tuple[int, int] = (100, 100),
) -> None:
    """Write a PNG where the left half is colour ``left`` and the right ``right``."""

    img = Image.new("RGB", size, left)
    img.paste(Image.new("RGB", (size[0] // 2, size[1]), right), (size[0] // 2, 0))
    img.save(path, "PNG")


def _make_solid_image(
    path: Path,
    color: tuple[int, int, int],
    size: tuple[int, int] = (20, 20),
) -> None:
    Image.new("RGB", size, color).save(path, "PNG")


# ---------------------------------------------------------------------------
# hex_to_rgb
# ---------------------------------------------------------------------------


def test_hex_to_rgb_basic() -> None:
    assert hex_to_rgb("#FF00FF") == (255, 0, 255)


def test_hex_to_rgb_no_hash() -> None:
    assert hex_to_rgb("FF00FF") == (255, 0, 255)


def test_hex_to_rgb_shorthand() -> None:
    assert hex_to_rgb("#F0F") == (255, 0, 255)


def test_hex_to_rgb_lowercase() -> None:
    assert hex_to_rgb("#ff00ff") == (255, 0, 255)


def test_hex_to_rgb_raises_on_garbage() -> None:
    with pytest.raises(ValueError):
        hex_to_rgb("not-a-color")


def test_hex_to_rgb_raises_on_wrong_length() -> None:
    with pytest.raises(ValueError):
        hex_to_rgb("#FF00")


# ---------------------------------------------------------------------------
# Default key color: now green
# ---------------------------------------------------------------------------


def test_default_key_is_green() -> None:
    """The new default key color must be green (#00FF00), not magenta."""
    assert DEFAULT_KEY_RGB == (0, 255, 0)


# ---------------------------------------------------------------------------
# keyout — happy path with auto_key=None (explicit key control)
# ---------------------------------------------------------------------------


def test_keyout_pure_green_to_transparent(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    # Left half green (default key), right half red.
    _make_half_image(src, left=(0, 255, 0), right=(255, 0, 0))

    # auto_key=None so we use the explicit key_rgb without border sampling.
    keyout(src, dst, feather_px=0, auto_key=None)

    out = Image.open(dst)
    assert out.mode == "RGBA"
    alpha = out.split()[-1]
    # Sample well inside each half.
    assert alpha.getpixel((10, 50)) == 0, "green half should be transparent"
    assert alpha.getpixel((90, 50)) == 255, "red half should be opaque"


def test_keyout_pure_magenta_to_transparent_explicit_key(tmp_path: Path) -> None:
    """Explicitly passing magenta key still works (backward compat)."""
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_half_image(src, left=(255, 0, 255), right=(255, 0, 0))

    # Must pass auto_key=None to use the explicit magenta key without border-sampling.
    keyout(src, dst, key_rgb=(255, 0, 255), feather_px=0, auto_key=None)

    out = Image.open(dst)
    assert out.mode == "RGBA"
    alpha = out.split()[-1]
    assert alpha.getpixel((10, 50)) == 0, "magenta half should be transparent"
    assert alpha.getpixel((90, 50)) == 255, "red half should be opaque"


def test_keyout_writes_rgba_png(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_solid_image(src, (255, 0, 0))

    keyout(src, dst)

    out = Image.open(dst)
    assert out.mode == "RGBA"
    assert out.format == "PNG"


def test_keyout_creates_missing_parent_dir(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst = tmp_path / "nested" / "deeper" / "out.png"
    _make_solid_image(src, (255, 0, 0))

    keyout(src, dst)
    assert dst.exists()
    assert dst.parent.is_dir()


def test_keyout_returns_chroma_result_with_correct_stats(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    # 100x100 = 10_000 pixels. Half green (default key), half red.
    _make_half_image(src, left=(0, 255, 0), right=(255, 0, 0))

    # Use edge_erode_px=0 + auto_key=None for exact pixel counts.
    result = keyout(src, dst, feather_px=0, edge_erode_px=0, auto_key=None)

    assert isinstance(result, ChromaResult)
    assert result.src_path == src
    assert result.dst_path == dst
    assert result.key_rgb == DEFAULT_KEY_RGB   # requested key is green
    assert result.effective_key_rgb == DEFAULT_KEY_RGB  # effective (no auto-sample)
    assert result.pixels_total == 10_000
    # No feather + no erosion + cleanly separated colors -> binary alpha.
    assert result.pixels_keyed_partial == 0
    assert result.pixels_keyed_fully == 5_000
    assert result.pixels_kept == 5_000
    assert result.elapsed_ms >= 0
    assert result.warnings == ()


# ---------------------------------------------------------------------------
# keyout — validation
# ---------------------------------------------------------------------------


def test_keyout_invalid_tolerance_raises(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_solid_image(src, (255, 0, 0))
    with pytest.raises(ValueError):
        keyout(src, dst, tolerance=150)


def test_keyout_invalid_rgb_raises(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_solid_image(src, (255, 0, 0))
    with pytest.raises(ValueError):
        keyout(src, dst, key_rgb=(300, 0, 0))


# ---------------------------------------------------------------------------
# keyout — alternate key colors
# ---------------------------------------------------------------------------


def test_keyout_with_green_key(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_half_image(src, left=(0, 255, 0), right=(255, 0, 0))

    keyout(src, dst, key_rgb=KEY_PRESETS["green"], feather_px=0, auto_key=None)

    out = Image.open(dst)
    alpha = out.split()[-1]
    assert alpha.getpixel((10, 50)) == 0
    assert alpha.getpixel((90, 50)) == 255


def test_keyout_with_magenta_key_explicit(tmp_path: Path) -> None:
    """Passing magenta explicitly (for green subjects) works as before."""
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_half_image(src, left=(255, 0, 255), right=(255, 0, 0))

    keyout(src, dst, key_rgb=KEY_PRESETS["magenta"], feather_px=0, auto_key=None)

    out = Image.open(dst)
    alpha = out.split()[-1]
    assert alpha.getpixel((10, 50)) == 0
    assert alpha.getpixel((90, 50)) == 255


# ---------------------------------------------------------------------------
# keyout — despill behavior (green key)
# ---------------------------------------------------------------------------


def test_keyout_despill_reduces_green_in_partial_pixels(tmp_path: Path) -> None:
    """Pixels with a green tint in the partial-alpha zone should have G reduced
    once despill runs (dominance-capping), relative to the no-despill baseline.
    """
    src = tmp_path / "src.png"
    dst_with = tmp_path / "with.png"
    dst_without = tmp_path / "without.png"

    # Build an image whose pixels sit in the transition band — closer to
    # the green key than 'opaque' but not exactly on it.
    # A greenish color (100, 230, 100): green channel dominant.
    Image.new("RGB", (40, 40), (100, 230, 100)).save(src, "PNG")

    keyout(src, dst_without, key_rgb=(0, 255, 0), despill=False, feather_px=0, auto_key=None)
    keyout(src, dst_with, key_rgb=(0, 255, 0), despill=True, feather_px=0, auto_key=None)

    a = Image.open(dst_with).convert("RGBA")
    b = Image.open(dst_without).convert("RGBA")
    ra, ga, ba, aa = a.split()
    rb, gb, bb, ab = b.split()

    # The alpha channels should be identical (despill never touches alpha).
    assert list(aa.getdata()) == list(ab.getdata())

    # At least one partial-alpha pixel must exist for the test to be meaningful.
    partials = [v for v in aa.getdata() if 0 < v < 255]
    assert partials, "test image did not produce partial-alpha pixels"

    # Average G should drop for the despilled image (key is green, G is the spill channel).
    def avg(band):
        data = list(band.getdata())
        return sum(data) / len(data)

    # Despilled G must be less than or equal to non-despilled G.
    assert avg(ga) <= avg(gb), (
        f"Despilled green avg ({avg(ga):.1f}) should be <= non-despilled ({avg(gb):.1f})"
    )


def test_keyout_despill_reduces_magenta_in_partial_pixels(tmp_path: Path) -> None:
    """Pixels with a magenta tint in the partial-alpha zone should have R and
    B reduced once despill runs, relative to the no-despill baseline.
    Tests backward compat with explicit magenta key.
    """

    src = tmp_path / "src.png"
    dst_with = tmp_path / "with.png"
    dst_without = tmp_path / "without.png"

    # Build an image whose pixels sit in the partial-alpha transition band.
    # (200, 150, 200) is partially key-colored (magenta spill) but with
    # enough G to keep it in the transition zone (not fully keyed out).
    Image.new("RGB", (40, 40), (200, 150, 200)).save(src, "PNG")

    keyout(src, dst_without, key_rgb=(255, 0, 255), despill=False, feather_px=0, auto_key=None)
    keyout(src, dst_with, key_rgb=(255, 0, 255), despill=True, feather_px=0, auto_key=None)

    a = Image.open(dst_with).convert("RGBA")
    b = Image.open(dst_without).convert("RGBA")
    ra, ga, ba, aa = a.split()
    rb, gb, bb, ab = b.split()

    # The alpha channels should be identical (despill never touches alpha).
    assert list(aa.getdata()) == list(ab.getdata())

    partials = [v for v in aa.getdata() if 0 < v < 255]
    assert partials, "test image did not produce partial-alpha pixels"

    def avg(band):
        data = list(band.getdata())
        return sum(data) / len(data)

    # R and B should drop for the despilled image (magenta key: R and B are spill).
    assert avg(ra) <= avg(rb)
    assert avg(ba) <= avg(bb)


# ---------------------------------------------------------------------------
# keyout — feather
# ---------------------------------------------------------------------------


def test_keyout_feather_softens_edges(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst_hard = tmp_path / "hard.png"
    dst_soft = tmp_path / "soft.png"
    _make_half_image(src, left=(0, 255, 0), right=(255, 0, 0))

    r_hard = keyout(src, dst_hard, feather_px=0, auto_key=None)
    r_soft = keyout(src, dst_soft, feather_px=5, auto_key=None)

    assert r_hard.pixels_keyed_partial == 0
    assert r_soft.pixels_keyed_partial > 0


def test_keyout_zero_feather_preserves_binary_alpha(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_half_image(src, left=(0, 255, 0), right=(255, 0, 0))

    result = keyout(src, dst, feather_px=0, auto_key=None)
    assert result.pixels_keyed_partial == 0


# ---------------------------------------------------------------------------
# keyout — tolerance edge cases
# ---------------------------------------------------------------------------


def test_keyout_subject_pixels_stay_opaque(tmp_path: Path) -> None:
    """Subject pixels that are far from the key color must stay fully opaque.

    Red (255, 0, 0) has large distance from green key (0, 255, 0), so it
    should remain at alpha=255 after keyout.
    """
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    # Backdrop is green, subject pixels are pure red — very far from key.
    img = Image.new("RGB", (40, 40), (0, 255, 0))
    img.paste(Image.new("RGB", (20, 40), (255, 0, 0)), (20, 0))
    img.save(src, "PNG")

    result = keyout(src, dst, key_rgb=(0, 255, 0), feather_px=0, auto_key=None)
    assert result.pixels_keyed_partial == 0
    out = Image.open(dst).convert("RGBA")
    alpha = out.split()[-1]
    # Backdrop side fully keyed, subject side fully opaque.
    assert alpha.getpixel((5, 20)) == 0
    assert alpha.getpixel((30, 20)) == 255


# ---------------------------------------------------------------------------
# keyout_bytes round-trip
# ---------------------------------------------------------------------------


def test_keyout_bytes_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    _make_half_image(src, left=(0, 255, 0), right=(255, 0, 0))
    src_bytes = src.read_bytes()

    out_bytes = keyout_bytes(src_bytes, feather_px=0, auto_key=None)
    assert isinstance(out_bytes, bytes)
    assert len(out_bytes) > 0

    decoded = Image.open(io.BytesIO(out_bytes))
    assert decoded.mode == "RGBA"
    assert decoded.size == Image.open(src).size

    # And it should agree with the file-based path on the alpha verdict.
    dst = tmp_path / "dst.png"
    keyout(src, dst, feather_px=0, auto_key=None)
    file_alpha = list(Image.open(dst).convert("RGBA").split()[-1].getdata())
    bytes_alpha = list(decoded.split()[-1].getdata())
    assert file_alpha == bytes_alpha


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_keyout_source_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        keyout(tmp_path / "nope.png", tmp_path / "out.png")


def test_pillow_missing_raises_importerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If PIL fails to import we surface a clear, actionable ImportError."""

    def boom():  # type: ignore[no-untyped-def]
        raise ImportError(
            "Pillow is required for the chroma-key pipeline. "
            "Install it with: pip install Pillow"
        )

    monkeypatch.setattr(_chroma, "_import_pillow", boom)

    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    src.write_bytes(b"")  # ensure existence check passes
    with pytest.raises(ImportError, match="Pillow"):
        keyout(src, dst)


# ---------------------------------------------------------------------------
# Pink/green fringe elimination tests
# ---------------------------------------------------------------------------


def _make_antialiased_blob_on_green(
    path: Path,
    size: int = 80,
) -> None:
    """Create a synthetic image: solid green background with a white circle
    that has anti-aliased soft edges blending toward green.
    """
    img = Image.new("RGB", (size, size), (0, 255, 0))
    pixels = img.load()
    cx = cy = size // 2
    r_inner = size // 4
    r_outer = r_inner + 6  # 6-pixel soft ring
    for y in range(size):
        for x in range(size):
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if dist <= r_inner:
                pixels[x, y] = (255, 255, 255)  # solid white subject
            elif dist <= r_outer:
                # Anti-aliased fringe: blend white toward green
                t = (dist - r_inner) / (r_outer - r_inner)
                r = int(255 * (1 - t))
                g = 255
                b = int(255 * (1 - t))
                pixels[x, y] = (r, g, b)
            # else: stays green
    img.save(path, "PNG")


def test_no_green_fringe_after_keyout(tmp_path: Path) -> None:
    """Synthetic image with green + soft anti-aliased blob.

    After keyout with green key, no pixel in the result should have the
    'green halo' signature (G > 200 AND R < 100 AND B < 100) with non-zero alpha.
    """
    src = tmp_path / "blob.png"
    dst = tmp_path / "blob_keyed.png"
    _make_antialiased_blob_on_green(src)

    keyout(src, dst, key_rgb=(0, 255, 0), despill=True, feather_px=0,
           edge_erode_px=1, auto_key=None)

    result_img = Image.open(dst).convert("RGBA")
    r_band, g_band, b_band, a_band = result_img.split()

    green_fringe_count = 0
    for r, g, b, a in zip(
        r_band.getdata(), g_band.getdata(), b_band.getdata(), a_band.getdata()
    ):
        # A "green halo" pixel: green-ish RGB AND still visible (alpha > 0).
        if g > 200 and r < 100 and b < 100 and a > 0:
            green_fringe_count += 1

    assert green_fringe_count == 0, (
        f"Found {green_fringe_count} green fringe pixel(s) with non-zero alpha "
        "after keyout with despill+erosion"
    )


def test_no_pink_fringe_after_keyout(tmp_path: Path) -> None:
    """Original pink-fringe test now uses magenta key explicitly."""
    src = tmp_path / "blob.png"
    dst = tmp_path / "blob_keyed.png"

    img = Image.new("RGB", (80, 80), (255, 0, 255))
    pixels = img.load()
    cx = cy = 40
    r_inner, r_outer = 20, 26
    for y in range(80):
        for x in range(80):
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if dist <= r_inner:
                pixels[x, y] = (255, 255, 255)
            elif dist <= r_outer:
                t = (dist - r_inner) / (r_outer - r_inner)
                pixels[x, y] = (255, int(255 * (1 - t)), 255)
    img.save(src, "PNG")

    keyout(src, dst, key_rgb=(255, 0, 255), despill=True, feather_px=0,
           edge_erode_px=1, auto_key=None)

    result_img = Image.open(dst).convert("RGBA")
    r_band, g_band, b_band, a_band = result_img.split()

    pink_fringe_count = 0
    for r, g, b, a in zip(
        r_band.getdata(), g_band.getdata(), b_band.getdata(), a_band.getdata()
    ):
        if r > 200 and b > 200 and g < 100 and a > 0:
            pink_fringe_count += 1

    assert pink_fringe_count == 0, (
        f"Found {pink_fringe_count} pink fringe pixel(s) with non-zero alpha"
    )


def test_edge_erode_px_zero_disables_erosion(tmp_path: Path) -> None:
    """edge_erode_px=0 must be accepted without error and disable erosion."""
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_half_image(src, left=(0, 255, 0), right=(255, 0, 0))

    result = keyout(src, dst, feather_px=0, edge_erode_px=0, auto_key=None)

    assert isinstance(result, ChromaResult)
    out = Image.open(dst)
    assert out.mode == "RGBA"
    alpha = out.split()[-1]
    assert alpha.getpixel((10, 50)) == 0    # green half: fully keyed
    assert alpha.getpixel((90, 50)) == 255  # red half: fully opaque


def test_edge_erode_px_two_erodes_more_aggressively(tmp_path: Path) -> None:
    """edge_erode_px=2 should key out more fringe pixels than edge_erode_px=1."""
    src = tmp_path / "src.png"
    dst1 = tmp_path / "erode1.png"
    dst2 = tmp_path / "erode2.png"
    _make_half_image(src, left=(0, 255, 0), right=(255, 0, 0), size=(200, 200))

    from codex_imagen._chroma import _pixel_stats

    keyout(src, dst1, feather_px=0, edge_erode_px=1, auto_key=None)
    keyout(src, dst2, feather_px=0, edge_erode_px=2, auto_key=None)

    _, keyed1, _, _ = _pixel_stats(Image.open(dst1).convert("RGBA"))
    _, keyed2, _, _ = _pixel_stats(Image.open(dst2).convert("RGBA"))

    assert keyed2 >= keyed1, (
        f"edge_erode_px=2 should key at least as many pixels as =1: "
        f"{keyed2} vs {keyed1}"
    )


def test_edge_erode_px_invalid_raises(tmp_path: Path) -> None:
    """Negative edge_erode_px must raise ValueError."""
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_solid_image(src, (255, 0, 0))
    with pytest.raises(ValueError, match="edge_erode_px"):
        keyout(src, dst, edge_erode_px=-1)


def test_existing_chroma_tests_still_pass_with_default_erosion(tmp_path: Path) -> None:
    """Smoke-test that the basic half-image keyout still works with erosion=1."""
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_half_image(src, left=(0, 255, 0), right=(255, 0, 0))

    result = keyout(src, dst, feather_px=0, auto_key=None)  # default edge_erode_px=1

    assert isinstance(result, ChromaResult)
    out = Image.open(dst).convert("RGBA")
    alpha = out.split()[-1]
    # The green-side center should be fully transparent.
    assert alpha.getpixel((10, 50)) == 0
    # The red-side center should be fully opaque (far from the eroded boundary).
    assert alpha.getpixel((90, 50)) == 255
    # Total pixel count unchanged.
    assert result.pixels_total == 10_000


# ---------------------------------------------------------------------------
# NEW: Auto-key border sampling tests
# ---------------------------------------------------------------------------


def test_auto_key_border_samples_actual_background_green(tmp_path: Path) -> None:
    """When auto_key='border', the effective key is sampled from the border,
    not the requested key_rgb. A solid-green image should be keyed out even
    if the user nominally passes a wrong key_rgb.
    """
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    # Solid pure green image (simulating model rendering #00FF00 backdrop).
    _make_solid_image(src, (0, 255, 0), size=(50, 50))

    # Request with a different key_rgb — auto_key=border should override it.
    result = keyout(
        src, dst,
        key_rgb=(255, 0, 255),  # wrong key nominally
        feather_px=0, edge_erode_px=0,
        auto_key="border",  # sample from border
    )

    # effective_key_rgb should be sampled from the border, not the requested.
    # Since the image is solid green, median border = (0, 255, 0).
    assert result.effective_key_rgb == (0, 255, 0), (
        f"Expected effective_key_rgb=(0,255,0), got {result.effective_key_rgb}"
    )
    # All pixels should be keyed out (solid background, no subject).
    assert result.pixels_kept == 0, "All-green image should be fully keyed out"


def test_auto_key_corners_mode(tmp_path: Path) -> None:
    """auto_key='corners' should also produce a valid effective key."""
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    # Solid green, all corners are green.
    _make_solid_image(src, (0, 255, 0), size=(60, 60))

    result = keyout(
        src, dst,
        key_rgb=(0, 255, 0),
        feather_px=0, edge_erode_px=0,
        auto_key="corners",
    )
    assert result.effective_key_rgb == (0, 255, 0)
    assert result.pixels_kept == 0


def test_auto_key_none_uses_key_rgb_exactly(tmp_path: Path) -> None:
    """auto_key=None must use the provided key_rgb without any sampling."""
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_solid_image(src, (0, 255, 0), size=(40, 40))

    result = keyout(
        src, dst,
        key_rgb=(0, 255, 0),
        feather_px=0, edge_erode_px=0,
        auto_key=None,
    )
    # With auto_key=None, effective_key_rgb == requested key_rgb.
    assert result.effective_key_rgb == (0, 255, 0)
    assert result.key_rgb == result.effective_key_rgb


def test_auto_key_handles_model_drift(tmp_path: Path) -> None:
    """Model might render #01FE00 instead of #00FF00. Border sampling should
    detect the actual color and still key it out cleanly.
    """
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    # Slightly drifted green — model drift simulation.
    drifted_green = (1, 254, 0)
    _make_solid_image(src, drifted_green, size=(50, 50))

    result = keyout(
        src, dst,
        key_rgb=(0, 255, 0),  # requested exact green
        feather_px=0, edge_erode_px=0,
        auto_key="border",  # should detect the drifted color
    )
    # Effective key should be the drifted color (median border sample).
    assert result.effective_key_rgb == drifted_green
    # All pixels should be keyed out (no subject, just background).
    assert result.pixels_kept == 0


# ---------------------------------------------------------------------------
# NEW: Dual-threshold soft matte tests
# ---------------------------------------------------------------------------


def test_dual_threshold_fully_transparent_at_low_distance(tmp_path: Path) -> None:
    """Pixels at distance ≤ transparent_threshold → alpha=0."""
    from codex_imagen._chroma import _soft_alpha_dual

    # At distance = transparent_threshold (12), alpha must be 0.
    assert _soft_alpha_dual(12, 12.0, 220.0) == 0
    # At distance = 0, definitely alpha=0.
    assert _soft_alpha_dual(0, 12.0, 220.0) == 0


def test_dual_threshold_fully_opaque_at_high_distance(tmp_path: Path) -> None:
    """Pixels at distance ≥ opaque_threshold → alpha=255."""
    from codex_imagen._chroma import _soft_alpha_dual

    assert _soft_alpha_dual(220, 12.0, 220.0) == 255
    assert _soft_alpha_dual(255, 12.0, 220.0) == 255


def test_dual_threshold_smoothstep_in_between(tmp_path: Path) -> None:
    """Pixels in the transition zone get a smoothstepped alpha in (0, 255)."""
    from codex_imagen._chroma import _soft_alpha_dual

    # Midpoint between 12 and 220.
    mid = (12 + 220) // 2
    alpha_mid = _soft_alpha_dual(mid, 12.0, 220.0)
    assert 0 < alpha_mid < 255, f"Mid-point alpha should be between 0 and 255, got {alpha_mid}"
    # At slightly above transparent_threshold, should be close to 0.
    alpha_low = _soft_alpha_dual(20, 12.0, 220.0)
    assert 0 < alpha_low < 128, f"Near-transparent alpha should be low, got {alpha_low}"


# ---------------------------------------------------------------------------
# NEW: Dominance-capping despill unit tests
# ---------------------------------------------------------------------------


def test_dominance_capping_despill_green_key(tmp_path: Path) -> None:
    """For green key: a pixel with G=200, R=50, B=50 should have G capped to
    max(non-spill) - 1 = max(R, B) - 1 = 50 - 1 = 49 after dominance despill.
    """
    from codex_imagen._chroma import _cleanup_spill_dominance

    key = (0, 255, 0)  # green
    # G is the spill channel; R and B are non-spill.
    # anchor = max(R, B) = max(50, 50) = 50 → cap = 49.
    rgb = (50, 200, 50)
    result = _cleanup_spill_dominance(rgb, key, alpha=100)
    r, g, b = result
    # G must be capped to ≤ max(R, B) - 1.
    assert g <= max(r, b) - 1 or g == 0, (
        f"G={g} should be capped to ≤ max(R={r},B={b})-1"
    )


def test_dominance_capping_despill_magenta_key(tmp_path: Path) -> None:
    """For magenta key (#FF00FF): R and B are spill channels.
    A pixel (200, 50, 200) should have R and B capped to ≤ G - 1 = 49.
    """
    from codex_imagen._chroma import _cleanup_spill_dominance

    key = (255, 0, 255)  # magenta
    # R and B are spill channels; G is non-spill.
    # anchor = G = 50 → cap = 49.
    rgb = (200, 50, 200)
    result = _cleanup_spill_dominance(rgb, key, alpha=100)
    r, g, b = result
    assert r <= g - 1 or r == 0, f"R={r} should be capped to ≤ G={g}-1"
    assert b <= g - 1 or b == 0, f"B={b} should be capped to ≤ G={g}-1"


def test_dominance_capping_despill_fully_opaque_untouched() -> None:
    """Fully opaque pixels (alpha ≥ 252) must NOT be despilled."""
    from codex_imagen._chroma import _cleanup_spill_dominance

    key = (0, 255, 0)
    rgb = (50, 200, 50)
    # alpha=255 → should return rgb unchanged.
    result = _cleanup_spill_dominance(rgb, key, alpha=255)
    assert result == rgb


# ---------------------------------------------------------------------------
# NEW: Complex-subject keyword warning tests
# ---------------------------------------------------------------------------


def test_complex_subject_warning_fires_for_fur() -> None:
    """Prompt containing 'fur' should trigger the warning."""
    warning = check_complex_subject("a fluffy white cat with soft fur")
    assert warning is not None
    assert "fur" in warning.lower() or "complex edges" in warning.lower()


def test_complex_subject_warning_fires_for_hair() -> None:
    """Prompt containing 'hair' should trigger the warning."""
    warning = check_complex_subject("a portrait of a woman with long flowing hair")
    assert warning is not None


def test_complex_subject_warning_fires_for_glass() -> None:
    """Prompt containing 'glass' should trigger the warning."""
    warning = check_complex_subject("a glass vase on a white table")
    assert warning is not None


def test_complex_subject_warning_fires_for_smoke() -> None:
    warning = check_complex_subject("wispy smoke rising from a candle")
    assert warning is not None


def test_complex_subject_warning_not_fired_for_ceramic_mug() -> None:
    """Plain non-complex subjects should NOT trigger the warning."""
    warning = check_complex_subject("a ceramic mug on a white background")
    assert warning is None


def test_complex_subject_warning_not_fired_for_simple_product() -> None:
    warning = check_complex_subject("a brass diya oil lamp, isolated subject")
    assert warning is None


def test_complex_subject_warning_not_fired_for_robot() -> None:
    warning = check_complex_subject("a robot in a sci-fi environment")
    assert warning is None


def test_complex_subject_warning_word_boundary_matching() -> None:
    """'haiku' should NOT trigger the 'hair' keyword (no word boundary match)."""
    warning = check_complex_subject("a beautiful haiku poem")
    assert warning is None


def test_complex_subject_warning_liquid_keyword() -> None:
    warning = check_complex_subject("liquid metal pouring from a container")
    assert warning is not None


def test_complex_subject_warning_feather_keyword() -> None:
    warning = check_complex_subject("an eagle with detailed feathers")
    assert warning is not None


def test_complex_subject_warning_translucent_keyword() -> None:
    warning = check_complex_subject("translucent jellyfish underwater")
    assert warning is not None


# ---------------------------------------------------------------------------
# NEW: ChromaResult has effective_key_rgb field
# ---------------------------------------------------------------------------


def test_chroma_result_effective_key_rgb_present(tmp_path: Path) -> None:
    """ChromaResult must expose effective_key_rgb as a field."""
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_solid_image(src, (0, 255, 0))

    result = keyout(src, dst, auto_key=None)
    assert hasattr(result, "effective_key_rgb")
    assert isinstance(result.effective_key_rgb, tuple)
    assert len(result.effective_key_rgb) == 3


def test_chroma_result_has_warnings_field(tmp_path: Path) -> None:
    """ChromaResult must expose a warnings tuple."""
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_solid_image(src, (0, 255, 0))

    result = keyout(src, dst, auto_key=None)
    assert hasattr(result, "warnings")
    assert isinstance(result.warnings, tuple)
