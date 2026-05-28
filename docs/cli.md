# CLI reference

`imagen` is the command-line interface for codex-imagen. It has dual output
modes (human-friendly TTY / JSON for scripting), auto-detected from whether
stdout is a terminal.

---

## Subcommands

### `imagen setup`

Register codex-imagen MCP in one or more AI clients.

```bash
imagen setup                                 # interactive walkthrough
imagen setup --all                           # install for all detected clients
imagen setup --client claude-code            # install for a specific client
imagen setup --client codex --client cursor  # multiple specific clients
imagen setup --dry-run --all                 # preview changes, no writes
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
