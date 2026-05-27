"""codex_imagen — Codex-OAuth image generation toolkit.

Public SDK entry points. The single user-facing function is :func:`imagen`,
which orchestrates one or more image generations through the Codex OAuth
bridge (``gpt-image-2`` via ``codex-image-gen``). It supports five batch
modes (single, parallel, variants, chain, branded-parallel), structured
Codex labeled-spec prompts, skill-file injection, and a Pillow chroma-key
pipeline for transparency.

This module re-exports the public surface only. Implementations live in
:mod:`codex_imagen.core` (orchestration + dataclasses) and the private
``_*`` submodules.
"""

from __future__ import annotations

from codex_imagen.core import (
    ImagenHealth,
    ImagenImage,
    ImagenOptions,
    ImagenResult,
    imagen,
)

__version__ = "0.1.0"

__all__ = [
    "imagen",
    "ImagenOptions",
    "ImagenResult",
    "ImagenImage",
    "ImagenHealth",
]
