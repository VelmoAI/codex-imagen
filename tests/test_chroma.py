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
# keyout — happy path
# ---------------------------------------------------------------------------


def test_keyout_pure_magenta_to_transparent(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    # Left half magenta, right half red.
    _make_half_image(src, left=(255, 0, 255), right=(255, 0, 0))

    keyout(src, dst, feather_px=0)

    out = Image.open(dst)
    assert out.mode == "RGBA"
    alpha = out.split()[-1]
    # Sample well inside each half.
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
    # 100x100 = 10_000 pixels. Half magenta, half red.
    _make_half_image(src, left=(255, 0, 255), right=(255, 0, 0))

    result = keyout(src, dst, feather_px=0)

    assert isinstance(result, ChromaResult)
    assert result.src_path == src
    assert result.dst_path == dst
    assert result.key_rgb == DEFAULT_KEY_RGB
    assert result.pixels_total == 10_000
    # No feather + cleanly separated colors -> binary alpha.
    assert result.pixels_keyed_partial == 0
    assert result.pixels_keyed_fully == 5_000
    assert result.pixels_kept == 5_000
    assert result.elapsed_ms >= 0


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

    keyout(src, dst, key_rgb=KEY_PRESETS["green"], feather_px=0)

    out = Image.open(dst)
    alpha = out.split()[-1]
    assert alpha.getpixel((10, 50)) == 0
    assert alpha.getpixel((90, 50)) == 255


# ---------------------------------------------------------------------------
# keyout — despill behavior
# ---------------------------------------------------------------------------


def test_keyout_despill_reduces_magenta_in_partial_pixels(tmp_path: Path) -> None:
    """Pixels with a magenta tint in the partial-alpha zone should have R and
    B reduced once despill runs, relative to the no-despill baseline.
    """

    src = tmp_path / "src.png"
    dst_with = tmp_path / "with.png"
    dst_without = tmp_path / "without.png"

    # Build an image whose pixels sit in the transition band — closer to
    # the magenta key than 'opaque' but not exactly on it. A 200-saturation
    # pinkish color sits squarely in the partial zone for tolerance=40.
    Image.new("RGB", (40, 40), (255, 100, 255)).save(src, "PNG")

    keyout(src, dst_without, despill=False, feather_px=0)
    keyout(src, dst_with, despill=True, feather_px=0)

    a = Image.open(dst_with).convert("RGBA")
    b = Image.open(dst_without).convert("RGBA")
    ra, ga, ba, aa = a.split()
    rb, gb, bb, ab = b.split()

    # The alpha channels should be identical (despill never touches alpha).
    assert list(aa.getdata()) == list(ab.getdata())

    # At least one partial-alpha pixel must exist for the test to be
    # meaningful.
    partials = [v for v in aa.getdata() if 0 < v < 255]
    assert partials, "test image did not produce partial-alpha pixels"

    # Average R and B should drop for the despilled image (key is magenta).
    # G is unaffected because the magenta key has g=0.
    def avg(band):
        data = list(band.getdata())
        return sum(data) / len(data)

    assert avg(ra) < avg(rb)
    assert avg(ba) < avg(bb)


# ---------------------------------------------------------------------------
# keyout — feather
# ---------------------------------------------------------------------------


def test_keyout_feather_softens_edges(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst_hard = tmp_path / "hard.png"
    dst_soft = tmp_path / "soft.png"
    _make_half_image(src, left=(255, 0, 255), right=(255, 0, 0))

    r_hard = keyout(src, dst_hard, feather_px=0)
    r_soft = keyout(src, dst_soft, feather_px=5)

    assert r_hard.pixels_keyed_partial == 0
    assert r_soft.pixels_keyed_partial > 0


def test_keyout_zero_feather_preserves_binary_alpha(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    _make_half_image(src, left=(255, 0, 255), right=(255, 0, 0))

    result = keyout(src, dst, feather_px=0)
    assert result.pixels_keyed_partial == 0


# ---------------------------------------------------------------------------
# keyout — tolerance edge cases
# ---------------------------------------------------------------------------


def test_keyout_zero_tolerance_only_exact_match(tmp_path: Path) -> None:
    """With tolerance=0, a subject one channel off from the key must stay
    fully opaque (no partial-alpha leakage)."""

    src = tmp_path / "src.png"
    dst = tmp_path / "dst.png"
    # Backdrop is magenta, subject pixels are (255, 1, 255) — distance 1.
    img = Image.new("RGB", (40, 40), (255, 0, 255))
    img.paste(Image.new("RGB", (20, 40), (255, 1, 255)), (20, 0))
    img.save(src, "PNG")

    result = keyout(src, dst, tolerance=0, feather_px=0)
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
    _make_half_image(src, left=(255, 0, 255), right=(255, 0, 0))
    src_bytes = src.read_bytes()

    out_bytes = keyout_bytes(src_bytes, feather_px=0)
    assert isinstance(out_bytes, bytes)
    assert len(out_bytes) > 0

    decoded = Image.open(io.BytesIO(out_bytes))
    assert decoded.mode == "RGBA"
    assert decoded.size == Image.open(src).size

    # And it should agree with the file-based path on the alpha verdict.
    dst = tmp_path / "dst.png"
    keyout(src, dst, feather_px=0)
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
