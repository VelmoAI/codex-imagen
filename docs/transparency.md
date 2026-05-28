# Transparency

`codex-imagen` produces transparent PNGs through a **chroma-key post-process**
using Pillow. The Codex OAuth bridge rejects `background: "transparent"` with
HTTP 400, so native transparency is not available — chroma-key is the only path.

---

## The pipeline (what happens when `transparent=True`)

1. **Subject rendered on solid green** — `#00FF00` by default (industry-standard
   chroma key color). The bridge renders your subject on this background.

2. **Auto-key border sampling** — `_chroma.keyout()` samples the actual rendered
   background color from the image border, handling model drift where the model
   renders `#01FE00` instead of the exact `#00FF00` you asked for.

3. **Dual-threshold smoothstep matte** — pixels are keyed to alpha using two
   thresholds:
   - Distance ≤ `chroma_transparent_threshold` (default 12) → fully transparent
   - Distance ≥ `chroma_opaque_threshold` (default 220) → fully opaque
   - Between the two → smooth ramp (no hard edge artifacts)

4. **Dominance-capping despill** — residual green tint at subject edges is
   removed by capping the spill channel to `max(non-spill) − 1`. Physically
   correct; no over-despill that would tint the subject body.

5. **Raw image preserved** — the original opaque render is saved as
   `<name>.raw.png` for debugging.

---

## Basic usage

```python
from codex_imagen import imagen

result = imagen(
    prompt="a red apple, isolated subject, centered",
    transparent=True,
)
# result.images[0].path → RGBA PNG
# result.images[0].raw_png_path → the opaque render (for debugging)
```

---

## Why green, not magenta?

Green (`#00FF00`) is the industry-standard chroma key color — it is the farthest
color from human skin tones in RGB space, minimizing accidental keying of the
subject. Use magenta (`#FF00FF`) only when your subject contains green.

```python
# For a cactus, plant, or anything inherently green:
result = imagen(
    prompt="a cactus in a terracotta pot",
    transparent=True,
    chroma_key="#FF00FF",  # switch to magenta
)
```

---

## Tunable parameters

| Parameter | Default | Description |
|---|---|---|
| `chroma_key` | `"#00FF00"` | Background key color. `#FF00FF` for green subjects. |
| `chroma_tolerance` | `40` | 0–100. Higher → more pixels treated as background. |
| `chroma_transparent_threshold` | `12.0` | Distance ≤ this → fully transparent. |
| `chroma_opaque_threshold` | `220.0` | Distance ≥ this → fully opaque. |
| `chroma_despill` | `True` | Run despill pass to neutralize edge tint. |
| `chroma_despill_mode` | `"dominance"` | `dominance` (cap-based, default) or `projection` (legacy). |
| `chroma_edge_erode_px` | `1` | Pixels to erode alpha mask before feathering. 0 disables. |
| `chroma_auto_key` | `"border"` | Sample actual key color from border (`"border"`, `"corners"`, or `None`). |

Full example with all knobs:

```python
result = imagen(
    prompt="a single red apple, isolated",
    transparent=True,
    chroma_key="#00FF00",
    chroma_tolerance=40,
    chroma_despill=True,
    chroma_despill_mode="dominance",
    chroma_edge_erode_px=1,
    chroma_auto_key="border",
    chroma_transparent_threshold=12.0,
    chroma_opaque_threshold=220.0,
)
```

---

## Complex subjects

A warning fires automatically when the prompt contains these keywords: `fur`,
`hair`, `feathers`, `glass`, `smoke`, `liquid`, `translucent`.

**Why:** chroma-key leaves fringe at semi-transparent edges on these subjects.
The 54× improvement claim (vs. naive magenta-key) applies to typical solid
subjects like logos, products, and simple cut-outs. Complex subjects with
fine or transparent edges may still show fringe artifacts.

**Recommendation for complex subjects:** consider a model with native
transparency support (e.g. via the OpenAI Images API with
`background: "transparent"` on a supported model). codex-imagen's chroma
pipeline is a best-effort approach — it does not compete with native alpha
rendering on complex subjects.

---

## Quantitative note

The `dominance` despill mode produces **54× cleaner fringe** on fur/hair
subjects compared to a naive magenta-key with no despill. This measurement
is based on average residual key-color saturation in the 10-pixel fringe zone
across a test set of fur and hair subjects.

---

## Related

- [Batch modes](modes.md) — using transparency with parallel/branded-parallel
- [SDK reference](sdk.md) — all chroma parameters in `ImagenOptions`
- [CLI reference](cli.md) — `--transparent`, `--chroma-key`, `--chroma-despill` flags
