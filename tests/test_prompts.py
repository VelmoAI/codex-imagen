"""Tests for codex_imagen._prompts.

Pure tests — no bridge, no I/O, no fixtures needed. We rely on the public
surface (build, render_prompt, BuiltPrompt) and the load-bearing constants.
"""

from __future__ import annotations

import pytest

from codex_imagen._prompts import (
    ANTI_REFINEMENT_INSTRUCTIONS,
    CHROMA_BLOCK_TEMPLATE,
    CODEX_LABELED_SPEC_PREAMBLE,
    LABELED_SPEC_FIELDS,
    LABELED_SPEC_LABELS,
    VERBATIM_ENFORCER,
    BuiltPrompt,
    build,
    render_prompt,
)


# ---------------------------------------------------------------------------
# Sanity check on the constants the rest of the code is built on
# ---------------------------------------------------------------------------


def test_labeled_spec_field_set_size_is_14() -> None:
    assert len(LABELED_SPEC_FIELDS) == 14
    # Every field has a label mapping.
    for field in LABELED_SPEC_FIELDS:
        assert field in LABELED_SPEC_LABELS


def test_canonical_labels_match_spec_exactly() -> None:
    # The model is trained on these exact strings. Guard them.
    assert LABELED_SPEC_LABELS["scene"] == "Scene/backdrop"
    assert LABELED_SPEC_LABELS["style_medium"] == "Style/medium"
    assert LABELED_SPEC_LABELS["composition"] == "Composition/framing"
    assert LABELED_SPEC_LABELS["lighting_mood"] == "Lighting/mood"
    assert LABELED_SPEC_LABELS["palette"] == "Color palette"
    assert LABELED_SPEC_LABELS["materials"] == "Materials/textures"
    assert LABELED_SPEC_LABELS["text_verbatim"] == "Text (verbatim)"


# ---------------------------------------------------------------------------
# render_prompt — str + dict + vars
# ---------------------------------------------------------------------------


def test_render_str_prompt_with_vars() -> None:
    assert render_prompt("hello {name}", vars={"name": "world"}) == "hello world"


def test_render_str_prompt_no_vars_passes_through() -> None:
    assert render_prompt("plain text, no tokens") == "plain text, no tokens"


def test_render_dict_prompt_emits_labeled_spec() -> None:
    out = render_prompt(
        {
            "use_case": "website-hero",
            "asset_type": "landing page hero",
            "primary_request": "a calming ceramic coffee mug",
            "scene": "morning sunlight on a wooden desk",
            "subject": "matte beige ceramic mug, steam rising",
            "style_medium": "editorial photography",
            "composition": "centered, top-down, lots of negative space",
            "lighting_mood": "soft side light, calm",
            "palette": "warm beige, ivory, accent terracotta",
            "materials": "matte unglazed ceramic, oak grain",
            "text_verbatim": "MORNING",
            "constraints": "no people",
            "avoid": "saturated colors, harsh shadows",
        }
    )
    expected = (
        "Use case: website-hero\n"
        "Asset type: landing page hero\n"
        "Primary request: a calming ceramic coffee mug\n"
        "Scene/backdrop: morning sunlight on a wooden desk\n"
        "Subject: matte beige ceramic mug, steam rising\n"
        "Style/medium: editorial photography\n"
        "Composition/framing: centered, top-down, lots of negative space\n"
        "Lighting/mood: soft side light, calm\n"
        "Color palette: warm beige, ivory, accent terracotta\n"
        "Materials/textures: matte unglazed ceramic, oak grain\n"
        'Text (verbatim): "MORNING"\n'
        "Constraints: no people\n"
        "Avoid: saturated colors, harsh shadows"
    )
    assert out == expected


def test_render_dict_skips_empty_fields() -> None:
    out = render_prompt(
        {
            "use_case": "logo",
            "primary_request": "a minimalist mark",
            "subject": "abstract geometric shape",
            # everything else missing → no line in output
        }
    )
    # Only the three present labels appear.
    assert "Use case: logo" in out
    assert "Primary request: a minimalist mark" in out
    assert "Subject: abstract geometric shape" in out
    # Empty fields are NOT padded with "N/A" or "none".
    assert "N/A" not in out
    assert "none" not in out.lower()
    assert "Scene/backdrop" not in out
    assert "Avoid" not in out


def test_render_dict_skips_empty_string_and_whitespace_only() -> None:
    out = render_prompt(
        {
            "use_case": "logo",
            "subject": "",
            "scene": "   ",
            "avoid": "harsh shadows",
        }
    )
    assert "Subject" not in out
    assert "Scene/backdrop" not in out
    assert "Use case: logo" in out
    assert "Avoid: harsh shadows" in out


def test_render_missing_var_raises_keyerror_with_helpful_msg() -> None:
    with pytest.raises(KeyError) as excinfo:
        render_prompt("hello {name}", vars={"other": "x"})
    # str(KeyError) wraps the message in quotes — match on the underlying
    # args content instead to keep the assertion readable.
    msg = excinfo.value.args[0]
    assert "name" in msg
    assert "vars" in msg
    assert "other" in msg  # lists available keys


def test_render_dict_substitutes_vars_in_values() -> None:
    out = render_prompt(
        {
            "use_case": "char-portrait",
            "subject": "{char} standing in {place}",
        },
        vars={"char": "a red-haired woman", "place": "an alpine meadow"},
    )
    assert "Subject: a red-haired woman standing in an alpine meadow" in out


def test_render_invalid_prompt_type_raises_typeerror() -> None:
    with pytest.raises(TypeError):
        render_prompt(42)  # type: ignore[arg-type]


def test_render_dict_text_verbatim_wraps_in_quotes() -> None:
    out = render_prompt({"text_verbatim": "WELCOME"})
    assert 'Text (verbatim): "WELCOME"' in out

    # Already quoted: leave as-is, do not double-wrap.
    out2 = render_prompt({"text_verbatim": '"WELCOME"'})
    assert 'Text (verbatim): "WELCOME"' in out2
    assert '""WELCOME""' not in out2


# ---------------------------------------------------------------------------
# build — mode resolution
# ---------------------------------------------------------------------------


def test_build_auto_resolves_to_raw_when_no_skills_no_transparent() -> None:
    bp = build("a simple sketch")
    assert bp.mode_used == "raw"
    assert bp.instructions == ANTI_REFINEMENT_INSTRUCTIONS
    assert bp.final_prompt == "a simple sketch"


def test_build_auto_resolves_to_medium_when_skills_present() -> None:
    bp = build("a simple sketch", skills_body="# Brand kit\nBe minimal.")
    assert bp.mode_used == "medium"
    assert CODEX_LABELED_SPEC_PREAMBLE.split("\n", 1)[0] in bp.instructions
    assert "Be minimal." in bp.instructions


def test_build_auto_resolves_to_medium_when_transparent() -> None:
    bp = build("a simple sketch", transparent=True)
    assert bp.mode_used == "medium"
    assert "chroma-keyed" in bp.instructions


def test_build_auto_resolves_to_medium_when_extra_instructions() -> None:
    bp = build("a simple sketch", extra_instructions="dramatic mood only")
    assert bp.mode_used == "medium"
    assert "dramatic mood only" in bp.instructions


def test_build_explicit_high_mode_respected_even_without_skills() -> None:
    bp = build("a simple sketch", mode="high")
    assert bp.mode_used == "high"
    # high uses the medium-shape instructions (preamble), not the raw block.
    assert bp.instructions != ANTI_REFINEMENT_INSTRUCTIONS
    assert "CANONICAL LABELED-SPEC FIELDS" in bp.instructions


def test_build_explicit_max_mode_respected() -> None:
    bp = build("a sketch", mode="max")
    assert bp.mode_used == "max"
    assert "CANONICAL LABELED-SPEC FIELDS" in bp.instructions


def test_build_invalid_mode_raises() -> None:
    with pytest.raises(ValueError):
        build("x", mode="extreme")


# ---------------------------------------------------------------------------
# build — raw-mode warnings
# ---------------------------------------------------------------------------


def test_build_raw_mode_ignores_skills_with_warning() -> None:
    bp = build("a sketch", mode="raw", skills_body="# brand stuff")
    assert bp.mode_used == "raw"
    assert bp.instructions == ANTI_REFINEMENT_INSTRUCTIONS
    assert any("Skills ignored" in w for w in bp.warnings)


def test_build_raw_mode_ignores_extra_instructions_with_warning() -> None:
    bp = build("a sketch", mode="raw", extra_instructions="dark mood")
    assert "extra_instructions ignored" in " ".join(bp.warnings)


def test_build_raw_mode_ignores_transparent_with_warning() -> None:
    bp = build("a sketch", mode="raw", transparent=True)
    assert any("transparent=True ignored" in w for w in bp.warnings)


# ---------------------------------------------------------------------------
# build — medium/high enrichment blocks
# ---------------------------------------------------------------------------


def test_build_medium_includes_codex_labeled_spec_preamble() -> None:
    bp = build("x", mode="medium")
    assert CODEX_LABELED_SPEC_PREAMBLE.rstrip() in bp.instructions


def test_build_transparent_appends_chroma_block() -> None:
    bp = build("a logo", transparent=True)
    # Default key is now green (#00FF00).
    assert "#00FF00" in bp.instructions
    assert "chroma-keyed" in bp.instructions


def test_build_transparent_uses_custom_chroma_key_magenta() -> None:
    bp = build("a logo", transparent=True, chroma_key_hex="#FF00FF")
    assert "#FF00FF" in bp.instructions


def test_build_transparent_default_key_is_green() -> None:
    """Default chroma key must be #00FF00 (green), not magenta."""
    bp = build("a logo", transparent=True)
    assert "#00FF00" in bp.instructions
    # The anti-prompt line should also reference the actual key.
    assert "Do not use #00FF00" in bp.instructions


def test_build_includes_batch_context_when_provided() -> None:
    ctx = "Match the visual style of the anchor image."
    bp = build("section 2: features", skills_body="# brand", batch_context=ctx)
    assert ctx in bp.instructions


def test_build_includes_extra_instructions_when_provided() -> None:
    bp = build(
        "a sketch",
        mode="medium",
        extra_instructions="MUST be black and white",
    )
    assert "MUST be black and white" in bp.instructions
    # extra_instructions appended last (strongest recency).
    assert bp.instructions.rstrip().endswith("MUST be black and white")


# ---------------------------------------------------------------------------
# build — verbatim text auto-bump and enforcer
# ---------------------------------------------------------------------------


def test_build_verbatim_text_appends_enforcer_and_bumps_to_high() -> None:
    bp = build(
        {
            "primary_request": "a poster",
            "text_verbatim": "FESTIVAL 2026",
        }
    )
    assert bp.mode_used == "high"
    assert VERBATIM_ENFORCER in bp.instructions
    assert any("verbatim text" in w.lower() for w in bp.warnings)


def test_build_explicit_raw_with_verbatim_stays_raw_no_bump() -> None:
    # Explicit raw must be respected — user asked for it; we don't bump.
    bp = build(
        {"text_verbatim": "HELLO"},
        mode="raw",
    )
    assert bp.mode_used == "raw"
    # No warning about auto-bump because we didn't auto-bump.
    assert not any("auto-resolved to high" in w for w in bp.warnings)


def test_build_explicit_high_with_verbatim_no_bump_warning() -> None:
    bp = build({"text_verbatim": "HELLO"}, mode="high")
    assert bp.mode_used == "high"
    # No warning about auto-bump (user already explicitly said high).
    assert not any("auto-resolved" in w for w in bp.warnings)
    # But enforcer still appended.
    assert VERBATIM_ENFORCER in bp.instructions


# ---------------------------------------------------------------------------
# build — references auto-injection on dict prompts
# ---------------------------------------------------------------------------


def test_build_includes_references_auto_input_images_line_when_dict_prompt_missing_input_images() -> None:
    bp = build(
        {"primary_request": "a hero shot"},
        mode="medium",
        references=["a.png", "b.png"],
    )
    assert "Input images: Image 1 = primary reference; Image 2 = secondary reference" in bp.final_prompt


def test_build_single_reference_uses_simple_label() -> None:
    bp = build(
        {"primary_request": "a hero shot"},
        mode="medium",
        references=["a.png"],
    )
    assert "Input images: Image 1 = reference" in bp.final_prompt


def test_build_does_not_overwrite_explicit_input_images() -> None:
    bp = build(
        {
            "primary_request": "edit",
            "input_images": "Image 1 = original photo to edit",
        },
        mode="medium",
        references=["a.png", "b.png"],
    )
    assert "Image 1 = original photo to edit" in bp.final_prompt
    assert "primary reference" not in bp.final_prompt


def test_build_str_prompt_with_references_does_not_inject_line() -> None:
    # Auto input_images line is dict-only; str prompts are passthrough.
    bp = build(
        "a hero shot",
        mode="medium",
        references=["a.png", "b.png"],
    )
    assert "Input images:" not in bp.final_prompt


# ---------------------------------------------------------------------------
# build — pure prompt rendering passthrough
# ---------------------------------------------------------------------------


def test_build_with_str_prompt_passes_through_unchanged_in_medium() -> None:
    bp = build("a coffee mug, minimal", mode="medium")
    # str prompts are NOT restructured into labeled spec; only vars are
    # substituted (none here).
    assert bp.final_prompt == "a coffee mug, minimal"


def test_build_with_str_prompt_substitutes_vars() -> None:
    bp = build(
        "a portrait of {char}",
        mode="medium",
        vars={"char": "an alpinist"},
    )
    assert bp.final_prompt == "a portrait of an alpinist"


def test_build_returns_built_prompt_dataclass() -> None:
    bp = build("hello")
    assert isinstance(bp, BuiltPrompt)
    assert isinstance(bp.warnings, tuple)


# ---------------------------------------------------------------------------
# build — instruction composition order
# ---------------------------------------------------------------------------


def test_build_medium_instruction_order_is_preamble_then_skills_then_chroma_then_batch_then_extra() -> None:
    bp = build(
        {"primary_request": "x"},
        skills_body="SKILL_BODY_MARKER",
        transparent=True,
        batch_context="BATCH_MARKER",
        extra_instructions="EXTRA_MARKER",
    )
    instr = bp.instructions
    i_preamble = instr.index("CANONICAL LABELED-SPEC FIELDS")
    i_skill = instr.index("SKILL_BODY_MARKER")
    i_chroma = instr.index("chroma-keyed")
    i_batch = instr.index("BATCH_MARKER")
    i_extra = instr.index("EXTRA_MARKER")
    assert i_preamble < i_skill < i_chroma < i_batch < i_extra


# ---------------------------------------------------------------------------
# Nice-to-have additions — boundary / type-strictness coverage
# ---------------------------------------------------------------------------


def test_build_input_images_non_string_value_triggers_auto_fill() -> None:
    # Non-string `input_images` (here: int 0) must be treated as "missing"
    # and overridden by the references-driven auto-fill line.
    bp = build(
        {"primary_request": "X", "input_images": 0},  # type: ignore[dict-item]
        references=["a.png"],
    )
    assert "Input images: Image 1 = reference" in bp.final_prompt


def test_render_dict_text_verbatim_with_format_tokens() -> None:
    # Var substitution must happen BEFORE verbatim-wrapping, so the final
    # quoted text contains the substituted value.
    out = render_prompt(
        {"text_verbatim": "PRICE {amount}"},
        vars={"amount": "$5"},
    )
    assert 'Text (verbatim): "PRICE $5"' in out


def test_render_dict_with_list_value_raises_typeerror() -> None:
    with pytest.raises(TypeError) as excinfo:
        render_prompt({"subject": ["a", "b"]})  # type: ignore[dict-item]
    msg = str(excinfo.value)
    assert "subject" in msg
    assert "list" in msg


def test_build_empty_references_list_is_noop() -> None:
    # Empty references list → no auto-fill, no Input images line at all.
    bp = build({"primary_request": "X"}, references=[])
    assert "Input images:" not in bp.final_prompt
