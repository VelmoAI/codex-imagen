# Transparent logo (chroma-key pipeline)

The Codex OAuth bridge silently rejects `background: "transparent"`. To deliver a real RGBA PNG with alpha, `codex-imagen` instead asks the model to render the subject on a solid magenta backdrop, then keys that magenta out in Pillow — with despill, edge feathering, and a debug raw artifact kept on disk.

## Use case

A logo, an icon, a product cutout, a sticker — anything that needs to drop onto an arbitrary background without a visible matte.

## How it works (3 steps)

1. `forge` prepends a chroma-key instruction block to `instructions=`, asking the model to place the subject on a solid `#FF00FF` (magenta) backdrop with no shadows touching the edges.
2. The bridge generates normally with `background="opaque"` (forced — the bridge refuses `"transparent"`).
3. `_chroma.keyout()` runs in Pillow: every pixel close to magenta becomes transparent, the subject's edges are despilled (the magenta tint is subtracted from RGB fringes), and the alpha edges are softly feathered for a clean cutout.

The raw opaque PNG is preserved as `<name>.raw.png` next to the keyed PNG so you can debug if a key looks chewed up.

## SDK

```python
from codex_imagen import forge

result = forge(
    prompt="an abstract leaf logo, simple geometric shapes, single-color "
           "deep green, centered on the canvas, no background elements, "
           "no drop shadow",
    transparent=True,
    chroma_tolerance=40,        # 0-100, default 40; raise if edges look messy
    chroma_despill=True,        # neutralizes magenta halo at edges (recommended)
    output_dir="./out/logo",
    size="1024x1024",
)

img = result.images[0]
print(img.path)              # ./out/logo/00.png         (RGBA, transparent)
print(img.raw_png_path)      # ./out/logo/00.raw.png     (opaque, magenta bg)
print(img.transparent)       # True
```

## CLI

```bash
imagen "an abstract leaf logo, simple geometric shapes, single-color \
deep green, centered on the canvas, no background elements, no drop shadow" \
  --transparent \
  --chroma-tolerance 40 \
  --chroma-despill \
  --size 1024x1024 \
  --output-dir ./out/logo
```

## Expected output

```
./out/logo/00.png            # RGBA PNG, magenta keyed to alpha
./out/logo/00.raw.png        # original opaque render with magenta backdrop
./out/logo/manifest.jsonl    # manifest line includes transparent=true and the raw path
```

Drop `00.png` onto a dark background, a photograph, anywhere — the alpha is clean.

## Tuning the tolerance

`chroma_tolerance` controls how aggressively pixels-close-to-magenta are treated as background.

| Symptom | Fix |
|---|---|
| Visible magenta halo at the subject's edges | raise tolerance (try `55`), confirm `chroma_despill=True` |
| Holes punched through the subject (parts of the logo went transparent) | lower tolerance (try `25`) — your subject has magenta-adjacent colors |
| Hairy / jagged edges | leave tolerance alone; consider regenerating with the model's "no drop shadow" constraint reinforced |
| Soft / blurry edges | tolerance is fine; the chroma-key feather is intentional (2 px) for clean compositing |

## When the subject color clashes with magenta

If your logo is itself pink/magenta-adjacent, switch the chroma key to a color the subject definitely doesn't use:

```python
forge(
    prompt="a hot pink stylized heart icon",
    transparent=True,
    chroma_key="#00FF00",          # bright green instead of magenta
    chroma_tolerance=35,
)
```

The instructions block is regenerated to ask for the new backdrop color automatically.

## Debugging a bad key

If `00.png` looks wrong, open `00.raw.png` first.

- If the raw image has anything other than solid magenta in the background (gradient, shadow, subtle pattern), the model didn't follow the chroma-key instruction. Try `mode="high"` or `mode="max"`, or strengthen the prompt with explicit phrasing like *"the entire background must be pure flat #FF00FF magenta with absolutely no shadows, gradients, or texture"*.
- If the raw image is correct but the keyed image looks bad, this is a tolerance / despill tuning problem — adjust the knobs above.

## Constraints worth knowing

- Requires Pillow (`pip install Pillow` — listed as a runtime dependency, so it's installed automatically with `codex-imagen`).
- The bridge does not support a *native* transparent background. The chroma-key pipeline is the supported path; the SPEC's [Verified bridge constraints](../README.md#verified-bridge-constraints) table documents why.
- The chroma pipeline is opt-in via `transparent=True`. Default output is opaque RGB.

## When to use `transparent`

- Logos, icons, stickers, product cutouts.
- Any asset that will later be composited onto an unknown background.

## When not to

- Photographic hero images where a colored or scenic background is part of the deliverable.
- Subjects whose natural colors include large areas of the chroma-key color (or pick a different `chroma_key=`).
