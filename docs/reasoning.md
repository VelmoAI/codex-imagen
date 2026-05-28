# Reasoning modes

`mode` controls how aggressively the mainline model (`gpt-5.5`) polishes your
prompt before the image tool runs. Internally this maps to the bridge's
`reasoning_effort`.

Use `auto` (default) unless you have a specific reason to override.

---

## Comparison table

| Mode | Effort | Typical wall-clock | When to use |
|---|---|---|---|
| `raw` | `none` | ~15–25 s | Prompt is already final. Skills are ignored. Fastest. |
| `medium` | `medium` | ~60–100 s | Default when skills are loaded; integrates them into the Codex labeled-spec scaffold. |
| `high` | `high` | ~90–150 s | Complex multi-subject briefs, exact text rendering. |
| `max` | `xhigh` | ~120–200 s | Maximum reasoning. Reserve for very complex multi-subject layouts. |
| `auto` | resolved | varies | Let the system decide — almost always the right default. |

---

## `raw`

Passes the prompt byte-for-byte to the bridge with no model polish.

```python
from codex_imagen import imagen

# You've already crafted the prompt — just pass it through
result = imagen(
    prompt="minimalist white ceramic mug, centered, studio light, 1:1",
    mode="raw",
)
```

**Constraints:**
- Skills are silently ignored in `raw` mode (a warning fires). If you need
  skills, use `medium` or `auto`.
- `transparent=True` with `raw` fires a warning: the subject-framing instruction
  may not be enforced, reducing transparency quality.

---

## `medium`

Integrates skill instructions into the Codex labeled-spec scaffold. Default
when any skill is loaded.

```python
result = imagen(
    prompt="hero shot for a fintech landing page",
    mode="medium",
    skills=["~/.claude/skills/brandkit/SKILL.md"],
)
```

Good balance of speed and quality for skill-driven work.

---

## `high`

Premium planning for complex briefs: multi-subject scenes, exact text rendering,
precise layout instructions.

```python
result = imagen(
    prompt={
        "Subject": "a minimalist desk lamp and a ceramic mug",
        "Text (verbatim)": "FOCUS",
        "Style": "editorial product photography",
        "Lighting": "soft diffused, single key from top-left",
    },
    mode="high",
)
```

**Auto-trigger:** `auto` resolves to `high` when the prompt dict contains
`Text (verbatim)` (exact-text rendering needs more polish).

---

## `max`

Maximum reasoning effort. Slower; reserve for very complex multi-subject scenes
where maximum visual fidelity matters more than speed.

```python
result = imagen(
    prompt="a surrealist clockwork cityscape with five distinct architectural styles",
    mode="max",
)
```

Typical wall-clock: 2–3 minutes. Do not set `wall_clock_timeout` below 180 s
when using `max`.

---

## `auto` resolution rules

`auto` resolves to one of the four concrete modes at call time:

1. Prompt dict contains `Text (verbatim)` → `high`
2. Skills are loaded (`skills=[...]` non-empty) → `medium`
3. `transparent=True` or `extra_instructions` is set → `medium`
4. Plain string, no extras → `raw`

The resolved mode is reported in `ImagenResult.mode` so you can see what the
system chose.

---

## Related

- [Batch modes](modes.md) — single, parallel, variants, chain, branded-parallel
- [Skills](skills.md) — how skill presence affects mode selection
- [SDK reference](sdk.md) — `mode` and `wall_clock_timeout` fields
