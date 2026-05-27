---
name: imagen
description: Generate, batch, chain, and brand-set images via the Codex OAuth bridge (uses ChatGPT subscription, no OPENAI_API_KEY). Five batch modes (single, parallel, variants, chain, branded-parallel), four reasoning modes (raw, medium, high, max), built-in Pillow chroma-key for transparent PNGs, skill-file loader, structured Codex labeled-spec prompts.
---

# imagen — AI caller reference

## What this is

`codex-imagen` generates images through the Codex OAuth bridge. It uses the local
`~/.codex/auth.json` token (your ChatGPT subscription quota). No `OPENAI_API_KEY`
needed. One engine, three interfaces: MCP tool, CLI, Python SDK.

---

## How to call it

| Interface | Invocation |
|---|---|
| MCP tool | `mcp__codex-imagen__imagen` |
| CLI | `imagen "prompt" [flags]` |
| Python SDK | `from codex_imagen import imagen` then `imagen(prompt=..., ...)` |

All three accept the same parameter surface (`ImagenOptions`). Results share the
same shape (`ImagenResult`).

---

## Return shape

```json
{
  "ok": true,
  "mode": "medium",
  "batch_mode": "branded-parallel",
  "images": [
    {"path": "/abs/path/00.png", "bytes": 412800, "index": 0, "prompt": "..."}
  ],
  "manifest_path": "/abs/path/manifest.jsonl",
  "elapsed_ms": 73400,
  "warnings": []
}
```

**Always check `ok` first.** On failure: `{"ok": false, "error": "...", "error_type": "..."}`.

---

## Batch mode guide

`batch_mode` controls how prompts are arranged and how many API calls are made.
Use `auto` (default) when unsure — it detects the right mode from prompt shape.

### `single`
One prompt → one image. The default when you pass a plain string.

```python
imagen(prompt="a ceramic mug, minimal hero shot")
```

When to use: any single image need.

---

### `parallel`
N independent prompts → N independent images, executed concurrently.

```python
imagen(
    prompt=["a coffee mug", "a notebook", "a ballpoint pen"],
    batch_mode="parallel",
    parallel=3,
)
```

When to use: unrelated assets that do not need to match each other visually.

---

### `variants`
One prompt + `count=N` → N stylistic variations of the same subject.
The prompt is used for every call; the model introduces variation naturally.

```python
imagen(
    prompt="abstract geometric logo, minimal",
    batch_mode="variants",
    count=4,
)
```

When to use: exploring art direction, A/B options for a single asset.

---

### `chain`
N prompts, sequential. Each call receives the previous image as a reference,
maintaining visual continuity across the set.

```python
imagen(
    prompt=["scene 1: dawn mist", "scene 2: noon sunlight", "scene 3: dusk glow"],
    batch_mode="chain",
    chain_mode="anchor+previous",  # default
)
```

`chain_mode` options: `previous` | `anchor` | `anchor+previous` | `window:N` | `all`

When to use: storyboards, before/after panels, sequential narrative.

---

### `branded-parallel`
N prompts + an anchor → anchor is generated first, then all others are generated
in parallel using the anchor image as a style reference. All output images share
the anchor's visual language.

```python
imagen(
    prompt=["hero section", "features section", "pricing section", "footer section"],
    anchor="warm beige editorial, soft serif headings, generous whitespace",
    batch_mode="branded-parallel",
    parallel=3,
)
```

If `anchor` is omitted, `prompt[0]` is used as the anchor and the rest are
generated referencing it.

When to use: **website sections, brand kits, hero + feature image sets**.
This is the right mode for any multi-image work that needs visual consistency.
**Use ONE branded-parallel call instead of N separate calls.**

---

## Reasoning mode guide

`mode` controls how much the mainline model (`gpt-5.5`) polishes the prompt before
the image tool runs. This maps to the bridge's `reasoning_effort`.

| Mode | Effort | Typical time | When to use |
|---|---|---|---|
| `raw` | none | ~15-25 s | Prompt is already final. Skills are ignored. Fastest. |
| `medium` | medium | ~60-100 s | Default with skills. Integrates skill instructions. |
| `high` | high | ~90-150 s | Complex multi-subject briefs, exact text rendering. |
| `max` | xhigh | ~120-200 s | Very complex scenes, maximum visual fidelity. |
| `auto` | resolved | varies | Let the system decide (based on prompt shape, skills, transparency). |

**Auto-resolution rules:**
- Prompt is a Codex labeled-spec dict with `Text (verbatim)` → `high`
- Skills are loaded → `medium`
- `transparent=True` or `extra_instructions` set → `medium`
- Plain string, no extras → `raw`

---

## Transparency

`transparent=True` activates the chroma-key pipeline:

1. Bridge renders subject on solid green (`#00FF00` by default — industry standard)
2. Auto-key border sampling detects the actual rendered key color (handles model drift)
3. Pillow keys out the background to alpha using dual-threshold smoothstep matte
4. Dominance-capping despill removes residual key-color tint at subject edges
5. Result: clean RGBA PNG with no color contamination on the subject body

```python
imagen(
    prompt="a red apple, isolated subject",
    transparent=True,
    # Green is the default and works for most subjects.
    # Use chroma_key="#FF00FF" (magenta) only for green subjects.
    chroma_despill=True,           # neutralizes green fringe at edges
    chroma_despill_mode="dominance",  # physically correct cap-based despill (default)
    chroma_auto_key="border",      # auto-detect actual border color (default)
    chroma_transparent_threshold=12.0,  # dual-threshold: distance ≤ this → alpha=0
    chroma_opaque_threshold=220.0,      # dual-threshold: distance ≥ this → alpha=255
)
```

The raw opaque render is preserved as `<name>.raw.png` for debugging.

Good for: product cut-outs, logo isolation, asset extraction, overlays.

**Complex subjects** (fur, hair, feathers, glass, smoke, liquids, translucent
materials): a warning fires automatically when the prompt contains these keywords.
Chroma-key may leave fringe at semi-transparent edges on these subjects — consider
a model with native transparency support for perfect alpha.

Do **not** combine `transparent=True` with `mode="raw"` — a warning fires and
the pipeline still runs but prompt quality may be lower.

---

## Skills

Skills are markdown files. Bodies are stripped of YAML frontmatter and merged
into the prompt builder's `instructions=` block. They encode brand guidelines,
design systems, domain knowledge.

```python
imagen(
    prompt="hero shot for a fintech landing page",
    skills=[
        "~/.claude/skills/brandkit/SKILL.md",
        "./project/skills/typography.md",
    ],
)
```

- Mode auto-upgrades to `medium` when skills are passed (needed for instruction injection)
- Skills are ignored in `mode="raw"` (a warning is appended to `result.warnings`)
- Skill directories: `~/.claude/skills/<name>/SKILL.md` or `~/.codex/skills/<name>/SKILL.md`

---

## Structured prompts (Codex labeled-spec)

For complex briefs, pass a dict instead of a string. The prompt builder
serializes it into the Codex labeled-spec format.

```python
imagen(
    prompt={
        "Subject": "a minimalist desk lamp",
        "Style": "editorial product photography, white background",
        "Lighting": "soft diffused, single key light from top-left",
        "Composition": "centered, slight three-quarter angle",
        "Output format": "1:1 square, clean margins",
    }
)
```

Common labeled-spec keys: `Subject`, `Style`, `Mood`, `Lighting`, `Composition`,
`Color palette`, `Text (verbatim)` (triggers `high` mode for exact rendering),
`Background`, `Output format`.

---

## Key parameters

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `prompt` | str \| list \| dict | required | String, list of strings, or labeled-spec dict |
| `batch_mode` | str | `"auto"` | single, parallel, variants, chain, branded-parallel |
| `mode` | str | `"auto"` | raw, medium, high, max |
| `anchor` | str \| None | None | Explicit anchor for branded-parallel; falls back to prompt[0] |
| `count` | int | 1 | Number of variants (variants mode only) |
| `skills` | list[str] | [] | Skill file paths |
| `references` | list[str] | [] | Reference image paths |
| `transparent` | bool | False | Enable chroma-key pipeline (auto-warns on complex subjects) |
| `parallel` | int | 2 | Max concurrent API calls |
| `output_dir` | str \| Path | `"./out"` | Where images + manifest.jsonl land |
| `size` | str | `"auto"` | `"WIDTHxHEIGHT"` — both axes multiples of 16, max 3840px |
| `output_format` | str | `"png"` | png, jpeg, webp |
| `extra_instructions` | str \| None | None | Appended to the prompt builder |
| `wall_clock_timeout` | int | 240 | Per-call timeout in seconds |

---

## Best practices for AI callers

1. **Multi-section work**: use ONE `branded-parallel` call, not N separate calls.
   One call = one anchor pass + N parallel calls. N separate calls = N unrelated images.

2. **Rate-limit safety**: set `parallel=3` for batches of 4+ images. The bridge
   has per-minute quota; exceeding it silently degrades quality or returns errors.

3. **Organize output**: pass `output_dir` to keep generated batches in named folders.
   The manifest at `output_dir/manifest.jsonl` has one entry per image with full metadata.

4. **Complex briefs**: use the labeled-spec dict format. It gives the prompt builder
   structured fields to work with rather than a monolithic string.

5. **Simple prompts**: pass a plain string with `mode="raw"` for the fastest path
   when you've already crafted the prompt yourself.

6. **Skill reuse**: the same `.md` skill files used by Claude Code / Codex work
   directly — pass the path, frontmatter is stripped automatically.

---

## Common pitfalls

- **N separate calls instead of branded-parallel**: generates N unrelated images.
  Use `batch_mode="branded-parallel"` for sets that need to match visually.

- **`mode="raw"` with skills**: skills are silently ignored. A warning appears in
  `result.warnings`. If you need skills, use `mode="medium"` (or `auto`).

- **`transparent=True` with `mode="raw"`**: warning fires. The pipeline runs but
  the subject framing (magenta background instruction) may not be enforced.

- **`wall_clock_timeout`**: defaults to 240 s per call. The bridge has variance;
  a warning is added to `result.warnings` at 180 s elapsed. Do not set this below
  60 s or you will get spurious timeouts on the first call of a session.

- **`size` constraints**: both axes must be multiples of 16, ratio ≤ 3:1, max 3840 px.
  The size validator will raise with a nearest-legal suggestion if you violate these.

- **`n > 1`**: the bridge rejects multiple images per call (HTTP 400). Parallelism
  is always achieved through multiple serial bridge calls, not `n`.

---

## Example: website image set

One branded-parallel call to generate a complete landing page image set:

```python
from codex_imagen import imagen

result = imagen(
    prompt=[
        "hero: bold product shot, above the fold",
        "feature 1: speed and performance, abstract motion",
        "feature 2: security and trust, clean icons on light background",
        "feature 3: simplicity, minimal hand interaction with device",
        "social proof: happy professional team, candid office moment",
    ],
    anchor="warm off-white editorial, soft navy accents, generous whitespace, modern sans-serif, high-end SaaS product",
    batch_mode="branded-parallel",
    mode="medium",
    parallel=3,
    output_dir="./out/landing-page",
    size="1536x1024",
    output_format="png",
)

if not result.ok:
    raise RuntimeError(result.error)

for img in result.images:
    print(img.path)

# Manifest with full metadata: ./out/landing-page/manifest.jsonl
```

CLI equivalent:

```bash
imagen -f sections.txt \
  --batch-mode branded-parallel \
  --anchor "warm off-white editorial, soft navy accents, generous whitespace" \
  --mode medium \
  --parallel 3 \
  --output-dir ./out/landing-page \
  --size 1536x1024
```
