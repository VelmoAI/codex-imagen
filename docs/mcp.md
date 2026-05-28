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
imagen setup                              # interactive walkthrough
imagen setup --all                        # install for all detected clients
imagen setup --client cursor              # target a specific client
imagen setup --dry-run --all              # preview what would happen
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
imagen uninstall --all            # remove from everywhere
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
