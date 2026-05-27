# Variants

One prompt + `count=N` → N parallel calls of the *same* prompt with subtle, automatically-injected variation hints. The subject stays identical; the composition, lighting, palette, and angle drift across the set so you actually get different-looking takes instead of four near-duplicates.

## Use case

You're exploring art direction for a logo, a hero illustration, or a product mood. You don't want unrelated subjects (`parallel`) — you want four credible interpretations of the same idea so you can pick or A/B test.

## SDK

```python
from codex_imagen import imagen

result = imagen(
    prompt="an abstract geometric logo for a fintech startup, minimal, "
           "single-color, suitable for both light and dark backgrounds",
    batch_mode="variants",
    count=4,
    parallel=4,                       # run them all at once
    output_dir="./out/logo-explore",
)

# Each image is the same subject, varied along a different axis.
for img in result.images:
    print(f"[{img.index}] {img.path}")
    # img.final_prompt shows the variation hint that was appended
```

## CLI

```bash
imagen "an abstract geometric logo for a fintech startup, minimal, \
single-color, suitable for both light and dark backgrounds" \
  --batch-mode variants \
  --count 4 \
  --parallel 4 \
  --output-dir ./out/logo-explore
```

## Expected output

```
./out/logo-explore/00.png         # variant 1: baseline (interpret freely)
./out/logo-explore/01.png         # variant 2: alt composition / angle
./out/logo-explore/02.png         # variant 3: alt lighting / mood
./out/logo-explore/03.png         # variant 4: alt color palette / materials
./out/logo-explore/manifest.jsonl
```

## What the variation hints look like

`imagen` appends one differentiation hint per variant to the prompt builder's `instructions=` block. The pattern (cycling on `count > 4`) is:

| Variant index | Appended hint |
|---|---|
| 0 | *"Interpret freely — establish the baseline."* |
| 1 | *"Vary the composition or angle."* |
| 2 | *"Vary the lighting or mood."* |
| 3 | *"Vary the color palette or materials."* |
| 4+ | Rotates back through the same axes. |

These are deliberately small nudges. The subject of your prompt doesn't shift — you don't get "a logo" then "a coffee mug" — but the visual treatment moves enough that the four images feel like four real takes, not four near-duplicates.

## Inspecting the variation

```python
for img in result.images:
    print(f"--- variant {img.index} ---")
    print(img.final_prompt)
    print()
```

The variation hint shows up at the bottom of `final_prompt`. This is also captured in `manifest.jsonl` so you can review later which hint produced which result.

## When to use `variants`

- Exploring art direction for a brand mark, illustration, or hero image.
- Generating A/B test candidates for a landing page.
- Producing several takes for a stakeholder to choose from.

## When to switch

- Need unrelated subjects, not variations → [`parallel`](./parallel.md).
- Want a shared anchor style across different prompts → [`branded_parallel`](./branded_parallel.md).
- Want a sequence that builds on itself → [`chain`](./chain.md).
