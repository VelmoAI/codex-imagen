# Third-party attributions

## OpenAI Codex `imagegen` skill (Apache 2.0)

Portions of the chroma-key algorithm in `src/codex_imagen/_chroma.py` are
inspired by the implementation in OpenAI's native Codex `imagegen` skill at
`~/.codex/skills/.system/imagegen/scripts/remove_chroma_key.py`, used under
the Apache License 2.0.

Specifically, the following techniques are derived from that implementation:

- Default key-color policy (#00FF00 green primary, #FF00FF magenta for green
  subjects)
- Border-sample auto-key detection (`_sample_border_key`)
- Dominance-capping despill: spill channels capped to ≤ max(non-spill channels) - 1
  (`_cleanup_spill_dominance`)
- Dual-threshold soft matte: `transparent_threshold` + `opaque_threshold` +
  smoothstep curve (`_soft_alpha_dual`)
- Key-channel dominance heuristic for partial-alpha detection
  (`_key_channel_dominance`, `_looks_key_colored`)

All code was reimplemented independently for this project; no source files were
copied verbatim. Original copyright remains with OpenAI.

Apache License 2.0: https://www.apache.org/licenses/LICENSE-2.0
