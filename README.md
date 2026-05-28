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
