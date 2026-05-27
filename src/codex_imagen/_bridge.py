"""codex_imagen._bridge — defensive wrapper around codex_image_gen.generate_image.

Purpose
-------
This module is the *single* place in ``codex_imagen`` that actually talks to
the Codex OAuth bridge via the upstream ``codex-image-gen`` library. Every
higher-level feature (modes, chain, branded-parallel, chroma key, CLI, MCP
server) goes through :func:`generate` here.

Key responsibilities
--------------------
1. **Parameter sanitization** — the Codex OAuth bridge silently drops or
   400-rejects certain values (see ``SPEC.md`` "Architecture Facts"). We
   filter them out *before* the call so the user gets a deterministic warning
   instead of a confusing server error.
2. **Retry with linear backoff** — transient network hiccups (incomplete
   read, connection reset, timeout) are common when streaming SSE through
   the bridge. We retry those a small bounded number of times. We do NOT
   retry deterministic 4xx OAuth errors.
3. **Disk persistence** — final image bytes are written to ``output_path``;
   optionally each streamed partial image is written next to it.
4. **Health probe** — :func:`health_check` inspects environment (library
   importability, auth file, JWT expiry) *without* making an API call, so
   the orchestrator can fail fast with an actionable hint instead of a
   500-style stack trace.

Role in architecture
--------------------
``core.forge()`` → ``_modes.run_*()`` → ``_bridge.generate()`` → upstream
``codex_image_gen.generate_image()`` → Codex OAuth Responses bridge.

This module does **not** know about ForgeOptions, modes, skills, chroma key,
or manifests. Keep it that way — those concerns live one layer up.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import importlib
import json
import os
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import codex_image_gen
from codex_image_gen import OAuthResponsesError

# Sanitizer allow-lists. These come straight from SPEC.md "Architecture Facts"
# (server-side enforced) and the upstream _client.py signature.
_ALLOWED_OUTPUT_FORMATS = {"png", "jpeg", "webp"}
_ALLOWED_BACKGROUNDS = {"auto", "opaque"}
_ALLOWED_MODERATION = {"auto", "low"}
_ALLOWED_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_ALLOWED_TEXT_VERBOSITIES = {"low", "medium", "high"}

# Substrings (case-insensitive) that mark a transient network/server error
# worth retrying. Kept in sync with the studio code referenced in the brief.
_TRANSIENT_ERROR_MARKERS: tuple[str, ...] = (
    "incompleteread",
    "connection reset",
    "connection aborted",
    "remote end closed",
    "temporarily unavailable",
    "timed out",
    "timeout",
)


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class BridgeError(Exception):
    """Base class for any error raised by the bridge layer.

    Wraps an unrecoverable upstream/library/HTTP failure after retries
    have been exhausted, so callers do not have to know about
    ``codex_image_gen`` internals.
    """


class BridgeUnavailableError(BridgeError):
    """Raised when the environment is not ready for a generation call.

    This is thrown *before* any API attempt — typically because the
    ``codex-image-gen`` library is missing or no Codex auth file exists.

    Attributes:
        hint: Actionable single-line message the caller can surface to
            the user (e.g. "Run: codex login").
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeResult:
    """Result of a single bridge call (one image written to disk).

    Returned by :func:`generate`. Field names mirror the previous dict
    shape so consumers can serialize directly with dataclasses.asdict.
    """

    path: str  # absolute path to written image
    bytes: int  # size of written image
    mime_type: str  # e.g. "image/png"
    response_id: str | None
    call_id: str | None
    revised_prompt: str | None
    reference_images: tuple[str, ...]  # tuple (immutable) of references used
    partial_image_paths: tuple[str, ...]
    elapsed_ms: int
    warnings: tuple[str, ...]


__all__ = [
    "BridgeError",
    "BridgeResult",
    "BridgeUnavailableError",
    "generate",
    "health_check",
]


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def _auth_file_path() -> Path:
    """Resolve the Codex auth file path.

    Mirrors the upstream library's ``_auth_file_path`` logic exactly, so the
    health check and the actual call agree on where to look.
    """
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def _jwt_exp(token: str | None) -> int | None:
    """Decode a JWT's ``exp`` claim using only stdlib.

    Returns the unix timestamp from the payload's ``exp`` field, or None if
    the token is missing/malformed. Intentionally lenient — we use this only
    for an advisory "expires soon?" warning, not for authentication.
    """
    if not token or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    # JWT uses urlsafe base64 without padding; restore padding so b64decode is happy.
    padded = payload + "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        claims = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return int(exp)
    return None


# TODO(task 9): Once core.ForgeHealth is fully defined, this should return
# ForgeHealth directly instead of a dict.
def health_check() -> dict[str, Any]:
    """Inspect the local environment without making any API call.

    Probes — in order of severity — whether:

    1. ``codex_image_gen`` is importable (hard requirement)
    2. ``PIL`` is importable (soft — only needed for transparency)
    3. The Codex auth file exists (hard requirement)
    4. The OAuth access token's ``exp`` claim leaves ≥ 60 s of runway
       (advisory: the upstream library refreshes automatically once
       remaining time drops below 300 s, so < 60 s is suspicious but not
       fatal).

    Returns:
        A dict with the shape documented in ``SPEC.md`` and the task
        prompt, containing ``ok`` plus diagnostic fields and an optional
        actionable ``hint``. Never raises — this is meant to be safe to
        call in preflight code paths.
    """
    # 1) codex_image_gen — required.
    try:
        importlib.import_module("codex_image_gen")
        codex_available = True
    except Exception:  # noqa: BLE001 — any import failure means "not available"
        codex_available = False

    # 2) Pillow — soft requirement (only chroma pipeline needs it).
    try:
        importlib.import_module("PIL")
        pillow_available = True
    except Exception:  # noqa: BLE001
        pillow_available = False

    # 3) Auth file.
    auth_path = _auth_file_path()
    auth_file_exists = auth_path.is_file()

    # 4) Token expiry — advisory.
    auth_expires_in: int | None = None
    if auth_file_exists:
        try:
            data = json.loads(auth_path.read_text("utf-8"))
            tokens = data.get("tokens") if isinstance(data, dict) else None
            access_token = (
                tokens.get("access_token") if isinstance(tokens, dict) else None
            )
            exp = _jwt_exp(access_token if isinstance(access_token, str) else None)
            if exp is not None:
                auth_expires_in = int(exp - time.time())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # A corrupt auth.json is a real problem, but reporting it as
            # "auth missing" is the most actionable framing for the user
            # ("run codex login"). Keep auth_file_exists=True so the hint
            # logic below still surfaces something useful.
            auth_expires_in = None

    # Compose hint by severity. First matching message wins.
    hint: str | None = None
    if not codex_available:
        hint = (
            "codex-image-gen library not installed. "
            "Run: python -m pip install --user codex-image-gen"
        )
    elif not auth_file_exists:
        hint = (
            f"Codex auth file not found at {auth_path}. Run: codex login"
        )
    elif auth_expires_in is not None and auth_expires_in < 60:
        hint = (
            f"Codex OAuth token expires in {auth_expires_in} seconds. "
            "Library will auto-refresh, but if calls fail run: "
            "codex logout && codex login"
        )

    # ok: hard requirements satisfied AND token not visibly stale.
    ok = (
        codex_available
        and auth_file_exists
        and (auth_expires_in is None or auth_expires_in >= 60)
    )

    return {
        "ok": ok,
        "codex_image_gen_available": codex_available,
        "pillow_available": pillow_available,
        "auth_file_exists": auth_file_exists,
        "auth_file_path": str(auth_path),
        "auth_expires_in_seconds": auth_expires_in,
        "hint": hint,
    }


# ---------------------------------------------------------------------------
# Parameter sanitization
# ---------------------------------------------------------------------------


def _ext_for_mime(mime: str | None) -> str:
    """Map a MIME type to a file extension for partial-image filenames.

    Defaults to ``.png`` because that is what the bridge returns when no
    explicit format is set.
    """
    if not mime:
        return ".png"
    mime = mime.lower()
    if mime == "image/jpeg":
        return ".jpg"
    if mime == "image/webp":
        return ".webp"
    if mime == "image/png":
        return ".png"
    return ".png"


def _sanitize_params(
    *,
    output_format: str,
    background: str,
    moderation: str | None,
    reasoning_effort: str | None,
    text_verbosity: str | None,
    partial_images: int | None,
    output_compression: int | None,
) -> tuple[dict[str, Any], list[str]]:
    """Filter forge-level kwargs down to what the bridge actually accepts.

    The Codex OAuth bridge silently drops or 400-rejects several plausible
    values (see ``SPEC.md`` "Architecture Facts"). We pre-filter so callers
    get a deterministic warning instead of a mysterious server error.

    Returns:
        A tuple ``(clean_kwargs, warnings)``. ``clean_kwargs`` is suitable
        to splat into :func:`codex_image_gen.generate_image`. ``warnings``
        is a list of human-readable strings explaining each downgrade.

    Raises:
        ValueError: If ``output_format`` is not one of png/jpeg/webp.
            Failing locally is friendlier than letting the bridge 400 us.
    """
    warnings: list[str] = []
    clean: dict[str, Any] = {}

    # output_format — fail fast locally. SPEC: bridge only accepts png|jpeg|webp.
    if output_format not in _ALLOWED_OUTPUT_FORMATS:
        raise ValueError(
            f"output_format={output_format!r} is invalid. "
            f"Must be one of: {sorted(_ALLOWED_OUTPUT_FORMATS)}"
        )
    clean["output_format"] = output_format

    # background — SPEC: bridge rejects 'transparent' with HTTP 400.
    # forge's chroma pipeline (Pillow) handles transparency post-process.
    if background == "transparent":
        warnings.append(
            "background='transparent' is not supported by the Codex bridge "
            "(HTTP 400). Downgraded to 'opaque'. Use the chroma-key "
            "pipeline (transparent=True in forge()) for alpha output."
        )
        clean["background"] = "opaque"
    elif background not in _ALLOWED_BACKGROUNDS:
        warnings.append(
            f"background={background!r} is not accepted by the bridge. "
            f"Downgraded to 'auto'. Allowed: {sorted(_ALLOWED_BACKGROUNDS)}."
        )
        clean["background"] = "auto"
    else:
        clean["background"] = background

    # moderation — SPEC: bridge only accepts auto/low.
    if moderation is not None:
        if moderation in _ALLOWED_MODERATION:
            clean["moderation"] = moderation
        else:
            warnings.append(
                f"moderation={moderation!r} is not accepted by the bridge. "
                f"Dropped. Allowed: {sorted(_ALLOWED_MODERATION)}."
            )

    # reasoning_effort — applied to mainline gpt-5.5, not the image tool.
    if reasoning_effort is not None:
        if reasoning_effort in _ALLOWED_REASONING_EFFORTS:
            clean["reasoning_effort"] = reasoning_effort
        else:
            warnings.append(
                f"reasoning_effort={reasoning_effort!r} is invalid. "
                f"Dropped. Allowed: {sorted(_ALLOWED_REASONING_EFFORTS)}."
            )

    # text_verbosity — Responses-API mainline knob.
    if text_verbosity is not None:
        if text_verbosity in _ALLOWED_TEXT_VERBOSITIES:
            clean["text_verbosity"] = text_verbosity
        else:
            warnings.append(
                f"text_verbosity={text_verbosity!r} is invalid. Dropped. "
                f"Allowed: {sorted(_ALLOWED_TEXT_VERBOSITIES)}."
            )

    # partial_images — int >= 0. Negative or non-int gets dropped.
    if partial_images is not None:
        if isinstance(partial_images, bool) or not isinstance(partial_images, int):
            warnings.append(
                f"partial_images={partial_images!r} is invalid (must be "
                "an int >= 0). Dropped."
            )
        elif partial_images < 0:
            warnings.append(
                f"partial_images={partial_images} is invalid (must be >= 0). Dropped."
            )
        else:
            clean["partial_images"] = partial_images

    # output_compression — only meaningful for jpeg/webp; PNG ignores it.
    # Library already drops it silently for PNG, but we mirror that here so
    # we don't pass noise that could confuse future debugging.
    if output_compression is not None:
        if output_format == "png":
            # Silent drop per the rules — library does the same. No warning.
            pass
        else:
            clean["output_compression"] = output_compression

    return clean, warnings


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    """Decide whether an exception is worth retrying.

    Treats network-level errors (IncompleteRead, ConnectionError, timeout)
    as transient. Treats deterministic OAuth 4xx errors as fatal — retrying
    them just wastes user time.
    """
    # Hard "no retry" signal: OAuth bridge returned a 4xx. That's a
    # configuration problem (bad auth, bad payload), not a flaky network.
    if isinstance(exc, OAuthResponsesError):
        status = getattr(exc, "status", None)
        if isinstance(status, int) and 400 <= status < 500:
            return False
        # OAuthResponsesError without status: assume transient (URLError path).
        return True

    if isinstance(
        exc,
        (
            http.client.IncompleteRead,
            TimeoutError,
            ConnectionError,
            urllib.error.URLError,
        ),
    ):
        return True

    # Fallback: string-match well-known transient phrases.
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_ERROR_MARKERS)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate(
    *,
    prompt: str,
    output_path: Path,
    references: list[str] | None = None,
    mask: str | None = None,
    instructions: str | None = None,
    size: str = "auto",
    output_format: str = "png",
    background: str = "auto",
    reasoning_effort: str | None = None,
    reasoning_summary: str | None = None,
    text_verbosity: str | None = None,
    moderation: str | None = None,
    partial_images: int | None = None,
    output_compression: int | None = None,
    timeout: int = 300,
    max_retries: int = 2,
    retry_delay_seconds: float = 2.0,
    save_partials: bool = False,
    extra: dict[str, Any] | None = None,
) -> BridgeResult:
    """Generate one image, persist it to disk, return metadata.

    This is the single chokepoint through which all higher-level forge
    code reaches the Codex bridge. It performs (in order): health check,
    parameter sanitization, retry loop, disk write, partial-image write,
    metadata assembly.

    Args:
        prompt: Final prompt text to send (caller is responsible for any
            labeled-spec / skill merging — this layer does no prompt
            mutation).
        output_path: Where to write the resulting image bytes. Parent
            directories are created if necessary.
        references: Optional list of file paths / URLs for reference
            images (multimodal input on the user message).
        mask: Optional alpha-channel mask file (for image edits).
        instructions: Mainline ``instructions=`` to pass to the bridge.
            None falls back to the upstream library's default.
        size: ``"auto"`` or ``"WIDTHxHEIGHT"``. Caller should pre-validate
            via ``_size.validate_size`` for friendlier errors.
        output_format: ``png`` | ``jpeg`` | ``webp``.
        background: ``"auto"`` or ``"opaque"``. ``"transparent"`` is
            silently downgraded with a warning — see SPEC.md.
        reasoning_effort: gpt-5.5 mainline effort knob.
        reasoning_summary: gpt-5.5 mainline summary knob.
        text_verbosity: mainline ``text.verbosity``.
        moderation: ``"auto"`` or ``"low"`` (others dropped with warning).
        partial_images: Number of partial images to stream (>= 0).
        output_compression: 0-100, jpeg/webp only.
        timeout: HTTP timeout per attempt, seconds.
        max_retries: Number of *additional* attempts after the first.
            ``max_retries=2`` means up to 3 total attempts.
        retry_delay_seconds: Base for linear backoff
            (``delay * (attempt + 1)``).
        save_partials: If True and the bridge streamed partials, write
            each one to disk next to ``output_path``.
        extra: Power-user escape hatch. Merged into the library kwargs
            verbatim (overrides anything we set). Use for keys like
            ``oauth_base_url`` or ``auth_file``.

    Returns:
        A :class:`BridgeResult` dataclass with the fields documented in the
        task brief: ``path``, ``bytes``, ``mime_type``, ``response_id``,
        ``call_id``, ``revised_prompt``, ``reference_images``,
        ``partial_image_paths``, ``elapsed_ms``, ``warnings``.

    Raises:
        ValueError: For locally-invalid params (e.g. unknown
            ``output_format`` or negative ``max_retries``).
        BridgeUnavailableError: If :func:`health_check` fails before any
            attempt. Carries an actionable ``hint``.
        BridgeError: If all retries are exhausted on a transient error,
            or the upstream library raises a non-transient error.
    """
    # --- 0) Validate retry bound locally. Negative would skip the loop ------
    # entirely and silently return nothing — fail fast instead.
    if max_retries < 0:
        raise ValueError(f"max_retries must be >= 0, got {max_retries}")

    # --- 1) Health gate — fail fast with an actionable hint. ----------------
    health = health_check()
    if not health["ok"]:
        # Only block on hard failures (missing library, missing auth).
        # An expiring token is just advisory — the library will refresh.
        if not health["codex_image_gen_available"] or not health["auth_file_exists"]:
            raise BridgeUnavailableError(
                "Codex bridge is not available: "
                + (health["hint"] or "unknown reason"),
                hint=health["hint"],
            )

    # --- 2) Sanitize forge-level kwargs into bridge-accepted shape. ---------
    # This may raise ValueError (output_format) — let it propagate; callers
    # need to know they passed something unsendable.
    clean, warnings = _sanitize_params(
        output_format=output_format,
        background=background,
        moderation=moderation,
        reasoning_effort=reasoning_effort,
        text_verbosity=text_verbosity,
        partial_images=partial_images,
        output_compression=output_compression,
    )

    # --- 3) Build library kwargs. ------------------------------------------
    lib_kwargs: dict[str, Any] = {
        "size": size,
        "timeout": timeout,
    }
    lib_kwargs.update(clean)
    if references:
        # The library accepts a single ImageInput or an iterable. We always
        # pass a list so the call signature is consistent regardless of count.
        lib_kwargs["images"] = list(references)
    if mask is not None:
        lib_kwargs["input_image_mask"] = mask
    if instructions is not None:
        lib_kwargs["instructions"] = instructions
    if reasoning_summary is not None:
        lib_kwargs["reasoning_summary"] = reasoning_summary
    # `extra` is the power-user escape hatch — last write wins on purpose,
    # so advanced callers can override anything we set above (e.g. point at
    # a custom oauth_base_url or auth_file).
    if extra:
        lib_kwargs.update(extra)

    # --- 4) Retry loop on transient errors. --------------------------------
    # Note: ``codex_image_gen`` is imported at module top. Tests patch
    # ``codex_image_gen.generate_image`` on the module object, so the lookup
    # below picks up the patched attribute on each call.
    started_at = time.monotonic()
    for attempt in range(max_retries + 1):
        try:
            result = codex_image_gen.generate_image(prompt, **lib_kwargs)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_retries or not _is_transient(exc):
                raise BridgeError(
                    f"Codex bridge call failed: {exc}"
                ) from exc
            # Linear backoff: 2s, 4s, 6s with default retry_delay_seconds=2.
            time.sleep(retry_delay_seconds * (attempt + 1))

    if not result.images:
        raise BridgeError("Codex bridge returned no images")

    primary = result.images[0]

    # --- 5) Write image bytes to disk. --------------------------------------
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(primary.data)

    # --- 6) Optionally persist partial frames next to the main image. -------
    partial_paths: list[str] = []
    if save_partials:
        partials = getattr(result, "partial_images", None) or ()
        stem = output_path.stem
        parent = output_path.parent
        for idx, partial in enumerate(partials):
            # Prefer the partial's own index if the upstream library set one;
            # otherwise fall back to enumeration order.
            partial_idx = (
                partial.index
                if isinstance(getattr(partial, "index", None), int)
                else idx
            )
            ext = _ext_for_mime(getattr(partial, "mime_type", None))
            partial_path = parent / f"{stem}-partial-{partial_idx}{ext}"
            partial_path.write_bytes(partial.data)
            partial_paths.append(str(partial_path.resolve()))

    elapsed_ms = int((time.monotonic() - started_at) * 1000)

    return BridgeResult(
        path=str(output_path.resolve()),
        bytes=len(primary.data),
        mime_type=primary.mime_type,
        response_id=result.response_id,
        call_id=primary.call_id,
        revised_prompt=primary.revised_prompt,
        reference_images=tuple(references) if references else (),
        partial_image_paths=tuple(partial_paths),
        elapsed_ms=elapsed_ms,
        warnings=tuple(warnings),
    )
