# README / Docs Restructure Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Split the current monolithic README.md into a short (~120-line) scannable README plus 9 focused docs/ files extracted from existing content.

**Architecture:** Pure documentation reorganization — no code changes. Extract sections from README.md and `src/codex_imagen/_assets/skills/imagen.md` (the canonical reference) into individual files. README becomes a gateway with highlights, install, 60-second quick start, and links.

**Tech Stack:** Markdown only. No new dependencies. Tests are code-only (no README references) so all 359 tests must remain green.

---

## Pre-flight checklist

Before starting, confirm:
- Working directory is `C:\Users\wauwa\Desktop\codex-image-forge`
- `python -m pytest tests/ -q` → 359 passed
- `RELEASING.md` exists at root (keep it; docs/releasing.md will be a copy)
- `docs/` directory already exists (it will after Task 1)

---

## Task 1: Create the docs/ directory

**Files:**
- Create: `docs/.gitkeep` (ensures the directory exists in git before any docs land)

**Step 1: Create the directory and placeholder**

```bash
mkdir -p docs
touch docs/.gitkeep
```

**Step 2: Verify**

```
ls docs/
```
Expected: `.gitkeep`

**Step 3: Commit**

```bash
git add docs/.gitkeep
git commit -m "chore: create docs/ directory"
```

---

## Task 2: Write docs/modes.md

**Files:**
- Create: `docs/modes.md`
- Source: README.md "The five batch modes" section + imagen.md batch mode guide

**Step 1: Write the file**

Content to write to `docs/modes.md`:

````markdown
# Batch modes

`codex-imagen` supports five batch modes. Set `batch_mode=` explicitly or let
`auto` (default) pick the right one based on prompt shape.

The mode is recorded in the manifest (`batch_mode` field) and on
`ImagenResult.batch_mode` for debuggability.

---

## `single`

One prompt → one image.

```python
from codex_imagen import imagen

result = imagen(prompt="a ceramic coffee mug, minimal hero shot")
print(result.images[0].path)
```

**When to use:** any single-image need. This is the default when you pass a
plain string and no batch hints are set.

**Output shape:** `result.images` has exactly one entry.

---

## `parallel`

N independent prompts → N independent images, generated concurrently.

```python
result = imagen(
    prompt=["a coffee mug", "a notebook", "a ballpoint pen"],
    batch_mode="parallel",
    parallel=3,
)
```

**When to use:** unrelated assets that do not need to match each other visually.
If they need to match, use `branded-parallel` instead.

**Output shape:** `result.images[i]` corresponds to `prompt[i]`.

---

## `branded-parallel`

N prompts + an anchor → anchor is generated first, then all others are generated
in parallel using the anchor image as a visual style reference. All images share
the anchor's visual language (palette, typography, lighting, mood).

```python
result = imagen(
    prompt=[
        "hero section: bold product shot, above the fold",
        "features section: speed and performance, abstract motion",
        "pricing section: clean table layout on light background",
        "footer section: minimal brand mark and contact info",
    ],
    anchor="warm off-white editorial, soft navy accents, generous whitespace, modern sans-serif",
    batch_mode="branded-parallel",
    parallel=3,
    output_dir="./out/landing-page",
)
```

If `anchor` is omitted, `prompt[0]` is used as the anchor prompt and the
remaining prompts are generated referencing it.

**When to use:** website sections, brand kits, hero + feature image sets — any
multi-image work that needs visual consistency. Use ONE `branded-parallel` call
instead of N separate calls; N separate calls produce N unrelated images.

**Output shape:** `result.images[0]` is the anchor; `result.images[1:]` are the
dependent images in prompt order.

**CLI:**

```bash
imagen -f sections.txt \
  --batch-mode branded-parallel \
  --anchor "warm off-white editorial, soft navy accents" \
  --parallel 3 \
  --output-dir ./out/landing-page
```

---

## `chain`

N prompts, executed sequentially. Each call receives the previous image as a
reference, maintaining visual continuity across the set.

```python
result = imagen(
    prompt=["scene 1: dawn mist", "scene 2: noon sunlight", "scene 3: dusk glow"],
    batch_mode="chain",
    chain_mode="anchor+previous",  # default
)
```

`chain_mode` options:

| Value | Behaviour |
|---|---|
| `previous` | Each call references only the immediately prior image |
| `anchor` | Each call references only the first image (anchor) |
| `anchor+previous` | Each call references both anchor and the prior image (default) |
| `window:N` | Each call references the last N images |
| `all` | Each call references all previous images |

**When to use:** storyboards, before/after panels, sequential narrative.

**Output shape:** `result.images[i]` corresponds to `prompt[i]` in order.

---

## `variants`

One prompt + `count=N` → N stylistic variations of the same subject. The prompt
is used for every call; the model introduces variation naturally.

```python
result = imagen(
    prompt="abstract geometric logo, minimal, single color",
    batch_mode="variants",
    count=4,
)
```

**When to use:** exploring art direction, A/B options for a single asset.

**Output shape:** `result.images` has `count` entries, all derived from the same
prompt.

---

## Frequency guide

| Mode | Best for | Typical call count |
|---|---|---|
| `single` | One hero shot | 1 |
| `parallel` | Unrelated assets | N |
| `branded-parallel` | Website sections, brand sets | 1 + N parallel |
| `chain` | Storyboards, narratives | N sequential |
| `variants` | Art direction exploration | N |

---

## Related

- [Reasoning modes](reasoning.md) — controls prompt polish quality
- [SDK reference](sdk.md) — full `ImagenOptions` parameter list
- [CLI reference](cli.md) — `--batch-mode`, `--anchor`, `--chain-mode` flags
````

**Step 2: Verify file exists and line count is reasonable**

```bash
wc -l docs/modes.md
```
Expected: ~130–160 lines.

**Step 3: Commit**

```bash
git add docs/modes.md
git commit -m "docs: add modes.md — 5 batch modes reference"
```

---

## Task 3: Write docs/reasoning.md

**Files:**
- Create: `docs/reasoning.md`
- Source: README.md "Reasoning modes" section + imagen.md reasoning mode guide

**Step 1: Write the file**

Content to write to `docs/reasoning.md`:

````markdown
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
- [Skills](skills.md) — how skill files interact with mode selection
- [SDK reference](sdk.md) — `mode` and `wall_clock_timeout` fields
````

**Step 2: Verify**

```bash
wc -l docs/reasoning.md
```
Expected: ~100–130 lines.

**Step 3: Commit**

```bash
git add docs/reasoning.md
git commit -m "docs: add reasoning.md — 4 reasoning modes with timing"
```

---

## Task 4: Write docs/transparency.md

**Files:**
- Create: `docs/transparency.md`
- Source: README.md "Transparency (chroma-key pipeline)" section + imagen.md Transparency section + `src/codex_imagen/mcp_server.py` chroma parameter descriptions

**Step 1: Write the file**

Content to write to `docs/transparency.md`:

````markdown
# Transparency

`codex-imagen` produces transparent PNGs through a **chroma-key post-process**
using Pillow. The Codex OAuth bridge rejects `background: "transparent"` with
HTTP 400, so native transparency is not available — chroma-key is the only path.

---

## The pipeline (what happens when `transparent=True`)

1. **Subject rendered on solid green** — `#00FF00` by default (industry-standard
   chroma key color). The bridge renders your subject on this background.

2. **Auto-key border sampling** — `_chroma.keyout()` samples the actual rendered
   background color from the image border, handling model drift where the model
   renders `#01FE00` instead of the exact `#00FF00` you asked for.

3. **Dual-threshold smoothstep matte** — pixels are keyed to alpha using two
   thresholds:
   - Distance ≤ `chroma_transparent_threshold` (default 12) → fully transparent
   - Distance ≥ `chroma_opaque_threshold` (default 220) → fully opaque
   - Between the two → smooth ramp (no hard edge artifacts)

4. **Dominance-capping despill** — residual green tint at subject edges is
   removed by capping the spill channel to `max(non-spill) − 1`. Physically
   correct; no over-despill that would tint the subject body.

5. **Raw image preserved** — the original opaque render is saved as
   `<name>.raw.png` for debugging.

---

## Basic usage

```python
from codex_imagen import imagen

result = imagen(
    prompt="a red apple, isolated subject, centered",
    transparent=True,
)
# result.images[0].path → RGBA PNG
# result.images[0].raw_png_path → the opaque render (for debugging)
```

---

## Why green, not magenta?

Green (`#00FF00`) is the industry-standard chroma key color — it is the farthest
color from human skin tones in RGB space, minimizing accidental keying of the
subject. Use magenta (`#FF00FF`) only when your subject contains green.

```python
# For a cactus, plant, or anything inherently green:
result = imagen(
    prompt="a cactus in a terracotta pot",
    transparent=True,
    chroma_key="#FF00FF",  # switch to magenta
)
```

---

## Tunable parameters

| Parameter | Default | Description |
|---|---|---|
| `chroma_key` | `"#00FF00"` | Background key color. `#FF00FF` for green subjects. |
| `chroma_tolerance` | `40` | 0–100. Higher → more pixels treated as background. |
| `chroma_transparent_threshold` | `12.0` | Distance ≤ this → fully transparent. |
| `chroma_opaque_threshold` | `220.0` | Distance ≥ this → fully opaque. |
| `chroma_despill` | `True` | Run despill pass to neutralize edge tint. |
| `chroma_despill_mode` | `"dominance"` | `dominance` (cap-based, default) or `projection` (legacy). |
| `chroma_edge_erode_px` | `1` | Pixels to erode alpha mask before feathering. 0 disables. |
| `chroma_auto_key` | `"border"` | Sample actual key color from border (`"border"`, `"corners"`, or `None`). |

Full example with all knobs:

```python
result = imagen(
    prompt="a single red apple, isolated",
    transparent=True,
    chroma_key="#00FF00",
    chroma_tolerance=40,
    chroma_despill=True,
    chroma_despill_mode="dominance",
    chroma_edge_erode_px=1,
    chroma_auto_key="border",
    chroma_transparent_threshold=12.0,
    chroma_opaque_threshold=220.0,
)
```

---

## Complex subjects

A warning fires automatically when the prompt contains these keywords: `fur`,
`hair`, `feathers`, `glass`, `smoke`, `liquid`, `translucent`.

**Why:** chroma-key leaves fringe at semi-transparent edges on these subjects.
The 54× improvement claim (vs. naive magenta-key) applies to typical solid
subjects like logos, products, and simple cut-outs. Complex subjects with
fine or transparent edges may still show fringe artifacts.

**Recommendation for complex subjects:** consider a model with native
transparency support (e.g. via the OpenAI Images API with
`background: "transparent"` on a supported model). codex-imagen's chroma
pipeline is a best-effort approach — it does not compete with native alpha
rendering on complex subjects.

---

## Quantitative note

The `dominance` despill mode produces **54× cleaner fringe** on fur/hair
subjects compared to a naive magenta-key with no despill. This measurement
is based on average residual key-color saturation in the 10-pixel fringe zone
across a test set of fur and hair subjects.

---

## Related

- [Batch modes](modes.md) — using transparency with parallel/branded-parallel
- [SDK reference](sdk.md) — all chroma parameters in `ImagenOptions`
- [CLI reference](cli.md) — `--transparent`, `--chroma-key`, `--chroma-despill` flags
````

**Step 2: Verify**

```bash
wc -l docs/transparency.md
```
Expected: ~130–160 lines.

**Step 3: Commit**

```bash
git add docs/transparency.md
git commit -m "docs: add transparency.md — chroma-key pipeline reference"
```

---

## Task 5: Write docs/skills.md

**Files:**
- Create: `docs/skills.md`
- Source: README.md "Skills" section + imagen.md Skills section + `src/codex_imagen/_skills.py` for format details

**Step 1: Write the file**

Content to write to `docs/skills.md`:

````markdown
# Skills

A "skill" is a Markdown file that encodes domain knowledge — brand guidelines,
design systems, style constraints. `codex-imagen` loads skills, strips YAML
frontmatter, and merges the body into the prompt builder's `instructions=` block.

Skills are the same format used by Claude Code and Codex. You can reuse skill
files that already live in `~/.claude/skills/` or `~/.codex/skills/` — just
pass the path.

---

## The bundled `imagen` skill

`imagen setup` installs a self-describing skill alongside the MCP config:

| Client | Skill location |
|---|---|
| Claude Code | `~/.claude/skills/imagen/SKILL.md` |
| Claude Desktop | `~/.claude/skills/imagen/SKILL.md` |
| Codex | `~/.codex/skills/imagen/SKILL.md` |
| Cursor / OpenCode | skill concept not yet supported |

After setup, your AI agent reads this skill at session start and automatically
knows how to call `imagen` — modes, batch strategies, transparency, brand sets —
without you explaining it each time.

The skill ships inside the wheel (`src/codex_imagen/_assets/skills/imagen.md`)
so it is always in sync with the installed version. `imagen uninstall` removes
it; `imagen status` shows whether it is present per client.

---

## External skill loader

Pass one or more `.md` skill file paths to inject brand or domain context:

```python
from codex_imagen import imagen

result = imagen(
    prompt="hero shot for a fintech landing page",
    skills=[
        "~/.claude/skills/brandkit/SKILL.md",
        "./project/skills/typography.md",
    ],
)
```

CLI:

```bash
imagen "hero shot for a fintech landing page" \
  --skill ~/.claude/skills/brandkit/SKILL.md \
  --skill ./project/skills/typography.md
```

Multiple skills are concatenated with a separator before being injected.

---

## Skill format

A skill file is plain Markdown with an optional YAML frontmatter block:

```markdown
---
name: my-brand
description: Brand guidelines for Acme Corp
---

# Acme Corp Brand Guidelines

## Color palette

Primary: #1A2B3C (deep navy)
Accent: #F5A623 (warm amber)
Background: #FAFAF8 (warm off-white)

## Typography

Headings: "Freight Display Pro", serif, medium weight
Body: "Inter", sans-serif, regular

## Photography style

Editorial product photography. Generous whitespace. Soft diffused lighting
from upper-left. No hard shadows. Subjects centered with breathing room.
```

YAML frontmatter is stripped before injection — only the Markdown body is
merged into the prompt builder. The `name` and `description` fields are
used for logging and manifest recording.

---

## Example: brand skill applied to a website image set

```python
result = imagen(
    prompt=[
        "hero: bold product shot",
        "features: clean icon grid on light background",
        "social proof: candid team photo",
    ],
    anchor="modern SaaS product, high-end editorial",
    batch_mode="branded-parallel",
    skills=["./brand/acme-brand.md"],
    output_dir="./out/website",
)
```

---

## Skill constraints

- Skills are **ignored in `mode="raw"`** — raw mode passes the prompt verbatim
  with no instruction block. A warning is appended to `result.warnings`.
- Mode **auto-upgrades to `medium`** when skills are passed (needed for
  instruction injection).
- Tilde `~` in paths is expanded to the home directory automatically.
- Missing skill files in non-strict mode emit a warning and are skipped;
  in strict mode they raise `FileNotFoundError`.

---

## Related

- [MCP server](mcp.md) — how `imagen setup` installs the bundled skill
- [Reasoning modes](reasoning.md) — how skill presence affects mode selection
- [SDK reference](sdk.md) — `skills` parameter in `ImagenOptions`
````

**Step 2: Verify**

```bash
wc -l docs/skills.md
```
Expected: ~120–150 lines.

**Step 3: Commit**

```bash
git add docs/skills.md
git commit -m "docs: add skills.md — bundled skill + external skill loader"
```

---

## Task 6: Write docs/mcp.md

**Files:**
- Create: `docs/mcp.md`
- Source: README.md "MCP integration" + "Quick install" sections + `src/codex_imagen/cli.py` setup/status/uninstall commands + `src/codex_imagen/_install.py` for client keys

**Step 1: Write the file**

Content to write to `docs/mcp.md`:

````markdown
# MCP server

`codex-imagen` ships a stdio MCP (Model Context Protocol) server that exposes a
single `imagen` tool. AI clients that support MCP can call `imagen` as a
structured tool — no CLI invocation or subprocess needed.

---

## What MCP gives you

- Claude Desktop, Cursor, and other MCP clients can call `imagen` as a first-class
  tool with full parameter validation
- The tool description embedded in the JSON schema teaches the model how to pick
  a `batch_mode`, when to use each reasoning mode, and how transparency/skills work
- Errors are returned as structured `{ok: false, error: "...", error_type: "..."}` —
  never as protocol-level exceptions — so the calling model can react and retry

---

## Quick install: `imagen setup`

The fastest path for all clients at once:

```bash
# macOS / Linux — one-liner (installs uv if needed, then runs setup)
curl -fsSL https://raw.githubusercontent.com/VelmoAI/codex-imagen/main/install.sh | sh

# Windows PowerShell
iwr https://raw.githubusercontent.com/VelmoAI/codex-imagen/main/install.ps1 | iex

# Already have uv?
uvx codex-imagen setup

# pip install?
pip install codex-imagen && imagen setup
```

`imagen setup` detects which AI clients are installed on your machine, writes
the MCP config snippet, and installs the bundled skill alongside it.

```bash
imagen setup           # interactive walkthrough
imagen setup --all     # install for all detected clients (no prompts)
imagen setup --client cursor          # target a specific client
imagen setup --dry-run --all          # preview what would happen
```

---

## Per-client install details

### Claude Code

Config location: `~/.claude/claude_desktop_config.json` (shared with Claude Desktop)

Snippet written by `imagen setup`:
```json
{
  "mcpServers": {
    "codex-imagen": {
      "command": "uvx",
      "args": ["codex-imagen-mcp"]
    }
  }
}
```

Skill location: `~/.claude/skills/imagen/SKILL.md`

After setup, restart Claude Code (or reload the window) for the MCP server to
be picked up.

### Claude Desktop

Same config file and skill location as Claude Code. Restart Claude Desktop after
setup.

### Codex CLI

Config location: `~/.codex/config.toml`

`imagen setup --client codex` also writes a preference snippet to
`~/.codex/AGENTS.md` so the Codex AI prefers `imagen` over the built-in
`imagegen` tool.

Skill location: `~/.codex/skills/imagen/SKILL.md`

### Cursor

Config location: Cursor's MCP settings file (detected automatically by `imagen setup`).

Skill: Cursor does not support the Claude/Codex skill convention. The MCP server
itself is registered but no skill file is written.

### OpenCode

Similar to Cursor — MCP config is written, no skill file.

### Generic / manual

If your client is not one of the five above, add this snippet manually:

```json
{
  "mcpServers": {
    "codex-imagen": {
      "command": "uvx",
      "args": ["codex-imagen-mcp"]
    }
  }
}
```

If you prefer a direct path (not uvx):

```json
{
  "mcpServers": {
    "codex-imagen": {
      "command": "python",
      "args": ["-m", "codex_imagen.mcp_server"]
    }
  }
}
```

---

## Check install state

```bash
imagen status
```

Output shows all 5 clients with detected / installed columns, plus skill status:

```
codex-imagen MCP installer — client status

#    Client            Detected    Installed   Config
---------------------------------------------------------------------------
1    Claude Code       yes         yes         /Users/me/.claude/claude_desktop_config.json
2    Claude Desktop    yes         yes         /Users/me/.claude/claude_desktop_config.json
3    Codex             yes         yes         /Users/me/.codex/config.toml
4    Cursor            no          -           (not found)
5    OpenCode          no          -           (not found)

Detected: 3/5  |  Installed: 3/5
```

---

## Uninstall

```bash
imagen uninstall --all        # remove from everywhere
imagen uninstall --client codex   # remove from one client
```

---

## Troubleshooting

**`imagen status` shows "not installed" after `imagen setup`:**
Restart the client. MCP configs are read at startup.

**"command not found: uvx":**
Install uv: `curl -fsSL https://astral.sh/uv/install.sh | sh`, then retry.

**Health check:**
```bash
imagen --health
```
Checks Codex OAuth auth, Pillow, and the bridge library. Run this before
reporting issues.

---

## MCP tool return shape

The `imagen` tool always returns a JSON payload:

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

On failure: `{"ok": false, "error": "...", "error_type": "..."}` — never a
protocol-level MCP error for input problems.

---

## Related

- [Skills](skills.md) — bundled skill installed alongside MCP config
- [CLI reference](cli.md) — `setup`, `uninstall`, `status` subcommands in detail
- [SDK reference](sdk.md) — same parameters available programmatically
````

**Step 2: Verify**

```bash
wc -l docs/mcp.md
```
Expected: ~160–200 lines.

**Step 3: Commit**

```bash
git add docs/mcp.md
git commit -m "docs: add mcp.md — per-client setup and config reference"
```

---

## Task 7: Write docs/cli.md

**Files:**
- Create: `docs/cli.md`
- Source: `src/codex_imagen/cli.py` (authoritative — enumerate every flag and subcommand)

**Step 1: Write the file**

Content to write to `docs/cli.md`:

````markdown
# CLI reference

`imagen` is the command-line interface for codex-imagen. It has dual output
modes (human-friendly TTY / JSON for scripting), auto-detected from whether
stdout is a terminal.

---

## Subcommands

### `imagen setup`

Register codex-imagen MCP in one or more AI clients.

```bash
imagen setup                            # interactive walkthrough
imagen setup --all                      # install for all detected clients
imagen setup --client claude-code       # install for a specific client
imagen setup --client codex --client cursor  # multiple specific clients
imagen setup --dry-run --all            # preview changes, no writes
```

Clients: `claude-code`, `claude-desktop`, `codex`, `cursor`, `opencode`

### `imagen uninstall`

Remove the MCP registration from AI clients.

```bash
imagen uninstall                        # interactive
imagen uninstall --all                  # remove from everywhere
imagen uninstall --client codex         # remove from one client
imagen uninstall --dry-run --all        # preview, no writes
```

### `imagen status`

Show detected clients and whether codex-imagen is installed.

```bash
imagen status
```

Prints a table of 5 clients with `detected`, `installed`, and config path columns,
plus a skill status table.

---

## Generate command (default)

When the first argument is not `setup`, `uninstall`, or `status`, `imagen` runs
image generation.

```bash
imagen "a ceramic coffee mug, minimal hero shot"
imagen "a ceramic mug" --transparent --size 1536x1024
```

### Input flags

| Flag | Short | Default | Description |
|---|---|---|---|
| `PROMPT` | — | — | Positional prompt for single image |
| `--file PATH` | `-f` | — | Read prompts from text file (one per line, `#` = comment) |
| `--prompt-json PATH` | — | — | Read structured Codex labeled-spec dict or list from JSON file |

Precedence: `--prompt-json` > `--file` > positional `PROMPT`.

### Mode flags

| Flag | Short | Default | Description |
|---|---|---|---|
| `--mode` | `-m` | `auto` | Reasoning mode: `auto`, `raw`, `medium`, `high`, `max` |
| `--batch-mode` | `-b` | `auto` | Batch strategy: `auto`, `single`, `parallel`, `variants`, `chain`, `branded-parallel` |
| `--chain-mode` | — | `anchor+previous` | Chain context: `previous`, `anchor`, `anchor+previous`, `window:N`, `all` |
| `--anchor` | `-a` | — | Explicit anchor prompt for `branded-parallel` |
| `--count` | `-n` | `1` | Number of variants (variants mode) |

### Content flags

| Flag | Short | Default | Description |
|---|---|---|---|
| `--reference PATH` | `-r` | — | Reference image (repeatable) |
| `--skill PATH` | `-s` | — | Skill `.md` file to inject (repeatable) |
| `--mask PATH` | — | — | Alpha mask for edits |
| `--extra TEXT` | `-e` | — | Extra instructions appended to prompt |

### Image flags

| Flag | Short | Default | Description |
|---|---|---|---|
| `--size` | — | `auto` | `auto` or `WIDTHxHEIGHT` (both axes multiples of 16, max 3840px) |
| `--output-format` | — | `png` | `png`, `jpeg`, `webp` |

### Transparency flags

| Flag | Default | Description |
|---|---|---|
| `--transparent` | off | Enable chroma-key pipeline (requires Pillow) |
| `--chroma-key HEX` | `#FF00FF` | Key color (`#00FF00` for most subjects, `#FF00FF` for green subjects) |
| `--chroma-tolerance N` | `40` | 0–100. Higher = more pixels treated as background |
| `--chroma-despill` / `--no-chroma-despill` | on | Enable/disable despill pass |

### Orchestration flags

| Flag | Short | Default | Description |
|---|---|---|---|
| `--parallel N` | `-p` | `2` | Max concurrent bridge calls |
| `--output-dir DIR` | `-o` | `./out` | Where images + `manifest.jsonl` are written |

### Variable substitution

```bash
imagen "a {{color}} mug" --var color=red
```

| Flag | Description |
|---|---|
| `--var key=value` | Variable substitution into prompts (repeatable) |
| `--advanced key=value` | Pass-through to codex-image-gen bridge (repeatable, power users) |

### Output / behavior flags

| Flag | Short | Default | Description |
|---|---|---|---|
| `--json` | — | auto | Force JSON output to stdout |
| `--pretty` | — | auto | Force human-readable output |
| `--quiet` | `-q` | off | Suppress progress messages (stderr) |
| `--health` | — | — | Run pre-flight check and exit |
| `--version` | — | — | Print version and exit |

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Health check failure (no API call was made) |
| `2` | Generation failure (all calls failed) |
| `3` | Invalid arguments |

---

## Common workflows

### Single image

```bash
imagen "a coffee mug, minimal hero"
imagen "a coffee mug" --size 1536x1024 --output-dir ./out/product
```

### Transparent cut-out

```bash
imagen "an abstract leaf logo" --transparent
imagen "a plant" --transparent --chroma-key "#FF00FF"  # green subject
```

### Multi-prompt branded set (website sections)

```bash
imagen -f sections.txt \
  --batch-mode branded-parallel \
  --anchor "warm beige editorial, soft serif headings, lots of whitespace" \
  --parallel 3 \
  --output-dir ./out/website
```

### Storyboard chain

```bash
imagen -f frames.txt --batch-mode chain --chain-mode anchor+previous
```

### With a skill file

```bash
imagen "hero shot for a fintech landing page" \
  --skill ~/.claude/skills/brandkit/SKILL.md \
  --output-dir ./out/hero
```

### JSON output for scripting

```bash
# Pipe into jq
imagen "a coffee mug" --json | jq '.images[0].path'

# Non-TTY auto-detects JSON mode
imagen "a coffee mug" > result.json
```

### Structured prompt from JSON file

```bash
# prompt.json contains a dict or list
imagen --prompt-json ./prompt.json --mode high
```

---

## Related

- [SDK reference](sdk.md) — same parameters available programmatically
- [MCP server](mcp.md) — `setup`, `uninstall`, `status` in depth
- [Batch modes](modes.md) — batch strategy details
````

**Step 2: Verify**

```bash
wc -l docs/cli.md
```
Expected: ~180–220 lines.

**Step 3: Commit**

```bash
git add docs/cli.md
git commit -m "docs: add cli.md — every subcommand and flag"
```

---

## Task 8: Write docs/sdk.md

**Files:**
- Create: `docs/sdk.md`
- Source: `src/codex_imagen/core.py` (ImagenOptions, ImagenResult, ImagenImage, imagen())

**Step 1: Write the file**

Content to write to `docs/sdk.md`:

````markdown
# Python SDK reference

```python
from codex_imagen import imagen
```

---

## `imagen(**kwargs) → ImagenResult`

Single entry point for all five batch modes. All parameters are forwarded to
`ImagenOptions` — unknown kwargs raise `TypeError`; invalid values raise
`ValueError`.

```python
from codex_imagen import imagen

# Minimal — one image
result = imagen(prompt="a ceramic coffee mug")

# Branded-parallel website set
result = imagen(
    prompt=["hero section", "features section", "pricing section"],
    anchor="warm editorial, soft navy accents, generous whitespace",
    batch_mode="branded-parallel",
    parallel=3,
    output_dir="./out/website",
)

if not result.ok:
    raise RuntimeError(result.error)

for img in result.images:
    print(img.path, img.bytes)
```

---

## `ImagenOptions` — all fields

Pass any of these as kwargs to `imagen()`.

### Core input

| Field | Type | Default | Description |
|---|---|---|---|
| `prompt` | `str \| dict \| list` | required | String, Codex labeled-spec dict, or list of either |
| `output_dir` | `str \| Path` | `"./out"` | Where images + manifest are written |

### Modes

| Field | Type | Default | Description |
|---|---|---|---|
| `mode` | `str` | `"auto"` | `auto`, `raw`, `medium`, `high`, `max` |
| `batch_mode` | `str` | `"auto"` | `auto`, `single`, `parallel`, `variants`, `chain`, `branded-parallel` |
| `chain_mode` | `str` | `"anchor+previous"` | Chain context strategy |
| `anchor` | `str \| dict \| None` | `None` | Explicit anchor for branded-parallel/chain |
| `count` | `int` | `1` | Variant count (variants mode only) |

### Content

| Field | Type | Default | Description |
|---|---|---|---|
| `references` | `tuple[str, ...]` | `()` | Reference image paths |
| `skills` | `tuple[str, ...]` | `()` | Skill `.md` file paths |
| `mask` | `str \| None` | `None` | Alpha mask for inpainting |
| `extra_instructions` | `str \| None` | `None` | Appended to prompt builder output |

### Image parameters

| Field | Type | Default | Description |
|---|---|---|---|
| `size` | `str` | `"auto"` | `"auto"` or `"WIDTHxHEIGHT"` (both axes ×16, max 3840px, ratio ≤ 3:1) |
| `output_format` | `str` | `"png"` | `"png"`, `"jpeg"`, `"webp"` |

### Transparency

| Field | Type | Default | Description |
|---|---|---|---|
| `transparent` | `bool` | `False` | Enable chroma-key pipeline |
| `chroma_key` | `str` | `"#00FF00"` | Background key color |
| `chroma_tolerance` | `int` | `40` | 0–100 |
| `chroma_despill` | `bool` | `True` | Run despill pass |
| `chroma_despill_mode` | `str` | `"dominance"` | `"dominance"` or `"projection"` |
| `chroma_edge_erode_px` | `int` | `1` | Alpha mask erosion pixels |
| `chroma_auto_key` | `str \| None` | `"border"` | Auto-detect key color: `"border"`, `"corners"`, or `None` |
| `chroma_transparent_threshold` | `float` | `12.0` | Distance ≤ this → fully transparent |
| `chroma_opaque_threshold` | `float` | `220.0` | Distance ≥ this → fully opaque |

### Orchestration

| Field | Type | Default | Description |
|---|---|---|---|
| `parallel` | `int` | `2` | Max concurrent bridge calls |
| `wall_clock_timeout` | `float` | `240.0` | Per-call timeout in seconds. Do not set below 60. |

### Misc

| Field | Type | Default | Description |
|---|---|---|---|
| `enhance_prompt` | `bool` | `False` | Extra polish pass on prompt |
| `vars` | `dict[str, str]` | `{}` | Variable substitutions for templated prompts |
| `advanced` | `dict[str, Any]` | `{}` | Pass-through kwargs to codex-image-gen bridge |

---

## `ImagenResult`

```python
@dataclass(frozen=True)
class ImagenResult:
    ok: bool                    # True if at least one image generated
    mode: str                   # resolved reasoning mode (raw/medium/high/max)
    batch_mode: str             # resolved batch mode
    images: tuple[ImagenImage, ...]
    manifest_path: Path | None  # path to manifest.jsonl
    elapsed_ms: int
    health: ImagenHealth
    error: str | None           # set when ok=False
    warnings: tuple[str, ...]   # non-fatal issues
```

**Always check `ok` before accessing `images`:**

```python
if not result.ok:
    print(f"Failed: {result.error}")
else:
    for img in result.images:
        print(img.path)
```

---

## `ImagenImage`

One generated image entry.

```python
@dataclass(frozen=True)
class ImagenImage:
    index: int              # 0-based position in the batch
    path: Path              # absolute path to saved file
    bytes: int              # file size in bytes
    mime_type: str          # e.g. "image/png"
    response_id: str | None
    call_id: str | None
    revised_prompt: str | None  # model's rewrite of your prompt, if any
    original_prompt: str        # your prompt as submitted
    final_prompt: str           # prompt as sent to the bridge
    transparent: bool
    references_used: tuple[str, ...]
    raw_png_path: Path | None   # opaque render (set when transparent=True)
```

---

## Common patterns

### Single image

```python
result = imagen(prompt="a brass diya oil lamp, editorial product shot")
print(result.images[0].path)
```

### Branded-parallel for websites

```python
result = imagen(
    prompt=[
        "hero: bold product, above the fold",
        "feature 1: speed metric visualization",
        "feature 2: security and trust, clean icons",
        "footer: minimal brand mark",
    ],
    anchor="warm off-white editorial, soft navy, generous whitespace, modern sans-serif",
    batch_mode="branded-parallel",
    mode="medium",
    parallel=3,
    output_dir="./out/landing",
    size="1536x1024",
)
```

### Chain for narrative storyboard

```python
result = imagen(
    prompt=["scene 1: dawn", "scene 2: noon", "scene 3: dusk", "scene 4: night"],
    batch_mode="chain",
    chain_mode="anchor+previous",
    output_dir="./out/storyboard",
)
```

### Variants for art direction

```python
result = imagen(
    prompt="abstract geometric logo, single-color, minimal",
    batch_mode="variants",
    count=4,
    output_dir="./out/logo-options",
)
```

### Transparent cut-out

```python
result = imagen(
    prompt="a red apple, isolated subject",
    transparent=True,
    output_dir="./out/cutouts",
)
# result.images[0].path → RGBA PNG
# result.images[0].raw_png_path → opaque debug render
```

### Wall-clock timeout (long sessions)

```python
result = imagen(
    prompt="very complex multi-subject scene",
    mode="max",
    wall_clock_timeout=300,  # 5 minutes; default is 240s
)
```

### Output manifest

Every successful generation writes a line to `<output_dir>/manifest.jsonl`:

```python
import json
from pathlib import Path

manifest = Path("./out/manifest.jsonl").read_text()
for line in manifest.strip().splitlines():
    entry = json.loads(line)
    print(entry["path"], entry["mode"], entry["batch_mode"])
```

---

## Related

- [Batch modes](modes.md) — mode-by-mode guide with examples
- [Reasoning modes](reasoning.md) — `mode` parameter in depth
- [Transparency](transparency.md) — all chroma parameters explained
- [Skills](skills.md) — `skills` parameter
- [CLI reference](cli.md) — same options as CLI flags
````

**Step 2: Verify**

```bash
wc -l docs/sdk.md
```
Expected: ~200–240 lines.

**Step 3: Commit**

```bash
git add docs/sdk.md
git commit -m "docs: add sdk.md — Python SDK reference"
```

---

## Task 9: Write docs/comparison.md

**Files:**
- Create: `docs/comparison.md`
- Source: README.md "Why use this over the native image_gen tool?" + imagen.md "When to prefer this" sections

**Step 1: Write the file**

Content to write to `docs/comparison.md`:

````markdown
# Why use this vs. native image_gen?

Both `codex-imagen` (this tool) and the native `imagegen` tool in Codex/Claude
Code use the same underlying `gpt-image-2` model via the same Codex OAuth bridge.
The difference is orchestration, post-processing, and workflow integration.

---

## Side-by-side

| Capability | codex-imagen | native image_gen |
|---|---|---|
| Multi-image in one call | Yes — parallel, chain, branded-parallel | No — one image per call |
| Brand consistency across images | Yes — anchor-driven branded-parallel | No — each call is independent |
| Transparency quality | Green chroma-key + dominance despill | Simple chroma-key or none |
| Skill loader | Yes — pass any `.md` skill file | No |
| Structured prompts | Yes — Codex labeled-spec 14-field dict | Free text only |
| Output manifest | Yes — JSONL per image | No |
| Predictable file paths | Yes — always writes to `output_dir` | Model-decided |
| Speed for a single image | ~Same | ~Same |
| Setup required | Yes (`imagen setup`) | None — built-in |

---

## When to prefer codex-imagen

**More than one image is needed:**
Use `batch_mode="parallel"` or `"branded-parallel"`. One call generates N images
with a single overhead — much faster than N separate calls with the native tool.

**Brand consistency across images:**
`branded-parallel` with an `anchor` locks palette, typography, and lighting
across all images in the batch. N separate calls with the native tool produce N
unrelated images regardless of how similar your prompts are.

**Transparency:**
Green chroma-key default + dominance despill produces 54× cleaner edges on
fur/hair subjects compared to naive magenta-key. The native tool's chroma
handling (if any) is simpler and less configurable.

**Skill-driven workflows:**
The same `.md` skill files you use in Claude Code / Codex can be passed to
`imagen` directly. The native tool has no skill loader.

**Structured Codex labeled-spec prompts:**
Pass a dict (`Subject`, `Style`, `Lighting`, `Text (verbatim)`, etc.) for
predictable, structured output. The native tool accepts free text only.

**Predictable file paths + manifest:**
Every generation writes to your `output_dir` with a `manifest.jsonl` for
auditing, automation, and downstream processing.

---

## When native image_gen is fine

- **One-off simple images** where speed and simplicity matter more than
  batching, consistency, or transparency quality
- **Zero-setup contexts** where you can't or don't want to run `imagen setup`
- **Agent sessions that already have imagegen loaded** and you only need a single
  quick image

The native tool is perfectly adequate for quick exploratory generation. Use
codex-imagen when the output is going into a real project.

---

## Coexistence

The native Codex skill is named `imagegen` (installed at
`~/.codex/skills/.system/imagegen/`). This tool's skill is named `imagen`
(no "g"). They are separate, do not conflict, and Codex can see both.

When `imagen setup --client codex` runs, it writes a preference snippet to
`~/.codex/AGENTS.md` asking the Codex AI to prefer `imagen` for multi-image,
transparency, and skill-driven work. The native `imagegen` remains available
for one-off calls.

You can always check which is active:
```bash
imagen status    # shows codex-imagen install state
```

---

## Related

- [Batch modes](modes.md) — parallel, variants, chain, branded-parallel
- [Transparency](transparency.md) — chroma-key pipeline in detail
- [Skills](skills.md) — skill file loader
- [MCP server](mcp.md) — setup and client configuration
````

**Step 2: Verify**

```bash
wc -l docs/comparison.md
```
Expected: ~100–130 lines.

**Step 3: Commit**

```bash
git add docs/comparison.md
git commit -m "docs: add comparison.md — vs. native image_gen, coexistence"
```

---

## Task 10: Write docs/releasing.md (copy from RELEASING.md)

**Files:**
- Create: `docs/releasing.md`
- Source: `RELEASING.md` at repo root (copy verbatim + add intro line)
- `RELEASING.md` at root: leave it in place (do NOT delete)

**Step 1: Write the file**

Content to write to `docs/releasing.md`:

````markdown
# Releasing

This document describes how to cut a new release of codex-imagen.

The canonical copy is in `docs/releasing.md`; `RELEASING.md` at the repo root
also exists for projects that expect it at the top level.

---

## One-time setup

1. Create a PyPI account at https://pypi.org and verify your email.
2. Generate an API token at https://pypi.org/manage/account/token/ — for the
   very first release, scope it to **Entire account** (the project `codex-imagen`
   doesn't exist on PyPI yet, so project-scoped tokens aren't available). After
   v0.1.0 ships you can replace it with a project-scoped token.
3. Add the token to GitHub: **Settings → Secrets and variables → Actions → New
   repository secret**
   - Name: `PYPI_API_TOKEN`
   - Value: `pypi-...` (the full token string from step 2)

## Each release

1. Bump `version` in `pyproject.toml` (e.g. `0.1.0` → `0.1.1`).
2. Update `__version__` in `src/codex_imagen/__init__.py` to match.
3. Update CHANGELOG if you keep one.
4. Commit and push to main:
   ```bash
   git add pyproject.toml src/codex_imagen/__init__.py
   git commit -m "Bump version to 0.1.1"
   git push origin main
   ```
5. Tag and push the tag:
   ```bash
   git tag v0.1.1
   git push --tags
   ```
6. GitHub Actions picks up the `v*.*.*` tag, builds the wheel + sdist, runs
   `twine check`, and uploads to PyPI automatically. Watch progress at
   https://github.com/VelmoAI/codex-imagen/actions.

## First release ever

After the first successful publish:
- Optionally re-scope the PyPI token to project `codex-imagen` only (more
  secure than an account-wide token).
- Verify the package page looks right: https://pypi.org/project/codex-imagen/
- Test a clean install: `pip install codex-imagen` in a fresh venv.
````

**Step 2: Verify RELEASING.md still exists at root**

```bash
ls RELEASING.md
```
Expected: the file exists.

**Step 3: Commit**

```bash
git add docs/releasing.md
git commit -m "docs: add docs/releasing.md (copy of root RELEASING.md)"
```

---

## Task 11: Rewrite README.md

**Files:**
- Modify: `README.md`

This is the only file that gets deleted/replaced. It must be ~120 lines.

**Step 1: Write the new README.md**

Full content:

````markdown
# codex-imagen

Production image-gen toolkit on top of the Codex OAuth bridge — uses your
ChatGPT subscription, no `OPENAI_API_KEY` needed.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-359%20passing-brightgreen)
![Powered by](https://img.shields.io/badge/powered%20by-Codex%20OAuth-black)

Three interfaces over one engine: Python SDK, `imagen` CLI, MCP server.

---

## Highlights

- **5 batch modes** — single / parallel / variants / chain / branded-parallel (best for coherent website sections)
- **4 reasoning modes** — raw (~20 s) → max (~3 min), auto-selected based on prompt shape
- **Better transparency** — green chroma-key + dominance despill, 54× cleaner on fur vs. magenta defaults
- **Bundled AI skill** — auto-installs into Claude Code / Codex / Claude Desktop on `imagen setup`
- **Skill loader** — pass any `.md` skill file, body merges into the prompt builder
- **Codex labeled-spec prompts** — 14-field structured prompts for predictable output
- **MCP server** — drop into 5 AI clients with one command

---

## Install

**One-liner (recommended):**

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/VelmoAI/codex-imagen/main/install.sh | sh

# Windows PowerShell
iwr https://raw.githubusercontent.com/VelmoAI/codex-imagen/main/install.ps1 | iex
```

This installs [uv](https://github.com/astral-sh/uv) (if missing) and runs
`imagen setup` — registers the MCP server and the bundled skill in your AI clients.

**Already have uv:** `uvx codex-imagen setup`

**pip:** `pip install codex-imagen && imagen setup`

**From GitHub (latest main):**
`pip install git+https://github.com/VelmoAI/codex-imagen.git && imagen setup`

---

## 60-second quick start

```python
from codex_imagen import imagen

# One image
r = imagen(prompt="a brass diya oil lamp, editorial product shot")
print(r.images[0].path)

# 6 brand-consistent website sections in one call
r = imagen(
    prompt=["hero", "about", "features", "gallery", "pricing", "footer"],
    anchor="modern Indian restaurant, warm terracotta and brass palette, editorial photography",
    batch_mode="branded-parallel",
    parallel=3,
    output_dir="./out/restaurant-site",
)
```

CLI:

```bash
imagen "a brass diya oil lamp"
imagen "a brass diya oil lamp" --transparent       # RGBA PNG
imagen setup                                        # install MCP + skill
imagen status                                       # install state per client
```

---

## Docs

- [Batch modes](docs/modes.md) — single, parallel, variants, chain, branded-parallel
- [Reasoning modes](docs/reasoning.md) — raw, medium, high, max — with timing
- [Transparency](docs/transparency.md) — chroma-key pipeline, despill, complex subjects
- [Skills](docs/skills.md) — bundled skill + external skill loader
- [MCP server](docs/mcp.md) — per-client setup (Claude Code, Desktop, Codex, Cursor)
- [CLI reference](docs/cli.md) — every subcommand and flag
- [SDK reference](docs/sdk.md) — Python API, ImagenOptions, ImagenResult
- [Why use this vs. native image\_gen?](docs/comparison.md)
- [Releasing](docs/releasing.md)

---

## License

MIT, Copyright (c) 2026 wauwa. See [LICENSE](LICENSE) for the full text.
````

**Step 2: Count lines**

```bash
wc -l README.md
```
Expected: ≤ 120 lines.

**Step 3: Run tests to confirm nothing broke**

```bash
python -m pytest tests/ -q
```
Expected: `359 passed`

**Step 4: Commit**

```bash
git add README.md
git commit -m "docs: condense README to scannable gateway + link to docs/"
```

---

## Task 12: Verify all cross-links resolve

**Step 1: Check that every linked file exists**

```bash
ls docs/modes.md docs/reasoning.md docs/transparency.md docs/skills.md \
   docs/mcp.md docs/cli.md docs/sdk.md docs/comparison.md docs/releasing.md
```
Expected: all 9 files listed without error.

**Step 2: Check that RELEASING.md still exists at root**

```bash
ls RELEASING.md
```

**Step 3: Verify README.md links use correct relative paths**

Open README.md and confirm every `docs/` link matches an existing file.

**Step 4: Check back-links in each docs/ file**

Each file has a "Related" section linking back to other docs/ files. Spot-check
that the relative paths (`reasoning.md`, `sdk.md`, etc.) are correct — they
should be bare filenames (same directory).

---

## Task 13: Final test run and push

**Step 1: Run full test suite**

```bash
python -m pytest tests/ -v 2>&1 | tail -20
```
Expected: `359 passed`

**Step 2: Create the final summary commit**

```bash
git add -A  # catch anything not yet staged
git status  # verify nothing unexpected
```

Then create the combined commit:

```bash
git commit -m "$(cat <<'EOF'
Split README into focused docs

- README.md condensed to scannable intro: highlights, install, 60s
  quick start, doc index
- docs/modes.md — 5 batch modes
- docs/reasoning.md — 4 reasoning modes with timing
- docs/transparency.md — chroma pipeline, despill, complex subjects
- docs/skills.md — bundled skill + external skill loader
- docs/mcp.md — per-client install paths and config
- docs/cli.md — every subcommand
- docs/sdk.md — Python API reference
- docs/comparison.md — vs. native image_gen, coexistence notes
- docs/releasing.md — moved/duplicated from RELEASING.md

No code changes. All cross-links verified.
EOF
)"
```

**Step 3: Push**

```bash
git push origin main
```

**Step 4: Verify push succeeded**

```bash
git log --oneline -5
```

---

## Acceptance criteria

- [ ] README.md is ≤ 120 lines
- [ ] All 9 `docs/*.md` files exist
- [ ] `RELEASING.md` still exists at repo root
- [ ] Every link in README.md resolves to an existing file
- [ ] Every "Related" section link within docs/ resolves
- [ ] `python -m pytest tests/ -q` → 359 passed
- [ ] `git push origin main` succeeded
- [ ] No code files were modified (only `.md` files changed)

---

## File sizes (targets)

| File | Target lines |
|---|---|
| `README.md` | ≤ 120 |
| `docs/modes.md` | 130–160 |
| `docs/reasoning.md` | 100–130 |
| `docs/transparency.md` | 130–160 |
| `docs/skills.md` | 120–150 |
| `docs/mcp.md` | 160–200 |
| `docs/cli.md` | 180–220 |
| `docs/sdk.md` | 200–240 |
| `docs/comparison.md` | 100–130 |
| `docs/releasing.md` | 50–70 |
