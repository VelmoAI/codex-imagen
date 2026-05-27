"""Tests for :mod:`codex_imagen.mcp_server`.

Strategy
--------
* Call the registered handlers directly via the public helpers
  (``_list_tools``, ``_call_tool``). We do NOT spin up the stdio loop —
  that would require an mcp client and pipes; the handlers themselves
  contain all the logic worth testing.
* :func:`asyncio.run` is used inline so we don't depend on the
  pytest-asyncio plugin's per-test fixtures (it's available but using
  ``asyncio.run`` keeps these tests dead-simple).
* :func:`forge` is monkey-patched at the module level in mcp_server so
  no real bridge calls happen.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from codex_imagen import mcp_server
from codex_imagen.core import (
    ForgeHealth,
    ForgeImage,
    ForgeOptions,
    ForgeResult,
    _VALID_BATCH_MODES,
    _VALID_MODES,
    _VALID_OUTPUT_FORMATS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    """Run an awaitable synchronously inside a test."""
    return asyncio.run(coro)


def _make_result(ok: bool = True) -> ForgeResult:
    """Build a minimal :class:`ForgeResult` with one image for tests."""
    img = ForgeImage(
        index=0,
        path=Path("./out/img_0.png"),
        bytes=1234,
        mime_type="image/png",
        response_id="resp_x",
        call_id="call_x",
        revised_prompt=None,
        original_prompt="cat",
        final_prompt="cat",
        transparent=False,
        references_used=(),
        raw_png_path=None,
    )
    return ForgeResult(
        ok=ok,
        mode="medium",
        batch_mode="single",
        images=(img,) if ok else (),
        manifest_path=Path("./out/manifest.jsonl") if ok else None,
        elapsed_ms=42,
        health=ForgeHealth(ok=True, codex_image_gen_available=True),
        error=None if ok else "synthetic failure",
        warnings=(),
    )


# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------


def test_list_tools_returns_one_tool_named_forge() -> None:
    tools = _run(mcp_server._list_tools())
    assert isinstance(tools, list)
    assert len(tools) == 1
    assert tools[0].name == "forge"


def test_tool_description_contains_mode_guide() -> None:
    """Every batch mode must appear in the description so the AI can pick."""
    desc = mcp_server.TOOL_DESCRIPTION
    for mode in ("single", "parallel", "variants", "chain", "branded-parallel"):
        assert mode in desc, f"batch mode {mode!r} missing from description"
    for reasoning in ("raw", "medium", "high", "max"):
        assert reasoning in desc, f"reasoning mode {reasoning!r} missing"


# ---------------------------------------------------------------------------
# Input schema drift pins
# ---------------------------------------------------------------------------


def test_input_schema_has_all_forge_options_fields() -> None:
    """Every ForgeOptions field must be exposed via the MCP schema.

    This is a drift test: adding a knob to ForgeOptions without exposing
    it here is the kind of silent omission that breaks AI clients in
    confusing ways.
    """
    props = mcp_server.TOOL_INPUT_SCHEMA["properties"]
    for field in dataclasses.fields(ForgeOptions):
        assert field.name in props, (
            f"ForgeOptions field {field.name!r} missing from MCP input schema"
        )


def test_input_schema_requires_prompt() -> None:
    assert mcp_server.TOOL_INPUT_SCHEMA["required"] == ["prompt"]


def test_input_schema_enums_match_validation() -> None:
    """Enum lists in the schema must match core.py's _VALID_* constants."""
    props = mcp_server.TOOL_INPUT_SCHEMA["properties"]
    assert tuple(props["mode"]["enum"]) == _VALID_MODES
    assert tuple(props["batch_mode"]["enum"]) == _VALID_BATCH_MODES
    assert tuple(props["output_format"]["enum"]) == _VALID_OUTPUT_FORMATS


def test_input_schema_each_property_has_description() -> None:
    """AI clients surface descriptions to the model — every prop needs one."""
    for name, spec in mcp_server.TOOL_INPUT_SCHEMA["properties"].items():
        assert "description" in spec, f"property {name!r} missing description"


# ---------------------------------------------------------------------------
# call_tool dispatch
# ---------------------------------------------------------------------------


def test_call_tool_invokes_forge_with_args(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_forge(**kwargs: Any) -> ForgeResult:
        captured.update(kwargs)
        return _make_result(ok=True)

    monkeypatch.setattr(mcp_server, "forge", fake_forge)

    _run(mcp_server._call_tool("forge", {"prompt": "cat", "count": 2}))
    assert captured == {"prompt": "cat", "count": 2}


def test_call_tool_returns_json_text_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "forge", lambda **kw: _make_result(ok=True))

    out = _run(mcp_server._call_tool("forge", {"prompt": "cat"}))
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0].type == "text"

    parsed = json.loads(out[0].text)
    assert parsed["ok"] is True
    assert parsed["batch_mode"] == "single"
    assert len(parsed["images"]) == 1
    # Paths must be stringified — JSON can't serialize PosixPath/WindowsPath.
    assert isinstance(parsed["images"][0]["path"], str)
    assert isinstance(parsed["manifest_path"], str)


def test_call_tool_value_error_returns_ok_false_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def bad_forge(**kw: Any) -> ForgeResult:
        raise ValueError("bad size")

    monkeypatch.setattr(mcp_server, "forge", bad_forge)

    out = _run(mcp_server._call_tool("forge", {"prompt": "cat"}))
    parsed = json.loads(out[0].text)
    assert parsed["ok"] is False
    assert parsed["error_type"] == "ValueError"
    assert "bad size" in parsed["error"]


def test_call_tool_type_error_returns_ok_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def bad_forge(**kw: Any) -> ForgeResult:
        raise TypeError("unexpected keyword argument 'frobnicate'")

    monkeypatch.setattr(mcp_server, "forge", bad_forge)

    out = _run(mcp_server._call_tool("forge", {"prompt": "x", "frobnicate": 1}))
    parsed = json.loads(out[0].text)
    assert parsed["ok"] is False
    assert parsed["error_type"] == "TypeError"


def test_call_tool_real_validation_error_surfaces_as_ok_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a real ForgeOptions validation error becomes ok=false."""
    # Don't monkeypatch — let the real forge() raise from ForgeOptions.
    out = _run(
        mcp_server._call_tool("forge", {"prompt": "cat", "mode": "bogus"})
    )
    parsed = json.loads(out[0].text)
    assert parsed["ok"] is False
    assert parsed["error_type"] == "ValueError"
    assert "mode" in parsed["error"]


def test_call_tool_unknown_tool_name_raises() -> None:
    """Unknown tool names are a client bug — surface as a protocol error."""
    with pytest.raises(ValueError, match="unknown tool"):
        _run(mcp_server._call_tool("bogus", {}))


def test_call_tool_non_dict_arguments_returns_ok_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: if the SDK ever hands us non-dict args, don't crash."""
    # Hit _call_forge_sync directly with a non-dict so we exercise the
    # defensive branch without depending on SDK internals.
    payload = mcp_server._call_forge_sync("not a dict")  # type: ignore[arg-type]
    parsed = json.loads(payload)
    assert parsed["ok"] is False
    assert parsed["error_type"] == "TypeError"


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------


def test_result_to_dict_paths_stringified() -> None:
    result = _make_result(ok=True)
    out = mcp_server._result_to_dict(result)
    assert isinstance(out["manifest_path"], str)
    assert isinstance(out["images"][0]["path"], str)
    # ForgeImage.raw_png_path was None — must survive as None, not "None".
    assert out["images"][0]["raw_png_path"] is None


def test_result_to_dict_handles_failure_with_no_images() -> None:
    result = _make_result(ok=False)
    out = mcp_server._result_to_dict(result)
    assert out["ok"] is False
    assert out["images"] == []
    assert out["manifest_path"] is None
    assert out["error"] == "synthetic failure"


def test_result_to_dict_is_json_serializable() -> None:
    """Belt-and-suspenders: full round-trip through json.dumps."""
    result = _make_result(ok=True)
    out = mcp_server._result_to_dict(result)
    encoded = json.dumps(out)
    decoded = json.loads(encoded)
    assert decoded["ok"] is True
    assert decoded["images"][0]["bytes"] == 1234


# ---------------------------------------------------------------------------
# Server wiring
# ---------------------------------------------------------------------------


def test_server_name_is_codex_imagen() -> None:
    assert mcp_server.app.name == "codex-imagen"


def test_main_is_callable() -> None:
    """``main`` must exist and be the console-script entry point."""
    assert callable(mcp_server.main)
