"""codex_imagen.mcp_server — stdio MCP server exposing a single ``imagen`` tool.

This module wires the :func:`codex_imagen.imagen` SDK entry point into a
stdio-based Model Context Protocol server so AI clients (Claude Desktop,
Cursor, etc.) can invoke image generation as a structured tool call.

Design notes
------------
* **One tool, one surface.** We expose exactly one tool named ``imagen``.
  Its JSON Schema mirrors :class:`codex_imagen.ImagenOptions` field-for-field,
  including the enum constraints for ``mode`` / ``batch_mode`` /
  ``output_format``. The schema is the contract — adding a knob to
  ``ImagenOptions`` REQUIRES adding it here, and a drift test pins this.

* **Errors are results, not exceptions.** Per SPEC.md the ``imagen`` contract
  guarantees ``ok=False`` + actionable hint on failure rather than crashing.
  We honor that at the MCP layer too: ``ValueError`` / ``TypeError`` raised
  by :class:`codex_imagen.ImagenOptions` (bad args) and by :func:`imagen`
  itself are caught and surfaced as JSON ``{ok: false, error, error_type}``
  in the tool result. The MCP client always gets a successful tool call
  with a structured payload it can introspect — never a protocol-level
  error for an input-validation problem.

* **No stdout pollution.** stdio MCP uses stdin/stdout for the JSON-RPC
  protocol itself. We never ``print`` anything. The only stderr usage is
  a single line at startup announcing the server is ready (suppressed in
  the MCP runtime by the client). This keeps the protocol stream clean.

Entry point: ``codex-imagen-mcp`` console script (see ``pyproject.toml``).
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from codex_imagen import __version__, imagen
from codex_imagen.core import ImagenResult


# ---------------------------------------------------------------------------
# Tool description + input schema
# ---------------------------------------------------------------------------

# The description is verbatim from SPEC.md lines 486-513. AI clients display
# this to help the model pick the right batch_mode / reasoning mode. Keep it
# in sync with the SPEC; do not paraphrase. Bullets use Unicode middle dot
# (U+2022) to match the SPEC exactly.
TOOL_DESCRIPTION = (
    "imagen — Codex image generator. Generate, batch, chain, or branded "
    "sets of images.\n"
    "\n"
    "PROMPT: string for single image, dict for structured Codex labeled-spec,\n"
    "        or array of either for multiple images.\n"
    "\n"
    "MODE SELECTION GUIDE (set batch_mode explicitly or let auto pick):\n"
    "  • single             — one prompt → one image\n"
    "  • parallel           — N unrelated prompts → N independent "
    "images (fast)\n"
    "  • variants           — 1 prompt + count=N → N stylistic "
    "variations\n"
    "  • chain              — N prompts → sequential narrative "
    "(each refs prior)\n"
    "  • branded-parallel   — N prompts + anchor → all match "
    "anchor's style (best for website sections, brand sets, hero+features)\n"
    "\n"
    "REASONING MODE (controls how aggressively gpt-5.5 polishes prompt):\n"
    "  • raw      — pass prompt 1:1, no skills allowed\n"
    "  • medium   — default when skills present, integrates them\n"
    "  • high     — premium planning for complex briefs\n"
    "  • max      — maximum reasoning for very complex multi-subject "
    "scenes\n"
    "\n"
    "TRANSPARENCY: set transparent=true to get PNG with alpha via chroma-key "
    "pipeline.\n"
    "              (Native transparent background is not available via Codex "
    "OAuth.)\n"
    "\n"
    "SKILLS: pass file paths to .md skill files. imagen reads them and merges "
    "into\n"
    "        instructions. Works with Claude/Codex skill files directly.\n"
    "\n"
    "Returns ImagenResult JSON with ok flag, images list, manifest path, "
    "health.\n"
    "On failure: ok=false + hint instead of crashing."
)


# Per-field descriptions are short by design — JSON Schema descriptions are
# surfaced to the AI alongside the main TOOL_DESCRIPTION, so we keep them
# tight and only repeat what a calling model genuinely needs to know.
TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "description": (
                "Image prompt: string (single image), structured Codex "
                "labeled-spec dict, or array of either for multiple images."
            ),
            "oneOf": [
                {"type": "string"},
                {"type": "object"},
                {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "object"},
                        ]
                    },
                },
            ],
        },
        "output_dir": {
            "description": "Directory where generated images are written.",
            "type": "string",
            "default": "./out",
        },
        "mode": {
            "description": (
                "Reasoning mode for prompt polishing. 'auto' resolves based "
                "on prompt shape, skills, and transparency."
            ),
            "type": "string",
            "enum": ["auto", "raw", "medium", "high", "max"],
            "default": "auto",
        },
        "batch_mode": {
            "description": (
                "Batch orchestration strategy. 'auto' picks one based on "
                "the prompt shape and anchor/count hints."
            ),
            "type": "string",
            "enum": [
                "auto",
                "single",
                "parallel",
                "variants",
                "chain",
                "branded-parallel",
            ],
            "default": "auto",
        },
        "chain_mode": {
            "description": (
                "How chained images reference each other. Only used when "
                "batch_mode resolves to 'chain'."
            ),
            "type": "string",
            "default": "anchor+previous",
        },
        "anchor": {
            "description": (
                "Anchor prompt/spec used by branded-parallel and chain "
                "modes to lock visual style."
            ),
            "type": ["string", "object", "null"],
        },
        "count": {
            "description": (
                "Number of images to generate. Meaningful in 'variants' "
                "mode; ignored when prompt is already a list."
            ),
            "type": "integer",
            "minimum": 1,
            "default": 1,
        },
        "references": {
            "description": (
                "Reference image paths attached to every call (e.g. brand "
                "examples, style swatches)."
            ),
            "type": "array",
            "items": {"type": "string"},
            "default": [],
        },
        "skills": {
            "description": (
                "Paths to .md skill files. Bodies are loaded and merged "
                "into the prompt builder. Ignored in mode='raw'."
            ),
            "type": "array",
            "items": {"type": "string"},
            "default": [],
        },
        "mask": {
            "description": "Optional mask image path for inpainting.",
            "type": ["string", "null"],
        },
        "extra_instructions": {
            "description": (
                "Free-text instructions appended to the prompt builder's "
                "output. Use sparingly."
            ),
            "type": ["string", "null"],
        },
        "size": {
            "description": (
                "Image size string (e.g. '1024x1024') or 'auto' to let "
                "the model decide."
            ),
            "type": "string",
            "default": "auto",
        },
        "output_format": {
            "description": "File format for the saved image.",
            "type": "string",
            "enum": ["png", "jpeg", "webp"],
            "default": "png",
        },
        "transparent": {
            "description": (
                "Run the chroma-key pipeline to produce a PNG with alpha. "
                "Requires Pillow."
            ),
            "type": "boolean",
            "default": False,
        },
        "chroma_key": {
            "description": "Hex color used as the chroma-key backdrop.",
            "type": "string",
            "default": "#FF00FF",
        },
        "chroma_tolerance": {
            "description": (
                "Chroma-key tolerance (0-100). Higher = more pixels treated "
                "as background."
            ),
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "default": 40,
        },
        "chroma_despill": {
            "description": "Run despill pass to neutralize key-color tint at edges.",
            "type": "boolean",
            "default": True,
        },
        "chroma_edge_erode_px": {
            "description": (
                "Pixels to erode the alpha mask before feathering. Eliminates "
                "pink fringe on soft subject edges. 0 disables; default 1."
            ),
            "type": "integer",
            "minimum": 0,
            "default": 1,
        },
        "enhance_prompt": {
            "description": (
                "Ask the prompt builder for an extra polish pass. Costs "
                "a bit of latency."
            ),
            "type": "boolean",
            "default": False,
        },
        "vars": {
            "description": "Variable substitutions for templated prompts.",
            "type": "object",
            "default": {},
        },
        "parallel": {
            "description": (
                "Max concurrent bridge calls. Higher = faster batches, "
                "more rate-limit risk."
            ),
            "type": "integer",
            "minimum": 1,
            "default": 2,
        },
        "wall_clock_timeout": {
            "description": (
                "Maximum wall-clock seconds for one generation attempt. "
                "Guards against unbounded SSE stream blocks. Default 240s."
            ),
            "type": "number",
            "exclusiveMinimum": 0,
            "default": 240.0,
        },
        "advanced": {
            "description": (
                "Escape hatch: extra kwargs forwarded to the codex-image-gen "
                "bridge call. Power-user only."
            ),
            "type": "object",
            "default": {},
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------


def _path_to_str(value: Any) -> Any:
    """Render a :class:`pathlib.Path` as an absolute string.

    ``Path.resolve()`` can fail on Windows for paths that don't exist yet
    (e.g. when a generation failed before writing). Fall back to
    ``absolute()`` in that case.
    """
    if isinstance(value, Path):
        try:
            return str(value.resolve())
        except OSError:
            return str(value.absolute())
    return value


def _normalize(value: Any) -> Any:
    """Recursively replace :class:`Path` with strings and tuples with lists."""
    if isinstance(value, Path):
        return _path_to_str(value)
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def _result_to_dict(result: ImagenResult) -> dict[str, Any]:
    """Convert :class:`ImagenResult` to a JSON-friendly dict.

    Parallels :func:`codex_imagen.cli._result_to_json_dict`. We keep a small
    duplicate here (rather than importing from ``cli.py``) because the CLI
    helper is private to that module and importing it would couple two
    user-facing surfaces.
    """
    return {
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


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def _build_error_payload(exc: BaseException) -> dict[str, Any]:
    """Build the ``ok=false`` payload returned when imagen() raises.

    SPEC mandates ``ok=false + hint`` over an exception. We classify the
    exception type so the calling AI can decide whether to retry with
    different args (TypeError/ValueError = bad input) or surface to the
    user (anything else = unexpected).
    """
    return {
        "ok": False,
        "error": str(exc) or exc.__class__.__name__,
        "error_type": exc.__class__.__name__,
    }


def _call_imagen_sync(arguments: dict[str, Any]) -> str:
    """Run :func:`imagen` defensively and return a JSON string.

    Wraps every expected failure into the ``ok=false`` shape. We catch
    :class:`ValueError` and :class:`TypeError` explicitly because those
    are the documented programming-error exits from ``ImagenOptions``
    validation. Other exceptions (e.g. ``KeyboardInterrupt``) are allowed
    to propagate so the MCP runtime can shut down cleanly.
    """
    if not isinstance(arguments, dict):
        # The MCP SDK should always hand us a dict, but be defensive.
        return json.dumps(
            _build_error_payload(
                TypeError(
                    f"arguments must be an object, got {type(arguments).__name__}"
                )
            ),
            indent=2,
        )

    try:
        result = imagen(**arguments)
    except (ValueError, TypeError) as exc:
        return json.dumps(_build_error_payload(exc), indent=2)

    return json.dumps(_result_to_dict(result), indent=2)


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------

# Module-level Server instance. Handlers are registered at import time so
# tests can introspect ``app.request_handlers``. The server only starts
# reading stdin when :func:`_serve` is invoked.
app: Server = Server("codex-imagen", version=__version__)


@app.list_tools()
async def _list_tools() -> list[Tool]:
    """Return the single-tool catalog for ``tools/list`` requests."""
    return [
        Tool(
            name="imagen",
            description=TOOL_DESCRIPTION,
            inputSchema=TOOL_INPUT_SCHEMA,
        )
    ]


@app.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle ``tools/call`` requests.

    Only ``imagen`` is recognized. Anything else raises ``ValueError`` which
    the MCP framework converts into a JSON-RPC error response — that's the
    right shape for "unknown tool name" because it indicates a client bug,
    not a user-facing imagen() failure.
    """
    if name != "imagen":
        raise ValueError(f"unknown tool: {name!r} (only 'imagen' is exposed)")

    payload = _call_imagen_sync(arguments or {})
    return [TextContent(type="text", text=payload)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _serve() -> None:
    """Run the stdio server loop until the client disconnects."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def main() -> int:
    """Console-script entry point for ``codex-imagen-mcp``.

    Returns:
        Process exit code. ``0`` on a clean shutdown, ``1`` if the event
        loop itself crashes with an unexpected exception (the MCP runtime
        normally handles per-request errors internally).
    """
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # pragma: no cover - defensive top-level catch
        sys.stderr.write(f"codex-imagen-mcp: fatal error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
