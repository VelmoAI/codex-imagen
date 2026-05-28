# Python SDK reference

```python
from codex_imagen import imagen
```

---

## `imagen(**kwargs) → ImagenResult`

Single entry point for all five batch modes. All parameters are forwarded to
`ImagenOptions` — unknown kwargs raise `TypeError`; invalid values raise
`ValueError`.

```python
from codex_imagen import imagen

# Minimal — one image
result = imagen(prompt="a ceramic coffee mug")

# Branded-parallel website set
result = imagen(
    prompt=["hero section", "features section", "pricing section"],
    anchor="warm editorial, soft navy accents, generous whitespace",
    batch_mode="branded-parallel",
    parallel=3,
    output_dir="./out/website",
)

if not result.ok:
    raise RuntimeError(result.error)

for img in result.images:
    print(img.path, img.bytes)
```

---

## `ImagenOptions` — all fields

Pass any of these as kwargs to `imagen()`.

### Core input

| Field | Type | Default | Description |
|---|---|---|---|
| `prompt` | `str \| dict \| list` | required | String, Codex labeled-spec dict, or list of either |
| `output_dir` | `str \| Path` | `"./out"` | Where images + manifest are written |

### Modes

| Field | Type | Default | Description |
|---|---|---|---|
| `mode` | `str` | `"auto"` | `auto`, `raw`, `medium`, `high`, `max` |
| `batch_mode` | `str` | `"auto"` | `auto`, `single`, `parallel`, `variants`, `chain`, `branded-parallel` |
| `chain_mode` | `str` | `"anchor+previous"` | Chain context strategy |
| `anchor` | `str \| dict \| None` | `None` | Explicit anchor for branded-parallel/chain |
| `count` | `int` | `1` | Variant count (variants mode only) |

### Content

| Field | Type | Default | Description |
|---|---|---|---|
| `references` | `tuple[str, ...]` | `()` | Reference image paths |
| `skills` | `tuple[str, ...]` | `()` | Skill `.md` file paths |
| `mask` | `str \| None` | `None` | Alpha mask for inpainting |
| `extra_instructions` | `str \| None` | `None` | Appended to prompt builder output |

### Image parameters

| Field | Type | Default | Description |
|---|---|---|---|
| `size` | `str` | `"auto"` | `"auto"` or `"WIDTHxHEIGHT"` (both axes ×16, max 3840px, ratio ≤ 3:1) |
| `output_format` | `str` | `"png"` | `"png"`, `"jpeg"`, `"webp"` |

### Transparency

| Field | Type | Default | Description |
|---|---|---|---|
| `transparent` | `bool` | `False` | Enable chroma-key pipeline |
| `chroma_key` | `str` | `"#00FF00"` | Background key color |
| `chroma_tolerance` | `int` | `40` | 0–100 |
| `chroma_despill` | `bool` | `True` | Run despill pass |
| `chroma_despill_mode` | `str` | `"dominance"` | `"dominance"` or `"projection"` |
| `chroma_edge_erode_px` | `int` | `1` | Alpha mask erosion pixels |
| `chroma_auto_key` | `str \| None` | `"border"` | Auto-detect key color: `"border"`, `"corners"`, or `None` |
| `chroma_transparent_threshold` | `float` | `12.0` | Distance ≤ this → fully transparent |
| `chroma_opaque_threshold` | `float` | `220.0` | Distance ≥ this → fully opaque |

### Orchestration

| Field | Type | Default | Description |
|---|---|---|---|
| `parallel` | `int` | `2` | Max concurrent bridge calls |
| `wall_clock_timeout` | `float` | `240.0` | Per-call timeout in seconds. Do not set below 60. |

### Misc

| Field | Type | Default | Description |
|---|---|---|---|
| `enhance_prompt` | `bool` | `False` | Extra polish pass on prompt |
| `vars` | `dict[str, str]` | `{}` | Variable substitutions for templated prompts |
| `advanced` | `dict[str, Any]` | `{}` | Pass-through kwargs to codex-image-gen bridge |

---

## `ImagenResult`

```python
@dataclass(frozen=True)
class ImagenResult:
    ok: bool                        # True if at least one image generated
    mode: str                       # resolved reasoning mode (raw/medium/high/max)
    batch_mode: str                 # resolved batch mode
    images: tuple[ImagenImage, ...]
    manifest_path: Path | None      # path to manifest.jsonl
    elapsed_ms: int
    health: ImagenHealth
    error: str | None               # set when ok=False
    warnings: tuple[str, ...]       # non-fatal issues
```

**Always check `ok` before accessing `images`:**

```python
if not result.ok:
    print(f"Failed: {result.error}")
else:
    for img in result.images:
        print(img.path)
```

---

## `ImagenImage`

One generated image entry.

```python
@dataclass(frozen=True)
class ImagenImage:
    index: int              # 0-based position in the batch
    path: Path              # absolute path to saved file
    bytes: int              # file size in bytes
    mime_type: str          # e.g. "image/png"
    response_id: str | None
    call_id: str | None
    revised_prompt: str | None  # model's rewrite of your prompt, if any
    original_prompt: str        # your prompt as submitted
    final_prompt: str           # prompt as sent to the bridge
    transparent: bool
    references_used: tuple[str, ...]
    raw_png_path: Path | None   # opaque render (set when transparent=True)
```

---

## Common patterns

### Single image

```python
result = imagen(prompt="a brass diya oil lamp, editorial product shot")
print(result.images[0].path)
```

### Branded-parallel for websites

```python
result = imagen(
    prompt=[
        "hero: bold product, above the fold",
        "feature 1: speed metric visualization",
        "feature 2: security and trust, clean icons",
        "footer: minimal brand mark",
    ],
    anchor="warm off-white editorial, soft navy, generous whitespace, modern sans-serif",
    batch_mode="branded-parallel",
    mode="medium",
    parallel=3,
    output_dir="./out/landing",
    size="1536x1024",
)
```

### Chain for narrative storyboard

```python
result = imagen(
    prompt=["scene 1: dawn", "scene 2: noon", "scene 3: dusk", "scene 4: night"],
    batch_mode="chain",
    chain_mode="anchor+previous",
    output_dir="./out/storyboard",
)
```

### Variants for art direction

```python
result = imagen(
    prompt="abstract geometric logo, single-color, minimal",
    batch_mode="variants",
    count=4,
    output_dir="./out/logo-options",
)
```

### Transparent cut-out

```python
result = imagen(
    prompt="a red apple, isolated subject",
    transparent=True,
    output_dir="./out/cutouts",
)
# result.images[0].path → RGBA PNG
# result.images[0].raw_png_path → opaque debug render
```

### Wall-clock timeout (long sessions)

```python
result = imagen(
    prompt="very complex multi-subject scene",
    mode="max",
    wall_clock_timeout=300,  # 5 minutes; default is 240s
)
```

### Output manifest

Every successful generation writes a line to `<output_dir>/manifest.jsonl`:

```python
import json
from pathlib import Path

manifest = Path("./out/manifest.jsonl").read_text()
for line in manifest.strip().splitlines():
    entry = json.loads(line)
    print(entry["path"], entry["mode"], entry["batch_mode"])
```

---

## Related

- [Batch modes](modes.md) — mode-by-mode guide with examples
- [Reasoning modes](reasoning.md) — `mode` parameter in depth
- [Transparency](transparency.md) — all chroma parameters explained
- [Skills](skills.md) — `skills` parameter
- [CLI reference](cli.md) — same options as CLI flags
