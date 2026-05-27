"""codex_imagen.core — public dataclasses + orchestrator entry point.

This is the GLUE layer. It owns the public-facing dataclasses
(:class:`ImagenOptions`, :class:`ImagenResult`, :class:`ImagenImage`,
:class:`ImagenHealth`) and the single user-facing function
:func:`imagen`. The actual work is delegated to the leaf modules:

* :mod:`codex_imagen._bridge`   — health check + low-level bridge call
* :mod:`codex_imagen._prompts`  — prompt builder (raw / medium / high / max)
* :mod:`codex_imagen._size`     — size string validator
* :mod:`codex_imagen._chroma`   — Pillow chroma-key pipeline
* :mod:`codex_imagen._skills`   — skill file loader
* :mod:`codex_imagen._modes`    — five-mode batch orchestration
* :mod:`codex_imagen._manifest` — JSONL manifest writer

High-level flow inside :func:`imagen`::

    imagen(**kwargs)
      -> ImagenOptions(**kwargs)               # validate via dataclass
      -> _bridge.health_check()                # if not ok: return ImagenResult(ok=False)
      -> resolve mode (raw/medium/high/max -> reasoning_effort)
      -> load skills (if non-raw and any paths)
      -> validate size (fallback to nearest_legal on failure)
      -> _modes.detect_mode(...)               # auto -> concrete
      -> _modes.plan(...)                      # build PlannedCalls
      -> _modes.execute_plan(...)              # actually call the bridge
      -> convert each call result -> ImagenImage
      -> append one manifest line per success
      -> return ImagenResult

This module deliberately knows nothing about HTTP, Pillow internals, or
Codex's instruction grammar. All of that lives one layer down.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codex_imagen import _bridge, _chroma, _manifest, _modes, _prompts
from codex_imagen import _size as _size_mod
from codex_imagen import _skills as _skills_mod
from codex_imagen._prompts import resolve_auto_mode

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImagenImage:
    """One generated image. See SPEC.md "ImagenResult" for field meanings."""

    index: int
    path: Path
    bytes: int
    mime_type: str
    response_id: str | None
    call_id: str | None
    revised_prompt: str | None
    original_prompt: str
    final_prompt: str
    transparent: bool
    # SPEC shows list[str]; we keep the dataclass frozen so we use a tuple
    # for immutability. Downstream consumers can ``list(images.references_used)``.
    references_used: tuple[str, ...]
    raw_png_path: Path | None


@dataclass(frozen=True)
class ImagenHealth:
    """Pre-flight health-check result. Always populated on ImagenResult."""

    ok: bool = False
    codex_image_gen_available: bool = False
    pillow_available: bool = False
    auth_file_exists: bool = False
    auth_file_path: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class ImagenResult:
    """Outcome of one :func:`imagen` invocation.

    ``ok`` is True iff at least one image was generated successfully.
    A health failure (no API call) returns ``ok=False`` with the actionable
    hint copied to :attr:`error`. A partial generation failure (some calls
    succeeded, some didn't) returns ``ok=True`` with the failures recorded
    in :attr:`warnings`.
    """

    ok: bool
    mode: str
    batch_mode: str
    images: tuple[ImagenImage, ...]
    manifest_path: Path | None
    elapsed_ms: int
    health: ImagenHealth
    error: str | None
    warnings: tuple[str, ...]


# Validation constants for ImagenOptions.
_VALID_MODES: tuple[str, ...] = ("auto", "raw", "medium", "high", "max")
_VALID_BATCH_MODES: tuple[str, ...] = (
    "auto",
    "single",
    "parallel",
    "variants",
    "chain",
    "branded-parallel",
)
_VALID_OUTPUT_FORMATS: tuple[str, ...] = ("png", "jpeg", "webp")


# Mapping user-facing mode -> mainline reasoning_effort. ``auto`` is
# resolved up front (see :func:`_resolve_auto_mode`) so this table only
# ever needs to handle concrete modes. We still accept ``auto`` as a
# defensive fallback (maps to None == "leave the bridge default").
_MODE_TO_REASONING_EFFORT: dict[str, str | None] = {
    "raw": "none",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
    "auto": None,
}


@dataclass(frozen=True)
class ImagenOptions:
    """User-facing options for :func:`imagen`. All fields have defaults so
    callers only need to pass ``prompt``.

    Fields mirror SPEC.md "ImagenOptions" — see that section for the
    authoritative description of every knob. ``__post_init__`` validates
    the constrained enums (mode / batch_mode / output_format) and the
    numeric ranges (count / parallel / chroma_tolerance) so the user gets
    a clear ``ValueError`` instead of a downstream crash.
    """

    # --- CORE INPUT ---
    prompt: Any = None  # str | dict | list[str|dict]
    output_dir: str | Path = "./out"

    # --- MODES ---
    mode: str = "auto"
    batch_mode: str = "auto"
    chain_mode: str = "anchor+previous"
    anchor: Any = None  # str | dict | None
    count: int = 1

    # --- CONTENT ---
    references: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    mask: str | None = None
    extra_instructions: str | None = None

    # --- IMAGE-TOOL PARAMS ---
    size: str = "auto"
    output_format: str = "png"

    # --- TRANSPARENCY ---
    transparent: bool = False
    chroma_key: str = "#FF00FF"
    chroma_tolerance: int = 40
    chroma_despill: bool = True
    chroma_edge_erode_px: int = 1

    # --- ENHANCEMENT ---
    enhance_prompt: bool = False
    vars: dict[str, str] = field(default_factory=dict)

    # --- ORCHESTRATION ---
    parallel: int = 2
    wall_clock_timeout: float = 240.0

    # --- ADVANCED ESCAPE HATCH ---
    advanced: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # prompt is technically required, but raising for None gives a
        # clearer message than letting _modes.detect_mode TypeError later.
        if self.prompt is None:
            raise ValueError("prompt is required (got None)")

        if self.mode not in _VALID_MODES:
            raise ValueError(
                f"mode must be one of {_VALID_MODES!r}, got {self.mode!r}"
            )
        if self.batch_mode not in _VALID_BATCH_MODES:
            raise ValueError(
                f"batch_mode must be one of {_VALID_BATCH_MODES!r}, "
                f"got {self.batch_mode!r}"
            )
        if self.output_format not in _VALID_OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {_VALID_OUTPUT_FORMATS!r}, "
                f"got {self.output_format!r}"
            )
        if self.count < 1:
            raise ValueError(f"count must be >= 1, got {self.count}")
        if self.parallel < 1:
            raise ValueError(f"parallel must be >= 1, got {self.parallel}")
        if not (0 <= self.chroma_tolerance <= 100):
            raise ValueError(
                f"chroma_tolerance must be in 0..100, got {self.chroma_tolerance}"
            )
        if not isinstance(self.chroma_edge_erode_px, int) or self.chroma_edge_erode_px < 0:
            raise ValueError(
                f"chroma_edge_erode_px must be a non-negative int, "
                f"got {self.chroma_edge_erode_px!r}"
            )
        if self.wall_clock_timeout <= 0:
            raise ValueError(
                f"wall_clock_timeout must be > 0, got {self.wall_clock_timeout}"
            )

        # Normalize collection inputs. Frozen dataclass: bypass setattr
        # protection via object.__setattr__ — standard pattern documented
        # in the stdlib dataclasses module for post-init coercion.
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        if isinstance(self.references, str):
            # Common mistake: pass a single path as a bare string. Coerce
            # to a 1-tuple instead of iterating its characters.
            object.__setattr__(self, "references", (self.references,))
        else:
            object.__setattr__(self, "references", tuple(self.references))
        if isinstance(self.skills, str):
            object.__setattr__(self, "skills", (self.skills,))
        else:
            object.__setattr__(self, "skills", tuple(self.skills))

        # Defensive copies of mutable dict inputs so caller mutations
        # after-the-fact can't poison a later imagen() call. ``vars``
        # default is the empty dict (per dataclasses.field default_factory).
        object.__setattr__(self, "vars", dict(self.vars))
        object.__setattr__(self, "advanced", dict(self.advanced))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _health_from_dict(health_dict: dict[str, Any]) -> ImagenHealth:
    """Convert the bridge's health dict to :class:`ImagenHealth`."""
    return ImagenHealth(
        ok=bool(health_dict.get("ok", False)),
        codex_image_gen_available=bool(
            health_dict.get("codex_image_gen_available", False)
        ),
        pillow_available=bool(health_dict.get("pillow_available", False)),
        auth_file_exists=bool(health_dict.get("auth_file_exists", False)),
        auth_file_path=health_dict.get("auth_file_path"),
        hint=health_dict.get("hint"),
    )


def _short_run_id() -> str:
    """A short, unique run identifier for the manifest. Not crypto-grade —
    just enough to disambiguate concurrent runs writing to the same dir.
    """
    return f"r_{uuid.uuid4().hex[:8]}"


def _iso_utc_now() -> str:
    """RFC 3339 / ISO-8601 UTC timestamp with a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_size(
    requested: str,
    warnings: list[str],
) -> tuple[str | None, str | None]:
    """Validate ``requested`` and return ``(effective_size, error)``.

    ``effective_size`` is None when the input was unrecoverable (the size
    string can't be parsed AND no nearest-legal suggestion exists). In
    that case ``error`` is populated and the caller should abort.

    When the size is invalid but a suggestion exists, a warning is
    appended and the suggestion is returned.

    The user-facing ``"auto"`` value is always valid — it passes straight
    through to the bridge as-is.
    """
    sv = _size_mod.validate(requested)
    if sv.is_valid:
        # Surface any soft warnings (e.g. experimental threshold).
        for w in sv.warnings:
            warnings.append(w)
        return requested, None
    # Invalid. If we have a suggestion, use it; otherwise bail out.
    if sv.suggestion is not None:
        warnings.append(
            f"size {requested!r} invalid: {sv.error}; "
            f"using nearest legal: {sv.suggestion}"
        )
        return sv.suggestion, None
    return None, (sv.error or "size could not be validated")


def _load_skill_body(
    skill_paths: tuple[str, ...],
    warnings: list[str],
) -> tuple[str, tuple[str, ...]]:
    """Load skill bodies (non-strict) and propagate any warnings.

    Returns ``(combined_body, skill_path_strs_used)`` so the manifest can
    record which skills were actually loaded.
    """
    if not skill_paths:
        return "", ()
    bundle = _skills_mod.load_skills(list(skill_paths), strict=False)
    for w in bundle.warnings:
        warnings.append(w)
    used = tuple(str(s.path) for s in bundle.skills)
    return bundle.combined_body, used


def _build_advanced(
    user_advanced: dict[str, Any],
    *,
    size: str,
    resolved_mode: str,
    transparent: bool,
) -> dict[str, Any]:
    """Compose the kwargs dict that gets splatted into the bridge call.

    ``size`` is always injected (bridge always wants it). ``reasoning_effort``
    is injected only when the user did not already set it via ``advanced``
    (escape hatch wins). ``background`` is forced to ``"opaque"`` when
    transparent=True because the bridge rejects ``"transparent"`` and the
    chroma keyer needs a solid backdrop.
    """
    out: dict[str, Any] = dict(user_advanced)
    out.setdefault("size", size)

    effort = _MODE_TO_REASONING_EFFORT.get(resolved_mode)
    if effort is not None:
        out.setdefault("reasoning_effort", effort)

    if transparent:
        # Force opaque so the bridge doesn't reject the request and so
        # the keyer has a clean backdrop to work with.
        out["background"] = "opaque"

    return out


def _call_to_image(call_result: dict[str, Any]) -> ImagenImage:
    """Convert a successful call_result dict (from execute_plan) into a
    :class:`ImagenImage`.
    """
    path = Path(call_result["path"])
    raw_path_str = call_result.get("raw_path")
    raw_path = Path(raw_path_str) if raw_path_str else None
    return ImagenImage(
        index=int(call_result["index"]),
        path=path,
        bytes=int(call_result.get("bytes") or 0),
        mime_type=str(call_result.get("mime_type") or "image/png"),
        response_id=call_result.get("response_id"),
        call_id=call_result.get("call_id"),
        revised_prompt=call_result.get("revised_prompt"),
        original_prompt=str(call_result.get("original_prompt") or ""),
        final_prompt=str(call_result.get("final_prompt") or ""),
        transparent=bool(call_result.get("transparent", False)),
        references_used=tuple(call_result.get("references_used") or ()),
        raw_png_path=raw_path,
    )


def _build_manifest_entry(
    *,
    call_result: dict[str, Any],
    run_id: str,
    mode_resolved: str,
    batch_mode: str,
    skills_used: tuple[str, ...],
    skills_hash: str,
    size: str,
    output_format: str,
) -> dict[str, Any]:
    """Build one JSONL manifest entry per SPEC.md "Manifest format"."""
    return {
        "ts": _iso_utc_now(),
        "run_id": run_id,
        "mode": mode_resolved,
        "batch_mode": batch_mode,
        "index": int(call_result["index"]),
        "prompt_original": call_result.get("original_prompt") or "",
        "prompt_final": call_result.get("final_prompt") or "",
        "skills_used": list(skills_used),
        "skills_hash": f"sha256:{skills_hash}" if skills_hash else None,
        "size": size,
        "output_format": output_format,
        "transparent": bool(call_result.get("transparent", False)),
        "references": list(call_result.get("references_used") or ()),
        "response_id": call_result.get("response_id"),
        "call_id": call_result.get("call_id"),
        "revised_prompt": call_result.get("revised_prompt"),
        "path": call_result.get("path"),
        "raw_path": call_result.get("raw_path"),
        "bytes": int(call_result.get("bytes") or 0),
        "elapsed_ms": int(call_result.get("elapsed_ms") or 0),
        "warnings": list(call_result.get("warnings") or ()),
    }


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


def _run(options: ImagenOptions) -> ImagenResult:
    """Execute one imagen() pipeline. See module docstring for the flow."""
    started = time.perf_counter()
    warnings: list[str] = []

    # ---- 1) Health check. ------------------------------------------------
    health_dict = _bridge.health_check()
    health = _health_from_dict(health_dict)

    if not health.ok:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ImagenResult(
            ok=False,
            mode=options.mode,
            batch_mode="auto",  # never resolved when health failed
            images=(),
            manifest_path=None,
            elapsed_ms=elapsed_ms,
            health=health,
            error=health.hint or "health check failed",
            warnings=(),
        )

    # ---- 2) Size validation. --------------------------------------------
    effective_size, size_error = _resolve_size(options.size, warnings)
    if effective_size is None:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ImagenResult(
            ok=False,
            mode=options.mode,
            batch_mode="auto",
            images=(),
            manifest_path=None,
            elapsed_ms=elapsed_ms,
            health=health,
            error=f"size {options.size!r} could not be validated: {size_error}",
            warnings=tuple(warnings),
        )

    # ---- 3) Mode + skill resolution. ------------------------------------
    # If the user is in raw mode, skills MUST be ignored (raw is a
    # passthrough — there's no instruction block to inject into). We warn
    # and drop them BEFORE loading so we don't waste a file read either.
    skills_body = ""
    skills_used: tuple[str, ...] = ()
    skills_hash = ""
    if options.skills:
        if options.mode == "raw":
            warnings.append(
                "skills ignored in raw mode (mode=raw passes the prompt "
                "verbatim, no instruction enrichment is possible)."
            )
        else:
            skills_body, skills_used = _load_skill_body(options.skills, warnings)
            # Hash for drift detection — useful in branded-parallel runs.
            skills_hash = _skills_mod.hash_body(skills_body) if skills_body else ""

    # ---- 4) Transparency feasibility. -----------------------------------
    # If the user asked for transparency but Pillow isn't installed, the
    # chroma keyer can't run. Warn loudly and drop to opaque.
    effective_transparent = options.transparent
    if effective_transparent and not health.pillow_available:
        warnings.append(
            "transparent=True requested but Pillow is not installed; "
            "output will be opaque. Install Pillow: pip install Pillow"
        )
        effective_transparent = False

    # ---- 5) Plan the run. ------------------------------------------------
    # detect_mode resolves "auto" using the shape of the prompt + a few
    # hints (chain_mode_set / anchor_set). "anchor+previous" is the
    # default for chain_mode; the user explicitly choosing chain via
    # batch_mode is enough to route to chain even when chain_mode is the
    # default value.
    try:
        # Plan & detect handle invalid inputs (e.g. chain with 1 prompt)
        # by raising ValueError / TypeError. We catch those and surface
        # them as ImagenResult.error rather than letting them escape.
        plan = _modes.plan(
            prompt=options.prompt,
            output_dir=Path(options.output_dir),
            output_format=options.output_format,
            count=options.count,
            batch_mode=options.batch_mode,
            chain_mode=options.chain_mode,
            anchor=options.anchor,
            references=list(options.references) if options.references else None,
            parallel=options.parallel,
        )
    except (ValueError, TypeError) as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ImagenResult(
            ok=False,
            mode=options.mode,
            batch_mode=options.batch_mode,
            images=(),
            manifest_path=None,
            elapsed_ms=elapsed_ms,
            health=health,
            error=f"plan failed: {exc}",
            warnings=tuple(warnings),
        )

    # ---- 6) Resolve mode (auto -> concrete) for manifest + reasoning. ---
    # We do this AFTER skill loading and transparency feasibility because
    # both feed into the auto-resolve decision. The result is used for:
    #   (a) the bridge's reasoning_effort knob (via _build_advanced), and
    #   (b) the manifest "mode" field (distinct from batch_mode).
    #
    # _prompts.resolve_auto_mode is the SINGLE source of truth — the same
    # call is used inside _prompts.build(), so the manifest mode and the
    # builder's mode_used can never diverge.
    resolved_builder_mode = resolve_auto_mode(
        options.prompt,
        requested=options.mode,
        has_skills=bool(skills_body),
        transparent=effective_transparent,
        extra_instructions=options.extra_instructions,
    )

    # ---- 7) Build advanced kwargs splat for the bridge. -----------------
    # Note: the user's advanced dict wins via setdefault inside, so power
    # users can still override e.g. reasoning_effort or size from the
    # outside.
    advanced = _build_advanced(
        options.advanced,
        size=effective_size,
        resolved_mode=resolved_builder_mode,
        transparent=effective_transparent,
    )

    # ---- 8) Execute the plan. -------------------------------------------
    # The chroma keyer is only wired in when transparency is *effectively*
    # enabled. We pass _chroma.keyout directly — execute_plan owns the
    # raw/final filename dance.
    call_results = _modes.execute_plan(
        plan,
        bridge_generate=_bridge.generate,
        prompt_build=_prompts.build,
        chroma_keyout=_chroma.keyout if effective_transparent else None,
        transparent=effective_transparent,
        chroma_key_hex=options.chroma_key,
        chroma_tolerance=options.chroma_tolerance,
        chroma_despill=options.chroma_despill,
        chroma_feather_px=2,
        chroma_edge_erode_px=options.chroma_edge_erode_px,
        skills_body=skills_body,
        mode_param=options.mode,
        extra_instructions=options.extra_instructions,
        vars=options.vars or None,
        advanced=advanced,
        wall_clock_timeout=options.wall_clock_timeout,
    )

    # ---- 9) Aggregate results & write manifest. -------------------------
    images: list[ImagenImage] = []
    failures: list[str] = []
    manifest_path = Path(options.output_dir) / "manifest.jsonl"
    run_id = _short_run_id()
    any_manifest_written = False

    for cr in call_results:
        # Surface any per-call warnings to the top-level result.
        for w in cr.get("warnings") or ():
            warnings.append(f"[index {cr.get('index')}] {w}")

        if cr.get("ok"):
            images.append(_call_to_image(cr))
            # Manifest line per successful call. ``mode`` is the resolved
            # prompt-builder mode (raw/medium/high/max) — distinct from
            # ``batch_mode`` (single/parallel/variants/chain/branded-parallel).
            entry = _build_manifest_entry(
                call_result=cr,
                run_id=run_id,
                mode_resolved=resolved_builder_mode,
                batch_mode=plan.mode,
                skills_used=skills_used,
                skills_hash=skills_hash,
                size=effective_size,
                output_format=options.output_format,
            )
            try:
                _manifest.append(manifest_path, entry)
                any_manifest_written = True
            except OSError as exc:
                # Don't fail the run on a manifest write error — just warn.
                warnings.append(f"manifest write failed: {exc}")
            except TypeError as exc:  # pragma: no cover - defensive
                warnings.append(f"manifest entry not serializable: {exc}")
        else:
            err = cr.get("error") or "unknown error"
            failures.append(f"call {cr.get('index')}: {err}")

    # ---- 10) Compose final result. --------------------------------------
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # Mode reporting: surface the resolved builder mode (the one actually
    # used to build prompts). For explicit modes that's just the user's
    # input; for auto we report the concrete resolution so callers can see
    # what the system decided.
    mode_for_result = resolved_builder_mode

    if not images:
        # All calls failed.
        return ImagenResult(
            ok=False,
            mode=mode_for_result,
            batch_mode=plan.mode,
            images=(),
            manifest_path=manifest_path if any_manifest_written else None,
            elapsed_ms=elapsed_ms,
            health=health,
            error=(
                "all calls failed: " + "; ".join(failures)
                if failures
                else "no images were generated"
            ),
            warnings=tuple(warnings),
        )

    # Partial success: at least one image, but some calls failed.
    if failures:
        warnings.append(
            f"{len(failures)} of {len(call_results)} calls failed: "
            + "; ".join(failures)
        )

    return ImagenResult(
        ok=True,
        mode=mode_for_result,
        batch_mode=plan.mode,
        images=tuple(images),
        manifest_path=manifest_path if any_manifest_written else None,
        elapsed_ms=elapsed_ms,
        health=health,
        error=None,
        warnings=tuple(warnings),
    )


def imagen(**kwargs: Any) -> ImagenResult:
    """Generate one or more images via the Codex OAuth bridge.

    Single entry point for all five batch modes (single, parallel,
    variants, chain, branded-parallel). All knobs live on
    :class:`ImagenOptions` — see that class for the full surface.

    Args:
        **kwargs: Forwarded to :class:`ImagenOptions`. Unknown keyword
            arguments raise ``TypeError`` (dataclass behavior). Bad
            values for known fields raise ``ValueError`` via
            :meth:`ImagenOptions.__post_init__`.

    Returns:
        A :class:`ImagenResult`. The ``ok`` flag is False on health
        failure (no API call was made) or when every generation call
        failed. Health failure is *not* an exception — callers get a
        structured result with an actionable ``hint`` in ``error``.

    Raises:
        TypeError: For unknown kwargs (dataclass surfaces this).
        ValueError: For invalid values on known kwargs.
    """
    options = ImagenOptions(**kwargs)
    return _run(options)
