"""Tests for bundled-skill install, uninstall, and status in codex_imagen._install.

All tests are isolated via monkeypatching ``_install._home`` to a tmp_path so
no real user config files are touched.
"""

from __future__ import annotations

import importlib.resources as ir
from pathlib import Path

import pytest
import yaml  # type: ignore[import]

import codex_imagen._install as _install_mod
from codex_imagen._install import (
    install_skill_for_client,
    skill_status_for_client,
    uninstall_skill_for_client,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect _install._home() to tmp_path."""
    monkeypatch.setattr(_install_mod, "_home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


# ---------------------------------------------------------------------------
# 1. Bundled skill importability and frontmatter validity
# ---------------------------------------------------------------------------


def test_bundled_skill_importable_via_importlib_resources() -> None:
    """imagen.md is loadable via importlib.resources from the installed package."""
    ref = ir.files("codex_imagen._assets.skills").joinpath("imagen.md")
    content = ref.read_text(encoding="utf-8")
    assert content.strip().startswith("---"), "skill file must begin with YAML frontmatter"


def test_bundled_skill_frontmatter_has_name_and_description() -> None:
    """Frontmatter must have both 'name' and 'description' keys."""
    ref = ir.files("codex_imagen._assets.skills").joinpath("imagen.md")
    content = ref.read_text(encoding="utf-8")
    # Extract the YAML block between the first pair of '---'
    parts = content.split("---", 2)
    assert len(parts) >= 3, "could not parse frontmatter block"
    fm = yaml.safe_load(parts[1])
    assert isinstance(fm, dict), "frontmatter must be a YAML mapping"
    assert "name" in fm, "frontmatter must have 'name'"
    assert "description" in fm, "frontmatter must have 'description'"
    assert fm["name"] == "imagen"


def test_bundled_skill_body_covers_all_batch_modes() -> None:
    """Skill body must mention all five batch_mode values."""
    ref = ir.files("codex_imagen._assets.skills").joinpath("imagen.md")
    content = ref.read_text(encoding="utf-8")
    for mode in ("single", "parallel", "variants", "chain", "branded-parallel"):
        assert mode in content, f"skill body must mention batch_mode '{mode}'"


def test_bundled_skill_body_covers_all_reasoning_modes() -> None:
    """Skill body must mention all four reasoning modes."""
    ref = ir.files("codex_imagen._assets.skills").joinpath("imagen.md")
    content = ref.read_text(encoding="utf-8")
    for mode in ("raw", "medium", "high", "max"):
        assert mode in content, f"skill body must mention reasoning mode '{mode}'"


def test_bundled_skill_under_500_lines() -> None:
    """Skill file must stay under 500 lines."""
    ref = ir.files("codex_imagen._assets.skills").joinpath("imagen.md")
    content = ref.read_text(encoding="utf-8")
    line_count = len(content.splitlines())
    assert line_count < 500, f"skill file is {line_count} lines — must be < 500"


# ---------------------------------------------------------------------------
# 2. install_skill_for_client
# ---------------------------------------------------------------------------


def test_install_skill_claude_code_creates_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_skill_for_client('claude-code') creates ~/.claude/skills/imagen/SKILL.md."""
    _patch_home(monkeypatch, tmp_path)
    ok, msg = install_skill_for_client("claude-code")
    assert ok is True
    skill_path = tmp_path / ".claude" / "skills" / "imagen" / "SKILL.md"
    assert skill_path.exists(), "SKILL.md must be created"
    content = skill_path.read_text(encoding="utf-8")
    assert "---" in content, "installed file must contain frontmatter"
    assert "installed at" in msg


def test_install_skill_claude_code_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Calling install_skill twice writes the file and reports 'updated' on the second call."""
    _patch_home(monkeypatch, tmp_path)
    ok1, msg1 = install_skill_for_client("claude-code")
    ok2, msg2 = install_skill_for_client("claude-code")
    assert ok1 is True
    assert ok2 is True
    assert "updated" in msg2


def test_install_skill_codex_creates_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_skill_for_client('codex') creates ~/.codex/skills/imagen/SKILL.md."""
    _patch_home(monkeypatch, tmp_path)
    ok, msg = install_skill_for_client("codex")
    assert ok is True
    skill_path = tmp_path / ".codex" / "skills" / "imagen" / "SKILL.md"
    assert skill_path.exists()


def test_install_skill_cursor_returns_not_supported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_skill_for_client('cursor') returns ok=True with 'not supported' note."""
    _patch_home(monkeypatch, tmp_path)
    ok, msg = install_skill_for_client("cursor")
    assert ok is True
    assert "not supported" in msg


def test_install_skill_opencode_returns_not_supported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_skill_for_client('opencode') returns ok=True with 'not supported' note."""
    _patch_home(monkeypatch, tmp_path)
    ok, msg = install_skill_for_client("opencode")
    assert ok is True
    assert "not supported" in msg


def test_install_skill_unknown_client_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_skill_for_client with unknown key returns (False, msg)."""
    _patch_home(monkeypatch, tmp_path)
    ok, msg = install_skill_for_client("nonexistent")
    assert ok is False
    assert "Unknown client key" in msg


def test_install_skill_dry_run_makes_no_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install_skill_for_client dry_run=True makes no filesystem changes."""
    _patch_home(monkeypatch, tmp_path)
    ok, msg = install_skill_for_client("claude-code", dry_run=True)
    assert ok is True
    assert "dry-run" in msg
    skill_path = tmp_path / ".claude" / "skills" / "imagen" / "SKILL.md"
    assert not skill_path.exists(), "dry-run must not create the file"


# ---------------------------------------------------------------------------
# 3. uninstall_skill_for_client
# ---------------------------------------------------------------------------


def test_uninstall_skill_removes_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uninstall_skill_for_client removes the skill file after install."""
    _patch_home(monkeypatch, tmp_path)
    install_skill_for_client("claude-code")
    skill_path = tmp_path / ".claude" / "skills" / "imagen" / "SKILL.md"
    assert skill_path.exists()

    ok, msg = uninstall_skill_for_client("claude-code")
    assert ok is True
    assert not skill_path.exists()
    assert "removed" in msg


def test_uninstall_skill_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uninstall_skill_for_client returns success even when skill is not installed."""
    _patch_home(monkeypatch, tmp_path)
    ok, msg = uninstall_skill_for_client("claude-code")
    assert ok is True
    assert "not installed" in msg


def test_uninstall_skill_dry_run_makes_no_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uninstall_skill_for_client dry_run=True leaves the file in place."""
    _patch_home(monkeypatch, tmp_path)
    install_skill_for_client("claude-code")
    skill_path = tmp_path / ".claude" / "skills" / "imagen" / "SKILL.md"
    assert skill_path.exists()

    ok, msg = uninstall_skill_for_client("claude-code", dry_run=True)
    assert ok is True
    assert "dry-run" in msg
    assert skill_path.exists(), "dry-run must not remove the file"


def test_uninstall_skill_unknown_client_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uninstall_skill_for_client with unknown key returns (False, msg)."""
    _patch_home(monkeypatch, tmp_path)
    ok, msg = uninstall_skill_for_client("bogus")
    assert ok is False
    assert "Unknown client key" in msg


# ---------------------------------------------------------------------------
# 4. skill_status_for_client
# ---------------------------------------------------------------------------


def test_skill_status_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """skill_status_for_client returns (True, path) after install."""
    _patch_home(monkeypatch, tmp_path)
    install_skill_for_client("claude-code")
    installed, path = skill_status_for_client("claude-code")
    assert installed is True
    assert path is not None
    assert path.exists()


def test_skill_status_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """skill_status_for_client returns (False, path) when not installed."""
    _patch_home(monkeypatch, tmp_path)
    installed, path = skill_status_for_client("claude-code")
    assert installed is False
    assert path is not None


def test_skill_status_not_supported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """skill_status_for_client returns (None, None) for unsupported clients."""
    _patch_home(monkeypatch, tmp_path)
    installed, path = skill_status_for_client("cursor")
    assert installed is None
    assert path is None


def test_skill_status_unknown_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """skill_status_for_client returns (None, None) for unknown keys."""
    _patch_home(monkeypatch, tmp_path)
    installed, path = skill_status_for_client("nonexistent")
    assert installed is None
    assert path is None


# ---------------------------------------------------------------------------
# 5. CLI integration — imagen setup --client claude-code installs skill
# ---------------------------------------------------------------------------


def test_cli_setup_installs_skill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """imagen setup --client claude-code writes the skill file."""
    from click.testing import CliRunner
    from codex_imagen.cli import _setup_command

    _patch_home(monkeypatch, tmp_path)
    # Claude Code requires .claude.json to exist (detection).
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(_setup_command, ["--client", "claude-code"])
    assert result.exit_code == 0, result.output
    skill_path = tmp_path / ".claude" / "skills" / "imagen" / "SKILL.md"
    assert skill_path.exists(), "setup must install the skill file"


def test_cli_uninstall_removes_skill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """imagen uninstall --client claude-code removes the skill file."""
    from click.testing import CliRunner
    from codex_imagen.cli import _setup_command, _uninstall_command

    _patch_home(monkeypatch, tmp_path)
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")

    runner = CliRunner()
    runner.invoke(_setup_command, ["--client", "claude-code"])
    skill_path = tmp_path / ".claude" / "skills" / "imagen" / "SKILL.md"
    assert skill_path.exists()

    result = runner.invoke(_uninstall_command, ["--client", "claude-code"])
    assert result.exit_code == 0, result.output
    assert not skill_path.exists(), "uninstall must remove the skill file"


def test_cli_status_reports_skill_presence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """imagen status output includes skill status section."""
    from click.testing import CliRunner
    from codex_imagen.cli import _status_command

    _patch_home(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(_status_command, [])
    assert result.exit_code == 0, result.output
    assert "skill" in result.output.lower(), "status must include skill section"
