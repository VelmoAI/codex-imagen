"""Tests for ``codex_imagen._modes`` — the 5-mode batch orchestrator.

All tests use fake callables (bridge_generate, prompt_build, chroma_keyout)
that record their inputs and return predictable SimpleNamespace results.
No real API calls. No real Pillow operations. The point is to verify the
*orchestration*: planning, dependency tracking, executor wiring, error
propagation.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codex_imagen._modes import (  # noqa: E402
    ALL_MODES,
    BRANDED_BATCH_CONTEXT,
    CHAIN_ALL,
    CHAIN_ANCHOR,
    CHAIN_ANCHOR_PREVIOUS,
    CHAIN_BATCH_CONTEXT,
    CHAIN_PREVIOUS,
    MODE_AUTO,
    MODE_BRANDED_PARALLEL,
    MODE_CHAIN,
    MODE_PARALLEL,
    MODE_SINGLE,
    MODE_VARIANTS,
    ModePlan,
    PlannedCall,
    VARIATION_HINTS,
    detect_mode,
    execute_plan,
    plan,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeBridge:
    """Record bridge_generate calls and write a dummy file to output_path."""

    def __init__(
        self,
        *,
        fail_on: set[int] | None = None,
        delay_ms: int = 0,
    ) -> None:
        self.calls: list[dict] = []
        self.fail_on = fail_on or set()
        self.delay_ms = delay_ms
        self.lock = threading.Lock()
        self._counter = 0
        # Track concurrent call peak — used to verify pool actually parallelises.
        self.active = 0
        self.peak_active = 0

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        with self.lock:
            self._counter += 1
            this_idx = self._counter
            self.active += 1
            if self.active > self.peak_active:
                self.peak_active = self.active
            self.calls.append(dict(kwargs))

        try:
            if self.delay_ms:
                time.sleep(self.delay_ms / 1000.0)

            if this_idx in self.fail_on:
                raise RuntimeError(f"forced bridge failure #{this_idx}")

            out: Path = kwargs["output_path"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"FAKEIMG-" + str(this_idx).encode())

            return SimpleNamespace(
                path=str(out),
                bytes=8 + len(str(this_idx)),
                mime_type="image/png",
                response_id=f"resp_{this_idx}",
                call_id=f"ig_{this_idx}",
                revised_prompt="(revised)",
                reference_images=tuple(kwargs.get("references") or ()),
                partial_image_paths=(),
                elapsed_ms=1,
                warnings=("bridge-warn",),
            )
        finally:
            with self.lock:
                self.active -= 1


class FakePromptBuild:
    """Record prompt_build calls and return a deterministic BuiltPrompt-shape."""

    def __init__(self, *, warnings: tuple[str, ...] = ()) -> None:
        self.calls: list[dict] = []
        self.warnings = warnings

    def __call__(self, prompt: Any, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({"prompt": prompt, **kwargs})
        # Render something predictable that downstream assertions can grep.
        if isinstance(prompt, dict):
            final = "DICT:" + ";".join(f"{k}={v}" for k, v in sorted(prompt.items()))
        else:
            final = f"STR:{prompt}"
        # Include extras + batch context in instructions so we can assert
        # the orchestrator forwards them correctly.
        instructions = (
            "INSTR"
            f"|extra={kwargs.get('extra_instructions') or ''}"
            f"|batch={kwargs.get('batch_context') or ''}"
            f"|mode={kwargs.get('mode')}"
        )
        return SimpleNamespace(
            final_prompt=final,
            instructions=instructions,
            mode_used=kwargs.get("mode", "auto"),
            warnings=self.warnings,
        )


class FakeChroma:
    """Copy src→dst (so the final file exists) and record the keyout call."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, src: Path, dst: Path, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({"src": str(src), "dst": str(dst), **kwargs})
        if self.fail:
            raise RuntimeError("forced chroma failure")
        shutil.copyfile(src, dst)
        return SimpleNamespace(pixels_total=100)


# ---------------------------------------------------------------------------
# detect_mode (10 tests)
# ---------------------------------------------------------------------------


def test_detect_str_prompt_count1_returns_single():
    assert detect_mode("hello", count=1) == MODE_SINGLE


def test_detect_str_prompt_count3_returns_variants():
    assert detect_mode("hello", count=3) == MODE_VARIANTS


def test_detect_dict_prompt_count1_returns_single():
    assert detect_mode({"primary_request": "x"}, count=1) == MODE_SINGLE


def test_detect_list_prompt_returns_parallel_by_default():
    assert detect_mode(["a", "b", "c"]) == MODE_PARALLEL


def test_detect_list_prompt_with_anchor_returns_branded_parallel():
    assert (
        detect_mode(["a", "b"], anchor_set=True) == MODE_BRANDED_PARALLEL
    )


def test_detect_list_prompt_with_chain_mode_set_returns_chain():
    assert (
        detect_mode(["a", "b"], chain_mode_set=True) == MODE_CHAIN
    )


def test_detect_explicit_mode_overrides_auto():
    # Explicit mode wins regardless of input shape.
    assert detect_mode("hi", batch_mode=MODE_PARALLEL) == MODE_PARALLEL
    assert detect_mode(["a", "b"], batch_mode=MODE_VARIANTS) == MODE_VARIANTS


def test_detect_invalid_mode_raises():
    with pytest.raises(ValueError):
        detect_mode("hi", batch_mode="not-a-mode")


def test_detect_list_of_one_treats_as_single():
    assert detect_mode(["only"], count=1) == MODE_SINGLE
    assert detect_mode(["only"], count=4) == MODE_VARIANTS


def test_detect_count_zero_raises_via_plan(tmp_path):
    with pytest.raises(ValueError):
        plan(prompt="x", output_dir=tmp_path, count=0)


# ---------------------------------------------------------------------------
# plan (12 tests)
# ---------------------------------------------------------------------------


def test_plan_single_creates_one_planned_call(tmp_path):
    p = plan(prompt="x", output_dir=tmp_path)
    assert p.mode == MODE_SINGLE
    assert len(p.calls) == 1
    assert p.calls[0].index == 0
    assert p.calls[0].output_path == tmp_path / "00.png"
    assert p.calls[0].depends_on == ()


def test_plan_parallel_creates_n_calls_indexed_00_to_NN(tmp_path):
    p = plan(prompt=["a", "b", "c"], output_dir=tmp_path, parallel=3)
    assert p.mode == MODE_PARALLEL
    assert len(p.calls) == 3
    assert [c.index for c in p.calls] == [0, 1, 2]
    assert p.calls[0].output_path == tmp_path / "00.png"
    assert p.calls[1].output_path == tmp_path / "01.png"
    assert p.calls[2].output_path == tmp_path / "02.png"


def test_plan_parallel_all_depends_on_empty(tmp_path):
    p = plan(prompt=["a", "b", "c"], output_dir=tmp_path)
    assert all(c.depends_on == () for c in p.calls)


def test_plan_variants_replicates_prompt_with_hints(tmp_path):
    p = plan(prompt="solo", output_dir=tmp_path, count=3, batch_mode=MODE_VARIANTS)
    assert p.mode == MODE_VARIANTS
    assert len(p.calls) == 3
    assert all(c.prompt == "solo" for c in p.calls)
    # First three variation hints assigned in order
    assert p.calls[0].variation_hint == VARIATION_HINTS[0]
    assert p.calls[1].variation_hint == VARIATION_HINTS[1]
    assert p.calls[2].variation_hint == VARIATION_HINTS[2]


def test_plan_variants_cycles_hints_when_count_exceeds_hint_pool(tmp_path):
    big = len(VARIATION_HINTS) + 2
    p = plan(
        prompt="solo",
        output_dir=tmp_path,
        count=big,
        batch_mode=MODE_VARIANTS,
    )
    assert len(p.calls) == big
    assert p.calls[len(VARIATION_HINTS)].variation_hint == VARIATION_HINTS[0]
    assert p.calls[len(VARIATION_HINTS) + 1].variation_hint == VARIATION_HINTS[1]


def test_plan_chain_each_depends_on_all_priors(tmp_path):
    p = plan(
        prompt=["frame1", "frame2", "frame3"],
        output_dir=tmp_path,
        batch_mode=MODE_CHAIN,
    )
    assert p.mode == MODE_CHAIN
    assert p.parallel == 1
    assert p.calls[0].depends_on == ()
    assert p.calls[1].depends_on == (0,)
    assert p.calls[2].depends_on == (0, 1)


def test_plan_chain_anchor_previous_refs_correct_for_call_2_plus(tmp_path):
    p = plan(
        prompt=["a", "b", "c"],
        output_dir=tmp_path,
        batch_mode=MODE_CHAIN,
        chain_mode=CHAIN_ANCHOR_PREVIOUS,
    )
    # call 0: no refs. call 1: just anchor. call 2: anchor + prev
    assert p.calls[0].references == ()
    assert p.calls[1].references == (str(tmp_path / "00.png"),)
    assert p.calls[2].references == (
        str(tmp_path / "00.png"),
        str(tmp_path / "01.png"),
    )
    # Batch context applied N>=1 only
    assert p.calls[0].batch_context is None
    assert p.calls[1].batch_context == CHAIN_BATCH_CONTEXT
    assert p.calls[2].batch_context == CHAIN_BATCH_CONTEXT


def test_plan_chain_window_3_refs_correct(tmp_path):
    p = plan(
        prompt=["a", "b", "c", "d", "e"],
        output_dir=tmp_path,
        batch_mode=MODE_CHAIN,
        chain_mode="window:3",
    )
    assert p.calls[0].references == ()
    assert p.calls[1].references == (str(tmp_path / "00.png"),)
    assert p.calls[3].references == (
        str(tmp_path / "00.png"),
        str(tmp_path / "01.png"),
        str(tmp_path / "02.png"),
    )
    # call 4 (5th): window of last 3 = (01, 02, 03)
    assert p.calls[4].references == (
        str(tmp_path / "01.png"),
        str(tmp_path / "02.png"),
        str(tmp_path / "03.png"),
    )


def test_plan_chain_all_refs_correct(tmp_path):
    p = plan(
        prompt=["a", "b", "c"],
        output_dir=tmp_path,
        batch_mode=MODE_CHAIN,
        chain_mode=CHAIN_ALL,
    )
    assert p.calls[2].references == (
        str(tmp_path / "00.png"),
        str(tmp_path / "01.png"),
    )


def test_plan_branded_parallel_implicit_anchor_uses_prompt0(tmp_path):
    p = plan(
        prompt=["anchor-prompt", "section1", "section2"],
        output_dir=tmp_path,
        batch_mode=MODE_BRANDED_PARALLEL,
    )
    assert p.mode == MODE_BRANDED_PARALLEL
    # index 0 is the anchor (prompt[0]); output goes to 00.png
    assert p.calls[0].prompt == "anchor-prompt"
    assert p.calls[0].output_path == tmp_path / "00.png"
    assert p.calls[0].depends_on == ()
    # Sections start at index 1 and 2; they depend on anchor
    assert p.calls[1].prompt == "section1"
    assert p.calls[1].depends_on == (0,)
    assert p.calls[1].references == (str(tmp_path / "00.png"),)


def test_plan_branded_parallel_explicit_anchor_renders_separate_anchor_file(tmp_path):
    p = plan(
        prompt=["section1", "section2"],
        output_dir=tmp_path,
        batch_mode=MODE_BRANDED_PARALLEL,
        anchor="brand anchor prompt",
    )
    # Explicit anchor writes to 00_anchor.png
    assert p.calls[0].prompt == "brand anchor prompt"
    assert p.calls[0].output_path == tmp_path / "00_anchor.png"
    # Sections at index 1, 2; reference the anchor file
    assert p.calls[1].prompt == "section1"
    assert p.calls[1].output_path == tmp_path / "01.png"
    assert p.calls[1].references == (str(tmp_path / "00_anchor.png"),)


def test_plan_branded_parallel_sections_depend_on_anchor_and_reference_it(tmp_path):
    p = plan(
        prompt=["s1", "s2", "s3"],
        output_dir=tmp_path,
        batch_mode=MODE_BRANDED_PARALLEL,
        anchor="anchor",
    )
    # All sections depend on (0,) and reference the anchor path
    anchor_path = str(tmp_path / "00_anchor.png")
    for call in p.calls[1:]:
        assert call.depends_on == (0,)
        assert call.references == (anchor_path,)
        assert call.batch_context == BRANDED_BATCH_CONTEXT


def test_plan_chain_single_prompt_raises(tmp_path):
    with pytest.raises(ValueError):
        plan(
            prompt=["just-one"],
            output_dir=tmp_path,
            batch_mode=MODE_CHAIN,
        )


def test_plan_invalid_chain_mode_raises(tmp_path):
    with pytest.raises(ValueError):
        plan(
            prompt=["a", "b"],
            output_dir=tmp_path,
            batch_mode=MODE_CHAIN,
            chain_mode="not-a-chain-mode",
        )


def test_plan_window_zero_raises(tmp_path):
    with pytest.raises(ValueError):
        plan(
            prompt=["a", "b"],
            output_dir=tmp_path,
            batch_mode=MODE_CHAIN,
            chain_mode="window:0",
        )


# ---------------------------------------------------------------------------
# execute_plan (8+ tests)
# ---------------------------------------------------------------------------


def _run(plan_obj, *, transparent=False, chroma=None, fb=None, fp=None, **kwargs):
    """Convenience runner with default fakes."""
    bridge = fb or FakeBridge()
    prompts = fp or FakePromptBuild()
    return (
        execute_plan(
            plan_obj,
            bridge_generate=bridge,
            prompt_build=prompts,
            chroma_keyout=chroma,
            transparent=transparent,
            **kwargs,
        ),
        bridge,
        prompts,
    )


def test_execute_single_calls_bridge_once_and_returns_one_result(tmp_path):
    p = plan(prompt="hello", output_dir=tmp_path)
    results, bridge, prompts = _run(p)
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["index"] == 0
    assert len(bridge.calls) == 1
    assert len(prompts.calls) == 1
    # File got written
    assert (tmp_path / "00.png").exists()


def test_execute_parallel_calls_bridge_n_times_in_parallel(tmp_path):
    p = plan(
        prompt=["a", "b", "c", "d"],
        output_dir=tmp_path,
        batch_mode=MODE_PARALLEL,
        parallel=4,
    )
    bridge = FakeBridge(delay_ms=40)
    results = execute_plan(
        p,
        bridge_generate=bridge,
        prompt_build=FakePromptBuild(),
        chroma_keyout=None,
    )
    assert len(results) == 4
    assert all(r["ok"] for r in results)
    # Concurrent execution: peak should be > 1 if the pool actually runs
    # multiple workers at once.
    assert bridge.peak_active >= 2


def test_execute_variants_passes_variation_hint_via_extra_instructions(tmp_path):
    p = plan(
        prompt="solo",
        output_dir=tmp_path,
        count=3,
        batch_mode=MODE_VARIANTS,
    )
    prompts = FakePromptBuild()
    execute_plan(
        p,
        bridge_generate=FakeBridge(),
        prompt_build=prompts,
        chroma_keyout=None,
        extra_instructions="user-extra",
    )
    # Each call's extra_instructions should contain "VARIATION HINT: <hint>".
    seen_hints: list[str] = []
    for call_record in prompts.calls:
        extra = call_record["extra_instructions"]
        assert "VARIATION HINT:" in extra
        assert "user-extra" in extra
        seen_hints.append(extra)
    # All three hints differ
    assert len({h for h in seen_hints}) == 3


def test_execute_chain_runs_strictly_sequentially(tmp_path):
    p = plan(
        prompt=["f1", "f2", "f3"],
        output_dir=tmp_path,
        batch_mode=MODE_CHAIN,
    )
    bridge = FakeBridge(delay_ms=20)
    results = execute_plan(
        p,
        bridge_generate=bridge,
        prompt_build=FakePromptBuild(),
        chroma_keyout=None,
    )
    assert len(results) == 3
    assert all(r["ok"] for r in results)
    # peak_active must never exceed 1 in chain mode
    assert bridge.peak_active == 1
    # Call ordering: bridge invoked with frame1's prompt first, then frame2, etc.
    assert "STR:f1" in bridge.calls[0]["prompt"]
    assert "STR:f2" in bridge.calls[1]["prompt"]
    assert "STR:f3" in bridge.calls[2]["prompt"]


def test_execute_branded_parallel_runs_anchor_first_then_sections_parallel(tmp_path):
    p = plan(
        prompt=["s1", "s2", "s3"],
        output_dir=tmp_path,
        batch_mode=MODE_BRANDED_PARALLEL,
        anchor="brand-anchor",
        parallel=3,
    )
    bridge = FakeBridge(delay_ms=30)
    results = execute_plan(
        p,
        bridge_generate=bridge,
        prompt_build=FakePromptBuild(),
        chroma_keyout=None,
    )
    assert len(results) == 4  # anchor + 3 sections
    assert all(r["ok"] for r in results)
    # Anchor is bridge call #1
    assert "STR:brand-anchor" in bridge.calls[0]["prompt"]
    # Anchor file exists at the special path
    assert (tmp_path / "00_anchor.png").exists()
    # peak_active reflects parallel sections (>= 2 if pool worked)
    assert bridge.peak_active >= 2


def test_execute_failure_in_parallel_does_not_stop_other_calls(tmp_path):
    p = plan(
        prompt=["a", "b", "c"],
        output_dir=tmp_path,
        batch_mode=MODE_PARALLEL,
        parallel=3,
    )
    # Force one bridge call to raise. Since execution order in a thread
    # pool isn't deterministic, target "any one of them" by killing the
    # second invocation.
    bridge = FakeBridge(fail_on={2})
    results = execute_plan(
        p,
        bridge_generate=bridge,
        prompt_build=FakePromptBuild(),
        chroma_keyout=None,
    )
    assert len(results) == 3
    ok_count = sum(1 for r in results if r["ok"])
    fail_count = sum(1 for r in results if not r["ok"])
    assert ok_count == 2
    assert fail_count == 1
    failed = [r for r in results if not r["ok"]][0]
    assert "bridge call failed" in failed["error"]


def test_execute_chain_failure_marks_downstream_dependency_failed(tmp_path):
    p = plan(
        prompt=["a", "b", "c"],
        output_dir=tmp_path,
        batch_mode=MODE_CHAIN,
    )
    # Fail the second bridge invocation (chain call index 1).
    bridge = FakeBridge(fail_on={2})
    results = execute_plan(
        p,
        bridge_generate=bridge,
        prompt_build=FakePromptBuild(),
        chroma_keyout=None,
    )
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert results[2]["ok"] is False
    assert results[2]["error"] == "dependency_failed"
    # Bridge should only have been called twice (call 0 + failed call 1);
    # call 2 short-circuited.
    assert len(bridge.calls) == 2


def test_execute_transparent_invokes_chroma_keyout_for_each_image(tmp_path):
    p = plan(prompt=["a", "b"], output_dir=tmp_path, parallel=2)
    chroma = FakeChroma()
    results = execute_plan(
        p,
        bridge_generate=FakeBridge(),
        prompt_build=FakePromptBuild(),
        chroma_keyout=chroma,
        transparent=True,
    )
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    assert all(r["transparent"] is True for r in results)
    assert len(chroma.calls) == 2
    # Raw paths exist (bridge wrote them) and final paths exist (chroma copied)
    for r in results:
        assert (Path(r["raw_path"])).exists()
        assert (Path(r["path"])).exists()


def test_execute_results_sorted_by_index(tmp_path):
    p = plan(
        prompt=["a", "b", "c", "d", "e"],
        output_dir=tmp_path,
        batch_mode=MODE_PARALLEL,
        parallel=5,
    )
    bridge = FakeBridge(delay_ms=10)
    results = execute_plan(
        p,
        bridge_generate=bridge,
        prompt_build=FakePromptBuild(),
        chroma_keyout=None,
    )
    assert [r["index"] for r in results] == [0, 1, 2, 3, 4]


def test_execute_propagates_warnings_from_bridge_and_prompts(tmp_path):
    p = plan(prompt="x", output_dir=tmp_path)
    prompts = FakePromptBuild(warnings=("prompt-warn-1", "prompt-warn-2"))
    bridge = FakeBridge()  # FakeBridge already returns warnings=("bridge-warn",)
    results = execute_plan(
        p,
        bridge_generate=bridge,
        prompt_build=prompts,
        chroma_keyout=None,
    )
    assert results[0]["ok"] is True
    warns = results[0]["warnings"]
    assert "prompt-warn-1" in warns
    assert "prompt-warn-2" in warns
    assert "bridge-warn" in warns


def test_execute_chain_batch_context_forwarded_to_prompt_build(tmp_path):
    """Chain calls N>=1 should pass CHAIN_BATCH_CONTEXT to prompt_build."""
    p = plan(
        prompt=["a", "b", "c"],
        output_dir=tmp_path,
        batch_mode=MODE_CHAIN,
    )
    prompts = FakePromptBuild()
    execute_plan(
        p,
        bridge_generate=FakeBridge(),
        prompt_build=prompts,
        chroma_keyout=None,
    )
    # Call 0: batch_context is None. Calls 1, 2: CHAIN_BATCH_CONTEXT.
    assert prompts.calls[0]["batch_context"] is None
    assert prompts.calls[1]["batch_context"] == CHAIN_BATCH_CONTEXT
    assert prompts.calls[2]["batch_context"] == CHAIN_BATCH_CONTEXT


def test_execute_transparent_without_chroma_keyout_marks_opaque(tmp_path):
    """transparent=True + chroma_keyout=None: file must end at final path,
    result['transparent'] must be False (reflecting reality, not the request),
    and a warning must explain it."""
    p = plan(prompt="x", output_dir=tmp_path)
    results = execute_plan(
        p,
        bridge_generate=FakeBridge(),
        prompt_build=FakePromptBuild(),
        chroma_keyout=None,
        transparent=True,
    )
    assert len(results) == 1
    r = results[0]
    assert r["ok"] is True
    # Fix 2: transparent must reflect what's actually on disk.
    assert r["transparent"] is False
    # A warning must explain the skipped chroma step.
    warn_text = " ".join(r["warnings"])
    assert ("opaque" in warn_text) or ("no chroma_keyout" in warn_text)
    # File exists at the final path, not the .raw.png intermediate.
    final_path = Path(r["path"])
    assert final_path == tmp_path / "00.png"
    assert final_path.exists()
    assert not (tmp_path / "00.raw.png").exists()
    # raw_path should be None since we renamed it into place.
    assert r["raw_path"] is None


def test_execute_chroma_failure_isolates_to_single_call(tmp_path):
    """One failing chroma step in a parallel batch must not poison siblings."""
    p = plan(
        prompt=["a", "b", "c"],
        output_dir=tmp_path,
        batch_mode=MODE_PARALLEL,
        parallel=3,
    )

    # Selective chroma: fail only for call index 01.
    def selective_chroma(src: Path, dst: Path, **kwargs: Any) -> SimpleNamespace:
        if "01" in str(src):
            raise RuntimeError("boom")
        shutil.copyfile(src, dst)
        return SimpleNamespace(pixels_total=42)

    results = execute_plan(
        p,
        bridge_generate=FakeBridge(),
        prompt_build=FakePromptBuild(),
        chroma_keyout=selective_chroma,
        transparent=True,
    )
    assert len(results) == 3
    ok_results = [r for r in results if r["ok"]]
    fail_results = [r for r in results if not r["ok"]]
    assert len(ok_results) == 2
    assert len(fail_results) == 1
    # The failed call is the one with chroma error.
    assert "chroma keyout failed" in fail_results[0]["error"]
    # Siblings completed and their final files exist (not poisoned).
    for r in ok_results:
        assert Path(r["path"]).exists()
        assert r["transparent"] is True


def test_execute_branded_anchor_failure_short_circuits_sections(tmp_path):
    p = plan(
        prompt=["s1", "s2"],
        output_dir=tmp_path,
        batch_mode=MODE_BRANDED_PARALLEL,
        anchor="anchor",
    )
    # FakeBridge fails on its first invocation (the anchor).
    bridge = FakeBridge(fail_on={1})
    results = execute_plan(
        p,
        bridge_generate=bridge,
        prompt_build=FakePromptBuild(),
        chroma_keyout=None,
    )
    assert results[0]["ok"] is False
    assert results[1]["ok"] is False
    assert results[1]["error"] == "dependency_failed"
    assert results[2]["error"] == "dependency_failed"
    # Sections never reached the bridge.
    assert len(bridge.calls) == 1
