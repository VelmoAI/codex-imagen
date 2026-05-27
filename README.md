# codex-imagen

A maximally-powerful, agent-friendly toolkit for image generation via the Codex OAuth bridge — no `OPENAI_API_KEY` required.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-325%20passing-brightgreen)
![Powered by](https://img.shields.io/badge/powered%20by-Codex%20OAuth-black)

`codex-imagen` is a single engine wrapped in three interfaces — a Python SDK, a dual-mode CLI, and a stdio MCP server. It speaks five batch modes (single / parallel / variants / chain / branded-parallel), builds Codex-grade labeled-spec prompts, post-processes transparency through a chroma-key pipeline, and accepts Claude/Codex skill files as drop-in style packs. Authentication piggybacks on the local ChatGPT login at `~/.codex/auth.json`, so you generate images on your subscription quota — no separate API key, no per-image billing surprises.

---

## Why this exists

ChatGPT subscribers already have image-generation quota that the Codex CLI uses through OAuth. That bridge — `gpt-image-2` exposed by `POST https://chatgpt.com/backend-api/codex/responses` — is real, reachable, and stable, but it has a narrower parameter surface than the paid OpenAI Images API. `codex-imagen` is the carefully-scoped wrapper that exposes everything the bridge *actually* supports (size, format, references, masks, reasoning effort, partial frames) and refuses to fake the features the bridge refuses (native transparent backgrounds, `n>1`, quality control). The verified constraints live in the [Verified bridge constraints](#verified-bridge-constraints) table below.

---

## Quick install

```bash
pip install codex-imagen        # PyPI publish pending — for now: pip install -e . from clone
imagen setup                    # interactive: pick which clients to register the MCP in
```

`imagen setup` detects and installs the `codex-imagen` MCP server for any of:
**Claude Code**, **Claude Desktop**, **Codex CLI**, **Cursor**, and **OpenCode**.
Use `imagen status` to see what's installed, and `imagen uninstall` to remove it.

```bash
imagen status                   # show all 5 clients and their install state
imagen setup --all              # install for all detected clients (no prompts)
imagen setup --client cursor    # target a specific client
imagen uninstall --all          # remove everywhere
imagen setup --dry-run --all    # preview what would happen, no changes
```

### Bundled AI skill

`imagen setup` also installs a self-describing skill file alongside the MCP config.
After setup, your AI agent automatically knows how to call `imagen` — modes, batch
strategies, transparency, brand sets — without you explaining it.

| Client | Skill location |
|---|---|
| Claude Code | `~/.claude/skills/imagen/SKILL.md` |
| Claude Desktop | `~/.claude/skills/imagen/SKILL.md` |
| Codex | `~/.codex/skills/imagen/SKILL.md` |
| Cursor / OpenCode | skill concept not yet supported |

The skill (`src/codex_imagen/_assets/skills/imagen.md`) ships inside the wheel so
it is always in sync with the installed version. `imagen uninstall` removes it;
`imagen status` shows whether it is present per client.

---

## Install

```bash
# From PyPI (after publish)
pip install codex-imagen

# From source
git clone https://github.com/VelmoAI/codex-imagen
cd codex-imagen
pip install -e .
```

**Prerequisite:** a working Codex OAuth login at `~/.codex/auth.json`. If you have never logged in, run the Codex CLI's login flow once — `codex-imagen` only ever reads that file, it never writes it.

```bash
# Sanity-check the auth, Pillow, and bridge installation:
imagen --health
```

---

## Quick start

Three super-short examples, one per interface, all of them produce `./out/00.png`.

### SDK

```python
from codex_imagen import imagen

result = imagen(prompt="a ceramic coffee mug, minimal hero shot")
print(result.images[0].path)
```

### CLI

```bash
imagen "a ceramic coffee mug, minimal hero shot"
```

### MCP (Claude Desktop / any MCP-aware agent)

```jsonc
// claude_desktop_config.json
{
  "mcpServers": {
    "codex-imagen": {
      "command": "codex-imagen-mcp"
    }
  }
}
```

Then in Claude: *"Use codex-imagen to generate a hero image of a ceramic coffee mug."*

---

## Three interfaces, one engine

| Interface | Surface | Best for |
|---|---|---|
| **SDK** (`from codex_imagen import imagen`) | `imagen(**ImagenOptions)` → `ImagenResult` | Python scripts, notebooks, server code, custom pipelines |
| **CLI** (`imagen ...`) | 25 flags, auto JSON-vs-pretty by TTY | Humans on the terminal, shell scripts, CI jobs, AI agents calling `subprocess` |
| **MCP** (`codex-imagen-mcp`) | One `imagen` tool over stdio | Claude Desktop, Cursor, any MCP-aware agent |

All three share the same dataclass — `ImagenOptions` — so a workflow built against one interface ports cleanly to the others.

---

## The five batch modes

A `batch_mode` decides how prompts and outputs are arranged. `auto` is the default and almost always right; the value is recorded in the manifest and on `ImagenResult.batch_mode` for debuggability.

| Mode | Input shape | Output | Use it for |
|---|---|---|---|
| `single` | one prompt | one image | a single hero shot |
| `parallel` | N prompts | N independent images | unrelated assets in one run |
| `variants` | one prompt + `count=N` | N stylistic variations of the same subject | exploring art direction |
| `chain` | N prompts | sequential narrative, each call references the prior | storyboards, before/after panels |
| `branded-parallel` | N prompts + anchor (explicit or `prompt[0]`) | anchor first, then everyone else in parallel referencing it | website hero + feature sections, brand kit batches |

```python
# parallel — three unrelated product shots
imagen(prompt=["a mug", "a notebook", "a pencil"], batch_mode="parallel")

# variants — four takes on the same logo idea
imagen(prompt="abstract geometric logo", count=4, batch_mode="variants")

# chain — a four-panel storyboard
imagen(prompt=["frame 1: dawn", "frame 2: noon", "frame 3: dusk", "frame 4: night"],
      batch_mode="chain")

# branded-parallel — hero + sections all matching the anchor's style
imagen(prompt=["hero", "feature 1", "feature 2", "feature 3"],
      anchor="warm beige editorial, soft serif headings",
      batch_mode="branded-parallel")
```

For longer worked examples see [`examples/single.md`](./examples/single.md), [`examples/parallel.md`](./examples/parallel.md), [`examples/variants.md`](./examples/variants.md), [`examples/chain.md`](./examples/chain.md), and [`examples/branded_parallel.md`](./examples/branded_parallel.md).

---

## Reasoning modes

`mode` controls how aggressively the mainline model (`gpt-5.5`, currently) polishes your prompt before invoking the image tool. Internally this maps to the bridge's `reasoning_effort`.

| Mode | Effort | Use when |
|---|---|---|
| `raw` | `none` | You want byte-for-byte passthrough — the prompt is the prompt. Skills are ignored in this mode. |
| `medium` | `medium` | Default when skills are loaded; integrates them into the Codex labeled-spec scaffold. |
| `high` | `high` | Premium planning for complex briefs (multi-subject scenes, exact text rendering). |
| `max` | `xhigh` | Maximum reasoning. Slower; reserve for very complex multi-subject layouts. |
| `auto` | resolved | Picks one of the four based on prompt shape, skill presence, transparency, and verbatim-text detection. |

Auto-detection rules (`_prompts.resolve_auto_mode`):

- `prompt` is a Codex labeled-spec dict containing `Text (verbatim)` → `high` (exact-text rendering needs more polish).
- Skills are loaded → `medium`.
- Transparency requested or `extra_instructions` set → `medium`.
- Plain string with no extras → `raw`.

---

## Transparency (chroma-key pipeline)

The Codex OAuth bridge silently rejects `background: "transparent"`. So instead of pretending we support it natively, `codex-imagen` runs a **chroma-key post-process** with Pillow:

1. The bridge generates your subject on solid magenta (`#FF00FF` by default).
2. `_chroma.keyout()` keys the magenta out into alpha, despills the chroma fringe at subject edges, and writes a clean RGBA PNG.
3. The raw opaque image is preserved as `<name>.raw.png` for debugging.

```python
imagen(
    prompt="a single red apple, isolated",
    transparent=True,
    chroma_tolerance=40,    # 0-100; raise if edges look chewed up
    chroma_despill=True,    # neutralizes magenta fringe at hair/edges
)
```

See [`examples/transparent_logo.md`](./examples/transparent_logo.md) for a full walk-through, including how to tune the tolerance when keying logos with fine detail.

---

## Skills (Claude / Codex compatible)

A "skill" here is just a markdown file. Point `imagen` at one (or several) and the body is loaded, YAML frontmatter is stripped, and the content is merged into the prompt builder's `instructions=` block. This lets you reuse the exact same brand-kit / design-system / domain skill files that already live in `~/.claude/skills/<name>/SKILL.md` or `~/.codex/skills/<name>` directories.

```python
imagen(
    prompt="hero shot for a fintech landing page",
    skills=[
        "~/.claude/skills/brandkit/SKILL.md",
        "./my-project/skills/voice.md",
    ],
)
```

Skills are silently ignored in `mode="raw"` (there's no instruction block to inject into — a warning is appended to `ImagenResult.warnings`).

---

## CLI cheatsheet

The CLI has ~25 flags; these are the ones you'll actually use. Run `imagen --help` for the full list.

| Flag | Meaning |
|---|---|
| `PROMPT` (positional) | The prompt for `single` mode. |
| `-f, --file PATH` | Read prompts from a text file, one per line (`#` lines skipped). |
| `--prompt-json PATH` | Read a structured Codex labeled-spec dict or list from JSON. |
| `-m, --mode {auto,raw,medium,high,max}` | Reasoning mode. |
| `-b, --batch-mode {auto,single,parallel,variants,chain,branded-parallel}` | Orchestration strategy. |
| `-a, --anchor TEXT` | Explicit anchor for `branded-parallel`. |
| `-n, --count N` | Variant count. |
| `-s, --skill PATH` | Skill file (repeatable). |
| `-r, --reference PATH` | Reference image (repeatable). |
| `--size 1536x1024` | gpt-image-2 size (both axes multiples of 16). |
| `--transparent` | Run the chroma-key pipeline. |
| `--var key=value` | Variable substitution into prompts (repeatable). |
| `-o, --output-dir DIR` | Where images and `manifest.jsonl` are written. |
| `--json` / `--pretty` | Force output mode (auto-detected from TTY otherwise). |
| `--health` | Run the pre-flight check and exit. |

Exit codes: `0` ok · `1` health failure · `2` generation failure · `3` invalid args.

```bash
# Human-friendly (TTY-detected)
imagen "a coffee mug, minimal hero" --size 1536x1024

# JSON-piped (e.g. into jq, or driven by a script)
imagen "a coffee mug" --json | jq '.images[0].path'

# Multi-prompt branded set
imagen -f sections.txt --batch-mode branded-parallel \
      --anchor "warm beige editorial design, soft serif headings, lots of whitespace"

# Storyboard
imagen -f frames.txt --batch-mode chain --chain-mode anchor+previous

# Transparent logo with a custom skill
imagen "an abstract leaf logo" --transparent \
      --skill ~/.claude/skills/brandkit/SKILL.md
```

---

## MCP integration

A single tool — `imagen` — is exposed over stdio. Its JSON schema mirrors `ImagenOptions` field-for-field (a drift-pin test ensures it stays in sync). The tool description embedded in the schema teaches the model how to pick a `batch_mode`, when to use each reasoning mode, and how transparency / skills behave. The authoritative version of that guide lives at the top of [`src/codex_imagen/mcp_server.py`](./src/codex_imagen/mcp_server.py).

```jsonc
// claude_desktop_config.json
{
  "mcpServers": {
    "codex-imagen": {
      "command": "codex-imagen-mcp"
    }
  }
}
```

The tool result is a JSON payload of `ImagenResult` — the same shape `imagen()` returns to the SDK, with `Path` values flattened to absolute strings. On failure (bad args, health failure, every call failed) the payload is still a normal MCP result with `{"ok": false, "error": "...", "error_type": "..."}` — never a protocol-level exception. Calling models can react to that shape and retry with different args.

---

## Configuration reference

[`SPEC.md`](./SPEC.md) is the authoritative reference for every field on `ImagenOptions`, the prompt-builder grammar, the size-validator rules, the chroma-key algorithm, and the manifest format. The README is the friendly intro; the SPEC is the contract.

---

## Architecture

The package is intentionally small and layered. Each leaf module has one job:

| Module | Responsibility |
|---|---|
| `_bridge.py` | Thin wrapper around `codex_image_gen.generate_image`, plus the pre-flight health check. |
| `_prompts.py` | Codex labeled-spec builder, raw passthrough, verbatim-text enforcement, auto-mode resolver. |
| `_size.py` | gpt-image-2 size validator and nearest-legal suggestion. |
| `_chroma.py` | Pillow chroma-key pipeline with despill and edge feathering. |
| `_skills.py` | File-path skill loader (handles `~`, strips YAML frontmatter, hashes invariants). |
| `_modes.py` | Five batch modes — plans calls, executes via `ThreadPoolExecutor`, owns the chain reference logic. |
| `_manifest.py` | One JSONL line per generated image, written to `<output_dir>/manifest.jsonl`. |
| `core.py` | Public `ImagenOptions` / `ImagenResult` / `imagen()` — orchestrates the modules above. |
| `cli.py` | Click CLI with dual human / JSON output and TTY auto-detect. |
| `mcp_server.py` | Stdio MCP server exposing a single `imagen` tool. |

`core.py` is the only module that knows about the full pipeline; everything else is replaceable in isolation. Tests reach into the leaf modules directly and use a `fake_bridge` fixture to keep the suite hermetic.

---

## Verified bridge constraints

These are real, observed limits of the Codex OAuth bridge. They were verified by live probes (see `probe_*.py` at the repo root). **Do not waste cycles trying to work around them — they are server-side filtered.**

| Behavior | Status |
|---|---|
| Endpoint | only `POST /responses` reachable via OAuth token |
| Mainline model | must be set (e.g. `gpt-5.5`); the image tool runs nested |
| `tool_choice: image_generation` + minimal instructions | makes mainline a near-passthrough |
| `n > 1` (multiple images per call) | rejected with HTTP 400 — *not available* |
| `background: "transparent"` | rejected with HTTP 400 — *not available* |
| `quality` field | silently dropped — server normalizes to `auto` |
| `model` inside tool dict | silently ignored — always routes to `gpt-image-2` |
| `moderation: low` / `auto` | accepted |
| `output_format: png \| webp \| jpeg` | accepted (validated) |
| `size` | accepted with constraints: both axes multiples of 16, max 3840px, ratio ≤ 3:1 |
| `partial_images: int` | accepted (streams intermediate frames) |
| `output_compression: int` | accepted only for jpeg/webp |
| `input_image_mask` | accepted (alpha-channel mask for edits) |
| `reasoning.effort` on mainline | accepted — controls how much `gpt-5.5` polishes the prompt before the tool call |

**Real levers we have:** structured Codex labeled-spec `instructions=`, per-call `reasoning_effort`, multi-image via parallel calls, transparency via chroma-key, reference images, masks.

---

## Testing

```bash
pip install -e .[dev]
python -m pytest tests/ -v
```

**325 tests, no real API calls.** The suite runs against a `fake_bridge` fixture in `tests/conftest.py` that returns deterministic synthetic images, so it is fast (sub-second), offline, and free.

---

## Manual smoke test

The pytest suite never touches the real bridge. To sanity-check against your actual Codex OAuth login:

```bash
imagen --health
imagen "a tiny placeholder test image" --size 1024x1024 --output-dir ./smoke-test
ls ./smoke-test
```

You should see `./smoke-test/00.png` and `./smoke-test/manifest.jsonl`.

---

## Contributing

PRs welcome. Please run `python -m pytest tests/ -v` before submitting — all 325 tests should still pass. Bug reports with minimal reproductions are especially helpful; see [`SPEC.md`](./SPEC.md) for the design constraints any change should respect.

---

## Credits

- Built on [`codex-image-gen`](https://github.com/smturtle2/codex-image-gen) by **Small Turtle 2** (MIT) — the underlying OAuth bridge client.
- The Codex labeled-spec prompt grammar follows the schema published in `openai/codex` (`imagegen/SKILL.md`).

Made with [Claude Code](https://claude.com/claude-code).

---

## License

MIT, Copyright (c) 2026 wauwa. See [`LICENSE`](./LICENSE) for the full text — it also acknowledges the upstream `codex-image-gen` MIT license.
