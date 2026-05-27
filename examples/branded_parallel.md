# Branded parallel (the marquee mode)

The marquee mode of the toolkit. Generate one anchor image first, then N more prompts in parallel, each referencing the anchor so the whole set shares a single visual language.

## Use case

A website landing page where the hero shot and four feature-section illustrations all need to feel like they belong to the same brand. A product slide deck where every slide has its own subject but a unified palette and treatment. A campaign asset pack.

This is the mode you reach for when "they should all look like they came from the same studio" matters.

## How it works

```
prompts: [hero,   feature_1, feature_2, feature_3, feature_4]
              ↓
           anchor = hero (or explicit `anchor=` parameter)
              ↓
           generate anchor first  →  ./out/00.png
              ↓
           feature_{1..4} run in parallel, each with anchor as reference
              ↓
           ./out/01.png  ./out/02.png  ./out/03.png  ./out/04.png
```

## SDK — implicit anchor (prompt[0])

```python
from codex_imagen import imagen

result = imagen(
    prompt=[
        "hero shot: a serene zen-garden workspace with a single laptop, "
        "warm beige editorial photography, soft serif headings vibe, "
        "lots of negative space, top-down composition",
        "feature 1: a close-up of hands typing on the same laptop",
        "feature 2: a clean code editor on the laptop screen",
        "feature 3: a small ceramic mug next to the laptop, "
        "casting a soft shadow",
    ],
    batch_mode="branded-parallel",
    parallel=3,
    output_dir="./out/landing",
    size="1536x1024",
)

# 00.png is the anchor; 01..03.png all reference it.
```

## SDK — explicit anchor

When you want the anchor to be a *style brief* rather than its own deliverable image, pass `anchor=`:

```python
result = imagen(
    prompt=[
        "section 1: hero with a coffee mug",
        "section 2: feature with a notebook",
        "section 3: feature with a pencil",
        "section 4: closing CTA with a small plant",
    ],
    anchor="warm beige editorial photography, soft serif headings vibe, "
           "lots of negative space, soft morning light, illustrative restraint",
    batch_mode="branded-parallel",
    skills=["~/.claude/skills/brandkit/SKILL.md"],
    mode="medium",                      # lets skills participate
    parallel=3,
    output_dir="./out/landing",
    size="1536x1024",
)
```

Either way, you get an `00.png` (the anchor) and then the rest of the set rendered in parallel.

## CLI

```text
# ./prompts/landing.txt
section 1: hero with a coffee mug
section 2: feature with a notebook
section 3: feature with a pencil
section 4: closing CTA with a small plant
```

```bash
imagen -f ./prompts/landing.txt \
  --batch-mode branded-parallel \
  --anchor "warm beige editorial photography, soft serif headings vibe, \
lots of negative space, soft morning light, illustrative restraint" \
  --skill ~/.claude/skills/brandkit/SKILL.md \
  --mode medium \
  --parallel 3 \
  --size 1536x1024 \
  --output-dir ./out/landing
```

## Expected output

```
./out/landing/00.png             # anchor (style-defining)
./out/landing/01.png             # section 1, references 00.png
./out/landing/02.png             # section 2, references 00.png
./out/landing/03.png             # section 3, references 00.png
./out/landing/04.png             # section 4, references 00.png
./out/landing/manifest.jsonl
```

Wall-clock time is roughly `(1 + N/parallel)` calls. With `parallel=3` and four follower prompts, the anchor runs alone first, then the four followers run as two rounds of three-then-one.

## Pairing with skills

Branded-parallel is the mode where skill files earn their keep. A `brandkit/SKILL.md` that defines palette, typography, photography style, and mood gets folded into the anchor's instructions *and* every follower's instructions. The anchor produces the canonical-looking image, the followers stay locked to it both via the reference image and via the skill body.

```python
imagen(
    prompt=["hero", "feature 1", "feature 2", "feature 3"],
    anchor="warm beige editorial, soft serif headings, lots of whitespace",
    skills=["./brand/SKILL.md", "./voice/SKILL.md"],
    batch_mode="branded-parallel",
)
```

The skill-body SHA-256 is recorded in every manifest line under `skills_hash`, so you can detect drift between runs (someone edited the skill between two generation rounds).

## When to use `branded-parallel`

- Landing pages, brand kits, slide decks, campaign asset packs.
- Any time "same studio, different subjects" matters.
- When you want the speed of `parallel` with the consistency of `chain`.

## When to switch

- Sequence with narrative continuity → [`chain`](./chain.md).
- Just unrelated images, no shared style → [`parallel`](./parallel.md).
- Many takes on one subject → [`variants`](./variants.md).
