# Single image

The simplest mode. One prompt in, one image out. Every other option has a sensible default so you can get to a first image in two lines of code.

## Use case

You want a single hero shot — a product image for a landing page, a placeholder for a blog post, a stock-photo replacement. No batching, no chain, no anchor.

## SDK

```python
from codex_imagen import imagen

result = imagen(
    prompt="a ceramic coffee mug, top-down hero shot on warm beige paper, "
           "soft morning light, minimalist editorial style",
    output_dir="./out/coffee",
)

assert result.ok
print(result.images[0].path)            # ./out/coffee/00.png
print(result.mode)                      # 'raw' (auto-resolved for plain strings)
print(result.batch_mode)                # 'single'
print(f"elapsed: {result.elapsed_ms} ms")
```

## CLI

```bash
imagen "a ceramic coffee mug, top-down hero shot on warm beige paper, \
soft morning light, minimalist editorial style" \
  --output-dir ./out/coffee
```

Default size is `auto` (the bridge picks a reasonable square). Pin the size explicitly when you need landscape or portrait:

```bash
imagen "a ceramic coffee mug, top-down hero shot ..." \
  --size 1536x1024 \
  --output-dir ./out/coffee
```

## Expected output

```
./out/coffee/00.png            # the generated image
./out/coffee/manifest.jsonl    # one JSONL line with the run metadata
```

The manifest line records `mode`, `batch_mode`, `size`, the final prompt sent to the bridge, the response/call IDs, and elapsed milliseconds. It is append-only — you can re-run `imagen` against the same directory and every call lands as a new line.

## When to use `single`

- One image, no narrative or set membership.
- Quick one-off generation from a script.
- Smoke-testing your install (`imagen --health` then this).

## When to switch to another mode

- Need several unrelated images in one run → [`parallel`](./parallel.md).
- Want to explore variations of the same idea → [`variants`](./variants.md).
- Need a sequence where each frame remembers the prior → [`chain`](./chain.md).
- Need a set of images that all share a visual language → [`branded_parallel`](./branded_parallel.md).
