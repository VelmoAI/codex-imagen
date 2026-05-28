# Batch modes

`codex-imagen` supports five batch modes. Set `batch_mode=` explicitly or let
`auto` (default) pick the right one based on prompt shape.

The mode is recorded in the manifest (`batch_mode` field) and on
`ImagenResult.batch_mode` for debuggability.

---

## `single`

One prompt → one image.

```python
from codex_imagen import imagen

result = imagen(prompt="a ceramic coffee mug, minimal hero shot")
print(result.images[0].path)
```

**When to use:** any single-image need. This is the default when you pass a
plain string and no batch hints are set.

**Output shape:** `result.images` has exactly one entry.

---

## `parallel`

N independent prompts → N independent images, generated concurrently.

```python
result = imagen(
    prompt=["a coffee mug", "a notebook", "a ballpoint pen"],
    batch_mode="parallel",
    parallel=3,
)
```

**When to use:** unrelated assets that do not need to match each other visually.
If they need to match, use `branded-parallel` instead.

**Output shape:** `result.images[i]` corresponds to `prompt[i]`.

---

## `branded-parallel`

N prompts + an anchor → anchor is generated first, then all others are generated
in parallel using the anchor image as a visual style reference. All images share
the anchor's visual language (palette, typography, lighting, mood).

```python
result = imagen(
    prompt=[
        "hero section: bold product shot, above the fold",
        "features section: speed and performance, abstract motion",
        "pricing section: clean table layout on light background",
        "footer section: minimal brand mark and contact info",
    ],
    anchor="warm off-white editorial, soft navy accents, generous whitespace, modern sans-serif",
    batch_mode="branded-parallel",
    parallel=3,
    output_dir="./out/landing-page",
)
```

If `anchor` is omitted, `prompt[0]` is used as the anchor prompt and the
remaining prompts are generated referencing it.

**When to use:** website sections, brand kits, hero + feature image sets — any
multi-image work that needs visual consistency. Use ONE `branded-parallel` call
instead of N separate calls; N separate calls produce N unrelated images.

**Output shape:** `result.images[0]` is the anchor; `result.images[1:]` are the
dependent images in prompt order.

**CLI:**

```bash
imagen -f sections.txt \
  --batch-mode branded-parallel \
  --anchor "warm off-white editorial, soft navy accents" \
  --parallel 3 \
  --output-dir ./out/landing-page
```

---

## `chain`

N prompts, executed sequentially. Each call receives the previous image as a
reference, maintaining visual continuity across the set.

```python
result = imagen(
    prompt=["scene 1: dawn mist", "scene 2: noon sunlight", "scene 3: dusk glow"],
    batch_mode="chain",
    chain_mode="anchor+previous",  # default
)
```

`chain_mode` options:

| Value | Behaviour |
|---|---|
| `previous` | Each call references only the immediately prior image |
| `anchor` | Each call references only the first image (anchor) |
| `anchor+previous` | Each call references both anchor and the prior image (default) |
| `window:N` | Each call references the last N images |
| `all` | Each call references all previous images |

**When to use:** storyboards, before/after panels, sequential narrative.

**Output shape:** `result.images[i]` corresponds to `prompt[i]` in order.

---

## `variants`

One prompt + `count=N` → N stylistic variations of the same subject. The prompt
is used for every call; the model introduces variation naturally.

```python
result = imagen(
    prompt="abstract geometric logo, minimal, single color",
    batch_mode="variants",
    count=4,
)
```

**When to use:** exploring art direction, A/B options for a single asset.

**Output shape:** `result.images` has `count` entries, all derived from the same
prompt.

---

## Frequency guide

| Mode | Best for | Typical call count |
|---|---|---|
| `single` | One hero shot | 1 |
| `parallel` | Unrelated assets | N |
| `branded-parallel` | Website sections, brand sets | 1 + N parallel |
| `chain` | Storyboards, narratives | N sequential |
| `variants` | Art direction exploration | N |

---

## Related

- [Reasoning modes](reasoning.md) — controls prompt polish quality
- [SDK reference](sdk.md) — full `ImagenOptions` parameter list
- [CLI reference](cli.md) — `--batch-mode`, `--anchor`, `--chain-mode` flags
