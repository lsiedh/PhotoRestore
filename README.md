# PhotoRestore

A command-line tool that takes a folder of scanned photographs and writes
out cleaned-up versions — fading corrected, color casts neutralized,
shadows lifted, and (optionally) dust and scratches removed.

It's designed for batch work. Point it at a folder of hundreds of TIFFs,
walk away, come back to a folder of restored JPGs and a CSV report
explaining what it did to each one.

Built for people scanning a family album, a research archive, or any pile
of analog prints where retouching every photo by hand isn't realistic.

## Table of contents

- [What this tool does (and doesn't do)](#what-this-tool-does-and-doesnt-do)
- [Quick start](#quick-start)
- [Your first run, walked through](#your-first-run-walked-through)
- [How the tool decides what to do](#how-the-tool-decides-what-to-do)
- [Common tasks (cookbook)](#common-tasks-cookbook)
- [How to improve results](#how-to-improve-results)
- [Dust and scratch removal (optional)](#dust-and-scratch-removal-optional)
- [CLI reference](#cli-reference)
- [Decision report reference](#decision-report-reference)
- [How the algorithms work (deep dive)](#how-the-algorithms-work-deep-dive)
- [Limits and known issues](#limits-and-known-issues)
- [Troubleshooting](#troubleshooting)
- [Files in this repo](#files-in-this-repo)

## What this tool does (and doesn't do)

**It does:**

- **Tonal correction** — stretches the levels so blacks are black and
  whites are white again, and gently brightens crushed shadows.
- **White balance** — removes overall color casts caused by aged paper
  or scanner illumination.
- **Nonlinear dye-fading correction** — color film dyes fade at
  different rates and unevenly across the tonal range; a per-channel
  midtone gamma rebalances them after white balance, recovering muddy
  midtones that a linear correction alone can't reach.
- **B&W vs. color routing** — looks at each photo and picks an
  appropriate pipeline. A stained black-and-white print won't be
  treated as a color photo just because the stain has hue.
- **Optional defect removal** — dust specks and thin scratches, with a
  hard guarantee it never invents pixels (see
  [Dust and scratch removal](#dust-and-scratch-removal-optional)).
- **Per-image audit log** — every decision is written to a CSV so you
  can see exactly what happened to each file and tune from there.

**It does *not*:**

- Sharpen, upscale, or denoise.
- Inpaint missing content, remove faces from emulsion damage, or
  reconstruct torn corners.
- Touch the originals. Output goes to a separate folder.
- Do anything to fingerprints, water marks, or large stains. Those
  need manual work in a real photo editor.

If you want AI-style "make this old photo look new" — generative
restoration — this isn't that tool. PhotoRestore is conservative on
purpose. Every output pixel is derived from the input pixels by simple,
auditable math.

## Quick start

You'll need Python 3.9 or newer. Check:

```bash
python3 --version
```

Clone or download this repo, `cd` into it, then:

```bash
pip install -r requirements.txt
```

That installs `Pillow`, `numpy`, `opencv-python`, and `tqdm`.

Now put some scanned TIFFs in a folder (`Photos/` is the conventional
name) and run:

```bash
python3 restore.py Photos Restored --report report.csv
```

You'll get:

- `Restored/` — a new folder of corrected JPGs, one per input TIFF.
- `report.csv` — one row per photo describing what was decided and
  applied.

That's the whole flow. The rest of this README is about understanding
what just happened and how to nudge it when something looks off.

## Your first run, walked through

Let's go through a concrete example so you know what to expect.

### 1. Set up a small test folder

Don't run on a thousand photos for your very first try. Make a
`Photos/` folder and copy in three or four scans — ideally a mix:

- One color photo
- One black-and-white photo
- One that's clearly faded or stained

```
PhotoRestore/
├── restore.py
├── requirements.txt
└── Photos/
    ├── color_1985.tif
    ├── bw_grandma.tif
    └── faded_yellowed.tif
```

### 2. Do a dry run first

A "dry run" analyzes every file but doesn't write any JPGs. Fast, and
it lets you sanity-check the classifier before committing to the full
batch.

```bash
python3 restore.py Photos Restored --dry-run --report audit.csv
```

You'll see a progress bar, then a one-line summary like:

```
Found 3 files. Workers: 7. Dry-run: True
100%|██████████████████| 3/3 [00:01<00:00,  2.41it/s]
Done. B&W: 1  Color: 2  Errors: 0
Wrote report: audit.csv
```

Open `audit.csv` in a spreadsheet. The columns you care about most on a
first pass:

| Column | What to check |
|---|---|
| `classified_as` | `bw` or `color`. Did each photo get routed correctly? |
| `chroma_p95` | Higher = more colorful after stretching. The classifier's main signal. |
| `mean_a_in`, `mean_b_in` | Color cast in the *input*. Big numbers (±20) = strong cast. |
| `error` | Should be empty. If not, that file failed. |

### 3. Do the real run

If the classification looks right and there are no errors, drop the
`--dry-run` flag:

```bash
python3 restore.py Photos Restored --report report.csv
```

Now `Restored/` contains your JPGs. Open them and compare to the
originals. If they look good, you're done.

### 4. If something looks off

Before retuning anything, open `report.csv` for the photo that looks
wrong and see what was applied. Then jump to
[How to improve results](#how-to-improve-results).

## How the tool decides what to do

For each photo, the tool runs through this sequence:

1. **Read the file** as RGB.
2. **Classify** it as B&W or color.
3. **Run the matching pipeline:**
   - **B&W path:** convert to luminance, stretch levels, lift shadows,
     save as a grayscale JPG.
   - **Color path:** correct white balance, stretch each channel,
     correct nonlinear dye fading with a per-channel midtone gamma,
     lift shadows in a hue-preserving way, neutralize any residual
     color cast, save as an RGB JPG.
4. **(Optional)** run dust and scratch removal.
5. **Write the JPG** to the output folder, preserving DPI metadata.
6. **Record everything** in the report CSV.

### Why classify at all?

Because what's "right" for a yellowed B&W print is different from
what's "right" for a faded color photo. If you treat a stained B&W
photo as color, the per-channel level stretch will turn the stain into
a vivid hue (you'll get a magenta or brown cast that wasn't there
before). If you treat a faded color photo as B&W, you throw away color
information that could be recovered.

The classifier is conservative: it routes a photo to B&W if **either**
of two signals indicates monochrome:

- Input chroma is already very low (it's a near-grayscale scan), or
- After a tentative per-channel stretch, the chroma 95th percentile
  *stays* low (the photo doesn't have real color signal hiding under
  the fade).

If both signals fail, the photo is treated as color. Misroutes are
rare but they happen — usually on heavily yellowed B&W prints. You can
override per-file with `--force-bw` or `--force-color`. See
[Common tasks](#common-tasks-cookbook).

## Common tasks (cookbook)

### Restore a folder of TIFFs (minimal)

```bash
python3 restore.py Photos Restored
```

Reads every `.tif`/`.tiff` in `Photos/` and writes corrected JPGs to
`Restored/`. Uses every default. Fine for a quick try; for any real
batch, add `--report`.

### Standard run with a decision report

```bash
python3 restore.py Photos Restored --report report.csv
```

Recommended for any non-trivial run. The report is what you'll consult
when something doesn't look right.

### Dry run — analyze without writing JPGs

```bash
python3 restore.py Photos Restored --dry-run --report audit.csv
```

The fastest way to sanity-check classification and tone parameters
before committing. You can iterate on flags here, then drop
`--dry-run` once the report looks right.

### Force a single photo to the B&W pipeline

When a yellowed B&W print is being misclassified as color:

```bash
python3 restore.py Photos Restored \
    --force-bw "Image_20260403_0001.tif" \
    --report report.csv
```

The pattern is a Unix shell glob (matched with `fnmatch`), so you can
also do:

```bash
--force-bw "*_portrait_bw.tif"
```

The flag is repeatable for multiple patterns:

```bash
python3 restore.py Photos Restored \
    --force-bw "Image_20260403_0001.tif" \
    --force-bw "Image_20260403_0042.tif" \
    --force-color "*_color_print.tif" \
    --report report.csv
```

### Force a photo to the color pipeline

When a heavily faded color photo is being misclassified as B&W:

```bash
python3 restore.py Photos Restored \
    --force-color "fade_*.tif" \
    --report report.csv
```

### Conservative tone — preserve highlights, gentler shadow lift

```bash
python3 restore.py Photos Restored \
    --white-point 99.0 \
    --shadow-min-gamma 0.8 \
    --report report.csv
```

Pulls the highlight clip back from `99.5%` to `99.0%` (less risk of
blowing out skies and skin) and softens how much shadows can be
brightened.

### Aggressive contrast — deeper shadows, brighter highlights

```bash
python3 restore.py Photos Restored \
    --black-point 0.2 \
    --white-point 99.8 \
    --shadow-min-gamma 0.5
```

### Disable shadow lift entirely

```bash
python3 restore.py Photos Restored --shadow-target 0
```

White balance, percentile stretch, dye-fading correction, and the
neutral-cast correction still apply — only the adaptive shadow
brightening is skipped.

### Higher-quality JPG with a custom suffix

```bash
python3 restore.py Photos Restored --quality 98 --suffix _restored
```

Writes `Image_0001_restored.jpg` etc. at quality 98. Useful when the
output folder already contains the originals.

### Single-threaded run (for debugging)

```bash
python3 restore.py Photos Restored --jobs 1 --report report.csv
```

Easier to read tracebacks because errors aren't interleaved across
worker processes.

### Process JPG/PNG inputs instead of TIFFs

```bash
python3 restore.py Scans Restored --ext jpg,jpeg,png --suffix _restored
```

### Tighten the B&W classifier (treat more photos as color)

```bash
python3 restore.py Photos Restored --bw-threshold 5.0 --bw-input-std 2.5
```

### Loosen the B&W classifier (treat more stained prints as B&W)

```bash
python3 restore.py Photos Restored --bw-threshold 12.0 --bw-input-std 6.0
```

### Disable residual color-cast neutralization

```bash
python3 restore.py Photos Restored --neutralize-strength 0
```

Useful when a warm or cool cast is part of the photo (sunsets,
candlelit interiors, underwater shots). The default neutralization is
already gentle, but `0` turns it off completely.

### Soften or disable dye-fading correction

```bash
python3 restore.py Photos Restored --dye-strength 0.3   # gentler
python3 restore.py Photos Restored --dye-strength 0     # off
```

The per-channel midtone gamma defaults to `0.5`. Lower it for a batch
of intentionally warm/cool images where the default pulls the midtones
too far toward neutral, or set `0` to disable it entirely and keep the
old linear-only color behavior. The gammas actually applied are logged
as `dye_gamma_R/G/B` in the report, so tune from there.

### Remove dust and thin scratches

```bash
python3 restore.py Photos Restored --despeckle --report report.csv
```

See [Dust and scratch removal](#dust-and-scratch-removal-optional).

## How to improve results

The single most useful debugging tool is `--report`. When a photo
doesn't look right, find its row in the CSV before changing any flags.
Each problem below maps to specific columns and specific fixes.

### "It got routed wrong (B&W treated as color, or vice versa)"

Look at: `classified_as`, `chroma_p95`, `std_a`, `std_b`.

- A B&W print routed as color usually has `chroma_p95` just barely
  above `--bw-threshold` (default `8.0`). The fix is one of:
  - **Per-file override** (safest): add `--force-bw "filename.tif"`.
  - **Raise the threshold** (affects all photos): try
    `--bw-threshold 12.0`.
- A faded color photo routed as B&W usually has `chroma_p95` just
  barely below the threshold. Either `--force-color` it or *lower*
  `--bw-threshold` to e.g. `5.0`.

Per-file overrides are almost always safer than retuning thresholds —
a threshold change can shift other borderline photos in unexpected
ways.

### "Shadows are still too dark"

Look at: `shadow_p5_pre`, `shadow_lift_gamma`.

- `shadow_p5_pre` is the 5th-percentile luminance after the level
  stretch — basically, "how dark are the dark parts." If it's near `0`
  and `shadow_lift_gamma` is at the floor (e.g. `0.7`), the lift
  hit its safety cap.
- Lower `--shadow-min-gamma` (e.g. to `0.5`) to allow stronger lifts,
  or raise `--shadow-target` (e.g. to `0.15`) to ask for brighter
  shadows.
- If shadows look fine on most photos but one specific photo is too
  dark, you generally don't want to retune globally — it's better to
  re-run just that file with stronger flags.

### "Shadows are washed out / midtones look milky"

The lift was too aggressive. Raise `--shadow-min-gamma` toward `0.8`
or `1.0` (`1.0` disables the lift entirely), or set `--shadow-target 0`.

### "Highlights are blown out (skies, skin, white shirts)"

Lower `--white-point` from `99.5` toward `99.0` or `98.5`. The
percentile is the share of pixels *kept* below the white clip — at
`99.5`, the brightest 0.5% of pixels become pure white; at `99.0`, the
brightest 1% do.

### "Blacks are crushed / dark detail is lost"

Raise `--black-point` from `0.5` toward `1.0`–`2.0`. (Yes,
counter-intuitive — *higher* `--black-point` = *more* shadow detail
preserved, because fewer pixels get clipped to pure black.)

### "Colors look wrong after restoration"

Look at: `illum_R/G/B`, `mean_a_out`, `mean_b_out`.

- `illum_R/G/B` is the white-balance estimate (mean-normalized to
  `1.0`). If one channel is wildly off (e.g. `0.6` or `1.5`), the
  Shades-of-Gray estimator was misled — usually by a photo that's
  dominated by a single hue (a forest, a sunset, a baby in a pink
  blanket).
- `mean_a_out`/`mean_b_out` is the residual cast after every
  correction. Should be near `0`. If `|mean_a_out| > 5` or
  `|mean_b_out| > 5`, the neutralization couldn't fully resolve it.
- If the result feels too neutral on a photo where warmth or coolness
  is intentional (sunsets, candlelight), turn off the neutralizer:
  `--neutralize-strength 0`.

### "I want to apply different settings to different photos"

Right now there are two paths:

1. Use `--force-bw` / `--force-color` globs for routing overrides.
2. Run the tool multiple times against subset folders or with
   different `--ext` filters, writing to different output folders.

There's no per-file tone override yet. For one or two outliers, it's
usually faster to copy them to a separate folder and re-run the tool
with adjusted flags on just that folder, using `--suffix` to keep the
filenames distinct.

### Recommended tuning workflow

1. Run `--dry-run --report audit.csv` on the full batch.
2. Open the CSV in a spreadsheet, sort by `chroma_p95` to find
   borderline classifications, scan `error` for failures.
3. Pick a handful of photos — the worst-looking, the borderline ones,
   the most colorful ones — and do small real runs on just those (drop
   them in a temporary folder).
4. Iterate flags on that small set until happy.
5. Re-run on the full batch.

Reach for `--shadow-min-gamma` first when fixing exposure (it directly
caps how much midtones can be brightened), then `--shadow-target`,
then the percentile points.

## Dust and scratch removal (optional)

This is an opt-in stage. Off by default. Enable it with `--despeckle`:

```bash
python3 restore.py Photos Restored --despeckle --report report.csv
```

When it runs, the report gains four columns:
`despeckle_applied`, `despeckle_mask_pixels`,
`despeckle_mask_components`, `despeckle_largest_component`.

### What it can remove

- **Round dust specks** — single-pixel and small (a few pixels)
  outliers on otherwise smooth areas.
- **Thin scratches** — linear defects up to about 3 pixels wide at
  the default `--despeckle-scratch-length 7`, up to about 4 pixels at
  length `9`.

### What it cannot (and won't) remove

- **Wide defects** — anything much wider than `scratch_length / 2`.
- **Fingerprints** — too large and too low-contrast to remove without
  inventing image content.
- **Water marks, fungal spots, emulsion damage** — these sit on real
  image gradient and are correctly protected as content.
- **1-px-wide real features** (eyelashes, fine hair, distant wires) —
  geometrically indistinguishable from scratches at the same scale.
  Both get smoothed.

By construction, every replacement value is the median of real
neighboring pixels — the algorithm cannot synthesize a value that
wasn't already in the image. This is a hard guarantee, not a
heuristic.

### Tuning despeckle

The defaults are tuned for typical 600-DPI scans. Common adjustments:

#### More aggressive scratch removal

```bash
python3 restore.py Photos Restored \
    --despeckle \
    --despeckle-threshold 0.04 \
    --despeckle-scratch-length 9 \
    --report report.csv
```

Lower threshold catches fainter scratches; longer kernel bridges
slightly wider ones. **Watch the `despeckle_largest_component` column
in the report** — if it grows into the thousands, real low-contrast
lines may be getting touched. Either raise `--despeckle-edge-protect`
or back off these knobs.

#### Conservative — round dust only, no scratch pass

```bash
python3 restore.py Photos Restored \
    --despeckle \
    --despeckle-scratch-length 0 \
    --report report.csv
```

Skips the oriented stage and runs only the 3×3 isotropic median.
Useful when a photo has fine real lines (chains, distant wires, stray
hairs) that the oriented stage might touch.

#### Protect more fine detail

```bash
python3 restore.py Photos Restored \
    --despeckle \
    --despeckle-edge-protect 0.10 \
    --report report.csv
```

A higher edge-protect threshold means more pixels are flagged as
"real edge — leave alone." Combine with a higher
`--despeckle-threshold` (e.g. `0.10`) for a very gentle pass.

For the algorithm details, see
[How the algorithms work](#how-the-algorithms-work-deep-dive).

## CLI reference

### Positional arguments

| Argument     | Description                                                   |
|--------------|---------------------------------------------------------------|
| `input_dir`  | Directory containing source images (TIFFs by default).        |
| `output_dir` | Directory where restored JPGs are written. Created if absent. |

### General options

#### `--quality INT` (default `92`)

JPEG quality, 1–100. The encoder uses `subsampling=0` (4:4:4 chroma)
and `optimize=True` regardless of quality. DPI metadata is preserved
from the source TIFF when present.

#### `--jobs INT` (default `cpu_count() - 1`)

Number of parallel worker processes. Set `--jobs 1` for sequential
execution (useful for debugging or low-memory machines). One worker
per file, dispatched via `multiprocessing.Pool.imap_unordered`.

#### `--ext STR` (default `tif,tiff,TIF,TIFF`)

Comma-separated list of input extensions (no leading dot). Files with
other extensions in `input_dir` are ignored. Add `jpg,jpeg,png` etc.
to process non-TIFF inputs.

#### `--suffix STR` (default `""`)

Suffix appended to the output filename stem. With `--suffix _restored`,
`Image_0001.tif` becomes `Image_0001_restored.jpg`.

#### `--report PATH`

Path to a per-image CSV decision report. See
[Decision report reference](#decision-report-reference).

#### `--dry-run`

Run the full classifier and analysis but **do not write JPGs**.
Combined with `--report`, this is the fastest way to audit the
classifier and tone parameters before committing to a full batch.

### Tone correction

#### `--black-point FLOAT` (default `0.5`)

Low percentile for the per-channel level stretch. The lowest
`black-point`% of pixel values in each channel is clipped to 0. A
value of `0.5` means the bottom 0.5% of pixels become pure black.
Raise to `1.0`–`2.0` for more conservative shadow placement; lower
toward `0.1` for more aggressive contrast.

#### `--white-point FLOAT` (default `99.5`)

High percentile for the per-channel level stretch. The top
`100 − white_point`% of pixels are clipped to 1.0. Lower (e.g. `99.0`)
to preserve more highlight detail; raise toward `99.9` for more
aggressive contrast.

#### `--shadow-target FLOAT` (default `0.10`)

Target value (0–1) for the 5th-percentile luminance after the stretch
is applied. When the stretched p5 falls below this target, an adaptive
gamma is solved that would lift p5 up to `--shadow-target`. Higher
values produce brighter shadows; set to `0` to disable the lift
entirely.

#### `--shadow-min-gamma FLOAT` (default `0.7`)

Floor on the shadow-lift gamma. The adaptive solver may demand a very
small gamma when an image has crushed shadows (`p5 ≈ 0`), which would
wash the photo out. This floor caps how aggressive the lift can be.
Rough rule of thumb on midtones:

| `min_gamma` | Lift on L*=0.5  | Lift on L*=0.7  |
|-------------|-----------------|-----------------|
| `0.5`       | +1.0 stop       | +0.5 stop       |
| `0.7`       | +0.4 stop       | +0.2 stop       |
| `0.8`       | +0.3 stop       | +0.1 stop       |
| `1.0`       | (lift disabled) | (lift disabled) |

#### `--neutralize-strength FLOAT` (default `0.5`)

Alpha for the damped LAB neutralization stage on the color path. After
white balance and stretching, if `|mean(a*)| > 3` or `|mean(b*)| > 3`,
the mean is shifted toward zero by `alpha` of its value. Conservative
on purpose — a sunset *should* be warm. Set `0` to disable.

#### `--dye-strength FLOAT` (default `0.5`)

Strength of the per-channel midtone gamma correction for nonlinear dye
fading, on the color path. White balance and the level stretch are
linear and only move the endpoints; photographic dye layers fade
nonlinearly at different rates, so a faded print's midtones stay
color-skewed even after both run. This stage takes each RGB channel's
median, sets a neutral target at the geometric mean of the three
medians (clamped to `[0.15, 0.85]`), and applies a per-channel gamma
`g = log(target)/log(median)`, damped toward `1.0` by this factor.

A near-grayscale image has near-equal medians, so all three gammas land
at ~1.0 and the stage is a no-op. A genuinely warm or cool scene
(sunset, candlelight) is only *partially* pulled toward neutral at the
default `0.5`; lower it or set `0` to opt out entirely. The gammas
actually applied are recorded as `dye_gamma_R/G/B` in the report CSV.

### Despeckle options

All require `--despeckle` to be set.

#### `--despeckle` (default off)

Enable selective-median dust/scratch removal. See
[Dust and scratch removal](#dust-and-scratch-removal-optional).

#### `--despeckle-threshold FLOAT` (default `0.06`)

Absolute `|pixel − chosen median|` deviation threshold in `[0, 1]`
image units. Pixels whose deviation from the best-orientation median
is below this are not modified. Raise toward `0.10`–`0.12` to be more
conservative on grainy film scans; lower toward `0.04` to catch
fainter scratches at the cost of touching more grain.

#### `--despeckle-edge-protect FLOAT` (default `0.08`)

Sobel gradient magnitude (normalized to `[0, 1]`) above which pixels
are protected from despeckle correction. Computed on the
3×3-median-filtered luminance so isolated specks don't protect
themselves. Raise toward `0.10`–`0.12` to protect more fine detail;
lower toward `0.04` to allow correction over more textured regions;
set `0` to disable the gate (rarely useful).

#### `--despeckle-scratch-length INT` (default `7`, must be odd)

Length, in pixels, of the four 1-D oriented median kernels (at 0°,
45°, 90°, 135°) that catch thin linear scratches. Longer kernels
break wider linear defects (length-7 cleanly bridges scratches up to
~3 px wide; length-9 up to ~4 px) but raise the risk of touching real
low-contrast lines. Set below `3` to skip the oriented stage entirely.
Even values are silently rounded up to odd.

#### `--despeckle-min-size INT` (default `4`)

Minimum connected-component size in pixels to correct. Components
below this are left alone — they are overwhelmingly film grain rather
than real defects. Lower toward `2`–`3` to also smooth grain at the
cost of touching genuine fine texture.

#### `--despeckle-max-size INT` (default `2000`)

Maximum connected-component size in pixels to correct. Larger blobs
are left alone — at that scale they're likely real image content. The
default is intentionally generous because long thin scratches form
components in the hundreds-to-low-thousands range; the per-pixel
edge-protect gate is the main safeguard against touching real content.
Lower toward `200`–`500` if you have no linear scratches and want a
stricter cap on dust size.

### Classifier options

#### `--bw-threshold FLOAT` (default `8.0`)

Post-stretch LAB chroma 95th-percentile threshold below which a photo
is classified as B&W. Lower values make the classifier more permissive
about treating images as color; raise it to be more conservative.

#### `--bw-input-std FLOAT` (default `4.0`)

Input `max(std a*, std b*)` below which a photo is forced to the B&W
path regardless of post-stretch chroma. Catches near-grayscale scans
where the per-channel stretch would amplify scanner noise into
apparent chroma.

#### `--force-bw GLOB` (repeatable)

Filename glob (Unix shell-style, via `fnmatch`) that forces matching
files to the B&W path regardless of classifier output. Repeat the
flag for multiple patterns:

```bash
--force-bw "Image_20260403_0001.tif" --force-bw "*_portrait.tif"
```

#### `--force-color GLOB` (repeatable)

Filename glob that forces matching files to the color path. Use when
a faded color photo with very low chroma is being routed to the B&W
pipeline.

## Decision report reference

Every column in the `--report` CSV:

| Column                        | Meaning                                                          |
|-------------------------------|------------------------------------------------------------------|
| `filename`                    | Source filename.                                                 |
| `w`, `h`                      | Pixel dimensions.                                                |
| `classified_as`               | `bw` or `color`.                                                 |
| `chroma_p95`                  | 95th-percentile LAB chroma after a tentative stretch.            |
| `std_a`, `std_b`              | Input LAB a\*/b\* standard deviation.                            |
| `mean_a_in`, `mean_b_in`      | Input LAB a\*/b\* mean (signed, neutral=0).                      |
| `black_pt_*`, `white_pt_*`    | Per-channel percentile values used for the stretch (0–1).        |
| `illum_R/G/B`                 | Shades-of-Gray illuminant estimate, mean-normalized to 1.        |
| `dye_gamma_R/G/B`             | Per-channel midtone gamma applied for dye fading (`1.0` = none). |
| `mean_a_out`, `mean_b_out`    | Final LAB a\*/b\* mean after all corrections.                    |
| `neutralization_applied`      | `True` if the damped LAB neutralization stage actually ran.      |
| `shadow_p5_pre`               | Stretched-image L\* 5th percentile (input to the gamma solver).  |
| `shadow_lift_gamma`           | Gamma actually applied (`1.0` = no lift).                        |
| `despeckle_applied`           | `True` if the despeckle stage ran on this file.                  |
| `despeckle_mask_pixels`       | Total pixels in the final mask (after the size cap).             |
| `despeckle_mask_components`   | Number of connected components replaced by their local median.   |
| `despeckle_largest_component` | Largest component size replaced; should be ≤ `--despeckle-max-size`. |
| `error`                       | Exception message if the file failed; empty otherwise.           |

## How the algorithms work (deep dive)

This section is for readers who want to know exactly what's happening
under the hood. You don't need any of this to use the tool.

### The B&W pipeline

1. **Rec. 709 luminance** — `0.2126·R + 0.7152·G + 0.0722·B`. Standard
   HDTV-era weighting, perceptually calibrated.
2. **Percentile stretch** — clip to `[--black-point%, --white-point%]`
   percentiles, rescale to `[0, 1]`.
3. **Adaptive shadow lift** — solve a gamma that would lift the 5th
   percentile to `--shadow-target`, capped at `--shadow-min-gamma`.
4. **Save** as a single-channel grayscale JPG.

### The color pipeline

1. **Shades-of-Gray white balance** (Finlayson & Trezzi 2004,
   Minkowski p=6) — generalizes gray-world (p=1) and white-patch
   (p=∞). The illuminant estimate is mean-normalized to `1.0` so
   overall exposure is preserved.
2. **Per-channel percentile stretch** — same percentiles as B&W, but
   applied independently to each channel.
3. **Per-channel midtone gamma** — corrects nonlinear dye fading that
   the linear white balance and stretch cannot. Each channel's median
   is pulled toward the geometric mean of the three medians (clamped to
   `[0.15, 0.85]`) via a gamma `g = log(target)/log(median)`, damped by
   `--dye-strength`. Near-equal medians (a neutral image) → all gammas
   ~1.0 → no-op.
4. **Adaptive shadow lift on LAB L\*** — gamma is applied only to the
   perceptual lightness channel, so hue is preserved.
5. **Damped LAB neutralization** — only runs if a residual cast
   remains (`|mean(a*)| > 3` or `|mean(b*)| > 3`). The mean is shifted
   toward zero by `--neutralize-strength` of its value. Conservative
   on purpose — a sunset *should* be warm.
6. **Save** as an RGB JPG.

### The classifier

A photo is treated as B&W if **either** signal indicates monochrome:

- **Input chroma is already low** —
  `max(std a*, std b*) < --bw-input-std`. Catches near-grayscale
  scans before per-channel stretching can amplify noise into
  apparent color.
- **Post-stretch chroma stays low** — the 95th percentile of LAB
  chroma after a tentative per-channel stretch is below
  `--bw-threshold`. Catches faded color photos that re-saturate
  when stretched (high chroma → color); stained B&W prints that
  don't (low chroma → B&W).

Both signals must fail for a photo to enter the color path.
Misclassifications can be overridden per-file with `--force-bw` /
`--force-color`.

### The despeckle algorithm (when `--despeckle` is on)

A two-stage selective median filter, run on luminance (LAB L\* in the
color path; the image itself in the B&W path).

1. **Stage 1 — 3×3 isotropic median.** Catches isolated round dust.
2. **Stage 2 — oriented 1-D medians at 0°/45°/90°/135°.** A scratch
   of any orientation is collinear with one of those lines: the
   parallel-to-scratch median is dominated by scratch pixels (no
   smoothing), but the orthogonal-to-scratch medians cross the
   scratch in just one pixel and are dominated by background. The
   orientation whose median differs **most** from the pixel value is
   the one most likely orthogonal to a passing scratch, and its
   value is used as the replacement.
3. **Threshold gate.** A pixel is flagged for replacement only where
   `|pixel − chosen median| > --despeckle-threshold`. Pixels that
   already agree with their neighborhood are left exactly alone — no
   broad smoothing.
4. **Edge gate.** Compute the gradient on the 3×3-median-filtered
   luminance (so isolated specks, which the 3×3 median has already
   smoothed, don't gate themselves out). Pixels with gradient above
   `--despeckle-edge-protect` are protected. Real multi-pixel edges
   and texture are never modified.
5. **Size-band filter.** Connected components in the apply mask are
   kept only when their pixel area falls in
   `[--despeckle-min-size, --despeckle-max-size]`.

In the color path, only L\* gets the oriented-median replacement. The
a\*/b\* chroma channels are replaced by a 3×3 isotropic median at the
same mask — chroma acuity is much lower than luminance, so directional
precision adds no benefit.

By construction, every replacement is the median of real neighboring
pixels. The algorithm cannot synthesize a value that wasn't already
present in the image's neighborhood.

## Limits and known issues

- **1-px-wide real features** (eyelashes, fine hair, distant wires)
  are geometrically indistinguishable from 1-px-wide scratches at the
  same orientation. Despeckle will smooth both. At typical scan
  resolutions (600 DPI and up), photographic features are usually
  several pixels wide, so this rarely bites.
- **Wide defects** (much wider than `scratch_length / 2`) are
  partially attenuated, not cleanly removed. Manual editing required.
- **Fingerprints** are not handled. They cover large amounts of real
  subject matter at low contrast. Removing them automatically would
  require either inpainting over genuine image content or heavy
  frequency-domain filtering. Neither meets this tool's
  no-invented-features bar.
- **Emulsion damage, water marks, fungal spots** sit on real image
  gradient — the edge gate correctly protects them as image content.
  These need manual retouching.
- **No per-file tone overrides yet.** Routing overrides exist
  (`--force-bw` / `--force-color`); per-file shadow or contrast knobs
  do not. Workaround: run outliers in a separate folder.

## Troubleshooting

### `No input files found in Photos with extensions ('.tif', '.tiff', ...)`

Either the folder is empty, the files have a different extension
(common: `.JPG` from a phone, `.png` from a download), or you typed
the path wrong. Pass `--ext jpg,jpeg,png` for non-TIFF inputs.

### `ModuleNotFoundError: No module named 'cv2'`

You skipped `pip install -r requirements.txt`, or you ran the install
in a different Python environment than you're running the tool in.
On macOS this is most commonly because `python3` and `pip3` point at
different installations. Check:

```bash
python3 -c "import sys; print(sys.executable)"
pip3 -c "import sys; print(sys.executable)"
```

If they differ, use `python3 -m pip install -r requirements.txt`
instead — that guarantees `pip` runs against the same Python.

### "It's so slow"

A few thousand 600-DPI TIFFs is genuinely a lot of work, even
parallel. Things you can do:

- Check `--jobs` is using your cores. The default is
  `cpu_count() - 1`. On an Apple Silicon M-series Mac this should be
  7 or higher.
- Drop `--despeckle` if you don't actually need it — it's the most
  expensive stage by a wide margin.
- Use `--dry-run` while you're tuning flags — it skips the JPG
  encode entirely.

### "The progress bar froze"

Usually it didn't — one worker is on a particularly large file (e.g.
a 16-bit 1.2 GB TIFF) and the bar only ticks when files complete.
Wait. If it's truly hung after several minutes, try
`--jobs 1` to see whether the issue is parallelism-related or in
processing one specific file.

### "One file failed but the rest succeeded"

Expected behavior. Failed files write a row to the report with the
exception in the `error` column and the JPG is not produced. Check
that column to find which files failed and why. Common causes:
corrupt TIFF, extremely small image (a few pixels per side),
unsupported color profile.

### "I want to redo just one photo"

Move it to a separate folder and run the tool against that folder
with whatever flags you want. Use `--suffix` if you're writing into
the same output directory as the previous run, so the existing JPG
isn't overwritten.

## Files in this repo

| Path                | Purpose                                       |
|---------------------|-----------------------------------------------|
| `restore.py`        | Tone/color restoration + dust/scratch removal. |
| `requirements.txt`  | Python dependencies.                          |
| `Photos/`           | Conventional input folder for source TIFFs.   |
| `Restored/`         | Conventional output folder.                   |
| `report.csv`        | Decision report from the most recent run.    |
| `sample_in/`        | Small input set for quick sanity checks.      |
| `sample_out/`       | Expected output for `sample_in/`.             |
| `sample_report.csv` | Expected report for the sample set.           |
