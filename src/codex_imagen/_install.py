"""codex_imagen._install — Self-contained MCP installer for codex-imagen.

Supports registering (and unregistering) the ``codex-imagen-mcp`` console
script as an MCP server in five clients:

* Claude Code   — ``~/.claude.json``
* Claude Desktop — platform-appropriate JSON config
* Codex CLI     — ``~/.codex/config.toml``
* Cursor        — ``~/.cursor/mcp.json``
* OpenCode      — ``~/.config/opencode/config.json`` or ``~/.opencode/config.json``

Design constraints
------------------
* Read-only detection (``detect_clients``) never modifies anything.
* All write operations are defensive: disk-full / permission errors are caught
  and returned as ``(False, message)`` rather than raised.
* Every install operation is idempotent — calling it twice reports
  "already installed" rather than duplicating the entry.
* JSON files are written with ``indent=2`` and a trailing newline.
* TOML for Codex is handled via ``tomli_w`` (write) + stdlib ``tomllib``
  (read, Python 3.11+).  We do NOT shell out to ``codex`` CLI.
* The AGENTS.md preference block is delimited by HTML-comment markers so it
  can be detected and removed precisely.
"""

from __future__ import annotations

import importlib.resources as _ir
import json
import os
import platform
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# tomli_w is a lightweight third-party package for TOML writing.
# Python's stdlib tomllib (3.11+) is read-only.
try:
    import tomli_w  # type: ignore[import-untyped]
    _TOMLI_W_AVAILABLE = True
except ImportError:
    _TOMLI_W_AVAILABLE = False

# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClientStatus:
    """Status of one supported MCP client."""

    name: str             # Human-readable: "Claude Code"
    key: str              # Slug: "claude-code"
    detected: bool        # Whether the client config/dir was found
    installed: bool       # Whether codex-imagen MCP entry exists
    config_path: Path | None  # File we would modify (or None if undetected)
    notes: str            # Short human-readable hint


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_MCP_SERVER_KEY = "codex-imagen"
_MCP_SERVER_COMMAND_DIRECT = "codex-imagen-mcp"


# ---------------------------------------------------------------------------
# Command-resolution helper
# ---------------------------------------------------------------------------

def _find_command(cmd: str) -> str | None:
    """Return the full path to ``cmd`` if it is on PATH, else ``None``.

    Uses :func:`shutil.which` so it respects PATH, file-extension rules on
    Windows, and executable permissions on POSIX.  Tests can monkeypatch
    :func:`shutil.which` to control the result.
    """
    return shutil.which(cmd)


def _mcp_server_entry() -> dict[str, object]:
    """Return the best MCP server config dict for this environment.

    Priority:
    1. ``uvx`` on PATH  → ``{"command": "uvx", "args": ["codex-imagen-mcp"]}``
    2. ``codex-imagen-mcp`` on PATH → ``{"command": "codex-imagen-mcp", "args": []}``
    3. neither found    → uvx form with a best-effort warning printed to stderr

    The uvx form is preferred because it works for both ``uvx``-installed and
    pip-installed packages (uvx re-uses the already-cached environment), and it
    does not require ``codex-imagen-mcp`` to be on PATH in a pip-installed
    setup that lacks the scripts directory in PATH.
    """
    if _find_command("uvx") is not None:
        return {"command": "uvx", "args": [_MCP_SERVER_COMMAND_DIRECT]}
    if _find_command(_MCP_SERVER_COMMAND_DIRECT) is not None:
        return {"command": _MCP_SERVER_COMMAND_DIRECT, "args": []}
    # Neither found — emit a warning and default to uvx (the modern path).
    print(
        "Warning: neither 'uvx' nor 'codex-imagen-mcp' was found on PATH. "
        "The MCP config will use 'uvx codex-imagen-mcp'; install uv "
        "(https://github.com/astral-sh/uv) to ensure it works.",
        file=sys.stderr,
    )
    return {"command": "uvx", "args": [_MCP_SERVER_COMMAND_DIRECT]}

_AGENTS_BLOCK_BEGIN = "<!-- BEGIN codex-imagen-preference -->"
_AGENTS_BLOCK_END = "<!-- END codex-imagen-preference -->"
_AGENTS_BLOCK_CONTENT = """\
<!-- BEGIN codex-imagen-preference -->
## Image Generation

When generating images, ALWAYS use the `imagen` MCP tool from the `codex-imagen` server.

Why: codex-imagen is purpose-built for Codex OAuth quota and offers 5 batch modes (single,
parallel, variants, chain, branded-parallel), 4 reasoning modes, structured Codex labeled-spec
prompts, skill-file injection, and Pillow chroma-key transparency. It supersedes the built-in
imagegen skill for all image generation needs.

If `imagen` is unavailable, fall back to the built-in image generation.
<!-- END codex-imagen-preference -->"""


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------

def _home() -> Path:
    """Return the user's home directory, respecting USERPROFILE on Windows."""
    # On Windows pathlib.Path.home() already uses USERPROFILE, but being
    # explicit here makes it easy for tests to monkeypatch.
    home_env = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if home_env:
        return Path(home_env)
    return Path.home()


def _claude_desktop_config_path() -> Path:
    """Return the platform-appropriate Claude Desktop config file path."""
    system = platform.system()
    if system == "Windows":
        app_data = os.environ.get("APPDATA") or str(_home() / "AppData" / "Roaming")
        return Path(app_data) / "Claude" / "claude_desktop_config.json"
    elif system == "Darwin":
        return _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        # Linux / other POSIX
        xdg_config = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
        return Path(xdg_config) / "Claude" / "claude_desktop_config.json"


def _opencode_config_path() -> Path | None:
    """Return the OpenCode config path that actually exists, or the primary one."""
    candidates = [
        _home() / ".config" / "opencode" / "config.json",
        _home() / ".opencode" / "config.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    # Return the primary location even if it doesn't exist yet; install will
    # create it. We return None if neither directory exists (undetected).
    if (candidates[0].parent.exists() or candidates[1].parent.exists()):
        # Return whichever parent dir exists, prefer first.
        for p in candidates:
            if p.parent.exists():
                return p
    return None


# ---------------------------------------------------------------------------
# JSON config helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON config file; return empty dict if missing or unreadable."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text) if text.strip() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> tuple[bool, str]:
    """Write ``data`` as indented JSON to ``path``. Returns (success, msg)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return True, f"Written: {path}"
    except OSError as exc:
        return False, f"Could not write {path}: {exc}"


def _is_mcp_installed_json(data: dict[str, Any]) -> bool:
    """Return True if the codex-imagen MCP entry exists in a JSON config."""
    servers = data.get("mcpServers", {})
    return isinstance(servers, dict) and _MCP_SERVER_KEY in servers


def _inject_mcp_json(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` with the codex-imagen MCP entry merged in."""
    result = dict(data)
    servers: dict[str, Any] = dict(result.get("mcpServers") or {})
    servers[_MCP_SERVER_KEY] = _mcp_server_entry()
    result["mcpServers"] = servers
    return result


def _remove_mcp_json(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` with the codex-imagen MCP entry removed."""
    result = dict(data)
    servers: dict[str, Any] = dict(result.get("mcpServers") or {})
    servers.pop(_MCP_SERVER_KEY, None)
    result["mcpServers"] = servers
    return result


# ---------------------------------------------------------------------------
# TOML helpers (Codex CLI)
# ---------------------------------------------------------------------------

def _read_toml(path: Path) -> dict[str, Any]:
    """Read a TOML config file; return empty dict if missing or unreadable."""
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _write_toml(path: Path, data: dict[str, Any]) -> tuple[bool, str]:
    """Write ``data`` as TOML to ``path``. Returns (success, msg)."""
    if not _TOMLI_W_AVAILABLE:
        return False, (
            "tomli_w is required for TOML writing but is not installed. "
            "Run: pip install tomli-w"
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(tomli_w.dumps(data).encode("utf-8"))
        return True, f"Written: {path}"
    except OSError as exc:
        return False, f"Could not write {path}: {exc}"


def _is_mcp_installed_toml(data: dict[str, Any]) -> bool:
    """Return True if the codex-imagen entry exists in the Codex TOML config."""
    servers = data.get("mcp_servers", {})
    return isinstance(servers, dict) and _MCP_SERVER_KEY in servers


def _inject_mcp_toml(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` with the codex-imagen MCP entry merged in."""
    result = dict(data)
    servers: dict[str, Any] = dict(result.get("mcp_servers") or {})
    servers[_MCP_SERVER_KEY] = _mcp_server_entry()
    result["mcp_servers"] = servers
    return result


def _remove_mcp_toml(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` with the codex-imagen MCP entry removed."""
    result = dict(data)
    servers: dict[str, Any] = dict(result.get("mcp_servers") or {})
    servers.pop(_MCP_SERVER_KEY, None)
    result["mcp_servers"] = servers
    return result


# ---------------------------------------------------------------------------
# Per-client detection helpers
# ---------------------------------------------------------------------------

def _detect_claude_code() -> ClientStatus:
    config_path = _home() / ".claude.json"
    detected = config_path.exists()
    if detected:
        data = _read_json(config_path)
        installed = _is_mcp_installed_json(data)
        notes = "already installed" if installed else "config found"
    else:
        installed = False
        notes = "config not found"
    return ClientStatus(
        name="Claude Code",
        key="claude-code",
        detected=detected,
        installed=installed,
        config_path=config_path,
        notes=notes,
    )


def _detect_claude_desktop() -> ClientStatus:
    config_path = _claude_desktop_config_path()
    detected = config_path.parent.exists()
    if detected and config_path.exists():
        data = _read_json(config_path)
        installed = _is_mcp_installed_json(data)
        notes = "already installed" if installed else "config found"
    elif detected:
        installed = False
        notes = "config dir found; config file missing (will be created)"
    else:
        installed = False
        notes = "config dir not found"
    return ClientStatus(
        name="Claude Desktop",
        key="claude-desktop",
        detected=detected,
        installed=installed,
        config_path=config_path if detected else None,
        notes=notes,
    )


def _detect_codex() -> ClientStatus:
    config_path = _home() / ".codex" / "config.toml"
    codex_dir_exists = config_path.parent.exists()

    # Also check if `codex` binary is on PATH as a secondary detection signal.
    import shutil
    codex_on_path = shutil.which("codex") is not None

    detected = codex_dir_exists or codex_on_path

    if config_path.exists():
        data = _read_toml(config_path)
        installed = _is_mcp_installed_toml(data)
        notes = "already installed" if installed else "config found"
    elif detected:
        installed = False
        notes = "codex detected (no config.toml yet; will be created)"
    else:
        installed = False
        notes = "not found"

    return ClientStatus(
        name="Codex",
        key="codex",
        detected=detected,
        installed=installed,
        config_path=config_path if detected else None,
        notes=notes,
    )


def _detect_cursor() -> ClientStatus:
    cursor_dir = _home() / ".cursor"
    config_path = cursor_dir / "mcp.json"
    detected = cursor_dir.exists()
    if detected and config_path.exists():
        data = _read_json(config_path)
        installed = _is_mcp_installed_json(data)
        notes = "already installed" if installed else "config found"
    elif detected:
        installed = False
        notes = "cursor dir found; mcp.json missing (will be created)"
    else:
        installed = False
        notes = "not found"
    return ClientStatus(
        name="Cursor",
        key="cursor",
        detected=detected,
        installed=installed,
        config_path=config_path if detected else None,
        notes=notes,
    )


def _detect_opencode() -> ClientStatus:
    config_path = _opencode_config_path()
    if config_path is not None:
        detected = True
        if config_path.exists():
            data = _read_json(config_path)
            installed = _is_mcp_installed_json(data)
            notes = "already installed" if installed else "config found"
        else:
            installed = False
            notes = "opencode dir found; config.json missing (will be created)"
    else:
        detected = False
        installed = False
        notes = "not found"

    # Assumption: OpenCode uses the same ``mcpServers`` top-level key as
    # Claude Code/Desktop. This is the most common MCP client convention and
    # matches the OpenCode docs as of 2025. If the format differs, this is
    # the single place to update.
    return ClientStatus(
        name="OpenCode",
        key="opencode",
        detected=detected,
        installed=installed,
        config_path=config_path,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_DETECTORS = {
    "claude-code": _detect_claude_code,
    "claude-desktop": _detect_claude_desktop,
    "codex": _detect_codex,
    "cursor": _detect_cursor,
    "opencode": _detect_opencode,
}

_CLIENT_ORDER = ["claude-code", "claude-desktop", "codex", "cursor", "opencode"]


def detect_clients() -> list[ClientStatus]:
    """Detect all supported clients. Read-only — never modifies anything."""
    return [_DETECTORS[key]() for key in _CLIENT_ORDER]


def install_for_client(key: str, *, dry_run: bool = False) -> tuple[bool, str]:
    """Register codex-imagen MCP for one client.

    Returns ``(success, message)``. Idempotent — safe to call twice.
    """
    if key not in _DETECTORS:
        return False, f"Unknown client key: {key!r}"

    status = _DETECTORS[key]()

    if status.installed:
        return True, f"{status.name}: already installed (no changes made)"

    if status.config_path is None:
        return False, (
            f"{status.name}: client not detected — cannot determine config "
            "path. Install the client first."
        )

    if dry_run:
        return True, f"[dry-run] {status.name}: would write to {status.config_path}"

    if key == "codex":
        return _install_codex(status.config_path)
    else:
        return _install_json_client(key, status)


def _install_json_client(key: str, status: ClientStatus) -> tuple[bool, str]:
    """Install (JSON-config) for a non-Codex client."""
    config_path = status.config_path
    assert config_path is not None
    data = _read_json(config_path)
    new_data = _inject_mcp_json(data)
    ok, msg = _write_json(config_path, new_data)
    if ok:
        return True, f"{status.name}: installed at {config_path}"
    return False, f"{status.name}: {msg}"


def _install_codex(config_path: Path) -> tuple[bool, str]:
    """Install into ~/.codex/config.toml."""
    data = _read_toml(config_path)
    new_data = _inject_mcp_toml(data)
    ok, msg = _write_toml(config_path, new_data)
    if ok:
        return True, f"Codex: installed at {config_path}"
    return False, f"Codex: {msg}"


def uninstall_for_client(key: str, *, dry_run: bool = False) -> tuple[bool, str]:
    """Remove codex-imagen MCP from one client.

    Returns ``(success, message)``. Idempotent — safe if not installed.
    """
    if key not in _DETECTORS:
        return False, f"Unknown client key: {key!r}"

    status = _DETECTORS[key]()

    if not status.installed:
        return True, f"{status.name}: not installed (nothing to remove)"

    if status.config_path is None:
        return True, f"{status.name}: not installed (nothing to remove)"

    if dry_run:
        return True, f"[dry-run] {status.name}: would remove entry from {status.config_path}"

    if key == "codex":
        return _uninstall_codex(status.config_path)
    else:
        return _uninstall_json_client(key, status)


def _uninstall_json_client(key: str, status: ClientStatus) -> tuple[bool, str]:
    """Uninstall (JSON-config) for a non-Codex client."""
    config_path = status.config_path
    assert config_path is not None
    data = _read_json(config_path)
    new_data = _remove_mcp_json(data)
    ok, msg = _write_json(config_path, new_data)
    if ok:
        return True, f"{status.name}: removed from {config_path}"
    return False, f"{status.name}: {msg}"


def _uninstall_codex(config_path: Path) -> tuple[bool, str]:
    """Uninstall from ~/.codex/config.toml."""
    data = _read_toml(config_path)
    new_data = _remove_mcp_toml(data)
    ok, msg = _write_toml(config_path, new_data)
    if ok:
        return True, f"Codex: removed from {config_path}"
    return False, f"Codex: {msg}"


# ---------------------------------------------------------------------------
# AGENTS.md preference snippet
# ---------------------------------------------------------------------------

def _agents_md_path() -> Path:
    return _home() / ".codex" / "AGENTS.md"


def write_codex_preference_snippet(dry_run: bool = False) -> tuple[bool, str]:
    """Append the codex-imagen preference block to ~/.codex/AGENTS.md.

    Idempotent — checks for the BEGIN marker before appending.
    """
    path = _agents_md_path()

    # Read existing content (if any).
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            return False, f"Could not read {path}: {exc}"

    # Idempotency: if the block is already there, skip.
    if _AGENTS_BLOCK_BEGIN in existing:
        return True, f"Preference snippet already present in {path}"

    if dry_run:
        return True, f"[dry-run] Would append preference snippet to {path}"

    # Append with a blank separator line.
    separator = "\n\n" if existing.strip() else ""
    new_content = existing + separator + _AGENTS_BLOCK_CONTENT + "\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_content, encoding="utf-8")
        return True, f"Preference snippet written to {path}"
    except OSError as exc:
        return False, f"Could not write {path}: {exc}"


def remove_codex_preference_snippet(dry_run: bool = False) -> tuple[bool, str]:
    """Remove the codex-imagen preference block from ~/.codex/AGENTS.md.

    Only removes the block delimited by our own BEGIN/END markers.
    Does not touch any content outside those markers.
    """
    path = _agents_md_path()

    if not path.exists():
        return True, f"No AGENTS.md found at {path} (nothing to remove)"

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"Could not read {path}: {exc}"

    if _AGENTS_BLOCK_BEGIN not in text:
        return True, f"Preference snippet not present in {path} (nothing to remove)"

    if dry_run:
        return True, f"[dry-run] Would remove preference snippet from {path}"

    # Pattern matches from BEGIN through END (inclusive), with optional
    # leading blank line.
    pattern = re.compile(
        r"\n?\n?" + re.escape(_AGENTS_BLOCK_BEGIN) + r".*?" + re.escape(_AGENTS_BLOCK_END) + r"\n?",
        re.DOTALL,
    )
    new_text = pattern.sub("", text)

    try:
        path.write_text(new_text, encoding="utf-8")
        return True, f"Preference snippet removed from {path}"
    except OSError as exc:
        return False, f"Could not write {path}: {exc}"


# ---------------------------------------------------------------------------
# Bundled skill install / uninstall / status
# ---------------------------------------------------------------------------
#
# Claude Code skill layout: ~/.claude/skills/<name>/SKILL.md
# Codex skill layout:       ~/.codex/skills/<name>/SKILL.md  (if dir exists)
# Claude Desktop:           shares ~/.claude/skills/ with Claude Code
# Cursor / OpenCode:        no skill concept yet — skip with a note
#
# ---------------------------------------------------------------------------

_SKILL_NAME = "imagen"
_SKILL_FILENAME = "SKILL.md"


def _skill_path_for_client(key: str) -> Path | None:
    """Return the target skill file path for ``key``, or None if unsupported."""
    if key in ("claude-code", "claude-desktop"):
        return _home() / ".claude" / "skills" / _SKILL_NAME / _SKILL_FILENAME
    if key == "codex":
        return _home() / ".codex" / "skills" / _SKILL_NAME / _SKILL_FILENAME
    # cursor / opencode: no skill directory concept yet
    return None


def _load_bundled_skill() -> str:
    """Read the bundled imagen.md from the installed package.

    Uses ``importlib.resources`` so it works whether the package is installed
    from a wheel or in editable mode from ``src/``.
    """
    ref = _ir.files("codex_imagen._assets.skills").joinpath("imagen.md")
    return ref.read_text(encoding="utf-8")


def install_skill_for_client(
    key: str, *, dry_run: bool = False
) -> tuple[bool, str]:
    """Install the bundled imagen skill for one client.

    Returns ``(success, message)``. Idempotent — overwrites and reports
    "updated" when the file already exists.
    """
    if key not in _DETECTORS:
        return False, f"Unknown client key: {key!r}"

    skill_path = _skill_path_for_client(key)
    if skill_path is None:
        return True, f"{key}: skill install not supported (no skill directory concept)"

    if dry_run:
        return True, f"[dry-run] {key}: would write skill to {skill_path}"

    existed = skill_path.exists()

    try:
        content = _load_bundled_skill()
    except Exception as exc:
        return False, f"{key}: could not load bundled skill: {exc}"

    try:
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return False, f"{key}: could not write skill to {skill_path}: {exc}"

    if existed:
        return True, f"{key}: skill updated at {skill_path}"
    return True, f"{key}: skill installed at {skill_path}"


def uninstall_skill_for_client(
    key: str, *, dry_run: bool = False
) -> tuple[bool, str]:
    """Remove the bundled imagen skill for one client.

    Returns ``(success, message)``. Idempotent — safe if file absent.
    """
    if key not in _DETECTORS:
        return False, f"Unknown client key: {key!r}"

    skill_path = _skill_path_for_client(key)
    if skill_path is None:
        return True, f"{key}: skill uninstall not applicable"

    if not skill_path.exists():
        return True, f"{key}: skill not installed (nothing to remove)"

    if dry_run:
        return True, f"[dry-run] {key}: would remove skill at {skill_path}"

    try:
        skill_path.unlink()
        # Remove the parent dir only if it's now empty.
        try:
            skill_path.parent.rmdir()
        except OSError:
            pass  # Not empty — leave it.
        return True, f"{key}: skill removed from {skill_path}"
    except OSError as exc:
        return False, f"{key}: could not remove skill at {skill_path}: {exc}"


def skill_status_for_client(key: str) -> tuple[bool | None, Path | None]:
    """Return ``(installed, path)`` for the skill of one client.

    ``installed`` is ``True`` if the file exists, ``False`` if the path is
    defined but missing, ``None`` if the client has no skill directory.
    """
    if key not in _DETECTORS:
        return None, None
    skill_path = _skill_path_for_client(key)
    if skill_path is None:
        return None, None
    return skill_path.exists(), skill_path
