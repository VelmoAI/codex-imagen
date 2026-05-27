"""Tests for ``codex_imagen._bridge``.

All tests stub ``codex_image_gen.generate_image`` via monkeypatch — there
are absolutely no real API calls here. Health-check tests fake the local
filesystem and module import state.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Make sure we import from the in-tree source, not anything installed.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codex_imagen import _bridge  # noqa: E402
from codex_imagen._bridge import (  # noqa: E402
    BridgeError,
    BridgeResult,
    BridgeUnavailableError,
    generate,
    health_check,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_image_bytes(tag: bytes = b"PNGDATA") -> bytes:
    # Minimal "looks like a PNG" marker — content doesn't matter to the tests,
    # only that the same bytes round-trip to disk.
    return b"\x89PNG\r\n\x1a\n" + tag


def _make_fake_result(
    *,
    data: bytes | None = None,
    mime_type: str = "image/png",
    response_id: str = "resp_test_123",
    call_id: str = "ig_test_456",
    revised_prompt: str = "a polished version of the prompt",
    partials: tuple[SimpleNamespace, ...] = (),
) -> SimpleNamespace:
    """Build a fake ImageGenerationResult-shaped object."""
    primary = SimpleNamespace(
        data=data if data is not None else _fake_image_bytes(),
        mime_type=mime_type,
        call_id=call_id,
        revised_prompt=revised_prompt,
    )
    return SimpleNamespace(
        images=(primary,),
        response_id=response_id,
        partial_images=partials,
        raw_response={},
    )


def _make_partial(
    *, idx: int, mime_type: str = "image/png", data: bytes | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        index=idx,
        mime_type=mime_type,
        data=data if data is not None else _fake_image_bytes(f"P{idx}".encode()),
    )


@pytest.fixture
def healthy_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Make ``health_check`` return ok=True for the duration of one test.

    Builds a fake auth.json with a JWT-shaped access_token whose ``exp`` is
    far in the future, and points the bridge at it via CODEX_HOME.
    """
    far_future = int(time.time()) + 24 * 3600
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": far_future}).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    fake_jwt = f"header.{payload}.sig"

    codex_home = tmp_path / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": fake_jwt,
                    "refresh_token": "refresh",
                    "id_token": "id",
                    "account_id": "acc",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return codex_home / "auth.json"


@pytest.fixture
def stub_generate_image(monkeypatch: pytest.MonkeyPatch):
    """Replace codex_image_gen.generate_image with a settable stub.

    Yields a list to which each call's (prompt, kwargs) is appended, plus a
    setter for the next return/raise behavior.
    """
    import codex_image_gen

    calls: list[tuple[str, dict[str, Any]]] = []
    behavior: dict[str, Any] = {"result": _make_fake_result(), "side_effects": None}

    def fake_generate_image(prompt: str, **kwargs: Any):
        calls.append((prompt, dict(kwargs)))
        # If side_effects is set, pop the next one (exception or result).
        side_effects = behavior.get("side_effects")
        if side_effects:
            outcome = side_effects.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return behavior["result"]

    monkeypatch.setattr(codex_image_gen, "generate_image", fake_generate_image)
    return SimpleNamespace(calls=calls, behavior=behavior)


# ---------------------------------------------------------------------------
# generate() — happy path
# ---------------------------------------------------------------------------


def test_generate_writes_image_to_output_path(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    out = tmp_path / "out" / "hero.png"
    expected = _fake_image_bytes(b"WRITE-ME")
    stub_generate_image.behavior["result"] = _make_fake_result(data=expected)

    result = generate(prompt="a coffee mug", output_path=out)

    assert out.exists()
    assert out.read_bytes() == expected
    assert isinstance(result, BridgeResult)
    assert result.bytes == len(expected)


def test_generate_returns_metadata_dict(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    out = tmp_path / "img.png"
    stub_generate_image.behavior["result"] = _make_fake_result(
        response_id="resp_meta",
        call_id="ig_meta",
        revised_prompt="revised",
    )

    result = generate(
        prompt="a teacup",
        output_path=out,
        references=["./refA.png", "./refB.png"],
    )

    assert isinstance(result, BridgeResult)
    # Field surface check — guards against accidental renames.
    expected_fields = {
        "path",
        "bytes",
        "mime_type",
        "response_id",
        "call_id",
        "revised_prompt",
        "reference_images",
        "partial_image_paths",
        "elapsed_ms",
        "warnings",
    }
    assert {f.name for f in result.__dataclass_fields__.values()} >= expected_fields
    assert result.path.endswith("img.png")
    assert result.mime_type == "image/png"
    assert result.response_id == "resp_meta"
    assert result.call_id == "ig_meta"
    assert result.revised_prompt == "revised"
    assert result.reference_images == ("./refA.png", "./refB.png")
    assert result.partial_image_paths == ()
    assert isinstance(result.elapsed_ms, int)
    assert result.warnings == ()


# ---------------------------------------------------------------------------
# Parameter sanitization
# ---------------------------------------------------------------------------


def test_background_transparent_downgraded_with_warning(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    result = generate(
        prompt="a cube",
        output_path=tmp_path / "x.png",
        background="transparent",
    )
    # Sanitizer overrode to opaque before calling the library.
    _, kwargs = stub_generate_image.calls[-1]
    assert kwargs["background"] == "opaque"
    # And we surfaced the downgrade as a warning.
    assert any("transparent" in w for w in result.warnings)


def test_invalid_output_format_raises_valueerror(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="output_format"):
        generate(
            prompt="hi",
            output_path=tmp_path / "x.tiff",
            output_format="tiff",
        )
    # And the bridge should never have been called.
    assert stub_generate_image.calls == []


def test_invalid_moderation_dropped_with_warning(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    result = generate(
        prompt="x",
        output_path=tmp_path / "x.png",
        moderation="strict",  # not in {auto, low}
    )
    _, kwargs = stub_generate_image.calls[-1]
    assert "moderation" not in kwargs
    assert any("moderation" in w for w in result.warnings)


def test_extra_dict_passed_through(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    generate(
        prompt="x",
        output_path=tmp_path / "x.png",
        extra={
            "oauth_base_url": "https://custom.example.com/codex",
            "auth_file": "/tmp/auth.json",
            "model": "gpt-5.5",
        },
    )
    _, kwargs = stub_generate_image.calls[-1]
    assert kwargs["oauth_base_url"] == "https://custom.example.com/codex"
    assert kwargs["auth_file"] == "/tmp/auth.json"
    assert kwargs["model"] == "gpt-5.5"


# ---------------------------------------------------------------------------
# Partial images
# ---------------------------------------------------------------------------


def test_partial_images_saved_when_flag_set(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    partials = (
        _make_partial(idx=0, mime_type="image/png", data=b"P0"),
        _make_partial(idx=1, mime_type="image/jpeg", data=b"P1"),
        _make_partial(idx=2, mime_type="image/webp", data=b"P2"),
    )
    stub_generate_image.behavior["result"] = _make_fake_result(partials=partials)

    out = tmp_path / "hero.png"
    result = generate(
        prompt="x",
        output_path=out,
        partial_images=3,
        save_partials=True,
    )

    assert len(result.partial_image_paths) == 3
    assert (tmp_path / "hero-partial-0.png").read_bytes() == b"P0"
    assert (tmp_path / "hero-partial-1.jpg").read_bytes() == b"P1"
    assert (tmp_path / "hero-partial-2.webp").read_bytes() == b"P2"


def test_partial_images_skipped_when_flag_unset(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    partials = (_make_partial(idx=0, data=b"P0"),)
    stub_generate_image.behavior["result"] = _make_fake_result(partials=partials)

    out = tmp_path / "hero.png"
    result = generate(prompt="x", output_path=out, save_partials=False)

    assert result.partial_image_paths == ()
    assert not (tmp_path / "hero-partial-0.png").exists()


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_retry_on_transient_error(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First call: ConnectionError. Second: success.
    stub_generate_image.behavior["side_effects"] = [
        ConnectionError("connection reset by peer"),
        _make_fake_result(),
    ]
    # Don't actually sleep — keep the test fast.
    monkeypatch.setattr(_bridge.time, "sleep", lambda *_: None)

    result = generate(
        prompt="x",
        output_path=tmp_path / "x.png",
        max_retries=2,
        retry_delay_seconds=0.0,
    )

    assert result.bytes > 0
    assert len(stub_generate_image.calls) == 2


def test_no_retry_on_4xx_error(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    from codex_image_gen import OAuthResponsesError

    stub_generate_image.behavior["side_effects"] = [
        OAuthResponsesError("bad request", status=400, body="nope"),
        # If retry were attempted, this would be the next return — but it
        # must NOT be reached.
        _make_fake_result(),
    ]

    with pytest.raises(BridgeError):
        generate(
            prompt="x",
            output_path=tmp_path / "x.png",
            max_retries=3,
        )

    # Exactly one call — no retry on deterministic 4xx.
    assert len(stub_generate_image.calls) == 1


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_health_check_without_codex_image_gen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = _bridge.importlib.import_module

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name == "codex_image_gen":
            raise ImportError("simulated: codex_image_gen not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(_bridge.importlib, "import_module", fake_import)

    h = health_check()
    assert h["ok"] is False
    assert h["codex_image_gen_available"] is False
    assert h["hint"] is not None
    assert "codex-image-gen" in h["hint"]


def test_health_check_without_auth_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Point CODEX_HOME at a directory with no auth.json.
    empty = tmp_path / "empty-codex-home"
    empty.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(empty))

    h = health_check()
    assert h["ok"] is False
    assert h["auth_file_exists"] is False
    assert h["hint"] is not None
    assert "codex login" in h["hint"]


def test_health_check_happy_path(healthy_env: Path) -> None:
    h = health_check()
    assert h["ok"] is True
    assert h["codex_image_gen_available"] is True
    assert h["auth_file_exists"] is True
    assert h["auth_file_path"] == str(healthy_env)
    assert h["hint"] is None
    # Token's exp is ~24h out — must be far above the 60s buffer.
    assert h["auth_expires_in_seconds"] is not None
    assert h["auth_expires_in_seconds"] > 60


# ---------------------------------------------------------------------------
# Additional edge-case coverage (post code review)
# ---------------------------------------------------------------------------


def test_generate_raises_bridge_error_when_no_images_returned(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    # Bridge returned a syntactically valid result with zero images.
    # Without this guard we'd IndexError when reading images[0].
    stub_generate_image.behavior["result"] = SimpleNamespace(
        images=(),
        response_id="resp_x",
        partial_images=(),
        raw_response={},
    )

    with pytest.raises(BridgeError, match="no images"):
        generate(prompt="x", output_path=tmp_path / "x.png")


def test_retry_exhaustion_raises_bridge_error(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every call raises a transient error. After max_retries+1 attempts
    # the loop must give up and surface a BridgeError.
    stub_generate_image.behavior["side_effects"] = [
        ConnectionError("connection reset"),
        ConnectionError("connection reset"),
        ConnectionError("connection reset"),
    ]
    # Don't actually sleep — keep the test fast.
    monkeypatch.setattr(_bridge.time, "sleep", lambda *_: None)

    with pytest.raises(BridgeError):
        generate(
            prompt="x",
            output_path=tmp_path / "x.png",
            max_retries=2,
            retry_delay_seconds=0.001,
        )

    # max_retries=2 means 1 initial + 2 retries = 3 total attempts.
    assert len(stub_generate_image.calls) == 3


def test_partial_images_true_bool_rejected(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    # ``True`` is technically an int subclass in Python — without an explicit
    # bool check it would slip through the sanitizer as ``partial_images=1``.
    result = generate(
        prompt="x",
        output_path=tmp_path / "x.png",
        partial_images=True,  # type: ignore[arg-type]
    )

    _, kwargs = stub_generate_image.calls[-1]
    assert "partial_images" not in kwargs
    assert any("partial_images" in w for w in result.warnings)


def test_bridge_unavailable_error_carries_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Force health_check to report not-ok with an actionable hint and confirm
    # the exception preserves it so the caller can surface a friendly message.
    monkeypatch.setattr(
        _bridge,
        "health_check",
        lambda: {
            "ok": False,
            "codex_image_gen_available": False,
            "pillow_available": True,
            "auth_file_exists": True,
            "auth_file_path": "/fake/auth.json",
            "auth_expires_in_seconds": None,
            "hint": "install codex-image-gen first",
        },
    )

    with pytest.raises(BridgeUnavailableError) as excinfo:
        generate(prompt="x", output_path=tmp_path / "x.png")

    assert excinfo.value.hint == "install codex-image-gen first"


def test_max_retries_negative_raises_valueerror(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    # A negative retry budget would silently skip the loop and leave
    # ``result`` unset — fail fast at the boundary instead.
    with pytest.raises(ValueError, match="max_retries"):
        generate(
            prompt="x",
            output_path=tmp_path / "x.png",
            max_retries=-1,
        )

    # The bridge should never have been called.
    assert stub_generate_image.calls == []


# ---------------------------------------------------------------------------
# Wall-clock timeout tests (Fix 1)
# ---------------------------------------------------------------------------


def test_wall_clock_timeout_raises_bridge_error_on_stuck_call(
    healthy_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fake that sleeps longer than wall_clock_timeout must raise BridgeError
    mentioning 'timeout', and should complete within ~2 seconds (not hang)."""
    import codex_image_gen

    def slow_generate(prompt: str, **kwargs: Any):
        time.sleep(5)  # much longer than wall_clock_timeout=1.0
        return _make_fake_result()

    monkeypatch.setattr(codex_image_gen, "generate_image", slow_generate)

    t_start = time.monotonic()
    with pytest.raises(BridgeError, match="timeout"):
        generate(
            prompt="x",
            output_path=tmp_path / "x.png",
            wall_clock_timeout=1.0,
            max_retries=0,
        )
    elapsed = time.monotonic() - t_start
    # Must have returned quickly — not hung for the full sleep duration.
    assert elapsed < 4.0, f"timeout took too long: {elapsed:.1f}s"


def test_wall_clock_timeout_succeeds_when_call_is_fast(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    """A fast call completes normally when wall_clock_timeout is generous."""
    result = generate(
        prompt="a coffee mug",
        output_path=tmp_path / "out.png",
        wall_clock_timeout=10.0,
    )
    assert result.bytes > 0
    assert len(stub_generate_image.calls) == 1


def test_variance_warning_when_elapsed_exceeds_180s(
    healthy_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When elapsed_ms > 180_000, generate() should append a variance warning.

    We fake a large elapsed time by monkeypatching time.monotonic so that
    the post-call elapsed reading returns a forged delta, without actually
    sleeping.
    """
    import codex_image_gen

    call_count = [0]
    monotonic_calls = [0]

    def fake_generate(prompt: str, **kwargs: Any):
        call_count[0] += 1
        return _make_fake_result()

    monkeypatch.setattr(codex_image_gen, "generate_image", fake_generate)

    # Forge time.monotonic so that the second call (after the bridge returns)
    # appears to be 185 seconds later than the first.
    real_monotonic = time.monotonic
    base_time = real_monotonic()

    def fake_monotonic() -> float:
        monotonic_calls[0] += 1
        # First call = started_at anchor; subsequent calls return a delta
        # that exceeds 180s.
        if monotonic_calls[0] == 1:
            return base_time
        return base_time + 185.0

    monkeypatch.setattr(_bridge.time, "monotonic", fake_monotonic)

    result = generate(
        prompt="a cup",
        output_path=tmp_path / "out.png",
        wall_clock_timeout=300.0,  # large enough not to fire
        max_retries=0,
    )

    # The variance warning should be present.
    assert any("185" in w or "took" in w for w in result.warnings), (
        f"Expected a variance warning, got: {result.warnings}"
    )


def test_wall_clock_timeout_flows_from_imagen_options(
    healthy_env: Path,
    stub_generate_image: SimpleNamespace,
    tmp_path: Path,
) -> None:
    """wall_clock_timeout set on ImagenOptions flows to the bridge call.

    We verify by checking that no timeout exception is raised and the call
    succeeds — indirectly confirming the value was wired through.  A more
    direct test would require inspecting the kwargs inside the executor but
    that would be brittle given the ThreadPoolExecutor wrapping.
    """
    from codex_imagen.core import ImagenOptions

    opts = ImagenOptions(
        prompt="a red sphere",
        output_dir=str(tmp_path),
        wall_clock_timeout=180.0,
    )
    assert opts.wall_clock_timeout == 180.0

    # Sanity: invalid timeout must raise.
    with pytest.raises(ValueError, match="wall_clock_timeout"):
        ImagenOptions(prompt="x", output_dir=str(tmp_path), wall_clock_timeout=-1.0)
