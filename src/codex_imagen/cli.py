"""codex_imagen.cli — Click-based CLI with dual-mode (human / JSON) output.

This module wires the :func:`codex_imagen.imagen` SDK entry point into a
console script named ``imagen``. It is the single user-facing CLI surface.

Dual-mode design
----------------
The CLI auto-detects whether stdout is a TTY:

* **TTY** → human-friendly "pretty" output: a one-line status, an indented
  list of generated image paths with sizes, and a summary footer.
* **Non-TTY** (pipe, redirect, programmatic invocation) → a single JSON
  object on stdout containing the full :class:`ImagenResult`. No decoration,
  no warnings interleaved into stdout — strictly machine-friendly.

Explicit flags override the detection:

* ``--json``   force JSON mode (e.g. from inside a TTY for agent integration)
* ``--pretty`` force human mode (e.g. when piping to ``less``)
* ``--quiet``  suppress progress messages on stderr; human mode also collapses
  the result to a single status line.

Stream discipline
-----------------
* **stdout** is reserved for the result payload only (pretty or JSON). It is
  safe to pipe into ``jq``, ``tee``, or another process.
* **stderr** carries progress messages, warnings, and errors so stdout is
  never polluted.

Exit codes
----------
* 0 — success (at least one image generated, partial successes count as OK)
* 1 — health-check failure (no API call was attempted)
* 2 — generation failure (``imagen()`` returned ``ok=False`` for non-health
  reasons, e.g. every call failed)
* 3 — invalid CLI arguments (our own validation, e.g. ``--var foo`` with no
  ``=``, malformed ``--prompt-json`` file, no input at all). Click's own
  parameter validation still exits with its default code (2).

The flags map 1:1 to :class:`codex_imagen.ImagenOptions` so there is no
hidden behavior between CLI and SDK.

Subcommands
-----------
``imagen setup``     — interactive / batch MCP installer for 5 clients
``imagen uninstall`` — remove the MCP registration
``imagen status``    — show detected clients and install state
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import click

from codex_imagen import __version__, _bridge
from codex_imagen.core import ImagenResult, imagen

# ---------------------------------------------------------------------------
# Constants — exit codes are the SPEC contract; centralising them keeps the
# CLI grep-friendly and ensures tests can reference the same names.
# ---------------------------------------------------------------------------

EXIT_OK: int = 0
EXIT_HEALTH_FAILURE: int = 1
EXIT_GENERATION_FAILURE: int = 2
EXIT_INVALID_ARGS: int = 3


class _InvalidArgsError(click.ClickException):
    """ClickException subclass that maps to our SPEC exit code 3.

    Click's default :class:`click.ClickException` exits with code 1, which
    would collide with our health-failure code. We override ``exit_code``
    so anywhere we ``raise _InvalidArgsError(...)`` inside the command body
    the process exits with 3 — matching the SPEC's "invalid args" slot.
    """

    exit_code = EXIT_INVALID_ARGS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_stdout_tty() -> bool:
    """Return True when stdout looks like an interactive terminal.

    Wrapped so tests can monkeypatch a single function instead of fiddling
    with ``sys.stdout`` directly. Some environments (Click's test runner,
    CI) report ``isatty=False`` even though they are interactive; that is
    fine because we treat "uncertain" as "not a TTY" and emit JSON, which
    is always safe to consume.
    """
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - defensive
        return False


def _parse_kv_list(
    pairs: tuple[str, ...],
    *,
    flag_name: str,
) -> dict[str, str]:
    """Parse a tuple of ``key=value`` strings into a dict.

    Used for ``--var`` and ``--advanced`` which Click collects as a tuple.
    Each entry must contain exactly one ``=`` separator. A missing
    separator is a user mistake and we raise :class:`click.ClickException`
    so the framework prints a clear error and we can map it to exit code 3.
    """
    out: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise _InvalidArgsError(
                f"{flag_name} expects key=value pairs, got {raw!r}"
            )
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            raise _InvalidArgsError(
                f"{flag_name} key may not be empty (got {raw!r})"
            )
        out[key] = value
    return out


def _load_prompts_file(path: Path) -> list[str]:
    """Read prompts from a text file — one prompt per non-empty line.

    Blank lines and lines starting with ``#`` (comments) are skipped. The
    list is returned even when only one line remains; the caller decides
    whether to unwrap to a single string.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _InvalidArgsError(
            f"could not read --file {path}: file is not valid UTF-8 "
            f"(re-save as UTF-8): {exc}"
        )
    except OSError as exc:
        raise _InvalidArgsError(f"could not read --file {path}: {exc}")

    prompts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        prompts.append(stripped)
    if not prompts:
        raise _InvalidArgsError(f"--file {path} contained no prompts")
    return prompts


def _load_prompt_json(path: Path) -> Any:
    """Load a JSON prompt file. The content may be a dict or a list.

    A dict is treated by ``imagen()`` as a Codex labeled-spec prompt. A list
    is treated as a multi-prompt batch (each element may itself be a string
    or a dict).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _InvalidArgsError(
            f"could not read --prompt-json {path}: file is not valid UTF-8 "
            f"(re-save as UTF-8): {exc}"
        )
    except OSError as exc:
        raise _InvalidArgsError(f"could not read --prompt-json {path}: {exc}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _InvalidArgsError(
            f"--prompt-json {path}: invalid JSON ({exc.msg} at line {exc.lineno})"
        )

    if not isinstance(data, (dict, list)):
        raise _InvalidArgsError(
            f"--prompt-json {path}: top-level value must be an object or array, "
            f"got {type(data).__name__}"
        )
    return data


def _result_to_json_dict(result: ImagenResult) -> dict[str, Any]:
    """Convert an ImagenResult dataclass tree into a JSON-friendly dict.

    Path objects become absolute strings; nested dataclasses (ImagenImage,
    ImagenHealth) are unpacked via :func:`dataclasses.asdict`. Tuples are
    converted to lists implicitly by ``asdict`` for the inner dataclasses,
    but the top-level tuples need explicit handling.
    """

    def _path_to_str(p: Any) -> Any:
        """Normalize Path-like values to absolute string paths."""
        if isinstance(p, Path):
            try:
                return str(p.resolve())
            except OSError:
                # ``resolve()`` can fail on Windows for non-existent files
                # in some edge cases — fall back to absolute().
                return str(p.absolute())
        return p

    def _normalize(value: Any) -> Any:
        """Recursively replace Path objects with strings and tuples with lists."""
        if isinstance(value, Path):
            return _path_to_str(value)
        if isinstance(value, dict):
            return {k: _normalize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_normalize(v) for v in value]
        return value

    payload: dict[str, Any] = {
        "ok": result.ok,
        "mode": result.mode,
        "batch_mode": result.batch_mode,
        "images": [_normalize(asdict(img)) for img in result.images],
        "manifest_path": _path_to_str(result.manifest_path),
        "elapsed_ms": result.elapsed_ms,
        "health": _normalize(asdict(result.health)),
        "error": result.error,
        "warnings": list(result.warnings),
    }
    return payload


def _format_bytes(n: int) -> str:
    """Render a byte count as a compact human-readable string."""
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.0f} KB"
    mb = kb / 1024
    return f"{mb:.1f} MB"


def _print_pretty(result: ImagenResult, *, quiet: bool) -> None:
    """Print a human-readable summary of ``result`` to stdout.

    In ``quiet`` mode we collapse to a single line so this still composes
    well with shell scripting. Warnings always go to stderr — never stdout
    — because stdout is the documented "result" stream.
    """
    if quiet:
        if result.ok:
            click.echo(f"[OK] Generated {len(result.images)} images")
        else:
            click.echo(f"[FAIL] {result.error or 'generation failed'}")
        return

    if result.ok:
        elapsed_s = result.elapsed_ms / 1000.0
        click.echo(f"[OK] Generated {len(result.images)} images in {elapsed_s:.1f}s")
        for img in result.images:
            # Resolve to an absolute path here so the user can copy/paste
            # without worrying about their working dir.
            try:
                shown = str(Path(img.path).resolve())
            except OSError:
                shown = str(img.path)
            click.echo(f"     {shown}  ({_format_bytes(img.bytes)})")
        click.echo("")
        click.echo(f"Mode: {result.mode} | Batch: {result.batch_mode}")
        if result.manifest_path is not None:
            click.echo(f"Manifest: {result.manifest_path}")
    else:
        click.echo(f"[FAIL] Generation failed: {result.error or 'unknown error'}")
        if result.health and not result.health.ok and result.health.hint:
            click.echo(f"Hint: {result.health.hint}")

    # Surface warnings on stderr so stdout stays a clean result stream.
    for warning in result.warnings:
        click.echo(f"warning: {warning}", err=True)


def _print_json(payload: dict[str, Any]) -> None:
    """Emit ``payload`` as a single JSON object on stdout.

    We use ``json.dumps`` directly rather than ``click.echo(..., nl=False)``
    plus a manual newline because we want full control over the trailing
    newline and want to ensure no ANSI color codes ever leak in.
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _emit_health(
    *,
    json_mode: bool,
) -> int:
    """Run ``--health`` and emit the result. Returns the process exit code."""
    health_dict = _bridge.health_check()
    ok = bool(health_dict.get("ok"))

    if json_mode:
        _print_json({"ok": ok, "health": health_dict})
    else:
        if ok:
            click.echo("[OK] health check passed")
        else:
            click.echo("[FAIL] health check failed")
            hint = health_dict.get("hint")
            if hint:
                click.echo(f"Hint: {hint}")
        # Always include the structured fields on stderr so users debugging
        # can see exactly which probe failed without parsing JSON.
        for key in (
            "codex_image_gen_available",
            "pillow_available",
            "auth_file_exists",
            "auth_file_path",
            "auth_expires_in_seconds",
        ):
            if key in health_dict:
                click.echo(f"  {key}: {health_dict[key]}", err=True)

    return EXIT_OK if ok else EXIT_HEALTH_FAILURE


def _resolve_prompt(
    *,
    prompt_arg: str | None,
    file_path: Path | None,
    prompt_json_path: Path | None,
) -> Any:
    """Decide which input source provides the prompt and load it.

    Precedence: ``--prompt-json`` > ``--file`` > positional ``PROMPT``.
    When more than one source is supplied we warn (stderr) and use the
    higher-precedence one. This matches the SPEC requirement that ``--file``
    wins over the positional arg.
    """
    if prompt_json_path is not None:
        if file_path is not None or prompt_arg is not None:
            click.echo(
                "warning: --prompt-json overrides --file and the positional PROMPT",
                err=True,
            )
        return _load_prompt_json(prompt_json_path)

    if file_path is not None:
        if prompt_arg is not None:
            click.echo(
                "warning: --file overrides the positional PROMPT", err=True
            )
        prompts = _load_prompts_file(file_path)
        # SDK accepts a single string OR a list. Unwrap one-element files so
        # the SDK can auto-detect "single" batch mode naturally.
        return prompts[0] if len(prompts) == 1 else prompts

    if prompt_arg is not None:
        return prompt_arg

    # No source — the caller (main) is responsible for treating this as
    # exit-code 3. We signal with ``None`` so the caller can centralise the
    # error message and the usage hint.
    return None


def _emit_result(
    result: ImagenResult,
    *,
    json_mode: bool,
    quiet: bool,
) -> int:
    """Print ``result`` in the chosen mode and return the process exit code."""
    if json_mode:
        _print_json(_result_to_json_dict(result))
    else:
        _print_pretty(result, quiet=quiet)

    if result.ok:
        return EXIT_OK
    # Distinguish health failure from generation failure for the exit code.
    if not result.health.ok:
        return EXIT_HEALTH_FAILURE
    return EXIT_GENERATION_FAILURE


# ---------------------------------------------------------------------------
# The Click command
# ---------------------------------------------------------------------------


# We define short_help and the full docstring explicitly so ``imagen --help``
# is useful out of the box. ``context_settings`` widen the help column for
# the long option names so users on standard 80-col terminals still see
# things lined up nicely.
@click.command(
    name="imagen",
    context_settings={
        "help_option_names": ["-h", "--help"],
        "max_content_width": 100,
    },
)
@click.argument("prompt", required=False)
# INPUT --------------------------------------------------------------------
@click.option(
    "-f",
    "--file",
    "file_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read prompts from a text file (one per line). Blank/# lines skipped.",
)
@click.option(
    "--prompt-json",
    "prompt_json_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read a JSON prompt (dict or list) from a file.",
)
# MODES --------------------------------------------------------------------
@click.option(
    "-m",
    "--mode",
    type=click.Choice(["auto", "raw", "medium", "high", "max"]),
    default="auto",
    show_default=True,
    help="Prompt-builder mode.",
)
@click.option(
    "-b",
    "--batch-mode",
    type=click.Choice(
        ["auto", "single", "parallel", "variants", "chain", "branded-parallel"]
    ),
    default="auto",
    show_default=True,
    help="Batch orchestration mode.",
)
@click.option(
    "--chain-mode",
    default="anchor+previous",
    show_default=True,
    help="Chain context strategy: previous | anchor | anchor+previous | window:N | all",
)
@click.option(
    "-a",
    "--anchor",
    default=None,
    help="Explicit anchor prompt for branded-parallel mode.",
)
@click.option(
    "-n",
    "--count",
    type=int,
    default=1,
    show_default=True,
    help="Number of variants (variants mode).",
)
# CONTENT ------------------------------------------------------------------
@click.option(
    "-r",
    "--reference",
    "references",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="Reference image path. Repeat to pass multiple references.",
)
@click.option(
    "-s",
    "--skill",
    "skills",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="Skill .md file to inject. Repeat for multiple skills.",
)
@click.option(
    "--mask",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    default=None,
    help="Alpha mask for edits.",
)
@click.option(
    "-e",
    "--extra",
    "extra_instructions",
    default=None,
    help="Extra instructions appended to the prompt.",
)
# IMAGE --------------------------------------------------------------------
@click.option(
    "--size",
    default="auto",
    show_default=True,
    help="'auto' or 'WIDTHxHEIGHT'.",
)
@click.option(
    "--output-format",
    type=click.Choice(["png", "jpeg", "webp"]),
    default="png",
    show_default=True,
    help="Output image format.",
)
# TRANSPARENCY -------------------------------------------------------------
@click.option(
    "--transparent",
    is_flag=True,
    default=False,
    help="Enable chroma-key transparency pipeline.",
)
@click.option(
    "--chroma-key",
    default="#FF00FF",
    show_default=True,
    help="Hex color for the chroma key.",
)
@click.option(
    "--chroma-tolerance",
    type=int,
    default=40,
    show_default=True,
    help="Chroma tolerance 0-100.",
)
@click.option(
    "--chroma-despill/--no-chroma-despill",
    default=True,
    show_default=True,
    help="Enable/disable despill.",
)
# ENHANCEMENT --------------------------------------------------------------
@click.option(
    "--enhance-prompt",
    is_flag=True,
    default=False,
    help="Run the local deterministic prompt enhancer.",
)
@click.option(
    "--var",
    "vars_",
    multiple=True,
    help="Variable for prompt templating, key=value (repeatable).",
)
# ORCHESTRATION ------------------------------------------------------------
@click.option(
    "-p",
    "--parallel",
    type=int,
    default=2,
    show_default=True,
    help="Max concurrent calls.",
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./out"),
    show_default=True,
    help="Output directory.",
)
# ADVANCED -----------------------------------------------------------------
@click.option(
    "--advanced",
    "advanced",
    multiple=True,
    help="Pass-through to codex_image_gen, key=value (repeatable).",
)
# OUTPUT MODE --------------------------------------------------------------
@click.option(
    "--json",
    "json_flag",
    is_flag=True,
    default=False,
    help="Force JSON output to stdout.",
)
@click.option(
    "--pretty",
    "pretty_flag",
    is_flag=True,
    default=False,
    help="Force pretty output (overrides TTY detection).",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress progress messages.",
)
# INFO ---------------------------------------------------------------------
@click.option(
    "--health",
    "health_flag",
    is_flag=True,
    default=False,
    help="Run health check and exit (no API call).",
)
@click.version_option(__version__, "--version", prog_name="imagen")
def _imagen_command(
    prompt: str | None,
    file_path: Path | None,
    prompt_json_path: Path | None,
    mode: str,
    batch_mode: str,
    chain_mode: str,
    anchor: str | None,
    count: int,
    references: tuple[str, ...],
    skills: tuple[str, ...],
    mask: str | None,
    extra_instructions: str | None,
    size: str,
    output_format: str,
    transparent: bool,
    chroma_key: str,
    chroma_tolerance: int,
    chroma_despill: bool,
    enhance_prompt: bool,
    vars_: tuple[str, ...],
    parallel: int,
    output_dir: Path,
    advanced: tuple[str, ...],
    json_flag: bool,
    pretty_flag: bool,
    quiet: bool,
    health_flag: bool,
) -> None:
    """Generate images via the Codex OAuth bridge.

    Provide a positional PROMPT, or pass ``-f FILE`` for multi-prompt
    batches, or ``--prompt-json FILE`` for structured Codex labeled-spec
    input. See ``--help`` for the full option surface; the flags map 1:1
    onto the ``ImagenOptions`` dataclass used by the SDK.
    """
    # ---- 1) Decide output mode. ----------------------------------------
    # Explicit flags win; otherwise default to JSON when stdout is not a
    # terminal so piping into another process Just Works.
    if json_flag and pretty_flag:
        # SPEC is ambiguous; we let --pretty win (the more conservative
        # choice for a human in a terminal who fat-fingered both).
        json_mode = False
        click.echo(
            "warning: --json and --pretty both set; --pretty wins", err=True
        )
    elif json_flag:
        json_mode = True
    elif pretty_flag:
        json_mode = False
    else:
        json_mode = not _is_stdout_tty()

    # ---- 2) Early exits: --health. -------------------------------------
    # --version is handled by Click's ``version_option`` (above) and never
    # reaches this body.
    if health_flag:
        sys.exit(_emit_health(json_mode=json_mode))

    # ---- 3) Parse repeatable key=value flags. --------------------------
    # ClickException is caught at the bottom and re-raised with exit 3.
    parsed_vars = _parse_kv_list(vars_, flag_name="--var")
    parsed_advanced = _parse_kv_list(advanced, flag_name="--advanced")

    # ---- 4) Resolve the prompt input. ----------------------------------
    resolved_prompt = _resolve_prompt(
        prompt_arg=prompt,
        file_path=file_path,
        prompt_json_path=prompt_json_path,
    )
    if resolved_prompt is None:
        # No prompt anywhere — print a friendly hint to stderr and bail with
        # exit code 3 (our "invalid args" code, distinct from Click's 2).
        click.echo(
            "error: no prompt provided. Pass a PROMPT, --file PATH, "
            "--prompt-json PATH, or --health.",
            err=True,
        )
        click.echo("Run 'imagen --help' for usage.", err=True)
        sys.exit(EXIT_INVALID_ARGS)

    # ---- 5) Build ImagenOptions kwargs and call the SDK. ----------------
    # We pass only fields that map cleanly; ``references`` and ``skills``
    # are tuples already (from Click's multiple=True).
    imagen_kwargs: dict[str, Any] = {
        "prompt": resolved_prompt,
        "output_dir": output_dir,
        "mode": mode,
        "batch_mode": batch_mode,
        "chain_mode": chain_mode,
        "anchor": anchor,
        "count": count,
        "references": references,
        "skills": skills,
        "mask": mask,
        "extra_instructions": extra_instructions,
        "size": size,
        "output_format": output_format,
        "transparent": transparent,
        "chroma_key": chroma_key,
        "chroma_tolerance": chroma_tolerance,
        "chroma_despill": chroma_despill,
        "enhance_prompt": enhance_prompt,
        "vars": parsed_vars,
        "parallel": parallel,
        "advanced": parsed_advanced,
    }

    if not quiet and not json_mode:
        click.echo("imagen: starting...", err=True)

    try:
        result = imagen(**imagen_kwargs)
    except (ValueError, TypeError) as exc:
        # ImagenOptions raised on a bad value we didn't pre-validate. This
        # is rare because most fields are constrained by Click's
        # ``click.Choice`` already, but e.g. an unparseable size still ends
        # up here through core's own validation path.
        click.echo(f"error: {exc}", err=True)
        sys.exit(EXIT_INVALID_ARGS)

    # ---- 6) Emit and exit. ---------------------------------------------
    sys.exit(_emit_result(result, json_mode=json_mode, quiet=quiet))


def main() -> None:
    """Console-script entry point declared in ``pyproject.toml``.

    Delegates to the Click command. ``_imagen_command`` uses ``sys.exit``
    directly for the SPEC-mandated codes (0/1/2). Our own validation
    errors are :class:`_InvalidArgsError`, which Click raises through with
    ``exit_code = 3``. Click's own parser errors (``UsageError``) keep
    their default code 2.
    """
    _imagen_command.main()


if __name__ == "__main__":  # pragma: no cover
    main()


# ---------------------------------------------------------------------------
# Installer subcommands — setup / uninstall / status
# ---------------------------------------------------------------------------
# These are wired as a separate Click group so that ``imagen setup`` etc. work
# as documented. The ``main()`` entry point above continues to use
# ``_imagen_command`` for backward compatibility. A thin ``imagen_cli`` group
# is exposed as ``imagen_group`` for the new subcommands; the pyproject.toml
# console script calls ``main()`` which delegates to ``_imagen_command``.
#
# To invoke as top-level subcommands the console script entry is split:
# the install commands live under ``_install_group`` and are registered on
# the main group created in ``main_group()``.
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    """Return True when stdout is an interactive terminal."""
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _styled(text: str, **kwargs: Any) -> str:
    """Apply click.style only when stdout is a TTY."""
    if _is_tty():
        return click.style(text, **kwargs)
    return text


def _print_client_table(statuses: list) -> None:  # type: ignore[type-arg]
    """Print a formatted table of client detection and install status."""
    from codex_imagen._install import ClientStatus

    header = (
        f"{'#':<3}  {'Client':<16}  {'Detected':<10}  {'Installed':<10}  Config"
    )
    click.echo(header)
    click.echo("-" * 75)
    for i, s in enumerate(statuses, start=1):
        detected_str = _styled("yes", fg="green") if s.detected else _styled("no", fg="red")
        if s.detected:
            installed_str = _styled("yes", fg="green") if s.installed else _styled("no", fg="yellow")
        else:
            installed_str = "-"
        config_str = str(s.config_path) if s.config_path else "(not found)"
        click.echo(f"{i:<3}  {s.name:<16}  {detected_str:<10}  {installed_str:<10}  {config_str}")


# ---------------------------------------------------------------------------
# imagen status
# ---------------------------------------------------------------------------

@click.command(name="status")
def _status_command() -> None:
    """Show detected MCP clients and whether codex-imagen is registered."""
    from codex_imagen._install import detect_clients, skill_status_for_client

    statuses = detect_clients()
    click.echo("codex-imagen MCP installer — client status\n")
    _print_client_table(statuses)
    click.echo()
    installed_count = sum(1 for s in statuses if s.installed)
    detected_count = sum(1 for s in statuses if s.detected)
    click.echo(
        f"Detected: {detected_count}/5  |  Installed: {installed_count}/5"
    )

    # Skill status table.
    click.echo()
    click.echo("Bundled skill (imagen.md) status:")
    click.echo(f"  {'Client':<16}  {'Skill':<10}  Path")
    click.echo("  " + "-" * 60)
    for s in statuses:
        skill_installed, skill_path = skill_status_for_client(s.key)
        if skill_installed is None:
            skill_str = "-"
            path_str = "(not supported)"
        elif skill_installed:
            skill_str = _styled("installed", fg="green")
            path_str = str(skill_path)
        else:
            skill_str = _styled("missing", fg="yellow")
            path_str = str(skill_path) if skill_path else "(unknown)"
        click.echo(f"  {s.name:<16}  {skill_str:<10}  {path_str}")


# ---------------------------------------------------------------------------
# imagen setup
# ---------------------------------------------------------------------------

@click.command(name="setup")
@click.option(
    "--all",
    "install_all",
    is_flag=True,
    default=False,
    help="Install for all detected clients without prompting.",
)
@click.option(
    "--client",
    "clients",
    multiple=True,
    type=click.Choice(
        ["claude-code", "claude-desktop", "codex", "cursor", "opencode"]
    ),
    help="Install for specific client(s). Repeatable.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would happen without making any changes.",
)
def _setup_command(install_all: bool, clients: tuple[str, ...], dry_run: bool) -> None:
    """Register codex-imagen MCP in one or more AI clients.

    Run without flags for an interactive walkthrough. Use --all to install
    for every detected client, or --client NAME to target specific clients.
    """
    from codex_imagen._install import (
        ClientStatus,
        detect_clients,
        install_for_client,
        write_codex_preference_snippet,
    )

    all_statuses = detect_clients()

    if dry_run:
        click.echo("[dry-run] No files will be modified.\n")

    # Determine which client keys to install.
    if clients:
        # Explicit --client flags.
        target_keys = list(clients)
    elif install_all:
        # All detected clients.
        target_keys = [s.key for s in all_statuses if s.detected]
        if not target_keys:
            click.echo("No supported clients detected on this system.")
            sys.exit(0)
    else:
        # Interactive walkthrough.
        click.echo("codex-imagen MCP installer\n")
        _print_client_table(all_statuses)
        click.echo()

        detected_keys = [s.key for s in all_statuses if s.detected]
        if not detected_keys:
            click.echo("No supported clients detected. Nothing to install.")
            sys.exit(0)

        click.echo(
            "Install for which clients? (comma-separated numbers, 'a' for all detected, 'q' to quit):"
        )
        for i, s in enumerate(all_statuses, start=1):
            if s.detected:
                click.echo(f"  {i}  {s.name}")

        raw = click.prompt("Selection", default="a")
        if raw.strip().lower() == "q":
            click.echo("Aborted.")
            sys.exit(0)

        if raw.strip().lower() == "a":
            target_keys = detected_keys
        else:
            # Parse comma-separated numbers.
            target_keys = []
            for part in raw.split(","):
                part = part.strip()
                if not part.isdigit():
                    click.echo(f"  Skipping invalid selection: {part!r}", err=True)
                    continue
                idx = int(part) - 1
                if 0 <= idx < len(all_statuses):
                    s = all_statuses[idx]
                    if s.detected:
                        target_keys.append(s.key)
                    else:
                        click.echo(
                            f"  Skipping {s.name} — not detected on this system.",
                            err=True,
                        )
                else:
                    click.echo(f"  Number {part} out of range.", err=True)

        if not target_keys:
            click.echo("No clients selected. Aborted.")
            sys.exit(0)

        # Confirmation.
        name_map = {s.key: s.name for s in all_statuses}
        names = ", ".join(name_map.get(k, k) for k in target_keys)
        click.echo(f"\nWill install codex-imagen MCP for: {names}")
        if not click.confirm("Proceed?", default=True):
            click.echo("Aborted.")
            sys.exit(0)

    # Execute installs.
    click.echo()
    success_count = 0
    codex_selected = "codex" in target_keys
    from codex_imagen._install import install_skill_for_client
    for key in target_keys:
        ok, msg = install_for_client(key, dry_run=dry_run)
        prefix = _styled("[OK]", fg="green") if ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {prefix}  {msg}")
        if ok:
            success_count += 1
        # Also install the bundled skill alongside the MCP config.
        s_ok, s_msg = install_skill_for_client(key, dry_run=dry_run)
        s_prefix = _styled("[OK]", fg="green") if s_ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {s_prefix}  skill: {s_msg}")

    # Codex AGENTS.md preference snippet.
    if codex_selected and not install_all and not clients:
        # Interactive: ask the user.
        click.echo()
        if click.confirm(
            "Add codex-imagen preference instruction to ~/.codex/AGENTS.md "
            "(so the AI prefers codex-imagen over the built-in imagegen)?",
            default=True,
        ):
            ok, msg = write_codex_preference_snippet(dry_run=dry_run)
            prefix = _styled("[OK]", fg="green") if ok else _styled("[FAIL]", fg="red")
            click.echo(f"  {prefix}  {msg}")
    elif codex_selected and (install_all or clients):
        # Non-interactive: always write the snippet.
        ok, msg = write_codex_preference_snippet(dry_run=dry_run)
        prefix = _styled("[OK]", fg="green") if ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {prefix}  {msg}")

    click.echo()
    if dry_run:
        click.echo(f"[dry-run complete] Would have installed for {len(target_keys)} client(s).")
    else:
        click.echo(
            f"Done. Installed for {success_count}/{len(target_keys)} client(s)."
        )
        if success_count < len(target_keys):
            sys.exit(1)


# ---------------------------------------------------------------------------
# imagen uninstall
# ---------------------------------------------------------------------------

@click.command(name="uninstall")
@click.option(
    "--all",
    "uninstall_all",
    is_flag=True,
    default=False,
    help="Uninstall from all clients without prompting.",
)
@click.option(
    "--client",
    "clients",
    multiple=True,
    type=click.Choice(
        ["claude-code", "claude-desktop", "codex", "cursor", "opencode"]
    ),
    help="Uninstall from specific client(s). Repeatable.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would happen without making any changes.",
)
def _uninstall_command(
    uninstall_all: bool, clients: tuple[str, ...], dry_run: bool
) -> None:
    """Remove the codex-imagen MCP registration from AI clients."""
    from codex_imagen._install import (
        detect_clients,
        remove_codex_preference_snippet,
        uninstall_for_client,
    )

    all_statuses = detect_clients()

    if dry_run:
        click.echo("[dry-run] No files will be modified.\n")

    if clients:
        target_keys = list(clients)
    elif uninstall_all:
        target_keys = [s.key for s in all_statuses if s.installed]
        if not target_keys:
            click.echo("codex-imagen is not installed in any detected client.")
            sys.exit(0)
    else:
        # Interactive.
        click.echo("codex-imagen MCP uninstaller\n")
        _print_client_table(all_statuses)
        click.echo()

        installed_keys = [s.key for s in all_statuses if s.installed]
        if not installed_keys:
            click.echo("codex-imagen is not installed in any detected client.")
            sys.exit(0)

        click.echo(
            "Uninstall from which clients? (comma-separated numbers, 'a' for all installed, 'q' to quit):"
        )
        for i, s in enumerate(all_statuses, start=1):
            if s.installed:
                click.echo(f"  {i}  {s.name}")

        raw = click.prompt("Selection", default="a")
        if raw.strip().lower() == "q":
            click.echo("Aborted.")
            sys.exit(0)

        if raw.strip().lower() == "a":
            target_keys = installed_keys
        else:
            target_keys = []
            for part in raw.split(","):
                part = part.strip()
                if not part.isdigit():
                    continue
                idx = int(part) - 1
                if 0 <= idx < len(all_statuses):
                    target_keys.append(all_statuses[idx].key)

        if not target_keys:
            click.echo("No clients selected. Aborted.")
            sys.exit(0)

        name_map = {s.key: s.name for s in all_statuses}
        names = ", ".join(name_map.get(k, k) for k in target_keys)
        click.echo(f"\nWill remove codex-imagen MCP from: {names}")
        if not click.confirm("Proceed?", default=True):
            click.echo("Aborted.")
            sys.exit(0)

    click.echo()
    success_count = 0
    codex_selected = "codex" in target_keys
    from codex_imagen._install import uninstall_skill_for_client
    for key in target_keys:
        ok, msg = uninstall_for_client(key, dry_run=dry_run)
        prefix = _styled("[OK]", fg="green") if ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {prefix}  {msg}")
        if ok:
            success_count += 1
        # Also remove the bundled skill.
        s_ok, s_msg = uninstall_skill_for_client(key, dry_run=dry_run)
        s_prefix = _styled("[OK]", fg="green") if s_ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {s_prefix}  skill: {s_msg}")

    if codex_selected:
        ok, msg = remove_codex_preference_snippet(dry_run=dry_run)
        prefix = _styled("[OK]", fg="green") if ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {prefix}  {msg}")

    click.echo()
    if dry_run:
        click.echo(f"[dry-run complete] Would have uninstalled from {len(target_keys)} client(s).")
    else:
        click.echo(
            f"Done. Removed from {success_count}/{len(target_keys)} client(s)."
        )
        if success_count < len(target_keys):
            sys.exit(1)


# ---------------------------------------------------------------------------
# Top-level group that merges image-generation + installer subcommands
# ---------------------------------------------------------------------------
# Strategy: make ``imagen`` a Click group with invoke_without_command=True.
# When called without a subcommand it falls through to ``_imagen_command``
# (the generator). Subcommands ``setup``, ``uninstall``, ``status`` are
# registered explicitly.
#
# ``main()`` above remains unchanged and still points to ``_imagen_command``
# for full backward compatibility when tests import it directly.
# The *new* console-script entry point is ``main_group()``.
# ---------------------------------------------------------------------------

@click.group(
    name="imagen",
    invoke_without_command=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
        "max_content_width": 100,
    },
)
@click.pass_context
def _imagen_group(ctx: click.Context) -> None:
    """imagen — Codex-OAuth image generation toolkit.

    Run without a subcommand to generate images. Subcommands:

    \b
      setup      Register codex-imagen MCP in your AI clients
      uninstall  Remove the MCP registration
      status     Show which clients have codex-imagen installed

    Examples:

    \b
      imagen "a ceramic mug"          # generate an image
      imagen setup                    # interactive MCP installer
      imagen setup --all              # install everywhere without prompts
      imagen status                   # show client status
      imagen uninstall --all          # remove everywhere
    """
    # When no subcommand was given, we fall through to the generator. This
    # makes ``imagen "my prompt"`` still work as before.
    if ctx.invoked_subcommand is None:
        # Re-invoke with the raw args stripped of the group wrapper.
        # Click has already consumed the group name; ctx.args holds
        # the remaining args. We forward to _imagen_command.
        pass  # The standalone-mode invoke below handles this.


_imagen_group.add_command(_setup_command)
_imagen_group.add_command(_uninstall_command)
_imagen_group.add_command(_status_command)


def main_group() -> None:
    """Console-script entry point for the top-level ``imagen`` group.

    Supports both image generation (``imagen "prompt" ...``) and installer
    subcommands (``imagen setup / uninstall / status``).

    When the first argument is not a known subcommand, all arguments are
    forwarded to the legacy ``_imagen_command`` generator so existing usage
    continues to work without change.
    """
    import sys as _sys

    # Detect if the first non-option argument is a known subcommand.
    _known_subcommands = {"setup", "uninstall", "status"}
    args = _sys.argv[1:]

    # Walk the args to find the first positional (non-flag) argument.
    first_positional: str | None = None
    for arg in args:
        if not arg.startswith("-"):
            first_positional = arg
            break

    if first_positional in _known_subcommands:
        # Dispatch to the group (subcommand routing).
        _imagen_group.main()
    else:
        # No subcommand — forward to the legacy generator command.
        _imagen_command.main()
