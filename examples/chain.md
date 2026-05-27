# Chain (sequential storyboard)

N prompts → N images, but each call references its predecessors. The result is a narrative sequence where character / setting / style persist from frame to frame.

## Use case

A four-panel sketch of a character's day. A before/after pair for a marketing comparison. A storyboard for an animation pitch. Anything where frame N must look like the same world / same character as frame N-1.

## SDK

```python
from codex_imagen import forge

result = forge(
    prompt=[
        "frame 1: a red-haired woman waking up in a sunlit loft, "
        "soft morning light, cozy interior, illustrative storybook style",
        "frame 2: the same red-haired woman walking through a "
        "city street with a coffee cup, midday light",
        "frame 3: the same red-haired woman sitting at a cafe table "
        "writing in a notebook, late-afternoon golden hour",
        "frame 4: the same red-haired woman returning home, soft evening "
        "light, lamps glowing in the window",
    ],
    batch_mode="chain",
    chain_mode="anchor+previous",     # the default
    output_dir="./out/storyboard",
    size="1536x1024",
)

for img in result.images:
    print(f"[{img.index}] {img.path}")
    print(f"        references: {list(img.references_used)}")
```

## CLI

```text
# ./prompts/storyboard.txt
frame 1: a {char} waking up in a sunlit loft, soft morning light, cozy interior, illustrative storybook style
frame 2: the same {char} walking through a city street with a coffee cup, midday light
frame 3: the same {char} sitting at a cafe table writing in a notebook, late-afternoon golden hour
frame 4: the same {char} returning home, soft evening light, lamps glowing in the window
```

```bash
imagen -f ./prompts/storyboard.txt \
  --batch-mode chain \
  --chain-mode anchor+previous \
  --var char="red-haired woman" \
  --size 1536x1024 \
  --output-dir ./out/storyboard
```

`--var` is a templating shortcut: `{char}` in any prompt line is replaced with `"red-haired woman"` before the prompt is sent. Use it for a single piece of identity you want to keep consistent without retyping.

## `chain_mode` strategies

| Strategy | What each frame references |
|---|---|
| `previous` | only the immediately prior frame's image |
| `anchor` | only the first frame's image |
| `anchor+previous` (default) | the first frame's image *and* the immediately prior frame |
| `window:N` | the last N frames' images |
| `all` | every prior frame's image |

`anchor+previous` is the sweet spot for most stories: the anchor pins the visual identity (character, style, palette) and `previous` carries scene continuity (pose, location). `all` gives the strongest consistency but the longest reference list (and the slowest call near the end of long chains).

## Expected output

```
./out/storyboard/00.png            # frame 1 — establishes the world
./out/storyboard/01.png            # frame 2 — references frame 1
./out/storyboard/02.png            # frame 3 — references frames 1 + 2
./out/storyboard/03.png            # frame 4 — references frames 1 + 3
./out/storyboard/manifest.jsonl
```

Calls are sequential, not parallel — frame N needs frame N-1 to exist before it can reference it. Expect roughly N × single-call latency.

## When to use `chain`

- Stories, storyboards, sequences, before/after pairs.
- Anything where the *same* subject must reappear across frames.
- Tutorials and step-by-step illustrations.

## When to switch

- Same style across *unrelated* prompts → [`branded_parallel`](./branded_parallel.md) (parallel, much faster).
- Different takes on one idea → [`variants`](./variants.md).
- Unrelated images in one run → [`parallel`](./parallel.md).
