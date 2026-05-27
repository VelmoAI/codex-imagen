# Parallel batch

N unrelated prompts in, N independent images out, all generated concurrently through a `ThreadPoolExecutor`. There is no shared anchor, no chain context — each call is its own thing.

## Use case

You're building a small content set for a marketing email and you need three product shots in one run. The shots have nothing to do with each other visually, you just want them all done at once.

## SDK

```python
from codex_imagen import imagen

result = imagen(
    prompt=[
        "a ceramic coffee mug, top-down hero shot, warm morning light",
        "a leather-bound notebook lying open on a wooden desk, golden hour",
        "a single mechanical pencil on white paper, soft studio lighting",
    ],
    batch_mode="parallel",     # explicit; "auto" would resolve here too
    parallel=3,                # run all three at once
    output_dir="./out/email-set",
    size="1536x1024",
)

for img in result.images:
    print(f"[{img.index}] {img.path}  ({img.bytes // 1024} KB)")
```

## CLI

Put one prompt per line in a text file (blank lines and `#` comments are ignored):

```text
# ./prompts/email-set.txt
a ceramic coffee mug, top-down hero shot, warm morning light
a leather-bound notebook lying open on a wooden desk, golden hour
a single mechanical pencil on white paper, soft studio lighting
```

Then:

```bash
imagen -f ./prompts/email-set.txt \
  --batch-mode parallel \
  --parallel 3 \
  --size 1536x1024 \
  --output-dir ./out/email-set
```

## Expected output

```
./out/email-set/00.png         # mug
./out/email-set/01.png         # notebook
./out/email-set/02.png         # pencil
./out/email-set/manifest.jsonl # three JSONL lines, one per image
```

Order in the output directory matches the order of prompts in the list/file. The indices are stable even when calls finish out of order (the parallel executor races; the indices don't).

## Tuning `parallel`

`parallel` is the max number of concurrent bridge calls. Default is `2`. For three prompts with `parallel=3` the wall-clock time is roughly one call's worth. For three prompts with `parallel=1` it's three calls' worth (effectively sequential). Raise it when you have CPU/network headroom and lower it if you start seeing rate-limit warnings in `ImagenResult.warnings`.

## When to use `parallel`

- Multiple unrelated assets in one run.
- You don't care about a shared style — each prompt is its own brief.
- You want speed.

## When to switch

- Want the images to share a visual language → [`branded_parallel`](./branded_parallel.md).
- Want variations of one idea, not unrelated subjects → [`variants`](./variants.md).
- Want each frame to build on the prior → [`chain`](./chain.md).
