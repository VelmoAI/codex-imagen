"""codex_imagen._manifest — JSONL run log writer.

Purpose
-------
After every successful bridge call, ``core.forge()`` appends one line to a
``manifest.jsonl`` inside the run's output directory. The manifest captures
*everything* a downstream agent or human needs to reproduce or audit the
run: the prompts, the resolved mode, references used, bridge IDs, timing,
and any non-fatal warnings.

The format is JSON Lines (one JSON object per line, newline-terminated).
This lets users ``tail -f`` the manifest in flight and lets agents parse
the file incrementally without needing the run to finish.

This module is intentionally minimal — just the writer. Schema concerns
live in ``SPEC.md`` ("Manifest format") and in :func:`core._build_manifest_entry`.

Failure policy
--------------
Manifest writes are *non-fatal*. If the disk is full or the parent
directory is read-only, the orchestrator catches :class:`OSError`,
turns it into a warning on ``ForgeResult.warnings``, and lets the run
finish. We never crash a successful generation because of a logging
glitch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["append"]


def append(manifest_path: Path, entry: dict[str, Any]) -> None:
    """Append a single JSON object to ``manifest_path`` as one line.

    Creates the parent directory if needed. Writes UTF-8 with
    ``ensure_ascii=False`` so non-ASCII prompts (German umlauts, emoji
    in skill bodies) round-trip without lossy escapes. Each line ends
    with ``\\n`` so the file is a valid JSONL stream.

    Args:
        manifest_path: Target file. Will be created on first write,
            appended to on every subsequent call. Parent directories
            are created automatically.
        entry: A plain dict. Must be JSON-serializable. ``Path`` values
            are NOT auto-coerced — the caller should stringify them
            before calling here (keeps this writer dependency-free).

    Raises:
        OSError: On filesystem failures (disk full, permission denied,
            parent path is a file not a dir, etc.). Callers should
            generally catch this and demote to a warning.
        TypeError: If ``entry`` contains values that ``json.dumps``
            can't handle. We surface this so a buggy entry doesn't
            silently swallow an audit record.
    """
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    # Open in append + text mode. JSON Lines convention: one object,
    # one line, newline-terminated. Using ``ensure_ascii=False`` so
    # Unicode survives literally; downstream parsers all handle UTF-8.
    line = json.dumps(entry, ensure_ascii=False)
    # Single write call: on POSIX, O_APPEND writes are atomic per-syscall
    # for records well under PIPE_BUF, so this prevents a concurrent
    # writer from interleaving a record between the JSON payload and the
    # terminating newline (which would produce malformed JSONL).
    with manifest_path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(line + "\n")
