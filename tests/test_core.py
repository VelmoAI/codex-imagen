"""Tests for codex_imagen.core.forge() and ForgeOptions.

All tests monkeypatch the bridge layer; nothing here calls a real API.
The strategy:

* ``codex_imagen.core._bridge.health_check`` is patched to return a known
  health dict.
* ``codex_imagen.core._bridge.generate`` is patched with a fake that writes
  a tiny PNG to ``output_path`` and returns a ``BridgeResult``-shaped
  ``SimpleNamespace``.
* ``codex_imagen.core._chroma.keyout`` is patched in transparency tests
  with a fake that just copies the raw file to the destination.

The fakes mirror the contracts of the real bridge/chroma functions
exactly so the orchestrator code under test sees no difference.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_imagen import core
from codex_imagen.core import ForgeImage, ForgeOptions, ForgeResult, forge


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _good_health() -> dict[str, Any]:
    """Default healthy bridge dict (ok=True, all probes pass)."""
    return {
        "ok": True,
        "codex_image_gen_available": True,
        "pillow_available": True,
        "auth_file_exists": True,
        "auth_file_path": "/fake/auth.json",
        "auth_expires_in_seconds": 3600,
        "hint": None,
    }


def _bad_health(hint: str = "codex login required") -> dict[str, Any]:
    """Failing health dict — used to verify the early-return path."""
    return {
        "ok": False,
        "codex_image_gen_available": False,
        "pillow_available": True,
        "auth_file_exists": False,
        "auth_file_path": "/fake/auth.json",
        "auth_expires_in_seconds": None,
        "hint": hint,
    }


class FakeBridge:
    """Records every generate() call and writes a 1-byte stub image."""

    def __init__(self, *, fail_indices: set[int] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        # Indices (in call order, 1-based) that should raise.
        self.fail_indices: set[int] = fail_indices or set()
        self._counter = 0

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        self._counter += 1
        n = self._counter
        self.calls.append(dict(kwargs))

        if n in self.fail_indices:
            raise RuntimeError(f"forced failure #{n}")

        out: Path = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        # Write a tiny stub so size.stat() works.
        out.write_bytes(b"FAKE" + str(n).encode())

        return SimpleNamespace(
            path=str(out),
            bytes=4 + len(str(n)),
            mime_type="image/png",
            response_id=f"resp_{n}",
            call_id=f"ig_{n}",
            revised_prompt=f"revised-{n}",
            reference_images=tuple(kwargs.get("references") or ()),
            partial_image_paths=(),
            elapsed_ms=1,
            warnings=(),
        )


def _fake_chroma_keyout(
    src_path: Path,
    dst_path: Path,
    **kwargs: Any,
) -> None:
    """Stand-in for _chroma.keyout — just copy bytes."""
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_path, dst_path)


# ---------------------------------------------------------------------------
# Shared fixture: patch health + bridge to healthy/successful defaults
# ---------------------------------------------------------------------------


@pytest.fixture
def healthy_bridge(monkeypatch: pytest.MonkeyPatch) -> FakeBridge:
    """Patch core._bridge.health_check to ok and core._bridge.generate to a fake."""
    monkeypatch.setattr(core._bridge, "health_check", lambda: _good_health())
    fb = FakeBridge()
    monkeypatch.setattr(core._bridge, "generate", fb)
    return fb


# ---------------------------------------------------------------------------
# 1) Health failure short-circuits before any bridge call
# ---------------------------------------------------------------------------


def test_forge_returns_health_failure_without_api_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        core._bridge, "health_check", lambda: _bad_health("codex login required")
    )
    fb = FakeBridge()
    monkeypatch.setattr(core._bridge, "generate", fb)

    result = forge(prompt="hello", output_dir=tmp_path)

    assert result.ok is False
    assert result.error == "codex login required"
    assert result.images == ()
    assert result.health.ok is False
    assert result.health.hint == "codex login required"
    # The bridge MUST NOT have been called.
    assert fb.calls == []


# ---------------------------------------------------------------------------
# 2-5) Input validation via ForgeOptions.__post_init__
# ---------------------------------------------------------------------------


def test_forge_invalid_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mode must be one of"):
        forge(prompt="hi", output_dir=tmp_path, mode="bogus")


def test_forge_invalid_batch_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_mode must be one of"):
        forge(prompt="hi", output_dir=tmp_path, batch_mode="nonsense")


def test_forge_invalid_output_format_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="output_format must be one of"):
        forge(prompt="hi", output_dir=tmp_path, output_format="bmp")


def test_forge_invalid_count_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="count must be >= 1"):
        forge(prompt="hi", output_dir=tmp_path, count=0)


def test_forge_invalid_chroma_tolerance_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="chroma_tolerance must be in 0..100"):
        forge(prompt="hi", output_dir=tmp_path, chroma_tolerance=500)


def test_forge_invalid_parallel_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="parallel must be >= 1"):
        forge(prompt="hi", output_dir=tmp_path, parallel=0)


# ---------------------------------------------------------------------------
# 6) Path conversion
# ---------------------------------------------------------------------------


def test_forge_path_options_converted_to_path(tmp_path: Path) -> None:
    options = ForgeOptions(prompt="hi", output_dir=str(tmp_path))
    assert isinstance(options.output_dir, Path)
    assert options.output_dir == tmp_path


# ---------------------------------------------------------------------------
# 7) Single happy path
# ---------------------------------------------------------------------------


def test_forge_single_prompt_happy_path(
    healthy_bridge: FakeBridge,
    tmp_path: Path,
) -> None:
    result = forge(prompt="a coffee mug", output_dir=tmp_path, mode="raw")

    assert result.ok is True
    assert len(result.images) == 1
    assert result.batch_mode == "single"
    img = result.images[0]
    assert isinstance(img, ForgeImage)
    assert img.index == 0
    assert img.path.exists()
    assert img.bytes > 0
    assert img.transparent is False
    # Exactly one bridge call
    assert len(healthy_bridge.calls) == 1
    # Manifest should be written.
    assert result.manifest_path is not None
    assert result.manifest_path.exists()


# ---------------------------------------------------------------------------
# 8) List of prompts auto-detects to parallel
# ---------------------------------------------------------------------------


def test_forge_parallel_list_prompts(
    healthy_bridge: FakeBridge,
    tmp_path: Path,
) -> None:
    result = forge(
        prompt=["a", "b", "c"],
        output_dir=tmp_path,
        mode="raw",
        parallel=2,
    )
    assert result.ok is True
    assert result.batch_mode == "parallel"
    assert len(result.images) == 3
    assert len(healthy_bridge.calls) == 3


# ---------------------------------------------------------------------------
# 9-10) Size validation
# ---------------------------------------------------------------------------


def test_forge_size_invalid_uses_nearest_legal_with_warning(
    healthy_bridge: FakeBridge,
    tmp_path: Path,
) -> None:
    # 800x800 is parseable but too small (below pixel floor).
    result = forge(
        prompt="x",
        output_dir=tmp_path,
        mode="raw",
        size="800x800",
    )
    assert result.ok is True
    assert any(
        "invalid" in w and "nearest legal" in w for w in result.warnings
    ), f"missing size-fallback warning; got: {result.warnings}"
    # The bridge should have received the *suggested* size, not "800x800".
    sent_size = healthy_bridge.calls[0].get("size") or healthy_bridge.calls[0]
    # size comes through advanced -> bridge as a kwarg
    assert healthy_bridge.calls[0]["size"] != "800x800"


def test_forge_size_invalid_unrecoverable_returns_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(core._bridge, "health_check", lambda: _good_health())
    fb = FakeBridge()
    monkeypatch.setattr(core._bridge, "generate", fb)

    # Unparseable string — _size.validate returns suggestion=None.
    result = forge(prompt="x", output_dir=tmp_path, size="not-a-size")

    assert result.ok is False
    assert result.error is not None
    assert "could not be validated" in result.error
    # No bridge call.
    assert fb.calls == []


# ---------------------------------------------------------------------------
# 11-12) Skills
# ---------------------------------------------------------------------------


def test_forge_skills_loaded_in_medium_mode(
    healthy_bridge: FakeBridge,
    tmp_path: Path,
) -> None:
    skill_file = tmp_path / "myskill.md"
    skill_file.write_text("# My skill\n\nBe extra editorial.\n", encoding="utf-8")

    result = forge(
        prompt="x",
        output_dir=tmp_path / "out",
        mode="medium",
        skills=[str(skill_file)],
    )
    assert result.ok is True
    # The bridge call should have received an `instructions=` arg that
    # mentions the skill body. The fake bridge stores all kwargs verbatim.
    # The orchestrator forwards `instructions` from prompt_build -> bridge.
    instructions = healthy_bridge.calls[0].get("instructions") or ""
    assert "Be extra editorial" in instructions


def test_forge_skills_ignored_in_raw_mode_with_warning(
    healthy_bridge: FakeBridge,
    tmp_path: Path,
) -> None:
    skill_file = tmp_path / "myskill.md"
    skill_file.write_text("# Should be ignored\n", encoding="utf-8")

    result = forge(
        prompt="x",
        output_dir=tmp_path / "out",
        mode="raw",
        skills=[str(skill_file)],
    )
    assert result.ok is True
    assert any("skills ignored in raw mode" in w for w in result.warnings)
    # The bridge's instructions should NOT contain the skill body.
    instructions = healthy_bridge.calls[0].get("instructions") or ""
    assert "Should be ignored" not in instructions


def test_forge_missing_skill_file_warns_but_succeeds(
    healthy_bridge: FakeBridge,
    tmp_path: Path,
) -> None:
    result = forge(
        prompt="x",
        output_dir=tmp_path / "out",
        mode="medium",
        skills=[str(tmp_path / "does-not-exist.md")],
    )
    assert result.ok is True
    assert any("not found" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 13-14) Transparency
# ---------------------------------------------------------------------------


def test_forge_transparent_routes_to_chroma(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(core._bridge, "health_check", lambda: _good_health())
    fb = FakeBridge()
    monkeypatch.setattr(core._bridge, "generate", fb)

    chroma_called: list[dict[str, Any]] = []

    def fake_keyout(src_path: Path, dst_path: Path, **kwargs: Any) -> None:
        chroma_called.append({"src": src_path, "dst": dst_path, **kwargs})
        _fake_chroma_keyout(src_path, dst_path, **kwargs)

    monkeypatch.setattr(core._chroma, "keyout", fake_keyout)

    result = forge(
        prompt="x",
        output_dir=tmp_path,
        mode="raw",
        transparent=True,
    )
    assert result.ok is True
    assert len(chroma_called) == 1
    assert result.images[0].transparent is True
    # The bridge call should have background='opaque' forced.
    assert fb.calls[0].get("background") == "opaque"


def test_forge_transparent_without_pillow_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bad_pillow = _good_health()
    bad_pillow["pillow_available"] = False
    monkeypatch.setattr(core._bridge, "health_check", lambda: bad_pillow)
    fb = FakeBridge()
    monkeypatch.setattr(core._bridge, "generate", fb)

    # Make sure the chroma keyer would explode if accidentally invoked.
    def boom(*a: Any, **kw: Any) -> None:
        raise AssertionError("chroma.keyout should not be called when pillow missing")

    monkeypatch.setattr(core._chroma, "keyout", boom)

    result = forge(
        prompt="x",
        output_dir=tmp_path,
        mode="raw",
        transparent=True,
    )
    assert result.ok is True
    assert result.images[0].transparent is False
    assert any(
        "Pillow is not installed" in w or "pillow" in w.lower()
        for w in result.warnings
    )


# ---------------------------------------------------------------------------
# 15-16) Partial / total failures
# ---------------------------------------------------------------------------


def test_forge_partial_failure_partial_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(core._bridge, "health_check", lambda: _good_health())
    # Fail the second call (call #2 in invocation order).
    fb = FakeBridge(fail_indices={2})
    monkeypatch.setattr(core._bridge, "generate", fb)

    result = forge(
        prompt=["a", "b", "c"],
        output_dir=tmp_path,
        mode="raw",
        parallel=1,  # serial so failure ordering is deterministic
    )
    assert result.ok is True
    assert len(result.images) == 2
    # Surviving images preserve their original indices.
    indices = sorted(img.index for img in result.images)
    assert indices == [0, 2]
    # Warning summarises the failure.
    assert any("failed" in w for w in result.warnings)


def test_forge_all_failures_returns_ok_false(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(core._bridge, "health_check", lambda: _good_health())
    fb = FakeBridge(fail_indices={1, 2})
    monkeypatch.setattr(core._bridge, "generate", fb)

    result = forge(
        prompt=["a", "b"],
        output_dir=tmp_path,
        mode="raw",
        parallel=1,
    )
    assert result.ok is False
    assert result.images == ()
    assert result.error is not None
    assert "all calls failed" in result.error


# ---------------------------------------------------------------------------
# 17) Manifest
# ---------------------------------------------------------------------------


def test_forge_manifest_written_jsonl(
    healthy_bridge: FakeBridge,
    tmp_path: Path,
) -> None:
    result = forge(
        prompt=["a", "b", "c"],
        output_dir=tmp_path,
        mode="raw",
        parallel=1,
    )
    assert result.ok is True
    assert result.manifest_path is not None
    lines = result.manifest_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)
        # Spot-check required keys per SPEC manifest format.
        assert "run_id" in parsed
        assert "index" in parsed
        assert "path" in parsed
        assert "bytes" in parsed
        assert "size" in parsed
        assert "batch_mode" in parsed


# ---------------------------------------------------------------------------
# 18) elapsed_ms populated
# ---------------------------------------------------------------------------


def test_forge_elapsed_ms_populated(
    healthy_bridge: FakeBridge,
    tmp_path: Path,
) -> None:
    result = forge(prompt="x", output_dir=tmp_path, mode="raw")
    assert isinstance(result.elapsed_ms, int)
    assert result.elapsed_ms >= 0


def test_forge_health_failure_still_reports_elapsed_ms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(core._bridge, "health_check", lambda: _bad_health())
    monkeypatch.setattr(core._bridge, "generate", FakeBridge())
    result = forge(prompt="x", output_dir=tmp_path)
    assert isinstance(result.elapsed_ms, int)
    assert result.elapsed_ms >= 0


# ---------------------------------------------------------------------------
# 19) Defensive copy of `vars`
# ---------------------------------------------------------------------------


def test_forge_options_defensive_copy_of_vars(tmp_path: Path) -> None:
    user_vars = {"char": "alice"}
    options = ForgeOptions(prompt="hi {char}", output_dir=tmp_path, vars=user_vars)
    # Caller mutates the original dict — must not affect the frozen options.
    user_vars["char"] = "bob"
    user_vars["new"] = "x"
    assert options.vars == {"char": "alice"}


def test_forge_options_defensive_copy_of_advanced(tmp_path: Path) -> None:
    user_adv = {"reasoning_effort": "high"}
    options = ForgeOptions(prompt="x", output_dir=tmp_path, advanced=user_adv)
    user_adv["reasoning_effort"] = "low"
    user_adv["new_key"] = "boom"
    assert options.advanced == {"reasoning_effort": "high"}


# ---------------------------------------------------------------------------
# Sanity: forge() is reachable through the package re-export
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 20) Manifest distinguishes prompt-builder `mode` from `batch_mode`
# ---------------------------------------------------------------------------


def test_forge_manifest_mode_and_batch_mode_are_distinct(
    healthy_bridge: FakeBridge,
    tmp_path: Path,
) -> None:
    """SPEC §Manifest: `mode` is the builder mode (raw/medium/high/max);
    `batch_mode` is the orchestration mode (single/parallel/...).
    They must carry distinct values.
    """
    result = forge(
        prompt=["a coffee mug", "a teapot"],
        output_dir=tmp_path,
        mode="medium",
        batch_mode="parallel",
        parallel=1,
    )
    assert result.ok is True
    assert result.batch_mode == "parallel"
    # Read manifest back and check both fields per entry.
    assert result.manifest_path is not None
    lines = result.manifest_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        entry = json.loads(line)
        assert entry["mode"] == "medium", (
            f"manifest mode must be the builder mode, got {entry['mode']!r}"
        )
        assert entry["batch_mode"] == "parallel", (
            f"manifest batch_mode must be the orchestration mode, "
            f"got {entry['batch_mode']!r}"
        )
        assert entry["mode"] != entry["batch_mode"]


# ---------------------------------------------------------------------------
# 21) `mode="auto"` resolves to a concrete reasoning_effort
# ---------------------------------------------------------------------------


def test_forge_auto_mode_resolves_reasoning_effort_for_transparent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """auto + transparent should resolve to medium → reasoning_effort=medium
    gets splatted into the bridge call (not left unset).
    """
    monkeypatch.setattr(core._bridge, "health_check", lambda: _good_health())
    fb = FakeBridge()
    monkeypatch.setattr(core._bridge, "generate", fb)
    monkeypatch.setattr(core._chroma, "keyout", _fake_chroma_keyout)

    result = forge(
        prompt="a coffee mug",
        output_dir=tmp_path,
        mode="auto",
        transparent=True,
    )
    assert result.ok is True
    assert fb.calls[0].get("reasoning_effort") == "medium"
    # And the result.mode should reflect the resolution.
    assert result.mode == "medium"


def test_forge_auto_mode_resolves_to_raw_when_no_enrichment(
    healthy_bridge: FakeBridge,
    tmp_path: Path,
) -> None:
    """auto + plain str prompt + no skills/transparent/extras → resolves
    to raw, which maps to reasoning_effort="none".
    """
    result = forge(
        prompt="a coffee mug",
        output_dir=tmp_path,
        mode="auto",
    )
    assert result.ok is True
    assert healthy_bridge.calls[0].get("reasoning_effort") == "none"
    assert result.mode == "raw"


def test_forge_auto_mode_resolves_to_high_for_verbatim_dict(
    healthy_bridge: FakeBridge,
    tmp_path: Path,
) -> None:
    """auto + dict prompt with non-empty text_verbatim → resolves to high
    (mirrors _prompts.build verbatim auto-bump).
    """
    result = forge(
        prompt={"subject": "a sign", "text_verbatim": "OPEN"},
        output_dir=tmp_path,
        mode="auto",
    )
    assert result.ok is True
    assert healthy_bridge.calls[0].get("reasoning_effort") == "high"
    assert result.mode == "high"


# ---------------------------------------------------------------------------
# 22) Manifest-write failure is demoted to a warning, run still succeeds
# ---------------------------------------------------------------------------


def test_forge_manifest_write_failure_demoted_to_warning(
    healthy_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When manifest append raises OSError, the run succeeds but reports a warning.

    Manifest writes are explicitly non-fatal (see _manifest module docstring
    "Failure policy"). A disk-full / read-only-FS condition must not crash a
    successful generation — the orchestrator catches OSError, demotes it to
    a warning on ForgeResult.warnings, leaves manifest_path=None, and still
    returns ok=True with the produced image(s).
    """

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(core._manifest, "append", boom)

    result = forge(prompt="a coffee mug", output_dir=tmp_path, mode="raw")

    assert result.ok is True
    assert len(result.images) >= 1
    assert result.manifest_path is None
    assert any(
        "manifest" in w.lower() for w in result.warnings
    ), f"missing manifest-write warning; got: {result.warnings}"


def test_forge_importable_from_package() -> None:
    import codex_imagen

    assert codex_imagen.forge is forge
    assert codex_imagen.ForgeOptions is ForgeOptions
    assert codex_imagen.ForgeResult is ForgeResult
