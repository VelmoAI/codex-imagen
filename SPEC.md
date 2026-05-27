# codex-imagen — Implementation Spec

This is the authoritative spec for the `codex_imagen` package. Every implementer
subagent gets the relevant excerpt of this file as context.

---

## Purpose

A maximally-powerful, agent-friendly toolkit on top of the **Codex OAuth image
generation bridge** (`gpt-image-2` via `https://chatgpt.com/backend-api/codex/responses`).
Uses the user's ChatGPT subscription quota — no `OPENAI_API_KEY` required.

Three interfaces, one engine:
1. **Python SDK** — `from codex_imagen import imagen; imagen(...)`
2. **CLI** — `imagen ...` (works for both humans and AI agents)
3. **MCP server** — single `imagen` tool, stdio

---

## Architecture Facts (verified by live probes)

The Codex OAuth bridge has these proven constraints. **Do not waste cycles
trying to work around them — they are server-side filtered.**

| Behavior | Status |
|---|---|
| Endpoint | only `POST /responses` reachable via OAuth token |
| Mainline model | must be set (e.g. `gpt-5.5`). Image tool runs nested. |
| `tool_choice: image_generation` + minimal instructions | makes mainline a near-passthrough |
| `n > 1` (multiple images per call) | rejected with HTTP 400 — *not available* |
| `background: "transparent"` | rejected with HTTP 400 — *not available* |
| `quality` field | silently dropped — server normalizes to `auto` |
| `model` inside tool dict | silently ignored — always routes to `gpt-image-2` |
| `moderation: low` / `auto` | accepted |
| `output_format: png | webp | jpeg` | accepted (validated) |
| `size` | accepted with constraints: both axes multiple of 16, max 3840px, ratio ≤ 3:1 |
| `partial_images: int` | accepted (streams intermediate frames) |
| `output_compression: int` | accepted only for jpeg/webp |
| `input_image_mask` | accepted (alpha-channel mask for edits) |
| `reasoning.effort` on mainline | accepted — controls how much gpt-5.5 polishes prompt before tool call |

**Real levers we have:**
- structured `instructions=` (Codex labeled spec → see Prompt Builder section)
- per-call `reasoning_effort` on mainline (none/minimal/low/medium/high/xhigh)
- multi-image via PARALLEL calls (one image per call)
- transparency via post-process **chroma key with Pillow** (magenta `#FF00FF`)
- reference images, masks, image-tool params above

---

## Package Layout

```
codex-imagen/
├── pyproject.toml           # hatchling build, deps: codex-image-gen, Pillow, click, mcp
├── README.md
├── SPEC.md                  # this file
├── src/codex_imagen/
│   ├── __init__.py          # public SDK exports: imagen, ImagenOptions, ImagenResult, ...
│   ├── _bridge.py           # thin wrapper around codex_image_gen.generate_image()
│   ├── _prompts.py          # Codex labeled-spec builder, raw-passthrough, instructions merge
│   ├── _size.py             # size validator + nearest-legal suggestion
│   ├── _chroma.py           # Pillow chroma-key pipeline (magenta → alpha PNG, with despill)
│   ├── _skills.py           # file-path skill loader (markdown bodies)
│   ├── _modes.py            # single | parallel | variants | chain | branded-parallel
│   ├── _manifest.py         # JSONL run logger
│   ├── core.py              # imagen() public entry point + ImagenOptions/Result dataclasses
│   ├── cli.py               # click CLI, dual-mode: human (pretty) / agent (--json)
│   └── mcp_server.py        # stdio MCP server, single `imagen` tool
├── tests/
│   ├── conftest.py
│   ├── test_prompts.py
│   ├── test_size.py
│   ├── test_chroma.py
│   ├── test_skills.py
│   ├── test_modes.py
│   ├── test_core.py
│   ├── test_cli.py
│   └── test_manifest.py
├── examples/
│   ├── single.md
│   ├── variants.md
│   ├── chain.md
│   ├── branded_parallel.md
│   └── transparent_logo.md
└── upstream/                # read-only reference clone of codex-image-gen
```

**Code style:**
- Every module starts with a docstring explaining purpose, key concepts, and how it fits in.
- Every public function/class has a docstring with Args/Returns/Raises.
- Inline comments where the *why* is non-obvious (especially around bridge quirks).
- Type hints everywhere, `from __future__ import annotations` for forward refs.
- No emoji in code/output unless user explicitly asks.
- Comments in German are FINE since this is a German-user project — pick whichever
  language makes the comment clearest. Default English for API-facing strings.

---

## Public API (Python SDK)

```python
from codex_imagen import imagen, ImagenOptions

result = imagen(
    prompt="a minimalist hero image of a ceramic coffee mug",
    output_dir="./out",
    # one-liner using all defaults — produces ./out/00.png
)

# Power use
result = imagen(
    prompt=["section 1: hero", "section 2: features", "section 3: cta"],
    batch_mode="branded-parallel",
    anchor="warm beige editorial design, soft serif headings, lots of whitespace",
    transparent=False,
    skills=["~/.claude/skills/brandkit/SKILL.md"],
    mode="medium",
    size="1536x1024",
    output_format="png",
    output_dir="./website/sections",
    parallel=3,
)
```

### `ImagenOptions` dataclass (frozen)

```python
@dataclass(frozen=True)
class ImagenOptions:
    # --- CORE INPUT ---
    prompt: str | list[str] | dict | list[dict]   # str/dict OR list for multi-image
    output_dir: str | Path = "./out"
    
    # --- MODES ---
    mode: str = "auto"                  # auto | raw | medium | high | max
                                        # controls reasoning_effort on mainline
    batch_mode: str = "auto"            # auto | single | parallel | variants | chain | branded-parallel
    chain_mode: str = "anchor+previous" # previous | anchor | anchor+previous | window:N | all
    anchor: str | dict | None = None    # optional explicit anchor prompt for branded-parallel
    count: int = 1                      # number of variants (variants mode)
    
    # --- CONTENT ---
    references: list[str] = ()          # file paths / URLs to reference images
    skills: list[str] = ()              # file paths to skill .md files
    mask: str | None = None             # file path to alpha mask (for edits)
    extra_instructions: str | None = None
    
    # --- IMAGE-TOOL PARAMS ---
    size: str = "auto"                  # "auto" or "WIDTHxHEIGHT" — validated
    output_format: str = "png"          # png | jpeg | webp
    
    # --- TRANSPARENCY (chroma-key only — bridge doesn't allow native) ---
    transparent: bool = False
    chroma_key: str = "#FF00FF"
    chroma_tolerance: int = 40          # 0-100
    chroma_despill: bool = True
    
    # --- ENHANCEMENT ---
    enhance_prompt: bool = False        # deterministic local enhancer (no extra LLM call)
    vars: dict[str, str] = ()           # {key} → value substitution in prompts
    
    # --- ORCHESTRATION ---
    parallel: int = 2                   # max concurrent calls
    
    # --- ADVANCED (escape hatch — override anything) ---
    advanced: dict = ()                 # passes through unchanged to codex_image_gen
                                        # supported keys: reasoning_effort, reasoning_summary,
                                        #   text_verbosity, moderation, partial_images,
                                        #   output_compression, timeout, instructions,
                                        #   model, background, auth_file, oauth_base_url
```

### `ImagenResult` dataclass

```python
@dataclass(frozen=True)
class ImagenImage:
    index: int
    path: Path
    bytes: int
    mime_type: str
    response_id: str | None
    call_id: str | None
    revised_prompt: str | None
    original_prompt: str
    final_prompt: str               # what we actually sent (after vars/enhance/labeled-spec)
    transparent: bool               # true if chroma pipeline applied
    references_used: list[str]
    raw_png_path: Path | None       # if transparent: original opaque PNG before chroma

@dataclass(frozen=True)
class ImagenHealth:
    ok: bool
    codex_image_gen_available: bool
    pillow_available: bool
    auth_file_exists: bool
    auth_file_path: str | None
    hint: str | None                # actionable message if not ok

@dataclass(frozen=True)
class ImagenResult:
    ok: bool                        # false if health failed before generation
    mode: str
    batch_mode: str
    images: list[ImagenImage]       # empty if ok=False
    manifest_path: Path | None
    elapsed_ms: int
    health: ImagenHealth            # ALWAYS populated
    error: str | None               # if ok=False
    warnings: list[str]             # non-fatal (e.g. "skills ignored in raw mode")
```

### `imagen()` entry point

```python
def imagen(**kwargs) -> ImagenResult:
    """Single entry point for all generation modes.
    
    See ImagenOptions for all kwargs.
    
    Returns ImagenResult with ok flag. On health failure returns ok=False
    with an actionable hint rather than raising.
    """
    options = ImagenOptions(**kwargs)
    return _run(options)
```

---

## Prompt Builder (`_prompts.py`)

The Codex labeled spec (verified from `openai/codex` repo `imagegen/SKILL.md`):

```
Use case: <taxonomy slug>
Asset type: <where the asset will be used>
Primary request: <user's main prompt>
Input images: <Image 1: role; Image 2: role> (optional)
Scene/backdrop: <environment>
Subject: <main subject>
Style/medium: <photo/illustration/3D/etc>
Composition/framing: <wide/close/top-down; placement>
Lighting/mood: <lighting + mood>
Color palette: <palette notes>
Materials/textures: <surface details>
Text (verbatim): "<exact text>"
Constraints: <must keep/must avoid>
Avoid: <negative constraints>
```

### Mode → `instructions=` built by imagen

**`raw` mode** — anti-refinement passthrough:
```
You are a passthrough proxy. The user message contains the EXACT prompt for the
image_generation tool. Forward it verbatim. Do not restructure, add scene/lighting
details, expand short prompts, normalize wording, or inject style guidance.
Call image_generation once with the prompt exactly as given.
```

**`medium` / `high` / `max` mode** — full Codex labeled spec:
```
[Codex official labeled spec scaffolding ...]
{augmentation rules from SKILL.md}

ADDITIONAL CONTEXT FROM LINKED SKILLS:
{skill bodies concatenated}

{chroma-key block if transparent=True}

{batch-mode hint if branded-parallel / chain — "match the visual style of the reference"}
```

### Prompt input forms

`prompt` accepts:
- **str** — plain text, used as `Primary request:` field
- **dict** — structured form with any of the 14 labeled-spec keys
- **list[str | dict]** — multi-image for batch/chain/branded-parallel modes

When `prompt` is a dict, builder emits all populated labeled-spec lines in canonical order.

### Verbatim text enforcer

If a `Text (verbatim)` field is present, auto-append:
> *Render the quoted text verbatim with no extra glyphs, no duplicate text, and no extra punctuation.*

Also auto-set `mode=high` if user is in `auto`/`medium` and verbatim text is present.

---

## Size Validator (`_size.py`)

gpt-image-2 size rules (verified):
- Both width and height must be multiples of 16
- Max edge ≤ 3840px
- Aspect ratio ≤ 3:1
- Total pixels: 655,360 – 8,294,400 (= 800×800 to 3840×2160 approx)
- Above 2560×1440 = experimental (emit warning)

```python
def validate_size(size: str) -> tuple[bool, str | None, str]:
    """Return (is_valid, nearest_legal_or_None, message).
    
    "auto" is always valid (passes through).
    """

def nearest_legal(size: str) -> str:
    """Return the closest legal size that satisfies all 4 rules.
    Round each axis to nearest multiple of 16, clamp to [16, 3840],
    enforce aspect ratio, fit in pixel budget."""
```

---

## Chroma Key Pipeline (`_chroma.py`)

Pillow-only. No external deps.

```python
def keyout(
    src_path: Path,
    dst_path: Path,
    *,
    key_rgb: tuple[int, int, int] = (255, 0, 255),
    tolerance: int = 40,         # 0-100, perceptual
    despill: bool = True,
    feather_px: int = 2,
) -> None:
    """Apply chroma-key to src image, save RGBA PNG to dst.
    
    Algorithm:
      1. Load RGB image, convert to RGBA.
      2. For each pixel compute Euclidean distance to key in RGB space.
      3. Map distance → alpha via smooth ramp (tolerance defines transition width).
      4. If despill: subtract key-color contribution from edge pixels' RGB channels
         to remove chroma fringing (e.g. magenta halo around subject).
      5. Feather alpha edges via small Gaussian blur (feather_px) for clean cutout.
    """
```

The imagen orchestrator, when `transparent=True`:
1. Prepend chroma-key instruction to `instructions=` (so the model renders on solid magenta)
2. Generate normally with `background="opaque"` (forced)
3. Save raw PNG as `<name>.raw.png` for debugging
4. Apply `keyout()` → `<name>.png`

---

## Skills Loader (`_skills.py`)

Skills are markdown files. Optionally YAML frontmatter. The body is what we inject.

```python
def load_skills(paths: list[str]) -> tuple[str, str]:
    """Load skill bodies from file paths.
    
    Returns (concatenated_body, sha256_hash_of_invariants).
    Hash is for drift detection in branded-parallel.
    
    Resolves ~ (home), strips YAML frontmatter, separates skills with '---'.
    Raises FileNotFoundError for missing paths (with hint).
    """
```

Used by `_prompts.py` to merge skill content into `instructions=`.

---

## Modes (`_modes.py`)

Five batch modes. Each takes `ImagenOptions` and a callable bridge function.

| Mode | Behavior |
|---|---|
| `single` | one prompt → one image |
| `parallel` | N prompts → N independent images, ThreadPoolExecutor(max_workers=parallel) |
| `variants` | 1 prompt + count=N → N parallel calls of SAME prompt. imagen adds subtle variation hints to each (alt angle, lighting, composition) so they actually differ |
| `chain` | N prompts → sequential. Each call's references include prior images per chain_mode |
| `branded-parallel` | 1 anchor (prompt 0 or explicit `anchor`) generated first → other N-1 prompts in parallel, each with anchor image as reference |

Auto-detection of `batch_mode="auto"`:
- string prompt → `single`
- list of prompts, len == count, count > 1 → `variants` if all same → else `parallel`
- list of prompts, `anchor` set → `branded-parallel`
- list of prompts, `chain_mode` set explicitly → `chain`
- list of prompts, default → `parallel`

### Variation hints (for `variants` mode)

imagen appends a different hint to each variant's instructions:
- variant 1: "interpret freely — establish the baseline"
- variant 2: "vary the composition or angle"
- variant 3: "vary the lighting or mood"
- variant 4: "vary the color palette or materials"
- variant 5+: rotate through above

Subtle, doesn't change the subject, but produces actually-different variants.

---

## Manifest (`_manifest.py`)

JSONL log per run at `<output_dir>/manifest.jsonl`. One line per generated image.

```jsonc
{
  "ts": "2026-05-27T14:32:10Z",
  "run_id": "r_abcd1234",
  "mode": "medium",
  "batch_mode": "branded-parallel",
  "index": 2,
  "prompt_original": "section 2: features",
  "prompt_final": "Use case: website-section\nAsset type: features section\n...",
  "skills_used": ["./brandkit/SKILL.md"],
  "skills_hash": "sha256:...",
  "size": "1536x1024",
  "output_format": "png",
  "transparent": false,
  "references": ["./out/00_anchor.png"],
  "response_id": "resp_...",
  "call_id": "ig_...",
  "revised_prompt": "...",
  "path": "./out/02.png",
  "bytes": 712340,
  "elapsed_ms": 21400,
  "warnings": []
}
```

---

## CLI (`cli.py`)

Single command `imagen`. Two output modes:
- **Default (human)** — pretty colored output, progress bars, tables
- **`--json` (agent)** — structured JSON to stdout, no decoration

```bash
# Human
imagen "a coffee mug, minimal hero" --size 1536x1024 --quality high

# Agent
imagen "a coffee mug" --json

# Multi-prompt from file (one per line)
imagen -f prompts.txt --batch-mode branded-parallel --anchor "warm beige editorial"

# Variants
imagen "abstract geometric logo" --count 4 --batch-mode variants

# Chain
imagen -f frames.txt --batch-mode chain --chain-mode anchor+previous

# Transparent
imagen "a single red apple, isolated" --transparent --skill ~/.claude/skills/brandkit/SKILL.md

# Show health
imagen --health

# Variables in prompts
imagen -f frames.txt --batch-mode chain --var char="red-haired woman"
```

Flags map 1:1 to ImagenOptions fields.

### Dual-mode UX (2026 style)

- TTY detection: `sys.stdout.isatty()` → human mode by default
- Non-TTY (pipe / file) → auto-switch to JSON mode (machine-friendly)
- Explicit `--json` or `--pretty` overrides detection
- `--quiet` suppresses progress, prints only final result
- Errors always go to stderr (so stdout stays clean for pipes)
- Exit codes: 0 ok, 1 health failure, 2 generation failure, 3 invalid args

---

## MCP Server (`mcp_server.py`)

Stdio. One tool: `imagen`. Full ImagenOptions surface as JSON schema.

The tool description embedded in the schema teaches the AI how to choose modes:

```
imagen — Codex image generator. Generate, batch, chain, or branded sets of images.

PROMPT: string for single image, dict for structured Codex labeled-spec,
        or array of either for multiple images.

MODE SELECTION GUIDE (set batch_mode explicitly or let auto pick):
  • single             — one prompt → one image
  • parallel           — N unrelated prompts → N independent images (fast)
  • variants           — 1 prompt + count=N → N stylistic variations
  • chain              — N prompts → sequential narrative (each refs prior)
  • branded-parallel   — N prompts + anchor → all match anchor's style (best for website sections, brand sets, hero+features)

REASONING MODE (controls how aggressively gpt-5.5 polishes prompt):
  • raw      — pass prompt 1:1, no skills allowed
  • medium   — default when skills present, integrates them
  • high     — premium planning for complex briefs
  • max      — maximum reasoning for very complex multi-subject scenes

TRANSPARENCY: set transparent=true to get PNG with alpha via chroma-key pipeline.
              (Native transparent background is not available via Codex OAuth.)

SKILLS: pass file paths to .md skill files. imagen reads them and merges into
        instructions. Works with Claude/Codex skill files directly.

Returns ImagenResult JSON with ok flag, images list, manifest path, health.
On failure: ok=false + hint instead of crashing.
```

---

## Health-Check (inline)

Run at start of every `imagen()` call:
- Check `codex_image_gen` importable
- Check Pillow importable (only if transparent or chroma needed — soft-fail)
- Check `~/.codex/auth.json` exists and parses as JSON with token fields
- If any hard-fail → return `ImagenResult(ok=False, health=..., hint="codex login required")`
  WITHOUT making any API call.

---

## Testing Strategy

- `tests/conftest.py` provides a `fake_bridge` fixture that monkeypatches
  `codex_image_gen.generate_image` to return deterministic fake images.
- Each module gets unit tests against the fake bridge — NO real API calls in tests.
- Real API smoke test is manual, not in pytest suite.
- Pillow operations tested against synthetic input images.
- CLI tests via Click's `CliRunner`.
- MCP server tested by invoking handlers directly (no actual stdio).

---

## Out of scope (do not build, even if tempting)

- Web UI / Dashboard (planning only, not in this build)
- Iterative refine loops (vision-eval driven)
- Upscale, outpaint
- Cost estimation
- Resume-from-crash
- Anything that requires extra API calls beyond what codex-image-gen does
- Forking codex-image-gen (we use it as-is — its parameter coverage matches what the bridge accepts)

---

## Implementation Order

1. `pyproject.toml` + package skeleton + `__init__.py`
2. `_bridge.py` — thin wrapper around codex_image_gen.generate_image
3. `_prompts.py` — Codex labeled-spec builder + raw + verbatim enforcer
4. `_size.py` — validator + nearest-legal
5. `_chroma.py` — Pillow pipeline
6. `_skills.py` — file loader + hash
7. `_manifest.py` — JSONL logger
8. `_modes.py` — 5 modes
9. `core.py` — ImagenOptions / ImagenResult / imagen() entry + health
10. `cli.py` — Click CLI with dual-mode output
11. `mcp_server.py` — stdio MCP
12. Tests + Examples
13. README

Each gets: implementer subagent (Opus) → spec review → code-quality review.
