"""Tests for codex_imagen._install — the multi-client MCP installer.

Strategy
--------
All tests use ``tmp_path`` and monkeypatch environment variables / module
globals to redirect config paths to a temporary directory so no real user
config files are touched.

We monkeypatch ``codex_imagen._install._home`` (and ``os.environ`` where
needed) to return ``tmp_path``, which gives us a fully isolated
filesystem sandbox per test.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

import codex_imagen._install as _install_mod
from codex_imagen._install import (
    ClientStatus,
    detect_clients,
    install_for_client,
    remove_codex_preference_snippet,
    uninstall_for_client,
    write_codex_preference_snippet,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make _install._home() return tmp_path, isolating all config access."""
    monkeypatch.setattr(_install_mod, "_home", lambda: tmp_path)
    # Also redirect APPDATA / XDG_CONFIG_HOME so platform helpers pick up tmp.
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. detect_clients — returns exactly 5 entries
# ---------------------------------------------------------------------------


def test_detect_clients_returns_five_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """detect_clients always returns exactly 5 ClientStatus objects."""
    _patch_home(monkeypatch, tmp_path)
    result = detect_clients()
    assert len(result) == 5
    keys = [s.key for s in result]
    assert keys == ["claude-code", "claude-desktop", "codex", "cursor", "opencode"]


def test_detect_clients_not_detected_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When no client configs exist, all are 'not detected'."""
    _patch_home(monkeypatch, tmp_path)
    # Also suppress PATH lookup for `codex` binary.
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = detect_clients()
    for s in result:
        assert s.detected is False, f"{s.key} should not be detected"
        assert s.installed is False


def test_detect_clients_claude_code_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Claude Code is detected when ~/.claude.json exists."""
    _patch_home(monkeypatch, tmp_path)
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")
    result = {s.key: s for s in detect_clients()}
    assert result["claude-code"].detected is True
    assert result["claude-code"].installed is False


def test_detect_clients_claude_code_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Claude Code shows installed=True when the MCP entry already exists."""
    _patch_home(monkeypatch, tmp_path)
    config = {"mcpServers": {"codex-imagen": {"command": "codex-imagen-mcp"}}}
    (tmp_path / ".claude.json").write_text(json.dumps(config), encoding="utf-8")
    result = {s.key: s for s in detect_clients()}
    assert result["claude-code"].installed is True


def test_detect_clients_codex_detected_via_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Codex is detected when ~/.codex/ directory exists."""
    _patch_home(monkeypatch, tmp_path)
    (tmp_path / ".codex").mkdir()
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = {s.key: s for s in detect_clients()}
    assert result["codex"].detected is True


def test_detect_clients_codex_detected_via_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Codex is detected when ``codex`` binary is on PATH even without dir."""
    _patch_home(monkeypatch, tmp_path)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/codex" if name == "codex" else None)
    result = {s.key: s for s in detect_clients()}
    assert result["codex"].detected is True


# ---------------------------------------------------------------------------
# 2. install_for_client — creates config when missing
# ---------------------------------------------------------------------------


def test_install_creates_missing_claude_code_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_for_client('claude-code') creates ~/.claude.json if absent."""
    _patch_home(monkeypatch, tmp_path)
    # Make the client 'detected' by creating the file.
    config_path = tmp_path / ".claude.json"
    config_path.write_text("{}", encoding="utf-8")

    ok, msg = install_for_client("claude-code")
    assert ok is True
    data = _read_json(config_path)
    assert data["mcpServers"]["codex-imagen"]["command"] == "codex-imagen-mcp"


def test_install_merges_existing_servers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_for_client preserves existing mcpServers entries."""
    _patch_home(monkeypatch, tmp_path)
    existing = {"mcpServers": {"other-server": {"command": "other-mcp"}}}
    config_path = tmp_path / ".claude.json"
    config_path.write_text(json.dumps(existing), encoding="utf-8")

    ok, msg = install_for_client("claude-code")
    assert ok is True
    data = _read_json(config_path)
    # Both servers must be present.
    assert "other-server" in data["mcpServers"]
    assert "codex-imagen" in data["mcpServers"]


def test_install_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Calling install twice returns 'already installed' on the second call."""
    _patch_home(monkeypatch, tmp_path)
    config_path = tmp_path / ".claude.json"
    config_path.write_text("{}", encoding="utf-8")

    ok1, _ = install_for_client("claude-code")
    ok2, msg2 = install_for_client("claude-code")
    assert ok1 is True
    assert ok2 is True
    assert "already installed" in msg2

    # File should still be valid and contain exactly one entry.
    data = _read_json(config_path)
    assert len(data["mcpServers"]) == 1


def test_install_cursor_creates_mcp_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_for_client('cursor') creates ~/.cursor/mcp.json."""
    _patch_home(monkeypatch, tmp_path)
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()

    ok, msg = install_for_client("cursor")
    assert ok is True
    config_path = cursor_dir / "mcp.json"
    assert config_path.exists()
    data = _read_json(config_path)
    assert data["mcpServers"]["codex-imagen"]["command"] == "codex-imagen-mcp"


def test_install_unknown_client_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_for_client with an unknown key returns (False, msg)."""
    _patch_home(monkeypatch, tmp_path)
    ok, msg = install_for_client("nonexistent-client")
    assert ok is False
    assert "Unknown client key" in msg


def test_install_cursor_undetected_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_for_client returns (False, msg) when a client with no known path
    is not detected (e.g. Cursor dir does not exist)."""
    _patch_home(monkeypatch, tmp_path)
    # Don't create .cursor dir — cursor will be undetected AND config_path=None.
    ok, msg = install_for_client("cursor")
    assert ok is False
    assert "not detected" in msg.lower() or "cannot determine" in msg.lower()


# ---------------------------------------------------------------------------
# 3. uninstall_for_client — removes entry, leaves others alone
# ---------------------------------------------------------------------------


def test_uninstall_removes_mcp_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uninstall_for_client removes the codex-imagen entry."""
    _patch_home(monkeypatch, tmp_path)
    config = {
        "mcpServers": {
            "codex-imagen": {"command": "codex-imagen-mcp"},
            "other-server": {"command": "other-mcp"},
        }
    }
    config_path = tmp_path / ".claude.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    ok, msg = uninstall_for_client("claude-code")
    assert ok is True
    data = _read_json(config_path)
    assert "codex-imagen" not in data["mcpServers"]
    # Other server must survive.
    assert "other-server" in data["mcpServers"]


def test_uninstall_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uninstall_for_client returns success even when MCP is not installed."""
    _patch_home(monkeypatch, tmp_path)
    config_path = tmp_path / ".claude.json"
    config_path.write_text("{}", encoding="utf-8")

    ok, msg = uninstall_for_client("claude-code")
    assert ok is True
    assert "not installed" in msg


def test_uninstall_unknown_client_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uninstall_for_client with unknown key returns (False, msg)."""
    _patch_home(monkeypatch, tmp_path)
    ok, msg = uninstall_for_client("bogus-client")
    assert ok is False
    assert "Unknown" in msg


# ---------------------------------------------------------------------------
# 4. Codex TOML — read/write/merge
# ---------------------------------------------------------------------------


def test_install_codex_creates_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_for_client('codex') creates ~/.codex/config.toml."""
    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    ok, msg = install_for_client("codex")
    assert ok is True
    config_path = codex_dir / "config.toml"
    assert config_path.exists()
    data = _read_toml(config_path)
    assert data["mcp_servers"]["codex-imagen"]["command"] == "codex-imagen-mcp"


def test_install_codex_preserves_other_toml_tables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Installing codex MCP preserves unrelated TOML tables and keys."""
    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    # Pre-existing config with unrelated content.
    existing_toml = (
        '[model]\nname = "gpt-5.5"\n\n'
        '[mcp_servers.existing-tool]\ncommand = "existing-mcp"\n'
    )
    (codex_dir / "config.toml").write_text(existing_toml, encoding="utf-8")

    ok, _ = install_for_client("codex")
    assert ok is True
    data = _read_toml(codex_dir / "config.toml")
    assert data["model"]["name"] == "gpt-5.5"
    assert "existing-tool" in data["mcp_servers"]
    assert "codex-imagen" in data["mcp_servers"]


def test_uninstall_codex_removes_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uninstall_for_client('codex') removes the entry from config.toml."""
    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    # Install first.
    install_for_client("codex")
    data_before = _read_toml(codex_dir / "config.toml")
    assert "codex-imagen" in data_before["mcp_servers"]

    # Uninstall.
    ok, _ = uninstall_for_client("codex")
    assert ok is True
    data_after = _read_toml(codex_dir / "config.toml")
    assert "codex-imagen" not in data_after.get("mcp_servers", {})


# ---------------------------------------------------------------------------
# 5. AGENTS.md preference snippet
# ---------------------------------------------------------------------------


def test_write_agents_snippet_creates_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """write_codex_preference_snippet creates AGENTS.md when absent."""
    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    ok, msg = write_codex_preference_snippet()
    assert ok is True
    agents_path = codex_dir / "AGENTS.md"
    assert agents_path.exists()
    content = agents_path.read_text(encoding="utf-8")
    assert "<!-- BEGIN codex-imagen-preference -->" in content
    assert "<!-- END codex-imagen-preference -->" in content
    assert "ALWAYS use the `imagen` MCP tool" in content


def test_write_agents_snippet_appends_to_existing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """write_codex_preference_snippet appends to existing AGENTS.md content."""
    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    existing = "# My existing instructions\n\nDo great work.\n"
    (codex_dir / "AGENTS.md").write_text(existing, encoding="utf-8")

    ok, _ = write_codex_preference_snippet()
    assert ok is True
    content = (codex_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert "My existing instructions" in content
    assert "<!-- BEGIN codex-imagen-preference -->" in content


def test_write_agents_snippet_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Calling write_codex_preference_snippet twice does not duplicate the block."""
    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()

    write_codex_preference_snippet()
    ok, msg = write_codex_preference_snippet()
    assert ok is True
    assert "already present" in msg

    content = (codex_dir / "AGENTS.md").read_text(encoding="utf-8")
    # Block should appear exactly once.
    assert content.count("<!-- BEGIN codex-imagen-preference -->") == 1


def test_remove_agents_snippet_removes_only_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """remove_codex_preference_snippet removes only the managed block."""
    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    before_content = "# Before\n\nKeep this.\n"
    after_content = "\n# After\n\nKeep this too.\n"
    agents_path = codex_dir / "AGENTS.md"
    agents_path.write_text(
        before_content
        + "\n\n<!-- BEGIN codex-imagen-preference -->\n## Image Generation\nblah\n<!-- END codex-imagen-preference -->\n"
        + after_content,
        encoding="utf-8",
    )

    ok, _ = remove_codex_preference_snippet()
    assert ok is True
    remaining = agents_path.read_text(encoding="utf-8")
    assert "Keep this." in remaining
    assert "Keep this too." in remaining
    assert "<!-- BEGIN codex-imagen-preference -->" not in remaining


def test_remove_agents_snippet_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """remove_codex_preference_snippet is safe when block not present."""
    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "AGENTS.md").write_text("# Some other content\n", encoding="utf-8")

    ok, msg = remove_codex_preference_snippet()
    assert ok is True
    assert "not present" in msg


def test_remove_agents_snippet_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """remove_codex_preference_snippet returns success when AGENTS.md absent."""
    _patch_home(monkeypatch, tmp_path)
    # Don't create .codex dir or AGENTS.md.
    ok, msg = remove_codex_preference_snippet()
    assert ok is True
    assert "No AGENTS.md" in msg


# ---------------------------------------------------------------------------
# 6. Dry-run mode — no filesystem changes
# ---------------------------------------------------------------------------


def test_dry_run_install_makes_no_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_for_client dry_run=True makes zero filesystem changes."""
    _patch_home(monkeypatch, tmp_path)
    config_path = tmp_path / ".claude.json"
    config_path.write_text("{}", encoding="utf-8")
    mtime_before = config_path.stat().st_mtime

    ok, msg = install_for_client("claude-code", dry_run=True)
    assert ok is True
    assert "dry-run" in msg
    # File must be untouched.
    assert config_path.stat().st_mtime == mtime_before
    # No MCP entry was written.
    data = _read_json(config_path)
    assert "mcpServers" not in data


def test_dry_run_uninstall_makes_no_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uninstall_for_client dry_run=True makes zero filesystem changes."""
    _patch_home(monkeypatch, tmp_path)
    config = {"mcpServers": {"codex-imagen": {"command": "codex-imagen-mcp"}}}
    config_path = tmp_path / ".claude.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    mtime_before = config_path.stat().st_mtime

    ok, msg = uninstall_for_client("claude-code", dry_run=True)
    assert ok is True
    assert "dry-run" in msg
    assert config_path.stat().st_mtime == mtime_before
    # Entry must still be present.
    data = _read_json(config_path)
    assert "codex-imagen" in data["mcpServers"]


def test_dry_run_agents_snippet_makes_no_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """write_codex_preference_snippet dry_run=True creates no files."""
    _patch_home(monkeypatch, tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    agents_path = codex_dir / "AGENTS.md"
    assert not agents_path.exists()

    ok, msg = write_codex_preference_snippet(dry_run=True)
    assert ok is True
    assert "dry-run" in msg
    assert not agents_path.exists()
