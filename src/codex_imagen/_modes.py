"""codex_imagen._modes — the batch-generation orchestrator (5 modes).

Purpose
-------
The Codex OAuth bridge only allows one image per call (``n > 1`` is rejected
with HTTP 400). Every higher-level batch behaviour — variants, branded sets,
narrative chains — therefore has to be implemented one layer up by issuing
multiple parallel or sequential single-image calls.

This module is that layer. It owns:

1. **Mode detection** — :func:`detect_mode` resolves ``batch_mode="auto"``
   into one of the five concrete modes based on the shape of the inputs.
2. **Execution planning** — :func:`plan` expands a ``(prompt, count, mode)``
   triple into a deterministic sequence of :class:`PlannedCall` objects with
   per-call output paths, reference lists, variation hints, batch context,
   and dependency edges.
3. **Plan execution** — :func:`execute_plan` walks the plan, building
   prompts via the injected ``prompt_build`` callable, calling the bridge
   via ``bridge_generate``, and (optionally) post-processing each image
   through ``chroma_keyout`` for transparency.

The five modes (recap)
----------------------
* ``single``            — 1 prompt → 1 image.
* ``parallel``          — N prompts → N independent images, ThreadPoolExecutor.
* ``variants``          — 1 prompt + count=N → N parallel calls with rotating
                          variation hints so the renders actually differ.
* ``chain``             — N prompts strictly sequential; each call references
                          prior images per ``chain_mode``.
* ``branded-parallel``  — anchor first (sequentially), then N section prompts
                          in parallel with the anchor image as reference.

Role in architecture
--------------------
``core.imagen()`` → :func:`detect_mode` → :func:`plan` → :func:`execute_plan`
→ ``_prompts.build()`` → ``_bridge.generate()`` → (optional)
``_chroma.keyout()``.

The bridge, prompt builder, and chroma keyer are *injected* (not imported)
so this module stays pure orchestration logic — easy to test with fakes,
no real API calls anywhere in the test suite.
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Mode names (public)
# ---------------------------------------------------------------------------

MODE_SINGLE = "single"
MODE_PARALLEL = "parallel"
MODE_VARIANTS = "variants"
MODE_CHAIN = "chain"
MODE_BRANDED_PARALLEL = "branded-parallel"
MODE_AUTO = "auto"

ALL_MODES: tuple[str, ...] = (
    MODE_SINGLE,
    MODE_PARALLEL,
    MODE_VARIANTS,
    MODE_CHAIN,
    MODE_BRANDED_PARALLEL,
)

# ---------------------------------------------------------------------------
# Chain sub-modes
# ---------------------------------------------------------------------------

CHAIN_PREVIOUS = "previous"
CHAIN_ANCHOR = "anchor"
CHAIN_ANCHOR_PREVIOUS = "anchor+previous"  # default
CHAIN_WINDOW_PREFIX = "window:"
CHAIN_ALL = "all"

VALID_CHAIN_MODES: tuple[str, ...] = (
    CHAIN_PREVIOUS,
    CHAIN_ANCHOR,
    CHAIN_ANCHOR_PREVIOUS,
    CHAIN_ALL,
)


# ---------------------------------------------------------------------------
# Variation hints (rotated through variants mode)
# ---------------------------------------------------------------------------

VARIATION_HINTS: tuple[str, ...] = (
    "interpret freely — establish the baseline",
    "vary the composition or camera angle",
    "vary the lighting or mood",
    "vary the color palette or materials",
    "vary the focal length or depth of field",
    "vary the styling or post-processing",
)


# ---------------------------------------------------------------------------
# Batch-context blurbs
# ---------------------------------------------------------------------------

# Appended to instructions for chain calls N>=1. Keeps character, palette,
# and lighting consistent across the sequence unless the prompt overrides.
CHAIN_BATCH_CONTEXT = (
    "Maintain visual consistency with the reference image(s). Same "
    "character, same style, same lighting unless the prompt explicitly "
    "directs change."
)

# Appended to instructions for branded-parallel sections. The anchor sets
# the brand language; sections render different content in the same world.
BRANDED_BATCH_CONTEXT = (
    "Match the visual style, palette, lighting, and feel of the anchor "
    "image. Different content, same aesthetic."
)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedCall:
    """One scheduled bridge call within a mode plan.

    Attributes:
        index: 0-based sequence number. Drives output filenames and the
            ``index`` field of result dicts. Stable regardless of execution
            order (calls may finish in any order in parallel modes).
        prompt: The prompt to render. ``str`` for plain prompts, ``dict``
            for Codex labeled-spec prompts. Forwarded verbatim to
            ``prompt_build``; variation hints and batch context are kept
            separate so the prompt itself isn't mutated.
        references: Absolute paths or URLs that this specific call must
            reference. For chain / branded-parallel modes these are
            **planned** paths that don't exist yet at plan() time — they
            become real once their producing call completes.
        output_path: Where the final image will be written. Pre-computed at
            plan() time so dependents can reference it.
        variation_hint: For ``variants`` mode only. Appended to the
            per-call instructions to nudge the model away from rendering
            an identical image six times.
        batch_context: For ``chain`` (N>=1) and ``branded-parallel``
            sections. Appended to per-call instructions to enforce
            cross-image consistency.
        depends_on: Indices of other planned calls that must complete
            before this one starts. Empty tuple for independent calls.
    """

    index: int
    prompt: str | dict
    references: tuple[str, ...]
    output_path: Path
    variation_hint: str | None
    batch_context: str | None
    depends_on: tuple[int, ...]


@dataclass(frozen=True)
class ModePlan:
    """The execution plan for a given mode + inputs.

    Attributes:
        mode: The resolved concrete mode. Never ``"auto"``.
        calls: The ordered tuple of planned calls. Index in this tuple
            equals ``PlannedCall.index`` by construction.
        parallel: Maximum concurrent workers for the executor. ``1`` for
            ``single`` and ``chain``; user-supplied otherwise.
    """

    mode: str
    calls: tuple[PlannedCall, ...]
    parallel: int


__all__ = [
    "ALL_MODES",
    "BRANDED_BATCH_CONTEXT",
    "CHAIN_ALL",
    "CHAIN_ANCHOR",
    "CHAIN_ANCHOR_PREVIOUS",
    "CHAIN_BATCH_CONTEXT",
    "CHAIN_PREVIOUS",
    "CHAIN_WINDOW_PREFIX",
    "MODE_AUTO",
    "MODE_BRANDED_PARALLEL",
    "MODE_CHAIN",
    "MODE_PARALLEL",
    "MODE_SINGLE",
    "MODE_VARIANTS",
    "ModePlan",
    "PlannedCall",
    "VALID_CHAIN_MODES",
    "VARIATION_HINTS",
    "detect_mode",
    "execute_plan",
    "plan",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_single_prompt(prompt: Any) -> bool:
    """Return True iff ``prompt`` is a non-list single prompt (str or dict)."""
    return isinstance(prompt, (str, dict))


def _normalize_prompt_list(prompt: Any) -> list:
    """Normalize the input to a list of prompts.

    Single ``str`` / ``dict`` becomes ``[prompt]``. A list is returned as a
    shallow copy (so caller mutations don't affect the plan). Anything else
    raises.
    """
    if isinstance(prompt, (str, dict)):
        return [prompt]
    if isinstance(prompt, list):
        return list(prompt)
    raise TypeError(
        f"prompt must be a str, dict, or list of str/dict; "
        f"got {type(prompt).__name__}"
    )


def _output_path(output_dir: Path, index: int, output_format: str) -> Path:
    """Compute a per-call output path with zero-padded numeric stem.

    Two-digit padding works fine up to 100 calls; the bridge's quota would
    run out long before we'd care about three-digit batch sizes.
    """
    return output_dir / f"{index:02d}.{output_format}"


def _anchor_output_path(output_dir: Path, output_format: str) -> Path:
    """Output path for an explicit anchor in branded-parallel mode."""
    return output_dir / f"00_anchor.{output_format}"


def _parse_window_size(chain_mode: str) -> int:
    """Parse the ``window:K`` syntax. Raise ValueError if K is malformed.

    Valid examples: ``window:1``, ``window:3``, ``window:10``. Invalid
    examples raise: ``window:``, ``window:0`` (zero makes no sense — that
    would be equivalent to no references), ``window:-1``, ``window:abc``.
    """
    suffix = chain_mode[len(CHAIN_WINDOW_PREFIX):]
    if not suffix:
        raise ValueError(
            f"chain_mode={chain_mode!r}: missing window size "
            f"(expected e.g. 'window:3')"
        )
    try:
        size = int(suffix)
    except ValueError as exc:
        raise ValueError(
            f"chain_mode={chain_mode!r}: window size must be an integer"
        ) from exc
    if size < 1:
        raise ValueError(
            f"chain_mode={chain_mode!r}: window size must be >= 1"
        )
    return size


# ---------------------------------------------------------------------------
# detect_mode
# ---------------------------------------------------------------------------


def detect_mode(
    prompt: str | dict | list,
    *,
    count: int = 1,
    batch_mode: str = MODE_AUTO,
    chain_mode_set: bool = False,
    anchor_set: bool = False,
) -> str:
    """Auto-detect the batch mode when ``batch_mode='auto'``.

    Args:
        prompt: The user-supplied prompt (str / dict / list).
        count: Variant count (``variants`` mode).
        batch_mode: Either ``"auto"`` or one of :data:`ALL_MODES`. Anything
            else raises.
        chain_mode_set: True if the caller explicitly passed a
            ``chain_mode`` parameter (signals "use chain mode" to auto).
        anchor_set: True if the caller explicitly passed an ``anchor`` arg
            (signals "use branded-parallel mode" to auto).

    Detection rules (only applied when ``batch_mode == "auto"``):

    * single ``str`` / ``dict`` prompt, ``count == 1`` → ``single``
    * single ``str`` / ``dict`` prompt, ``count > 1``  → ``variants``
    * single-item list, ``count == 1``                 → ``single``
    * single-item list, ``count > 1``                  → ``variants``
    * multi-item list with ``anchor_set``              → ``branded-parallel``
    * multi-item list with ``chain_mode_set``          → ``chain``
    * multi-item list (default)                        → ``parallel``

    Returns:
        The resolved concrete mode (one of :data:`ALL_MODES`).

    Raises:
        ValueError: If ``batch_mode`` is not ``"auto"`` and not in
            :data:`ALL_MODES`.
        TypeError: If ``prompt`` is not str / dict / list.
    """
    if batch_mode != MODE_AUTO:
        if batch_mode not in ALL_MODES:
            raise ValueError(
                f"batch_mode must be one of "
                f"{(MODE_AUTO, *ALL_MODES)}, got {batch_mode!r}"
            )
        return batch_mode

    # Validate prompt type up front. _normalize_prompt_list raises the
    # right error if it's neither str/dict/list.
    prompts = _normalize_prompt_list(prompt)

    if _is_single_prompt(prompt) or len(prompts) == 1:
        return MODE_VARIANTS if count > 1 else MODE_SINGLE

    # Multi-item list path.
    if anchor_set:
        return MODE_BRANDED_PARALLEL
    if chain_mode_set:
        return MODE_CHAIN
    return MODE_PARALLEL


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _plan_single(
    *,
    prompt: Any,
    output_dir: Path,
    output_format: str,
    common_refs: tuple[str, ...],
) -> tuple[PlannedCall, ...]:
    """Build the planned-call tuple for ``single`` mode."""
    prompts = _normalize_prompt_list(prompt)
    one = prompts[0]
    out = _output_path(output_dir, 0, output_format)
    return (
        PlannedCall(
            index=0,
            prompt=one,
            references=common_refs,
            output_path=out,
            variation_hint=None,
            batch_context=None,
            depends_on=(),
        ),
    )


def _plan_parallel(
    *,
    prompt: Any,
    output_dir: Path,
    output_format: str,
    common_refs: tuple[str, ...],
) -> tuple[PlannedCall, ...]:
    """Build the planned-call tuple for ``parallel`` mode."""
    prompts = _normalize_prompt_list(prompt)
    calls: list[PlannedCall] = []
    for i, pr in enumerate(prompts):
        calls.append(
            PlannedCall(
                index=i,
                prompt=pr,
                references=common_refs,
                output_path=_output_path(output_dir, i, output_format),
                variation_hint=None,
                batch_context=None,
                depends_on=(),
            )
        )
    return tuple(calls)


def _plan_variants(
    *,
    prompt: Any,
    output_dir: Path,
    output_format: str,
    common_refs: tuple[str, ...],
    count: int,
) -> tuple[PlannedCall, ...]:
    """Build the planned-call tuple for ``variants`` mode.

    Replicates the (single) prompt ``count`` times with rotating variation
    hints. If the count exceeds the hint pool, hints cycle.
    """
    prompts = _normalize_prompt_list(prompt)
    one = prompts[0]
    calls: list[PlannedCall] = []
    for i in range(count):
        hint = VARIATION_HINTS[i % len(VARIATION_HINTS)]
        calls.append(
            PlannedCall(
                index=i,
                prompt=one,
                references=common_refs,
                output_path=_output_path(output_dir, i, output_format),
                variation_hint=hint,
                batch_context=None,
                depends_on=(),
            )
        )
    return tuple(calls)


def _chain_refs_for(
    *,
    index: int,
    chain_mode: str,
    prior_paths: list[str],
) -> tuple[str, ...]:
    """Resolve the chain-mode reference list for call N (0-based).

    Call 0 has no prior images, so it always gets ``()``. For N>=1 we look
    up paths in ``prior_paths`` (positions 0..N-1).
    """
    if index == 0:
        return ()

    if chain_mode == CHAIN_PREVIOUS:
        return (prior_paths[index - 1],)

    if chain_mode == CHAIN_ANCHOR:
        return (prior_paths[0],)

    if chain_mode == CHAIN_ANCHOR_PREVIOUS:
        # For N>=2: both anchor and previous. For N==1: only anchor
        # (which equals previous; deduplicate).
        if index == 1:
            return (prior_paths[0],)
        return (prior_paths[0], prior_paths[index - 1])

    if chain_mode == CHAIN_ALL:
        return tuple(prior_paths[:index])

    if chain_mode.startswith(CHAIN_WINDOW_PREFIX):
        k = _parse_window_size(chain_mode)
        start = max(0, index - k)
        return tuple(prior_paths[start:index])

    raise ValueError(f"Unknown chain_mode: {chain_mode!r}")


def _plan_chain(
    *,
    prompt: Any,
    output_dir: Path,
    output_format: str,
    common_refs: tuple[str, ...],
    chain_mode: str,
) -> tuple[PlannedCall, ...]:
    """Build the planned-call tuple for ``chain`` mode.

    Each call N>=1 references the prior images per the chain sub-mode.
    Dependencies cascade: call N depends on (0, 1, ..., N-1) so the
    executor never starts call N until all priors finished.
    """
    prompts = _normalize_prompt_list(prompt)
    if len(prompts) < 2:
        raise ValueError(
            "chain mode requires at least 2 prompts; "
            "use single mode for a one-shot generation."
        )

    # Validate chain_mode early — _chain_refs_for would otherwise raise
    # mid-loop on the very first non-anchor call.
    if chain_mode.startswith(CHAIN_WINDOW_PREFIX):
        _parse_window_size(chain_mode)  # raises if malformed
    elif chain_mode not in VALID_CHAIN_MODES:
        raise ValueError(
            f"chain_mode must be one of {VALID_CHAIN_MODES} or "
            f"'window:K', got {chain_mode!r}"
        )

    # Pre-compute the output path for every call so chain refs can point
    # at them by string before any image actually exists on disk.
    output_paths = [
        _output_path(output_dir, i, output_format)
        for i in range(len(prompts))
    ]
    prior_strs = [str(p) for p in output_paths]

    calls: list[PlannedCall] = []
    for i, pr in enumerate(prompts):
        chain_refs = _chain_refs_for(
            index=i,
            chain_mode=chain_mode,
            prior_paths=prior_strs,
        )
        # `common_refs` (user-supplied) are applied to every call; chain
        # refs are appended so prior images come second (the model uses
        # the first reference most heavily).
        refs = common_refs + chain_refs
        ctx = CHAIN_BATCH_CONTEXT if i >= 1 else None
        depends = tuple(range(i))  # call N depends on 0..N-1
        calls.append(
            PlannedCall(
                index=i,
                prompt=pr,
                references=refs,
                output_path=output_paths[i],
                variation_hint=None,
                batch_context=ctx,
                depends_on=depends,
            )
        )
    return tuple(calls)


def _plan_branded_parallel(
    *,
    prompt: Any,
    output_dir: Path,
    output_format: str,
    common_refs: tuple[str, ...],
    anchor: str | dict | None,
) -> tuple[PlannedCall, ...]:
    """Build the planned-call tuple for ``branded-parallel`` mode.

    Two layouts depending on whether ``anchor`` is provided explicitly:

    * **Implicit anchor** (``anchor=None``): ``prompt[0]`` becomes the
      anchor (output ``00.{ext}``), prompts[1..] are sections (output
      ``01.{ext}``, ``02.{ext}``, ...). Requires at least 2 prompts.
    * **Explicit anchor**: anchor renders to ``00_anchor.{ext}``, all
      caller prompts become sections (output ``01.{ext}``, ``02.{ext}``,
      ...). Single-prompt input is allowed in this layout.

    Sections depend on the anchor (index 0) and reference the anchor path.
    """
    prompts = _normalize_prompt_list(prompt)

    if anchor is None:
        # Implicit anchor: need at least 2 prompts (one anchor + one section).
        if len(prompts) < 2:
            raise ValueError(
                "branded-parallel mode without an explicit anchor "
                "requires at least 2 prompts (the first is the anchor)."
            )
        anchor_prompt = prompts[0]
        section_prompts = prompts[1:]
        anchor_out = _output_path(output_dir, 0, output_format)
        # Section indices start at 1, matching their output filenames.
        section_start_index = 1
    else:
        # Explicit anchor: the anchor renders to a distinct filename so it
        # doesn't collide with section index 0's output. Sections start
        # at index 1 (output 01.png, 02.png, ...).
        if not isinstance(anchor, (str, dict)):
            raise TypeError(
                "anchor must be a str or dict prompt; got "
                f"{type(anchor).__name__}"
            )
        anchor_prompt = anchor
        section_prompts = prompts
        anchor_out = _anchor_output_path(output_dir, output_format)
        section_start_index = 1

    anchor_path_str = str(anchor_out)

    calls: list[PlannedCall] = []
    # Anchor: index 0, no batch context, no dependencies.
    calls.append(
        PlannedCall(
            index=0,
            prompt=anchor_prompt,
            references=common_refs,
            output_path=anchor_out,
            variation_hint=None,
            batch_context=None,
            depends_on=(),
        )
    )

    # Sections: depend on anchor, reference anchor path, get the brand
    # consistency batch context.
    for offset, pr in enumerate(section_prompts):
        idx = section_start_index + offset
        section_refs = common_refs + (anchor_path_str,)
        calls.append(
            PlannedCall(
                index=idx,
                prompt=pr,
                references=section_refs,
                output_path=_output_path(output_dir, idx, output_format),
                variation_hint=None,
                batch_context=BRANDED_BATCH_CONTEXT,
                depends_on=(0,),
            )
        )

    return tuple(calls)


def plan(
    *,
    prompt: str | dict | list,
    output_dir: Path,
    output_format: str = "png",
    count: int = 1,
    batch_mode: str = MODE_AUTO,
    chain_mode: str = CHAIN_ANCHOR_PREVIOUS,
    anchor: str | dict | None = None,
    references: list[str] | None = None,
    parallel: int = 2,
) -> ModePlan:
    """Build the execution plan for a generation run.

    Resolves ``batch_mode="auto"`` via :func:`detect_mode`, then expands
    inputs into a tuple of :class:`PlannedCall` with per-call output paths,
    references, variation hints, batch context, and dependency edges.

    Args:
        prompt: The user prompt (str / dict / list).
        output_dir: Directory where images will be written. NOT created
            here — that's the executor's job. Used to compute output paths.
        output_format: File extension (without dot). ``png`` / ``jpeg`` /
            ``webp``.
        count: Variant count for ``variants`` mode.
        batch_mode: ``auto`` or one of :data:`ALL_MODES`.
        chain_mode: For ``chain`` mode. One of :data:`VALID_CHAIN_MODES`
            or ``window:K``.
        anchor: Optional explicit anchor for ``branded-parallel`` mode.
        references: Common reference images applied to every call.
        parallel: Max concurrent workers in the executor. Forced to 1 for
            ``single`` and ``chain``.

    Returns:
        A :class:`ModePlan` ready to feed to :func:`execute_plan`.

    Raises:
        ValueError: For invalid combinations — ``count < 1``,
            ``parallel < 1``, unknown ``chain_mode``, ``chain`` with a
            single prompt, ``branded-parallel`` without enough prompts and
            no explicit anchor.
        TypeError: If ``prompt`` is not str / dict / list, or ``anchor``
            is the wrong type.
    """
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")
    if parallel < 1:
        raise ValueError(f"parallel must be >= 1, got {parallel}")

    output_dir = Path(output_dir)
    common_refs: tuple[str, ...] = tuple(references) if references else ()

    # Resolve "auto" to a concrete mode. Note: detect_mode does NOT know
    # about `count > 1 with single prompt` -> variants nuance vs auto
    # batch_mode == something else; the caller drives chain/anchor flags.
    resolved = detect_mode(
        prompt,
        count=count,
        batch_mode=batch_mode,
        chain_mode_set=(batch_mode == MODE_CHAIN),
        anchor_set=(anchor is not None),
    )

    if resolved == MODE_SINGLE:
        calls = _plan_single(
            prompt=prompt,
            output_dir=output_dir,
            output_format=output_format,
            common_refs=common_refs,
        )
        return ModePlan(mode=resolved, calls=calls, parallel=1)

    if resolved == MODE_PARALLEL:
        calls = _plan_parallel(
            prompt=prompt,
            output_dir=output_dir,
            output_format=output_format,
            common_refs=common_refs,
        )
        # parallel mode happily accepts the user's parallel arg, but never
        # spawns more workers than there are calls (no point).
        return ModePlan(
            mode=resolved,
            calls=calls,
            parallel=min(parallel, max(1, len(calls))),
        )

    if resolved == MODE_VARIANTS:
        calls = _plan_variants(
            prompt=prompt,
            output_dir=output_dir,
            output_format=output_format,
            common_refs=common_refs,
            count=count,
        )
        return ModePlan(
            mode=resolved,
            calls=calls,
            parallel=min(parallel, max(1, len(calls))),
        )

    if resolved == MODE_CHAIN:
        calls = _plan_chain(
            prompt=prompt,
            output_dir=output_dir,
            output_format=output_format,
            common_refs=common_refs,
            chain_mode=chain_mode,
        )
        # Chain is strictly sequential — enforce parallel=1.
        return ModePlan(mode=resolved, calls=calls, parallel=1)

    if resolved == MODE_BRANDED_PARALLEL:
        calls = _plan_branded_parallel(
            prompt=prompt,
            output_dir=output_dir,
            output_format=output_format,
            common_refs=common_refs,
            anchor=anchor,
        )
        # Anchor runs first (sequentially); sections run in a pool of
        # `parallel` workers. We store the user's parallel here and let
        # execute_plan handle the staged execution.
        return ModePlan(
            mode=resolved,
            calls=calls,
            parallel=min(parallel, max(1, len(calls) - 1)),
        )

    # Should be unreachable thanks to detect_mode validating batch_mode.
    raise ValueError(f"Unhandled resolved mode: {resolved!r}")


# ---------------------------------------------------------------------------
# execute_plan
# ---------------------------------------------------------------------------


@dataclass
class _CallContext:
    """Internal carrier for everything one bridge call needs.

    Lives only on the stack — never serialized, never returned. Splitting
    this out keeps :func:`_run_one_call` readable.
    """

    call: PlannedCall
    bridge_generate: Callable
    prompt_build: Callable
    chroma_keyout: Callable | None
    transparent: bool
    chroma_key_hex: str
    chroma_tolerance: int
    chroma_despill: bool
    chroma_feather_px: int
    chroma_edge_erode_px: int
    skills_body: str
    mode_param: str
    extra_instructions: str | None
    vars: dict[str, str] | None
    advanced: dict[str, Any]
    output_format: str
    wall_clock_timeout: float


def _merge_extra_instructions(
    extra: str | None,
    variation_hint: str | None,
) -> str | None:
    """Combine user extras with the variant hint, if any.

    Batch context is NOT merged here — it's passed through ``batch_context``
    to :func:`_prompts.build`, which keeps it semantically distinct in the
    instructions block.
    """
    parts: list[str] = []
    if extra and extra.strip():
        parts.append(extra.strip())
    if variation_hint and variation_hint.strip():
        parts.append(f"VARIATION HINT: {variation_hint.strip()}")
    if not parts:
        return None
    return "\n\n".join(parts)


def _raw_and_final_paths(
    output_path: Path,
    *,
    transparent: bool,
) -> tuple[Path, Path | None]:
    """Return ``(bridge_target_path, raw_path_if_any)``.

    For transparent calls the bridge writes ``<stem>.raw.png`` first; the
    chroma keyer then writes the final ``<stem>.png``. For opaque calls
    the bridge writes directly to ``output_path`` and ``raw_path`` is None.
    """
    if not transparent:
        return output_path, None
    # Always store the raw frame as PNG — chroma needs a lossless source.
    raw_path = output_path.with_name(f"{output_path.stem}.raw.png")
    return raw_path, raw_path


def _run_one_call(ctx: _CallContext) -> dict[str, Any]:
    """Execute a single planned call. Never raises — returns a result dict.

    Per-call failure is captured in ``ok=False`` plus ``error``. Raising
    out of here would short-circuit the executor in unwanted ways
    (especially for ``parallel`` mode where one bad call must not poison
    its siblings).
    """
    call = ctx.call
    started = time.monotonic()
    warnings: list[str] = []

    # 1) Build prompt + instructions through the injected prompt_build.
    extra_merged = _merge_extra_instructions(
        ctx.extra_instructions,
        call.variation_hint,
    )

    try:
        built = ctx.prompt_build(
            call.prompt,
            mode=ctx.mode_param,
            skills_body=ctx.skills_body,
            transparent=ctx.transparent,
            chroma_key_hex=ctx.chroma_key_hex,
            references=list(call.references),
            extra_instructions=extra_merged,
            vars=ctx.vars,
            batch_context=call.batch_context,
        )
    except Exception as exc:  # noqa: BLE001 — surface error in result, not crash
        return _error_result(
            call,
            error=f"prompt build failed: {exc}",
            elapsed=time.monotonic() - started,
        )

    # Surface any warnings the prompt builder emitted.
    builder_warnings = tuple(getattr(built, "warnings", ()) or ())
    warnings.extend(builder_warnings)

    final_prompt = getattr(built, "final_prompt", "")
    instructions = getattr(built, "instructions", "")

    # 2) Decide where the bridge should write.
    bridge_target, raw_path = _raw_and_final_paths(
        call.output_path,
        transparent=ctx.transparent,
    )

    # 3) Call the bridge. References are stringified into a list so the
    # bridge sees the same shape regardless of the input container.
    references_list = [str(r) for r in call.references]
    try:
        bridge_result = ctx.bridge_generate(
            prompt=final_prompt,
            output_path=bridge_target,
            references=references_list if references_list else None,
            instructions=instructions,
            output_format=ctx.output_format,
            wall_clock_timeout=ctx.wall_clock_timeout,
            **ctx.advanced,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_result(
            call,
            error=f"bridge call failed: {exc}",
            elapsed=time.monotonic() - started,
            original_prompt=final_prompt,
            final_prompt=final_prompt,
            warnings=warnings,
            references_used=references_list,
        )

    bridge_warnings = tuple(getattr(bridge_result, "warnings", ()) or ())
    warnings.extend(bridge_warnings)

    # 4) Transparency post-process. If chroma_keyout is None but
    # transparent=True we silently skip — but flag it as a warning so
    # callers know the output isn't actually transparent.
    final_path = call.output_path
    # Track whether the on-disk file is actually transparent. Starts as the
    # caller's request; flips to False if we skip the chroma step.
    effective_transparent = ctx.transparent
    if ctx.transparent:
        if ctx.chroma_keyout is None:
            warnings.append(
                "transparent=True but no chroma_keyout callable was "
                "provided; output is opaque."
            )
            # The chroma step is skipped, so the file on disk is opaque.
            # The result dict must reflect that — not the caller's request.
            effective_transparent = False
            # Still place the file at the expected final path.
            if raw_path is not None and raw_path != final_path:
                try:
                    raw_path.replace(final_path)
                except OSError as exc:
                    return _error_result(
                        call,
                        error=f"could not rename raw image: {exc}",
                        elapsed=time.monotonic() - started,
                        original_prompt=final_prompt,
                        final_prompt=final_prompt,
                        warnings=warnings,
                        references_used=references_list,
                    )
                raw_path = None
        else:
            try:
                # Convert tolerance/hex to whatever the keyer expects.
                # _chroma.keyout takes key_rgb=tuple, tolerance=int. We
                # parse the hex here so the orchestrator can stay
                # hex-string-centric.
                key_rgb, hex_fallback = _hex_to_rgb(ctx.chroma_key_hex)
                if hex_fallback:
                    warnings.append(
                        f"chroma_key_hex {ctx.chroma_key_hex!r} is "
                        f"malformed; falling back to magenta (#FF00FF)"
                    )
                ctx.chroma_keyout(
                    raw_path,
                    final_path,
                    key_rgb=key_rgb,
                    tolerance=ctx.chroma_tolerance,
                    despill=ctx.chroma_despill,
                    feather_px=ctx.chroma_feather_px,
                    edge_erode_px=ctx.chroma_edge_erode_px,
                )
            except Exception as exc:  # noqa: BLE001
                return _error_result(
                    call,
                    error=f"chroma keyout failed: {exc}",
                    elapsed=time.monotonic() - started,
                    original_prompt=final_prompt,
                    final_prompt=final_prompt,
                    warnings=warnings,
                    references_used=references_list,
                )

    # 5) Compose the result dict. Use the bridge's reported size when
    # available, otherwise stat the final file.
    try:
        final_bytes = final_path.stat().st_size
    except OSError:
        final_bytes = int(getattr(bridge_result, "bytes", 0) or 0)

    elapsed_ms = int((time.monotonic() - started) * 1000)

    return {
        "index": call.index,
        "ok": True,
        "path": str(final_path),
        "raw_path": str(raw_path) if (effective_transparent and raw_path) else None,
        "bytes": final_bytes,
        "mime_type": getattr(bridge_result, "mime_type", "image/png"),
        "response_id": getattr(bridge_result, "response_id", None),
        "call_id": getattr(bridge_result, "call_id", None),
        "revised_prompt": getattr(bridge_result, "revised_prompt", None),
        "original_prompt": final_prompt,
        "final_prompt": final_prompt,
        "transparent": effective_transparent,
        "references_used": references_list,
        "warnings": list(warnings),
        "error": None,
        "elapsed_ms": elapsed_ms,
    }


def _hex_to_rgb(hex_color: str) -> tuple[tuple[int, int, int], bool]:
    """Parse ``#RRGGBB`` → ``((R, G, B), fallback_used)``. Tolerant of casing.

    Returns a tuple of (rgb, fallback_used). ``fallback_used`` is True when the
    input could not be parsed and the magenta default ``(255, 0, 255)`` was
    substituted. Callers should surface this as a warning so a typo in
    ``chroma_key_hex`` doesn't silently key against the wrong color.
    """
    s = (hex_color or "").lstrip("#").strip()
    if len(s) != 6:
        return (255, 0, 255), True
    try:
        return (
            int(s[0:2], 16),
            int(s[2:4], 16),
            int(s[4:6], 16),
        ), False
    except ValueError:
        return (255, 0, 255), True


def _error_result(
    call: PlannedCall,
    *,
    error: str,
    elapsed: float,
    original_prompt: str = "",
    final_prompt: str = "",
    warnings: list[str] | None = None,
    references_used: list[str] | None = None,
) -> dict[str, Any]:
    """Build the standard ``ok=False`` result dict."""
    return {
        "index": call.index,
        "ok": False,
        "path": str(call.output_path),
        "raw_path": None,
        "bytes": 0,
        "mime_type": None,
        "response_id": None,
        "call_id": None,
        "revised_prompt": None,
        "original_prompt": original_prompt,
        "final_prompt": final_prompt,
        "transparent": False,
        "references_used": references_used or [],
        "warnings": warnings or [],
        "error": error,
        "elapsed_ms": int(elapsed * 1000),
    }


def _dependency_failed_result(call: PlannedCall) -> dict[str, Any]:
    """Build the result for a call whose dependency failed."""
    return _error_result(
        call,
        error="dependency_failed",
        elapsed=0.0,
    )


def execute_plan(
    plan: ModePlan,
    *,
    bridge_generate: Callable,
    prompt_build: Callable,
    chroma_keyout: Callable | None,
    transparent: bool = False,
    chroma_key_hex: str = "#FF00FF",
    chroma_tolerance: int = 40,
    chroma_despill: bool = True,
    chroma_feather_px: int = 2,
    chroma_edge_erode_px: int = 1,
    skills_body: str = "",
    mode_param: str = "auto",
    extra_instructions: str | None = None,
    vars: dict[str, str] | None = None,
    advanced: dict[str, Any] | None = None,
    wall_clock_timeout: float = 240.0,
) -> list[dict[str, Any]]:
    """Execute a :class:`ModePlan` and return per-call result dicts.

    Concurrency model:

    * ``single``                  — direct call, no executor.
    * ``parallel`` / ``variants`` — ``ThreadPoolExecutor(max_workers=
      plan.parallel)``.
    * ``chain``                   — strictly sequential loop. A failed
      call marks every downstream call as ``dependency_failed`` without
      running them.
    * ``branded-parallel``        — anchor runs synchronously; if it
      succeeds, sections run in a thread pool. If the anchor fails, every
      section is marked ``dependency_failed``.

    Args:
        plan: From :func:`plan`.
        bridge_generate: Callable matching :func:`_bridge.generate` kwargs.
        prompt_build: Callable matching :func:`_prompts.build`.
        chroma_keyout: Callable matching :func:`_chroma.keyout`. May be
            ``None`` when ``transparent=False``; if ``None`` is passed
            while ``transparent=True`` the executor warns and renames the
            raw image into place.
        transparent: Pass-through to the prompt builder AND triggers
            ``chroma_keyout`` post-process.
        chroma_key_hex / chroma_tolerance / chroma_despill /
        chroma_feather_px: Chroma settings.
        skills_body: Pre-loaded skill body (from :func:`_skills.load_skills`).
        mode_param: ``raw`` / ``medium`` / ``high`` / ``max`` / ``auto`` —
            forwarded to :func:`_prompts.build` as ``mode``.
        extra_instructions: Free-form extras forwarded to ``prompt_build``.
        vars: Variable substitution map for the prompt builder.
        advanced: Passed verbatim as ``**kwargs`` to ``bridge_generate`` —
            user escape hatch for things like ``reasoning_effort``.

    Returns:
        A list of result dicts, sorted by ``index``.
    """
    advanced = dict(advanced) if advanced else {}

    # Ensure the output directory exists before any work starts. ModePlan
    # doesn't carry the directory directly, so derive it from the first
    # call's output_path. With an empty plan there's nothing to do.
    if plan.calls:
        plan.calls[0].output_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-build a per-call context so we don't repeat the kwargs noise.
    def make_ctx(call: PlannedCall) -> _CallContext:
        return _CallContext(
            call=call,
            bridge_generate=bridge_generate,
            prompt_build=prompt_build,
            chroma_keyout=chroma_keyout,
            transparent=transparent,
            chroma_key_hex=chroma_key_hex,
            chroma_tolerance=chroma_tolerance,
            chroma_despill=chroma_despill,
            chroma_feather_px=chroma_feather_px,
            chroma_edge_erode_px=chroma_edge_erode_px,
            skills_body=skills_body,
            mode_param=mode_param,
            extra_instructions=extra_instructions,
            vars=vars,
            advanced=advanced,
            output_format=_format_for_output(call.output_path),
            wall_clock_timeout=wall_clock_timeout,
        )

    mode = plan.mode

    # ---- single -----------------------------------------------------------
    if mode == MODE_SINGLE:
        return [_run_one_call(make_ctx(plan.calls[0]))]

    # ---- chain (strictly sequential) -------------------------------------
    if mode == MODE_CHAIN:
        return _execute_chain(plan, make_ctx)

    # ---- branded-parallel (anchor first, then pool) ----------------------
    if mode == MODE_BRANDED_PARALLEL:
        return _execute_branded_parallel(plan, make_ctx)

    # ---- parallel / variants (flat pool) ---------------------------------
    return _execute_pool(plan, make_ctx)


def _format_for_output(path: Path) -> str:
    """Derive ``output_format`` from an output path (sans dot).

    The bridge needs to know whether to ask for PNG / JPEG / WEBP. We
    derived it once at plan-time via the filename, so we can read it back
    here without threading another field through the plan.
    """
    suffix = path.suffix.lstrip(".").lower()
    if suffix in {"png", "jpeg", "webp"}:
        return suffix
    if suffix == "jpg":
        return "jpeg"
    return "png"


def _execute_chain(
    plan: ModePlan,
    make_ctx: Callable[[PlannedCall], _CallContext],
) -> list[dict[str, Any]]:
    """Run chain calls strictly sequentially with dependency short-circuit."""
    results: list[dict[str, Any]] = []
    failed_indices: set[int] = set()

    for call in plan.calls:
        # If any prior dependency failed, short-circuit this call.
        if any(dep in failed_indices for dep in call.depends_on):
            results.append(_dependency_failed_result(call))
            failed_indices.add(call.index)
            continue

        result = _run_one_call(make_ctx(call))
        results.append(result)
        if not result["ok"]:
            failed_indices.add(call.index)

    return results


def _execute_branded_parallel(
    plan: ModePlan,
    make_ctx: Callable[[PlannedCall], _CallContext],
) -> list[dict[str, Any]]:
    """Run anchor first (sync), then sections in parallel."""
    # Anchor is always index 0 by construction in _plan_branded_parallel.
    anchor_call = plan.calls[0]
    section_calls = plan.calls[1:]

    anchor_result = _run_one_call(make_ctx(anchor_call))
    results_by_index: dict[int, dict[str, Any]] = {
        anchor_call.index: anchor_result,
    }

    if not anchor_result["ok"]:
        # Anchor failed → every section gets dependency_failed without
        # executing. Reference paths point at a file that doesn't exist.
        for call in section_calls:
            results_by_index[call.index] = _dependency_failed_result(call)
        return [results_by_index[i] for i in sorted(results_by_index)]

    # Anchor succeeded. Run sections in parallel.
    workers = max(1, plan.parallel)
    if not section_calls:
        return [anchor_result]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: dict[Future, PlannedCall] = {
            pool.submit(_run_one_call, make_ctx(call)): call
            for call in section_calls
        }
        for fut, call in futures.items():
            try:
                results_by_index[call.index] = fut.result()
            except Exception as exc:  # noqa: BLE001 — defensive; _run_one_call shouldn't raise
                results_by_index[call.index] = _error_result(
                    call,
                    error=f"executor exception: {exc}",
                    elapsed=0.0,
                )

    return [results_by_index[i] for i in sorted(results_by_index)]


def _execute_pool(
    plan: ModePlan,
    make_ctx: Callable[[PlannedCall], _CallContext],
) -> list[dict[str, Any]]:
    """Flat parallel execution for parallel + variants modes.

    All calls are independent (``depends_on=()``), so we just dispatch
    everything into a thread pool and collect by index when futures
    resolve.
    """
    workers = max(1, plan.parallel)
    results_by_index: dict[int, dict[str, Any]] = {}

    if workers == 1 or len(plan.calls) == 1:
        # Tiny optimization: skip executor overhead when there's nothing
        # to parallelize. Behaviour is identical.
        for call in plan.calls:
            results_by_index[call.index] = _run_one_call(make_ctx(call))
        return [results_by_index[i] for i in sorted(results_by_index)]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: dict[Future, PlannedCall] = {
            pool.submit(_run_one_call, make_ctx(call)): call
            for call in plan.calls
        }
        for fut, call in futures.items():
            try:
                results_by_index[call.index] = fut.result()
            except Exception as exc:  # noqa: BLE001
                results_by_index[call.index] = _error_result(
                    call,
                    error=f"executor exception: {exc}",
                    elapsed=0.0,
                )

    return [results_by_index[i] for i in sorted(results_by_index)]
