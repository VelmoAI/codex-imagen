"""codex_imagen._prompts — prompt + instructions builder for the Codex bridge.

Purpose
-------
Every call to ``_bridge.generate()`` requires *two* strings:

1. ``final_prompt``  — the user-facing content sent as the ``input`` of the
   mainline model (currently ``gpt-5.5``). This is the text the model
   ultimately forwards (more or less, depending on mode) to the nested
   ``image_generation`` tool.
2. ``instructions`` — the *system* instructions for the mainline model.
   These control how aggressively the model restructures, enriches, or
   passes through the prompt before calling the tool.

This module is the only place that builds those two strings. It knows:

* the canonical Codex labeled-spec field order + labels
  (see ``upstream/codex/imagegen/SKILL.md``),
* the four reasoning modes (``raw`` / ``medium`` / ``high`` / ``max``)
  and the ``auto`` resolver,
* the verbatim-text enforcement trick that prevents tool-side misspellings,
* the chroma-key instruction block injected when ``transparent=True``,
* ``{var}`` style variable substitution shared by str + dict prompts,
* reference-image auto-labelling for dict prompts that omit
  ``input_images``.

Role in architecture
--------------------
``core.imagen()`` → ``_modes.run_*()`` → ``_prompts.build()`` → returns
``BuiltPrompt`` → ``_bridge.generate(prompt=…, instructions=…, …)``.

This module is intentionally bridge-agnostic. It must not import
``_bridge`` or ``codex_image_gen``. Pure stdlib, pure functions, easy to
test, easy to reason about.

Mode behavior summary
---------------------
* ``raw``    — passthrough. ``instructions`` is a fixed anti-refinement
  block. Skills / transparent / extra_instructions are *ignored* with
  warnings. The prompt is only var-substituted, never restructured.
* ``medium`` — default when any enrichment input is present. Full Codex
  labeled-spec preamble + skill bodies + optional chroma block + optional
  batch context + optional extra instructions.
* ``high``   — same instruction shape as ``medium`` but the caller is
  expected to raise ``reasoning_effort`` on the mainline. Also auto-
  selected when a ``Text (verbatim)`` field is present (in ``auto`` /
  ``medium``), because verbatim-text accuracy benefits from more polish.
* ``max``    — same shape; caller pushes ``reasoning_effort`` to
  ``xhigh``. We don't change the instruction text further.

The actual mapping of mode -> ``reasoning_effort`` happens one layer up,
in ``core``/``_modes``. This module only emits the textual artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Canonical Codex labeled-spec field set
# ---------------------------------------------------------------------------
#
# Order matters: the model is trained on this exact sequence. Reordering
# would silently degrade output quality. Keep in sync with
# ``upstream/codex/imagegen/SKILL.md``.
LABELED_SPEC_FIELDS: tuple[str, ...] = (
    "use_case",
    "asset_type",
    "primary_request",
    "input_images",
    "scene",
    "subject",
    "style_medium",
    "composition",
    "lighting_mood",
    "palette",
    "materials",
    "text_verbatim",
    "constraints",
    "avoid",
)

# Mapping of dict key -> the *exact* label that goes on the wire. Wording,
# casing and punctuation copied verbatim from the canonical spec.
LABELED_SPEC_LABELS: dict[str, str] = {
    "use_case": "Use case",
    "asset_type": "Asset type",
    "primary_request": "Primary request",
    "input_images": "Input images",
    "scene": "Scene/backdrop",
    "subject": "Subject",
    "style_medium": "Style/medium",
    "composition": "Composition/framing",
    "lighting_mood": "Lighting/mood",
    "palette": "Color palette",
    "materials": "Materials/textures",
    "text_verbatim": "Text (verbatim)",
    "constraints": "Constraints",
    "avoid": "Avoid",
}

# Reasoning modes the builder understands. Note: ``auto`` is the *input*
# value the user passes; it is always resolved away before BuiltPrompt is
# returned, so the ``mode_used`` field never contains ``auto``.
VALID_MODES: tuple[str, ...] = ("raw", "medium", "high", "max")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltPrompt:
    """The pair of strings handed to ``_bridge.generate`` plus diagnostics.

    Attributes:
        final_prompt: Text sent as the user message content (the ``input``
            of the mainline model). For dict prompts this is the rendered
            labeled-spec block. For str prompts this is the substituted
            string verbatim.
        instructions: System instructions for the mainline model. For
            ``raw`` mode this is the fixed anti-refinement block. For
            ``medium`` / ``high`` / ``max`` it is the labeled-spec
            preamble plus any enrichment (skills, chroma, batch context,
            extras, verbatim enforcer).
        mode_used: One of ``raw`` / ``medium`` / ``high`` / ``max`` —
            always concrete, never ``auto``.
        warnings: Non-fatal diagnostics (e.g. "skills ignored in raw
            mode"). Empty tuple when nothing is worth flagging.
    """

    final_prompt: str
    instructions: str
    mode_used: str
    warnings: tuple[str, ...]


# ---------------------------------------------------------------------------
# Constants — the load-bearing text blocks
# ---------------------------------------------------------------------------


# ``raw`` mode: tell the mainline to forward the prompt verbatim. This is
# the cheapest possible call shape and the only way to bypass gpt-5.5's
# "helpful" prompt restructuring.
ANTI_REFINEMENT_INSTRUCTIONS = (
    "You are a passthrough proxy. The user message contains the EXACT "
    "prompt for the image_generation tool. Forward it verbatim. Do not "
    "restructure, add scene/lighting details, expand short prompts, "
    "normalize wording, or inject style guidance. Call image_generation "
    "once with the prompt exactly as given."
)


# ``medium`` / ``high`` / ``max`` baseline. This is the full Codex
# labeled-spec preamble; skill bodies and other enrichment are appended to
# it by :func:`build`.
#
# Word budget target: 400-700 words. Detailed because this is the entire
# instruction footprint the mainline model has to learn from — terseness
# here is paid back many times over in low-quality renders downstream.
CODEX_LABELED_SPEC_PREAMBLE = """\
You are an image-generation orchestrator. Your job is to take the user
message, distill it into the Codex labeled-spec format below, and then
call the image_generation tool exactly once with that distilled prompt.

CANONICAL LABELED-SPEC FIELDS (use these exact labels, in this order,
including capitalization and punctuation):

  Use case: <a short taxonomy slug describing what this image is for>
  Asset type: <where/how the asset will be used>
  Primary request: <the user's main creative request in one sentence>
  Input images: <if reference images are present: "Image 1 = <role>;
               Image 2 = <role>"; omit entirely if no references>
  Scene/backdrop: <the environment, setting, location, context>
  Subject: <the main focal subject and any secondary subjects>
  Style/medium: <photo | illustration | 3D render | watercolor | etc.>
  Composition/framing: <wide / close / top-down; placement; rule-of-thirds;
                       focal point; depth of field hints>
  Lighting/mood: <quality + direction of light, mood / atmosphere>
  Color palette: <dominant hues, accent colors, harmony, brand notes>
  Materials/textures: <surface, fabric, finish, grain, weathering>
  Text (verbatim): "<exact text to render, in straight double quotes>"
  Constraints: <hard must-keeps and brand invariants>
  Avoid: <negative constraints — things explicitly disallowed>

AUGMENTATION RULES:

* Keep it short. Only add details that materially improve the result.
  Filler hurts more than it helps — the model overfits to noise.
* For edits, explicitly list invariants ("keep the existing logo
  unchanged", "preserve the original lighting") — without them, the model
  will silently restyle things it shouldn't.
* Quote verbatim text exactly. If the user provided text to render,
  copy it into Text (verbatim) inside straight double quotes, character
  for character. Do not paraphrase, abbreviate, or "fix" it.
* Refer to reference images by index ("the colors of Image 1", "the
  pose of Image 2"). Never assume the model can tell which image is which
  without explicit roles in Input images.
* Ask a clarifying question if a critical visual detail is missing.
  Do NOT silently invent specifics that materially change the brief.
* Compose in narrative order: scene first, then subject, then style and
  detail, then constraints last. This matches how the underlying image
  model parses the prompt.

OUTPUT FORMAT INVARIANTS:

* Emit ONLY populated fields. Do not insert placeholder values like
  "N/A", "none", or "default". An absent line is interpreted correctly;
  a "none" line confuses the image model.
* Do not include execution-layer notes (Quality:, Input fidelity:,
  output paths, mask filenames, model identifiers). Those belong to the
  bridge layer and are injected separately if needed; if you add them
  here they will appear as literal text in the rendered image.
* Do not add fields that are not in the canonical list above. The image
  model is trained on these labels — invented labels are ignored at best
  and weighted as creative content at worst.

TOOL CALL DISCIPLINE:

* Call image_generation exactly once. Multiple calls per user message
  are not supported by this orchestrator and waste quota.
* Do not narrate what you are about to do. Do not echo the prompt back
  to the user. Your only job is the single tool call.
"""


# Appended to ``instructions`` when the user requested transparent output.
# We use chroma-key post-processing (Pillow) because the bridge rejects
# ``background: "transparent"`` with HTTP 400. The model has to know to
# render against a flat key-color background or the keyer can't do its job.
#
# The template accepts a ``{key}`` placeholder (the actual hex color,
# e.g. ``#00FF00``) and ``{key_lower}`` (the same value, used in
# the "do not use" line for readability).  Both are identical strings;
# ``key_lower`` is kept for template flexibility.
CHROMA_BLOCK_TEMPLATE = (
    "This output will be chroma-keyed post-process to remove the background. "
    "Render the subject isolated on a perfectly flat, untextured, solid "
    "{key} background. "
    "The background must be exactly {key} with no shadows, no gradients, "
    "no floor plane, no reflections, no texture, and no lighting variation. "
    "Do not use {key} anywhere in the subject. "
    "Subject edges must be clean and crisp with generous padding around the subject."
)


# Appended to ``instructions`` when the prompt dict carries a non-empty
# ``text_verbatim`` field. The underlying image model has a strong bias
# toward "improving" rendered text — this counterweight is needed.
VERBATIM_ENFORCER = (
    "VERBATIM TEXT: A 'Text (verbatim)' field is present. Render the "
    "quoted text exactly as written — no extra glyphs, no duplicate "
    "text, no extra punctuation. If the text contains tricky words, "
    "treat each letter individually. Place the text where specified."
)


__all__ = [
    "ANTI_REFINEMENT_INSTRUCTIONS",
    "BuiltPrompt",
    "CHROMA_BLOCK_TEMPLATE",
    "CODEX_LABELED_SPEC_PREAMBLE",
    "LABELED_SPEC_FIELDS",
    "LABELED_SPEC_LABELS",
    "VALID_MODES",
    "VERBATIM_ENFORCER",
    "build",
    "render_prompt",
    "resolve_auto_mode",
]


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------


class _StrictFormatDict(dict):
    """A dict for ``str.format_map`` that raises a helpful KeyError.

    The default ``KeyError`` from ``format_map`` only contains the missing
    key name with no hint about *where* it was referenced or what the
    caller should do. We override ``__missing__`` so the error message is
    actually useful in agent logs.
    """

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        raise KeyError(
            f"Prompt references variable '{{{key}}}' but no value was "
            f"provided in vars=. Available keys: "
            f"{sorted(self.keys()) if self else '[]'}"
        )


def _substitute(text: str, variables: dict[str, str] | None) -> str:
    """Substitute ``{key}`` tokens in ``text`` from ``variables``.

    Uses ``str.format_map`` so that bare ``{`` / ``}`` from JSON-like
    content do not accidentally match. Callers that need literal braces in
    a prompt should double them (``{{`` / ``}}``) the same way they would
    with any other ``str.format`` call.
    """
    if not variables:
        # Fast path: no substitution requested. Still validate that the
        # prompt doesn't reference a variable, otherwise users get a very
        # confusing "no error, just literal {name} in the output".
        if "{" not in text:
            return text
        # Fall through to format_map with an empty strict dict so any
        # ``{name}`` reference raises our helpful KeyError instead of
        # silently rendering as literal text.
        return text.format_map(_StrictFormatDict())
    return text.format_map(_StrictFormatDict(variables))


# ---------------------------------------------------------------------------
# Labeled-spec rendering
# ---------------------------------------------------------------------------


def _wrap_verbatim(value: str) -> str:
    """Ensure the verbatim-text value is wrapped in straight double quotes.

    The canonical spec shows quoted text. If the caller already wrapped
    the value, leave it alone — re-wrapping would produce ``""hello""``
    which the model interprets as an empty string followed by hello.
    """
    stripped = value.strip()
    if (
        len(stripped) >= 2
        and stripped.startswith('"')
        and stripped.endswith('"')
    ):
        return stripped
    return f'"{stripped}"'


def _render_labeled_spec(prompt: dict[str, str]) -> str:
    """Render a populated labeled-spec dict to its on-the-wire string form.

    Empty / missing fields are *skipped* (per spec — placeholder values
    confuse the image model). Iteration order follows
    :data:`LABELED_SPEC_FIELDS` so output is stable regardless of dict
    insertion order.

    All values must be ``str``; non-string values raise ``TypeError`` at
    the boundary. This keeps the public contract strict and prevents
    silent data-coercion bugs.
    """
    lines: list[str] = []
    for field in LABELED_SPEC_FIELDS:
        value = prompt.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise TypeError(
                f"Labeled-spec field {field!r} must be str, got "
                f"{type(value).__name__}"
            )
        if not value.strip():
            continue
        label = LABELED_SPEC_LABELS[field]
        if field == "text_verbatim":
            value = _wrap_verbatim(value)
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def render_prompt(
    prompt: str | dict[str, str],
    *,
    vars: dict[str, str] | None = None,
) -> str:
    """Render a prompt (str or labeled-spec dict) to a single string.

    Args:
        prompt: Either a plain string (used as-is, after var
            substitution) or a dict whose keys are a subset of
            :data:`LABELED_SPEC_FIELDS`. Dict **values must be strings**
            — non-string values raise ``TypeError``. Values are first
            var-substituted, then formatted as the Codex labeled spec in
            canonical order, skipping empty fields.
        vars: Optional ``{name: value}`` map for ``{name}`` token
            substitution. Applied to the str prompt or to every string
            value of a dict prompt.

    Returns:
        The rendered prompt string.

    Raises:
        KeyError: If a ``{name}`` token references a variable not
            provided in ``vars``. The message names the missing variable
            and lists the available ones.
        TypeError: If ``prompt`` is neither a str nor a dict, or if any
            dict value is not a string.
    """
    if isinstance(prompt, str):
        return _substitute(prompt, vars)

    if isinstance(prompt, dict):
        # Substitute vars into every string value first, then render.
        # Non-string values are passed through to _render_labeled_spec,
        # which raises a clear TypeError naming the offending field.
        substituted: dict[str, str] = {}
        for key, value in prompt.items():
            if isinstance(value, str):
                substituted[key] = _substitute(value, vars)
            else:
                substituted[key] = value
        return _render_labeled_spec(substituted)

    raise TypeError(
        f"prompt must be a str or dict, got {type(prompt).__name__}"
    )


# ---------------------------------------------------------------------------
# Reference-image auto-labelling
# ---------------------------------------------------------------------------


def _auto_input_images_line(references: list[str]) -> str:
    """Build a default ``Input images: …`` line from a references list.

    The model needs to know which index corresponds to which role; if the
    caller didn't tell it, we synthesize a sensible default. The first
    image is treated as the primary reference (the one whose style /
    subject should dominate); subsequent images are secondary.
    """
    n = len(references)
    if n == 0:
        return ""
    if n == 1:
        return "Image 1 = reference"
    parts = ["Image 1 = primary reference"]
    for idx in range(2, n + 1):
        parts.append(f"Image {idx} = secondary reference")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def _has_text_verbatim(prompt: object) -> bool:
    """Return True iff a dict / list-of-dict prompt carries verbatim text.

    Accepts both the dict-key spelling ``"text_verbatim"`` (the canonical
    internal key) and the SPEC label spelling ``"Text (verbatim)"`` (in
    case a caller hand-built a dict using the human-facing label). Walks
    list prompts so batched inputs are inspected element-by-element.
    """

    def _check_one(p: object) -> bool:
        if not isinstance(p, dict):
            return False
        for key in ("text_verbatim", "Text (verbatim)"):
            v = p.get(key)
            if isinstance(v, str) and v.strip():
                return True
        return False

    if isinstance(prompt, list):
        return any(_check_one(p) for p in prompt)
    return _check_one(prompt)


def resolve_auto_mode(
    prompt: object,
    *,
    requested: str = "auto",
    has_skills: bool,
    transparent: bool,
    extra_instructions: str | None,
) -> str:
    """Resolve ``mode='auto'`` to a concrete builder mode.

    This is the SINGLE source of truth for auto-mode resolution. Both the
    prompt builder (:func:`build`) and the core orchestrator (for the
    ``reasoning_effort`` bridge knob and the manifest ``mode`` field) must
    call this function so they never diverge.

    Explicit modes pass through unchanged (after validation). For
    ``requested == "auto"`` the resolution priority is:

    * verbatim text in the prompt → ``high``
    * skills loaded / transparent / extra_instructions → ``medium``
    * otherwise → ``raw``

    Args:
        prompt: The user prompt (str / dict / list of either). Used to
            detect a populated ``text_verbatim`` field which forces a bump
            to ``high``.
        requested: The user-supplied mode value. Explicit ``raw`` /
            ``medium`` / ``high`` / ``max`` pass through unchanged.
            ``auto`` triggers the resolution rules above.
        has_skills: Whether a non-empty skills body will be injected.
        transparent: Whether transparency (chroma key) will be applied.
        extra_instructions: User-provided extra instructions, if any.
            Treated as enrichment when non-blank.

    Returns:
        One of ``raw`` / ``medium`` / ``high`` / ``max``.

    Raises:
        ValueError: When ``requested`` is not ``auto`` and not one of
            :data:`VALID_MODES`.
    """
    if requested != "auto":
        if requested not in VALID_MODES:
            raise ValueError(
                f"mode must be one of {('auto', *VALID_MODES)}, "
                f"got {requested!r}"
            )
        return requested

    # auto: verbatim text dominates (forces high)
    if _has_text_verbatim(prompt):
        return "high"

    has_extra = bool(extra_instructions and extra_instructions.strip())
    if has_skills or transparent or has_extra:
        return "medium"
    return "raw"


# ---------------------------------------------------------------------------
# The main build() entry point
# ---------------------------------------------------------------------------


def build(
    prompt: str | dict[str, str],
    *,
    mode: str = "auto",
    skills_body: str = "",
    transparent: bool = False,
    chroma_key_hex: str = "#00FF00",
    references: list[str] | None = None,
    extra_instructions: str | None = None,
    vars: dict[str, str] | None = None,
    batch_context: str | None = None,
) -> BuiltPrompt:
    """Build the ``(final_prompt, instructions)`` pair for one bridge call.

    See module docstring for the semantics of each mode.

    Args:
        prompt: Plain string OR labeled-spec dict (keys from
            :data:`LABELED_SPEC_FIELDS`). Dict **values must be strings**
            — non-string values raise ``TypeError`` during rendering.
        mode: One of ``auto`` / ``raw`` / ``medium`` / ``high`` / ``max``.
            ``auto`` resolves to ``raw`` when nothing needs enriching,
            ``medium`` otherwise. ``high`` is auto-selected for verbatim-
            text prompts (from ``auto`` / ``medium``).
        skills_body: Pre-loaded skill markdown body (concatenated by
            :mod:`_skills`). Empty string means "no skills". Ignored in
            ``raw`` mode (with warning).
        transparent: If True, append :data:`CHROMA_BLOCK_TEMPLATE` to the
            instructions so the model renders against a flat key colour.
            Ignored in ``raw`` mode (with warning).
        chroma_key_hex: The hex colour to render against. Defaults to
            green ``#00FF00`` — the industry-standard chroma-key color,
            matches the default chroma key in :mod:`_chroma`. Use
            ``#FF00FF`` (magenta) only when the subject is green.
        references: File paths / URLs of reference images, used to auto-
            generate the ``Input images:`` line on dict prompts that
            don't supply one explicitly.
        extra_instructions: Free-form text appended at the very end of
            the instructions block. Ignored in ``raw`` mode (with
            warning).
        vars: ``{name: value}`` map for ``{name}`` token substitution in
            the prompt.
        batch_context: Optional per-batch hint (e.g. "Match the visual
            style of the anchor image."). Appended after the chroma block
            but before ``extra_instructions``.

    Returns:
        A :class:`BuiltPrompt` with the rendered prompt, composed
        instructions, the concrete resolved mode, and a tuple of
        warnings.

    Raises:
        ValueError: If ``mode`` is not one of the valid modes / ``auto``.
        KeyError: If the prompt references a variable not in ``vars``.
        TypeError: If ``prompt`` is neither str nor dict.
    """
    warnings: list[str] = []

    # ---- 1) Detect enrichment signals up front. -------------------------
    has_skills = bool(skills_body and skills_body.strip())
    has_extra = bool(extra_instructions and extra_instructions.strip())
    has_verbatim = _has_text_verbatim(prompt)

    # ---- 2) Resolve mode via the single source of truth. ----------------
    # resolve_auto_mode handles both the explicit-mode pass-through (with
    # validation) and the auto resolution rules. For explicit ``medium``
    # we *still* apply the verbatim auto-bump below — verbatim rendering
    # benefits from the extra polish even when the caller didn't ask for
    # auto. Explicit ``raw`` / ``high`` / ``max`` are respected as-given.
    resolved = resolve_auto_mode(
        prompt,
        requested=mode,
        has_skills=has_skills,
        transparent=transparent,
        extra_instructions=extra_instructions,
    )

    if has_verbatim and mode == "medium" and resolved != "high":
        resolved = "high"
        warnings.append(
            "Mode auto-resolved to high due to verbatim text."
        )
    elif has_verbatim and mode == "auto" and resolved == "high":
        # resolve_auto_mode already bumped us to high for auto+verbatim.
        # Surface the same warning so the diagnostic is consistent.
        warnings.append(
            "Mode auto-resolved to high due to verbatim text."
        )

    # ---- 3) For dict prompts: inject auto Input images line if missing. -
    # Done here (not in render_prompt) because only build() knows about
    # `references`. Operate on a shallow copy so we don't mutate caller
    # state. "Missing" means: key absent, None, empty/whitespace string,
    # OR a non-string value (defensive: ignore garbage and auto-fill).
    effective_prompt: str | dict[str, str] = prompt
    if isinstance(prompt, dict) and references:
        existing = prompt.get("input_images")
        has_input_images = isinstance(existing, str) and bool(existing.strip())
        if not has_input_images:
            # Build the line FROM the references list, then patch it in.
            # Replaces any non-string / empty value already present.
            line = _auto_input_images_line(list(references))
            if line:
                effective_prompt = {**prompt, "input_images": line}

    # ---- 4) Render the final prompt (single source of truth). -----------
    final_prompt = render_prompt(effective_prompt, vars=vars)

    # ---- 5) RAW mode: short-circuit. -----------------------------------
    if resolved == "raw":
        if has_skills:
            warnings.append(
                "Skills ignored in raw mode (mode=raw passes prompt "
                "verbatim, no instruction enrichment)."
            )
        if has_extra:
            warnings.append("extra_instructions ignored in raw mode.")
        if transparent:
            warnings.append(
                "transparent=True ignored in raw mode (chroma "
                "instructions not injected). Generate-and-key-out will "
                "still happen, but the model isn't told to use a flat "
                "magenta background — results may be poor."
            )
        return BuiltPrompt(
            final_prompt=final_prompt,
            instructions=ANTI_REFINEMENT_INSTRUCTIONS,
            mode_used="raw",
            warnings=tuple(warnings),
        )

    # ---- 6) Medium / High / Max: compose enriched instructions. ---------
    # Order is significant: the labeled-spec preamble first establishes
    # the contract, then skills add brand/voice context, then per-call
    # modifiers (chroma, batch hint, extras, verbatim enforcer) tune the
    # behaviour for *this* call. Putting per-call stuff last keeps it
    # closest to the model's attention.
    parts: list[str] = [CODEX_LABELED_SPEC_PREAMBLE.rstrip()]

    if has_skills:
        parts.append("ADDITIONAL CONTEXT FROM LINKED SKILLS:")
        parts.append(skills_body.strip())

    if transparent:
        parts.append(CHROMA_BLOCK_TEMPLATE.format(key=chroma_key_hex))

    if batch_context and batch_context.strip():
        parts.append(batch_context.strip())

    if has_extra:
        # extra_instructions appended last so it has the strongest recency
        # bias — power users typically use this to override / steer.
        assert extra_instructions is not None  # narrowing for type-checker
        parts.append(extra_instructions.strip())

    if has_verbatim:
        parts.append(VERBATIM_ENFORCER)

    instructions = "\n\n".join(parts)

    return BuiltPrompt(
        final_prompt=final_prompt,
        instructions=instructions,
        mode_used=resolved,
        warnings=tuple(warnings),
    )
