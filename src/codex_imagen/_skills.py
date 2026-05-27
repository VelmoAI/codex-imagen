"""codex_imagen._skills — file-path skill loader.

Purpose
-------
Skills are plain Markdown files that users (or AI agents calling imagen)
reference by file path via the ``skills=`` parameter. This module reads
those files, strips optional YAML frontmatter, concatenates the bodies
with a sentinel separator, and returns a hash for drift detection.

The concatenated body is later passed to :func:`codex_imagen._prompts.build`
as ``skills_body``. This module is intentionally bridge-agnostic — it does
not import ``_bridge``, ``_prompts``, ``_chroma``, or ``codex_image_gen``.
Pure stdlib, pure functions of (paths -> text).

Frontmatter rules (mini-parser, not full YAML)
----------------------------------------------
* A file has frontmatter iff its very first three characters are ``---``
  followed by a newline.
* The block ends at the next line whose stripped content is exactly
  ``---``.
* Between the delimiters, each non-empty / non-comment line is parsed as
  ``key: value`` where:

  - ``key`` is alphanumeric plus ``_`` and ``-``.
  - ``value`` is the rest of the line after the first ``:``, stripped of
    whitespace; surrounding single or double quotes are removed.
  - Lines starting with ``#`` (after lstrip) are comments and ignored.
  - Lines without a top-level ``:`` are skipped.

* Multi-line / nested / list values are **not** supported. This is
  deliberate: skills only need ``name`` and ``description`` today.
* If no closing ``---`` is found, the file is treated as pure body
  (frontmatter dict is empty).
* The body starts at the line *after* the closing ``---``; leading blank
  lines are stripped from it.

Where skills live
-----------------
:func:`discover_skills` searches, in order:

1. ``./skills/``               (project-local)
2. ``./.forge/skills/``        (project-local, hidden)
3. ``~/.codex-imagen/skills/``
4. ``~/.claude/skills/``       (Claude Code's skill dir, if present)
5. Each path in ``extra_dirs`` (caller-supplied)

A "skill file" is either a top-level ``.md`` file in one of those dirs or
``SKILL.md`` (any case) inside a first-level subdirectory. Missing
directories are silently skipped — discovery never raises.

Drift detection
---------------
:func:`hash_body` returns a sha256 hexdigest of arbitrary text.
:class:`LoadedSkill` carries a hash of its own body and
:class:`SkillBundle` carries a hash of the joined ``combined_body`` so
callers can detect when on-disk skill content has changed between runs
(used by the ``branded-parallel`` mode to keep anchor + variants in sync).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "LoadedSkill",
    "SkillBundle",
    "discover_skills",
    "hash_body",
    "load_skill",
    "load_skills",
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedSkill:
    """Parsed metadata + body of one skill file.

    Attributes:
        path: Absolute, resolved filesystem path of the skill file.
        name: ``name`` from frontmatter if provided, else the file stem.
        description: ``description`` from frontmatter, or ``None``.
        body: Markdown content after frontmatter stripping, with leading
            blank lines removed.
        frontmatter: Parsed ``{key: value}`` pairs (string values only).
        sha256: Hex digest of ``body`` (UTF-8). Used for drift detection.
    """

    path: Path
    name: str
    description: str | None
    body: str
    frontmatter: dict[str, str] = field(default_factory=dict)
    sha256: str = ""


@dataclass(frozen=True)
class SkillBundle:
    """Result of loading multiple skill files.

    Attributes:
        skills: Tuple of :class:`LoadedSkill` in the order they were
            requested (and successfully loaded).
        combined_body: All skill bodies joined with ``"\\n\\n---\\n\\n"``.
            Empty string when no skills were loaded.
        combined_hash: sha256 hex digest of ``combined_body`` — a single
            stable token for drift detection across a whole bundle.
        warnings: Non-fatal diagnostics (e.g. "skill file not found:
            ./missing.md"). Empty tuple when nothing is worth flagging.
    """

    skills: tuple[LoadedSkill, ...]
    combined_body: str
    combined_hash: str
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def hash_body(text: str) -> str:
    """Return the sha256 hex digest of ``text`` encoded as UTF-8.

    Used for drift detection on skill bodies and the combined bundle.

    Args:
        text: Input string (may be empty — that case is well-defined and
            produces sha256 of the empty byte string).

    Returns:
        Lowercase hexadecimal sha256 digest (64 chars).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Frontmatter parsing — the trickiest bit, hence the comment density
# ---------------------------------------------------------------------------

# Characters allowed in a frontmatter key. Mirrors the spec "identifier"
# definition: alphanumeric plus underscore and dash. Anything else makes
# the line ineligible to be parsed as a key/value pair.
_KEY_ALLOWED_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _is_valid_key(candidate: str) -> bool:
    """Return True iff ``candidate`` is a non-empty identifier we accept."""
    if not candidate:
        return False
    return all(ch in _KEY_ALLOWED_CHARS for ch in candidate)


def _strip_wrapping_quotes(value: str) -> str:
    """Remove a single layer of matching wrapping quotes if present.

    ``"hello"`` -> ``hello``. ``'hello'`` -> ``hello``. ``"hello`` is left
    alone (mismatched). Inner quotes are preserved.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``text`` into (frontmatter_dict, body_text).

    See the module docstring for the exact rules. This function is
    deliberately tolerant: bad lines inside frontmatter are skipped, not
    fatal, because skill authors hand-edit these files and the cost of a
    parse error blocking a render is high.

    Returns ``({}, original_text_with_leading_blanks_stripped)`` if there
    is no frontmatter or if it's malformed (e.g. missing closing
    delimiter).
    """
    # Frontmatter must START at the very first character. We check the
    # literal prefix rather than splitting on lines so a file that starts
    # with whitespace + "---" is correctly treated as having NO
    # frontmatter (matches Jekyll / Hugo / Astro conventions).
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, _strip_leading_blank_lines(text)

    # Normalize line endings for the scan, but remember to use the
    # original text when slicing the body so we don't silently change
    # the user's CRLFs.
    lines = text.splitlines(keepends=True)

    # The first line is "---" (with optional CR). Find the closing "---".
    close_idx: int | None = None
    for idx in range(1, len(lines)):
        # rstrip removes the trailing newline AND any CR; we then
        # compare the actual content. Lines like "  ---" (leading
        # whitespace) are not treated as a closing delimiter — same
        # convention as common static-site generators.
        if lines[idx].rstrip("\r\n") == "---":
            close_idx = idx
            break

    if close_idx is None:
        # No closing delimiter: per spec, treat the whole file as body
        # (no frontmatter). This is the safe fallback — alternative
        # would be to error, but a half-edited file shouldn't break
        # the world.
        return {}, _strip_leading_blank_lines(text)

    # Parse the lines strictly between the two delimiters.
    frontmatter: dict[str, str] = {}
    for raw_line in lines[1:close_idx]:
        line = raw_line.rstrip("\r\n")
        stripped = line.lstrip()

        # Skip blank lines and comment lines.
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue

        # Require a top-level ":" — anything else is malformed and
        # silently skipped.
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()

        if not _is_valid_key(key):
            # Reject keys containing spaces, dots, brackets, etc. This
            # also rejects deeply-nested YAML which we explicitly do
            # not support.
            continue

        value = value.strip()
        value = _strip_wrapping_quotes(value)
        frontmatter[key] = value

    # Body is everything after the closing delimiter. Use the original
    # `lines` join so we preserve the user's exact line endings.
    body = "".join(lines[close_idx + 1:])
    return frontmatter, _strip_leading_blank_lines(body)


def _strip_leading_blank_lines(text: str) -> str:
    """Drop leading lines that contain only whitespace.

    Used after frontmatter stripping so the body doesn't start with a
    wodge of empty lines that the original file used as visual
    separation.
    """
    if not text:
        return text
    lines = text.splitlines(keepends=True)
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    return "".join(lines[idx:])


# ---------------------------------------------------------------------------
# Single-file load
# ---------------------------------------------------------------------------


def _resolve_path(path: str | Path) -> Path:
    """Expand ``~`` and resolve ``path`` to an absolute Path.

    We don't call ``.resolve(strict=True)`` here because we want a
    consistent FileNotFoundError shape from :func:`load_skill` rather
    than the OS-specific message that strict-resolve would emit on a
    missing path.
    """
    return Path(path).expanduser().resolve()


def load_skill(path: str | Path) -> LoadedSkill:
    """Load a single skill file from disk.

    Args:
        path: Filesystem path to a Markdown skill file. May contain
            ``~`` (expanded to the user's home directory). May be a
            relative path (resolved against the current working dir).

    Returns:
        A :class:`LoadedSkill` with parsed frontmatter, body, and hash.

    Raises:
        FileNotFoundError: If ``path`` does not resolve to an existing
            file. The message includes the resolved path to ease
            debugging.
        OSError: On other read failures (permission denied, decode
            error, etc.). UTF-8 is required — non-UTF-8 files surface
            as ``UnicodeDecodeError`` which is a subclass of ``OSError``
            indirectly via ``ValueError`` in newer Pythons, so callers
            relying on ``except OSError`` should be aware. We don't
            translate it here because the original exception carries
            byte-offset detail that's useful for the author.
        IsADirectoryError: If ``path`` points to a directory.
    """
    resolved = _resolve_path(path)

    if not resolved.exists():
        raise FileNotFoundError(
            f"Skill file not found: {resolved} "
            f"(original argument: {path!r})"
        )
    if resolved.is_dir():
        raise IsADirectoryError(
            f"Skill path is a directory, not a file: {resolved}. "
            f"Point at the SKILL.md inside it."
        )

    text = resolved.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)

    name = frontmatter.get("name") or resolved.stem
    description = frontmatter.get("description")  # already None if absent

    return LoadedSkill(
        path=resolved,
        name=name,
        description=description,
        body=body,
        frontmatter=frontmatter,
        sha256=hash_body(body),
    )


# ---------------------------------------------------------------------------
# Bundle load
# ---------------------------------------------------------------------------


# Sentinel that separates skill bodies inside the combined block. Kept as
# a module-level constant so tests can import it if they need to.
_SKILL_SEPARATOR = "\n\n---\n\n"


def load_skills(
    paths: list[str | Path],
    *,
    strict: bool = False,
) -> SkillBundle:
    """Load multiple skill files and bundle them into a single block.

    Args:
        paths: List of file paths (str or Path). ``~`` is expanded per
            entry. An empty list returns an empty bundle (no error).
        strict: If True, a missing file raises :class:`FileNotFoundError`
            immediately. If False (default), the missing file is
            recorded as a warning and skipped — useful for agent
            workflows where some skills are optional.

    Returns:
        A :class:`SkillBundle` containing the successfully-loaded skills,
        the concatenated body, the bundle hash, and any warnings.

    Raises:
        FileNotFoundError: Only when ``strict=True`` and a path is
            missing. Other OS errors always propagate regardless of
            ``strict``.
    """
    skills: list[LoadedSkill] = []
    warnings: list[str] = []

    for raw in paths:
        try:
            skills.append(load_skill(raw))
        except FileNotFoundError as exc:
            if strict:
                raise
            # Non-strict: record a warning and keep going. We deliberately
            # don't include the resolved path here — the original argument
            # is what the caller actually typed and is more debuggable.
            warnings.append(f"skill file not found: {raw} ({exc})")

    # Build the combined body. Joining an empty list yields "", which
    # hashes to the well-known sha256 of the empty string — that's
    # intentional and stable.
    combined_body = _SKILL_SEPARATOR.join(s.body for s in skills)

    return SkillBundle(
        skills=tuple(skills),
        combined_body=combined_body,
        combined_hash=hash_body(combined_body),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _candidate_dirs(extra_dirs: list[str | Path] | None) -> list[Path]:
    """Return the ordered list of directories to scan.

    Order matters: later entries override earlier ones if filenames
    collide (the caller-supplied extras are last, so they win).
    """
    dirs: list[Path] = [
        Path.cwd() / "skills",
        Path.cwd() / ".imagen" / "skills",
        Path.home() / ".codex-imagen" / "skills",
        Path.home() / ".claude" / "skills",
    ]
    if extra_dirs:
        for extra in extra_dirs:
            dirs.append(Path(extra).expanduser())
    return dirs


def _scan_directory(directory: Path) -> list[Path]:
    """Return skill files inside ``directory``.

    Two layouts are recognized:

    * Flat: any ``*.md`` directly inside ``directory``.
    * Nested: any first-level subdirectory containing a ``SKILL.md``
      (case-insensitive on the stem).

    Hidden directories (starting with ``.``) are skipped. Errors
    iterating the directory are swallowed — discovery is best-effort.
    """
    results: list[Path] = []
    try:
        entries = sorted(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return results

    for entry in entries:
        try:
            if entry.is_file() and entry.suffix.lower() == ".md":
                # Flat layout: skills/foo.md
                results.append(entry.resolve())
            elif entry.is_dir() and not entry.name.startswith("."):
                # Nested layout: skills/foo/SKILL.md (any case).
                for child in sorted(entry.iterdir()):
                    if (
                        child.is_file()
                        and child.stem.upper() == "SKILL"
                        and child.suffix.lower() == ".md"
                    ):
                        results.append(child.resolve())
                        # Only the first match per subdirectory counts —
                        # SKILL.md is the canonical entry point.
                        break
        except (FileNotFoundError, PermissionError):
            # A file/dir that disappeared mid-scan or that we can't
            # stat is silently ignored; discovery shouldn't fail just
            # because one entry is unreadable.
            continue

    return results


def discover_skills(
    *,
    extra_dirs: list[str | Path] | None = None,
) -> list[Path]:
    """Find candidate skill files in the standard locations.

    Args:
        extra_dirs: Additional directories to scan after the four
            built-in locations. Each may contain ``~``. Files found in
            extras override files of the same name from earlier
            directories.

    Returns:
        A sorted list of absolute paths to skill files. Empty list when
        nothing is found. Never raises — missing directories are
        silently skipped, as are unreadable entries.
    """
    # Discover, but keep dedup semantics: later dirs win on filename
    # collisions. We track by the file's stem to match the spec wording
    # ("duplicate filenames"); resolving full paths would consider
    # ``./skills/foo.md`` and ``~/.claude/skills/foo.md`` distinct, which
    # is the opposite of what the spec asks for.
    by_stem: dict[str, Path] = {}

    for directory in _candidate_dirs(extra_dirs):
        for skill_path in _scan_directory(directory):
            # Use a key that uniquely identifies a skill: SKILL.md inside
            # different parent dirs are different skills, so we include
            # the parent dir name when it's a nested layout.
            if skill_path.stem.upper() == "SKILL":
                key = skill_path.parent.name
            else:
                key = skill_path.stem
            by_stem[key] = skill_path

    # Sort by path for stable output. Sorting by key would tie SKILL.md
    # entries together; sorting by full path is more predictable.
    return sorted(by_stem.values())
