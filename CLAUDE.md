# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -r requirements.txt
```

Dependencies: `Pillow`, `numpy`, `opencv-python`, `tqdm`.

## Running the tool

```bash
# Basic run
python restore.py Photos Restored

# With decision report (recommended for any non-trivial run)
python restore.py Photos Restored --report report.csv

# Dry run — analyze without writing output
python restore.py Photos Restored --dry-run --report audit.csv

# Single-threaded (for debugging)
python restore.py Photos Restored --jobs 1 --report report.csv

# Enable dust/scratch removal
python restore.py Photos Restored --despeckle
```

## Architecture

Everything lives in `restore.py`. `multiprocessing.Pool.imap_unordered` parallelizes per-file work; each worker calls `process_one()`.

**Classification** (`lab_stats`, `post_stretch_chroma_p95`): A photo routes to the B&W path if *either* the input LAB chroma std is below `--bw-input-std` *or* the post-stretch chroma p95 is below `--bw-threshold`. Both signals must fail for color path entry. Per-file overrides via `--force-bw` / `--force-color` (Unix globs via `fnmatch`).

**B&W path** (`restore_bw`): Rec. 709 luminance → percentile stretch → adaptive shadow lift → single-channel grayscale JPG.

**Color path** (`restore_color`): Shades-of-Gray white balance (Minkowski p=6) → per-channel percentile stretch → per-channel midtone gamma (nonlinear dye-fading correction) → adaptive shadow lift on LAB L* → damped LAB neutralization if residual cast remains → RGB JPG.

**Nonlinear dye fading** (`per_channel_midtone_gamma`, on by default): WB and stretch are linear and cannot reshape a channel's tone curve, but dye layers fade nonlinearly at different rates, leaving faded midtones color-skewed even after the endpoints are fixed. This stage takes the median of each RGB channel, sets the neutral target to the geometric mean of the three medians (clamped to `[0.15, 0.85]`), and applies a per-channel gamma `g = log(target)/log(median)` damped toward 1.0 by `--dye-strength` (default 0.5). A channel whose median is near 0 or 1 is skipped; gammas are clamped to `[0.4, 2.5]`. Near-grayscale images have near-equal medians, so all gammas land at ~1.0 (no-op). Tradeoff: a genuinely warm/cool scene (sunset) is *partially* pulled toward neutral — lower `--dye-strength` or set it to 0 to opt out.

**Defect removal** (`despeckle`, off by default): Two-stage selective median (Photoshop "Dust & Scratches" with directional kernels). Stage 1 is a 3×3 isotropic median for round dust. Stage 2 takes 1-D medians of length `--despeckle-scratch-length` (default 7, must be odd) at 0/45/90/135 deg; for each pixel the orientation whose median differs *most* from the pixel value is the one most likely orthogonal to a passing scratch, and its median is used as the replacement. A pixel is replaced only if (a) `max_diff > --despeckle-threshold` and (b) the gradient on the 3×3-median-filtered L is below `--despeckle-edge-protect` (raw-L gradient would let isolated specks self-protect). Connected components outside `[--despeckle-min-size, --despeckle-max-size]` are dropped. In color, only L* uses the oriented median; a*/b* use a 3×3 isotropic median at the same mask. Cannot invent features — every replacement value is a real neighboring pixel.

**`Stats` dataclass**: Accumulates per-image metrics written to the `--report` CSV (classifier outputs, illuminant estimates, tone parameters, despeckle counts, errors).

## Key parameters to tune

| Goal | Flag(s) |
|---|---|
| Classifier misrouting | `--bw-threshold`, `--bw-input-std`, or `--force-bw`/`--force-color` per file |
| Shadow brightness | `--shadow-min-gamma` (primary), then `--shadow-target` |
| Contrast / clipping | `--black-point`, `--white-point` |
| Suppress color-cast correction | `--neutralize-strength 0` |
| Nonlinear dye fading | `--dye-strength` (default 0.5; 0 disables, 1.0 fully neutralizes midtones) |
| Despeckle aggressiveness | `--despeckle-threshold` (default 0.06), `--despeckle-min-size` (default 4), `--despeckle-max-size` (default 2000), `--despeckle-scratch-length` (default 7), `--despeckle-edge-protect` (default 0.08) |

Per-file overrides are safer than retuning global thresholds, which can shift other borderline photos.

## Despeckle design constraints

- **Edge gate must use median-filtered L**, not raw L. Isolated specks have high raw gradient and would self-protect, producing a zero-pixel mask.
- **No `cv2.inpaint`.** It was removed in v2 (slow, PDE bleed softens image beyond the mask). Replacement is always a local median.
- **Fingerprints not handled.** Too large and low-contrast to remove without risk to real content.
- **1-px real features** (eyelashes, fine hair) are geometrically indistinguishable from 1-px scratches — both are smoothed. This is documented in the README as a known limitation.

