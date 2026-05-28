# Skills

A "skill" is a Markdown file that encodes domain knowledge — brand guidelines,
design systems, style constraints. `codex-imagen` loads skills, strips YAML
frontmatter, and merges the body into the prompt builder's `instructions=` block.

Skills are the same format used by Claude Code and Codex. You can reuse skill
files that already live in `~/.claude/skills/` or `~/.codex/skills/` — just
pass the path.

---

## The bundled `imagen` skill

`imagen setup` installs a self-describing skill alongside the MCP config:

| Client | Skill location |
|---|---|
| Claude Code | `~/.claude/skills/imagen/SKILL.md` |
| Claude Desktop | `~/.claude/skills/imagen/SKILL.md` |
| Codex | `~/.codex/skills/imagen/SKILL.md` |
| Cursor / OpenCode | skill concept not yet supported |

After setup, your AI agent reads this skill at session start and automatically
knows how to call `imagen` — modes, batch strategies, transparency, brand sets —
without you explaining it each time.

The skill ships inside the wheel (`src/codex_imagen/_assets/skills/imagen.md`)
so it is always in sync with the installed version. `imagen uninstall` removes
it; `imagen status` shows whether it is present per client.

---

## External skill loader

Pass one or more `.md` skill file paths to inject brand or domain context:

```python
from codex_imagen import imagen

result = imagen(
    prompt="hero shot for a fintech landing page",
    skills=[
        "~/.claude/skills/brandkit/SKILL.md",
        "./project/skills/typography.md",
    ],
)
```

CLI:

```bash
imagen "hero shot for a fintech landing page" \
  --skill ~/.claude/skills/brandkit/SKILL.md \
  --skill ./project/skills/typography.md
```

Multiple skills are concatenated with a separator before being injected.

---

## Skill format

A skill file is plain Markdown with an optional YAML frontmatter block:

```markdown
---
name: my-brand
description: Brand guidelines for Acme Corp
---

# Acme Corp Brand Guidelines

## Color palette

Primary: #1A2B3C (deep navy)
Accent: #F5A623 (warm amber)
Background: #FAFAF8 (warm off-white)

## Typography

Headings: "Freight Display Pro", serif, medium weight
Body: "Inter", sans-serif, regular

## Photography style

Editorial product photography. Generous whitespace. Soft diffused lighting
from upper-left. No hard shadows. Subjects centered with breathing room.
```

YAML frontmatter is stripped before injection — only the Markdown body is
merged into the prompt builder. The `name` and `description` fields are
used for logging and manifest recording.

---

## Example: brand skill applied to a website image set

```python
result = imagen(
    prompt=[
        "hero: bold product shot",
        "features: clean icon grid on light background",
        "social proof: candid team photo",
    ],
    anchor="modern SaaS product, high-end editorial",
    batch_mode="branded-parallel",
    skills=["./brand/acme-brand.md"],
    output_dir="./out/website",
)
```

---

## Skill constraints

- Skills are **ignored in `mode="raw"`** — raw mode passes the prompt verbatim
  with no instruction block. A warning is appended to `result.warnings`.
- Mode **auto-upgrades to `medium`** when skills are passed (needed for
  instruction injection).
- Tilde `~` in paths is expanded to the home directory automatically.
- Missing skill files in non-strict mode emit a warning and are skipped;
  in strict mode they raise `FileNotFoundError`.

---

## Related

- [MCP server](mcp.md) — how `imagen setup` installs the bundled skill
- [Reasoning modes](reasoning.md) — how skill presence affects mode selection
- [SDK reference](sdk.md) — `skills` parameter in `ImagenOptions`
