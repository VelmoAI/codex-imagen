"""Tests for codex_imagen.cli — the Click dual-mode CLI.

Strategy
--------
All tests use Click's :class:`CliRunner`. We monkeypatch
``codex_imagen.cli.imagen`` to return a deterministic :class:`ImagenResult`
so no real bridge or filesystem image is required, and we monkeypatch
``codex_imagen.cli._bridge.health_check`` for the ``--health`` tests.

We test the dual-mode contract (JSON vs pretty), the parsing of
repeatable key=value flags (``--var``, ``--advanced``), the prompt input
precedence (PROMPT vs ``--file`` vs ``--prompt-json``), and the four
SPEC-mandated exit codes (0/1/2/3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from codex_imagen import cli as cli_mod
from codex_imagen.core import ImagenHealth, ImagenImage, ImagenResult


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


def _ok_health() -> ImagenHealth:
    """A healthy ImagenHealth — used for the default fake-result fixture."""
    return ImagenHealth(
        ok=True,
        codex_image_gen_available=True,
        pillow_available=True,
        auth_file_exists=True,
        auth_file_path="/fake/auth.json",
        hint=None,
    )


def _make_image(index: int, path: Path) -> ImagenImage:
    """Build a deterministic ImagenImage for use in fake results."""
    return ImagenImage(
        index=index,
        path=path,
        bytes=1024 * (index + 1),
        mime_type="image/png",
        response_id=f"resp_{index}",
        call_id=f"ig_{index}",
        revised_prompt=f"revised-{index}",
        original_prompt="orig",
        final_prompt="final",
        transparent=False,
        references_used=(),
        raw_png_path=None,
    )


def _ok_result(tmp_path: Path, n: int = 1) -> ImagenResult:
    """Construct an OK ImagenResult with ``n`` images for a fake imagen() call."""
    images = tuple(_make_image(i, tmp_path / f"{i:02d}.png") for i in range(n))
    return ImagenResult(
        ok=True,
        mode="medium",
        batch_mode="single" if n == 1 else "parallel",
        images=images,
        manifest_path=tmp_path / "manifest.jsonl",
        elapsed_ms=1500,
        health=_ok_health(),
        error=None,
        warnings=(),
    )


def _fail_result_generation(tmp_path: Path) -> ImagenResult:
    """A ``ok=False`` result where health is fine — i.e. generation failed."""
    return ImagenResult(
        ok=False,
        mode="medium",
        batch_mode="single",
        images=(),
        manifest_path=None,
        elapsed_ms=200,
        health=_ok_health(),
        error="all calls failed: boom",
        warnings=(),
    )


class FakeImagen:
    """Records its kwargs and returns a pre-baked ImagenResult.

    The CLI calls :func:`codex_imagen.cli.imagen`, which we monkeypatch
    onto an instance of this class. Tests can later inspect ``self.calls``
    to assert what the CLI actually passed through.
    """

    def __init__(self, result_factory: Any) -> None:
        # ``result_factory`` is a callable that builds the ImagenResult on
        # demand so test fixtures can use tmp_path lazily.
        self._result_factory = result_factory
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> ImagenResult:
        self.calls.append(dict(kwargs))
        return self._result_factory()


@pytest.fixture
def runner() -> CliRunner:
    """A Click CliRunner. Click 8.2+ keeps stderr separate by default."""
    return CliRunner()


@pytest.fixture
def patched_imagen_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> FakeImagen:
    """Replace cli.imagen with a fake that always returns one OK image."""
    fake = FakeImagen(lambda: _ok_result(tmp_path, n=1))
    monkeypatch.setattr(cli_mod, "imagen", fake)
    return fake


# ---------------------------------------------------------------------------
# 1) Basic CLI plumbing
# ---------------------------------------------------------------------------


def test_cli_help_works(runner: CliRunner) -> None:
    """--help renders without crashing and lists the core flags."""
    result = runner.invoke(cli_mod._imagen_command, ["--help"])
    assert result.exit_code == 0
    assert "PROMPT" in result.output
    assert "--json" in result.output
    assert "--health" in result.output


def test_cli_version_prints_version(runner: CliRunner) -> None:
    """--version emits the package version and exits 0."""
    result = runner.invoke(cli_mod._imagen_command, ["--version"])
    assert result.exit_code == 0
    from codex_imagen import __version__

    assert __version__ in result.output


def test_cli_no_args_exits_3(runner: CliRunner) -> None:
    """Empty invocation prints a usage hint on stderr and exits 3."""
    result = runner.invoke(cli_mod._imagen_command, [])
    assert result.exit_code == cli_mod.EXIT_INVALID_ARGS
    assert "no prompt provided" in result.stderr


# ---------------------------------------------------------------------------
# 2) Output mode: pretty vs JSON
# ---------------------------------------------------------------------------


def test_cli_simple_prompt_pretty_mode(
    runner: CliRunner, patched_imagen_ok: FakeImagen
) -> None:
    """`imagen "cat" --pretty` exits 0 and shows the pretty summary."""
    result = runner.invoke(cli_mod._imagen_command, ["cat", "--pretty"])
    assert result.exit_code == 0
    assert "[OK]" in result.output
    assert len(patched_imagen_ok.calls) == 1
    assert patched_imagen_ok.calls[0]["prompt"] == "cat"


def test_cli_simple_prompt_json_mode(
    runner: CliRunner, patched_imagen_ok: FakeImagen
) -> None:
    """`imagen "cat" --json` emits a single JSON object on stdout."""
    result = runner.invoke(cli_mod._imagen_command, ["cat", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["mode"] == "medium"
    assert isinstance(payload["images"], list)
    assert len(payload["images"]) == 1
    assert "path" in payload["images"][0]
    assert isinstance(payload["images"][0]["path"], str)


def test_cli_json_auto_when_non_tty(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    patched_imagen_ok: FakeImagen,
) -> None:
    """When stdout is not a TTY (CliRunner default), JSON is emitted."""
    monkeypatch.setattr(cli_mod, "_is_stdout_tty", lambda: False)
    result = runner.invoke(cli_mod._imagen_command, ["cat"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True


def test_cli_pretty_when_tty(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    patched_imagen_ok: FakeImagen,
) -> None:
    """When stdout looks like a TTY, default to pretty mode."""
    monkeypatch.setattr(cli_mod, "_is_stdout_tty", lambda: True)
    result = runner.invoke(cli_mod._imagen_command, ["cat"])
    assert result.exit_code == 0
    assert "[OK]" in result.output
    # No JSON braces on the first character — pretty mode never emits raw JSON.
    assert not result.output.lstrip().startswith("{")


def test_cli_json_pretty_both_set_pretty_wins(
    runner: CliRunner, patched_imagen_ok: FakeImagen
) -> None:
    """When both --json and --pretty are set, pretty wins and we warn."""
    result = runner.invoke(
        cli_mod._imagen_command, ["cat", "--json", "--pretty"]
    )
    assert result.exit_code == 0
    assert "[OK]" in result.output
    assert "warning" in result.stderr.lower()


# ---------------------------------------------------------------------------
# 3) Input sources
# ---------------------------------------------------------------------------


def test_cli_file_input_three_prompts(
    runner: CliRunner,
    patched_imagen_ok: FakeImagen,
    tmp_path: Path,
) -> None:
    """`-f FILE` with three lines passes a list of three prompts to imagen()."""
    f = tmp_path / "prompts.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    result = runner.invoke(
        cli_mod._imagen_command, ["--json", "-f", str(f)]
    )
    assert result.exit_code == 0
    assert patched_imagen_ok.calls[0]["prompt"] == ["alpha", "beta", "gamma"]


def test_cli_file_input_skips_blanks_and_comments(
    runner: CliRunner,
    patched_imagen_ok: FakeImagen,
    tmp_path: Path,
) -> None:
    """Blank lines and # comments are ignored by --file."""
    f = tmp_path / "prompts.txt"
    f.write_text("# header\n\nalpha\n# inline\nbeta\n", encoding="utf-8")
    result = runner.invoke(
        cli_mod._imagen_command, ["--json", "-f", str(f)]
    )
    assert result.exit_code == 0
    assert patched_imagen_ok.calls[0]["prompt"] == ["alpha", "beta"]


def test_cli_prompt_json_dict(
    runner: CliRunner,
    patched_imagen_ok: FakeImagen,
    tmp_path: Path,
) -> None:
    """A dict prompt-json file is passed through as a dict."""
    f = tmp_path / "prompt.json"
    f.write_text(json.dumps({"Primary request": "x"}), encoding="utf-8")
    result = runner.invoke(
        cli_mod._imagen_command, ["--json", "--prompt-json", str(f)]
    )
    assert result.exit_code == 0
    assert patched_imagen_ok.calls[0]["prompt"] == {"Primary request": "x"}


def test_cli_prompt_json_list(
    runner: CliRunner,
    patched_imagen_ok: FakeImagen,
    tmp_path: Path,
) -> None:
    """A list prompt-json file is passed through as a list."""
    f = tmp_path / "prompt.json"
    f.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    result = runner.invoke(
        cli_mod._imagen_command, ["--json", "--prompt-json", str(f)]
    )
    assert result.exit_code == 0
    assert patched_imagen_ok.calls[0]["prompt"] == ["a", "b"]


def test_cli_prompt_json_invalid_exits_3(
    runner: CliRunner,
    patched_imagen_ok: FakeImagen,
    tmp_path: Path,
) -> None:
    """A malformed --prompt-json file triggers exit 3."""
    f = tmp_path / "bad.json"
    f.write_text("this is not json", encoding="utf-8")
    result = runner.invoke(
        cli_mod._imagen_command, ["--json", "--prompt-json", str(f)]
    )
    assert result.exit_code == cli_mod.EXIT_INVALID_ARGS
    assert patched_imagen_ok.calls == []


def test_cli_prompt_json_wrong_shape_exits_3(
    runner: CliRunner,
    patched_imagen_ok: FakeImagen,
    tmp_path: Path,
) -> None:
    """A JSON file whose top-level is a scalar is rejected with exit 3."""
    f = tmp_path / "scalar.json"
    f.write_text('"just a string"', encoding="utf-8")
    result = runner.invoke(
        cli_mod._imagen_command, ["--json", "--prompt-json", str(f)]
    )
    assert result.exit_code == cli_mod.EXIT_INVALID_ARGS


# ---------------------------------------------------------------------------
# 4) Repeatable flags
# ---------------------------------------------------------------------------


def test_cli_multiple_references(
    runner: CliRunner,
    patched_imagen_ok: FakeImagen,
    tmp_path: Path,
) -> None:
    """``-r`` may appear multiple times; imagen() receives a tuple of paths."""
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    result = runner.invoke(
        cli_mod._imagen_command,
        ["cat", "--json", "-r", str(a), "-r", str(b)],
    )
    assert result.exit_code == 0
    refs = patched_imagen_ok.calls[0]["references"]
    assert len(refs) == 2
    assert str(a) in refs
    assert str(b) in refs


def test_cli_var_parsing(
    runner: CliRunner, patched_imagen_ok: FakeImagen
) -> None:
    """``--var key=value`` is parsed into the ``vars`` dict on ImagenOptions."""
    result = runner.invoke(
        cli_mod._imagen_command,
        ["cat", "--json", "--var", "char=alice", "--var", "color=red"],
    )
    assert result.exit_code == 0
    assert patched_imagen_ok.calls[0]["vars"] == {
        "char": "alice",
        "color": "red",
    }


def test_cli_var_invalid_exits_3(
    runner: CliRunner, patched_imagen_ok: FakeImagen
) -> None:
    """A ``--var`` without ``=`` is a user error → exit 3, no imagen() call."""
    result = runner.invoke(
        cli_mod._imagen_command,
        ["cat", "--json", "--var", "lonely"],
    )
    assert result.exit_code == cli_mod.EXIT_INVALID_ARGS
    assert patched_imagen_ok.calls == []


def test_cli_advanced_parsing(
    runner: CliRunner, patched_imagen_ok: FakeImagen
) -> None:
    """``--advanced key=value`` is parsed into the advanced dict."""
    result = runner.invoke(
        cli_mod._imagen_command,
        ["cat", "--json", "--advanced", "reasoning_summary=full"],
    )
    assert result.exit_code == 0
    assert patched_imagen_ok.calls[0]["advanced"] == {
        "reasoning_summary": "full"
    }


def test_cli_chroma_no_despill_flag(
    runner: CliRunner, patched_imagen_ok: FakeImagen
) -> None:
    """``--no-chroma-despill`` flips the default-True chroma_despill flag."""
    result = runner.invoke(
        cli_mod._imagen_command,
        ["cat", "--json", "--no-chroma-despill"],
    )
    assert result.exit_code == 0
    assert patched_imagen_ok.calls[0]["chroma_despill"] is False


def test_cli_chroma_despill_default_true(
    runner: CliRunner, patched_imagen_ok: FakeImagen
) -> None:
    """The default chroma_despill setting is True (the SPEC default)."""
    result = runner.invoke(cli_mod._imagen_command, ["cat", "--json"])
    assert result.exit_code == 0
    assert patched_imagen_ok.calls[0]["chroma_despill"] is True


# ---------------------------------------------------------------------------
# 5) Health subcommand and exit codes
# ---------------------------------------------------------------------------


def test_cli_health_ok_exits_0(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    patched_imagen_ok: FakeImagen,
) -> None:
    """``--health`` with a healthy bridge exits 0 and never calls imagen()."""
    monkeypatch.setattr(
        cli_mod._bridge,
        "health_check",
        lambda: {"ok": True, "hint": None},
    )
    result = runner.invoke(cli_mod._imagen_command, ["--health", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert patched_imagen_ok.calls == []


def test_cli_health_fail_exits_1(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    patched_imagen_ok: FakeImagen,
) -> None:
    """``--health`` failure maps to exit code 1."""
    monkeypatch.setattr(
        cli_mod._bridge,
        "health_check",
        lambda: {"ok": False, "hint": "codex login required"},
    )
    result = runner.invoke(cli_mod._imagen_command, ["--health", "--json"])
    assert result.exit_code == cli_mod.EXIT_HEALTH_FAILURE
    assert patched_imagen_ok.calls == []


def test_cli_imagen_returns_ok_false_health_fail_exits_1(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An imagen() result whose health is also failing maps to exit 1."""
    fake_health = ImagenHealth(
        ok=False,
        codex_image_gen_available=True,
        pillow_available=True,
        auth_file_exists=False,
        auth_file_path=None,
        hint="login expired",
    )
    fake_result = ImagenResult(
        ok=False,
        mode="auto",
        batch_mode="auto",
        images=(),
        manifest_path=None,
        elapsed_ms=10,
        health=fake_health,
        error="login expired",
        warnings=(),
    )
    monkeypatch.setattr(cli_mod, "imagen", lambda **kw: fake_result)
    result = runner.invoke(cli_mod._imagen_command, ["cat", "--json"])
    assert result.exit_code == cli_mod.EXIT_HEALTH_FAILURE


def test_cli_imagen_returns_ok_false_exits_2(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """imagen() ok=False with healthy bridge → exit 2 (generation failure)."""
    monkeypatch.setattr(
        cli_mod, "imagen", lambda **kw: _fail_result_generation(tmp_path)
    )
    result = runner.invoke(cli_mod._imagen_command, ["cat", "--json"])
    assert result.exit_code == cli_mod.EXIT_GENERATION_FAILURE
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "all calls failed" in payload["error"]


# ---------------------------------------------------------------------------
# 6) Quiet mode
# ---------------------------------------------------------------------------


def test_cli_quiet_human_mode_single_line(
    runner: CliRunner, patched_imagen_ok: FakeImagen
) -> None:
    """In pretty+quiet mode we collapse output to a one-liner."""
    result = runner.invoke(
        cli_mod._imagen_command, ["cat", "--pretty", "--quiet"]
    )
    assert result.exit_code == 0
    # Filter out empty lines and check there's only one summary line.
    non_empty = [
        line for line in result.output.splitlines() if line.strip()
    ]
    assert len(non_empty) == 1
    assert "[OK]" in non_empty[0]


def test_cli_quiet_json_still_emits_full_payload(
    runner: CliRunner, patched_imagen_ok: FakeImagen
) -> None:
    """JSON + quiet emits the full payload (quiet only affects pretty)."""
    result = runner.invoke(
        cli_mod._imagen_command, ["cat", "--json", "--quiet"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert len(payload["images"]) == 1


# ---------------------------------------------------------------------------
# 7) Choice validation (Click's exit code 2)
# ---------------------------------------------------------------------------


def test_cli_invalid_mode_exits_2_click(
    runner: CliRunner, patched_imagen_ok: FakeImagen
) -> None:
    """Click rejects unknown ``--mode`` values with its own exit code (2)."""
    result = runner.invoke(cli_mod._imagen_command, ["cat", "--mode", "bogus"])
    assert result.exit_code == 2  # Click default for usage errors
    assert patched_imagen_ok.calls == []


# ---------------------------------------------------------------------------
# 8) Error rescue paths (non-UTF-8 input, imagen() raising ValueError)
# ---------------------------------------------------------------------------


def test_cli_file_with_invalid_encoding_exits_3(
    runner: CliRunner,
    patched_imagen_ok: FakeImagen,
    tmp_path: Path,
) -> None:
    """Non-UTF-8 file → exit 3 with a clear encoding error.

    UnicodeDecodeError is a subclass of ValueError (not OSError), so until
    we explicitly rescue it the CLI would crash uncaught on cp1252/UTF-16
    inputs that Windows users routinely produce. We assert the encoding
    hint is surfaced so the user knows how to fix the file.
    """
    bad = tmp_path / "prompts.txt"
    # Bytes that are not a valid UTF-8 sequence — UTF-16 BOM + ASCII.
    bad.write_bytes(b"\xff\xfecat\n")
    result = runner.invoke(cli_mod._imagen_command, ["-f", str(bad)])
    assert result.exit_code == cli_mod.EXIT_INVALID_ARGS
    combined = (result.output + (result.stderr or "")).lower()
    assert "utf-8" in combined or "encoding" in combined
    assert patched_imagen_ok.calls == []


def test_cli_imagen_raises_value_error_exits_3(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When imagen() raises ValueError (bad ImagenOptions), CLI exits 3.

    The CLI already has a ``except (ValueError, TypeError)`` rescue around
    the SDK call so an unparseable ``--size`` or similar surfaces as a
    clean exit-3 message rather than a crash traceback.
    """

    def boom(**kwargs: Any) -> ImagenResult:
        raise ValueError("bad size suggestion")

    monkeypatch.setattr(cli_mod, "imagen", boom)
    result = runner.invoke(cli_mod._imagen_command, ["cat", "--json"])
    assert result.exit_code == cli_mod.EXIT_INVALID_ARGS
    combined = result.output + (result.stderr or "")
    assert "bad size" in combined
