# Why use this vs. native image_gen?

Both `codex-imagen` (this tool) and the native `imagegen` tool in Codex/Claude
Code use the same underlying `gpt-image-2` model via the same Codex OAuth bridge.
The difference is orchestration, post-processing, and workflow integration.

---

## Side-by-side

| Capability | codex-imagen | native image_gen |
|---|---|---|
| Multi-image in one call | Yes — parallel, chain, branded-parallel | No — one image per call |
| Brand consistency across images | Yes — anchor-driven branded-parallel | No — each call is independent |
| Transparency quality | Green chroma-key + dominance despill | Simple chroma-key or none |
| Skill loader | Yes — pass any `.md` skill file | No |
| Structured prompts | Yes — Codex labeled-spec 14-field dict | Free text only |
| Output manifest | Yes — JSONL per image | No |
| Predictable file paths | Yes — always writes to `output_dir` | Model-decided |
| Speed for a single image | ~Same | ~Same |
| Setup required | Yes (`imagen setup`) | None — built-in |

---

## When to prefer codex-imagen

**More than one image is needed:**
Use `batch_mode="parallel"` or `"branded-parallel"`. One call generates N images
with a single overhead — much faster than N separate calls with the native tool.

**Brand consistency across images:**
`branded-parallel` with an `anchor` locks palette, typography, and lighting
across all images in the batch. N separate calls with the native tool produce N
unrelated images regardless of how similar your prompts are.

**Transparency:**
Green chroma-key default + dominance despill produces 54× cleaner edges on
fur/hair subjects compared to naive magenta-key. The native tool's chroma
handling (if any) is simpler and less configurable.

**Skill-driven workflows:**
The same `.md` skill files you use in Claude Code / Codex can be passed to
`imagen` directly. The native tool has no skill loader.

**Structured Codex labeled-spec prompts:**
Pass a dict (`Subject`, `Style`, `Lighting`, `Text (verbatim)`, etc.) for
predictable, structured output. The native tool accepts free text only.

**Predictable file paths + manifest:**
Every generation writes to your `output_dir` with a `manifest.jsonl` for
auditing, automation, and downstream processing.

---

## When native image_gen is fine

- **One-off simple images** where speed and simplicity matter more than
  batching, consistency, or transparency quality
- **Zero-setup contexts** where you can't or don't want to run `imagen setup`
- **Agent sessions that already have imagegen loaded** and you only need a single
  quick image

The native tool is perfectly adequate for quick exploratory generation. Use
codex-imagen when the output is going into a real project.

---

## Coexistence

The native Codex skill is named `imagegen` (installed at
`~/.codex/skills/.system/imagegen/`). This tool's skill is named `imagen`
(no "g"). They are separate, do not conflict, and Codex can see both.

When `imagen setup --client codex` runs, it writes a preference snippet to
`~/.codex/AGENTS.md` asking the Codex AI to prefer `imagen` for multi-image,
transparency, and skill-driven work. The native `imagegen` remains available
for one-off calls.

You can always check which is active:

```bash
imagen status    # shows codex-imagen install state
```

---

## Related

- [Batch modes](modes.md) — parallel, variants, chain, branded-parallel
- [Transparency](transparency.md) — chroma-key pipeline in detail
- [Skills](skills.md) — skill file loader
- [MCP server](mcp.md) — setup and client configuration
