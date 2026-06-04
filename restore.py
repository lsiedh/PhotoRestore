#!/usr/bin/env python3
"""Adaptive batch photo restoration: TIFF -> JPG.

Inspects each photo, classifies it as B&W or color, and applies an
appropriate correction pipeline:

  B&W path:   Rec.709 luminance -> percentile stretch -> grayscale JPG.
  Color path: Shades-of-Gray white balance (Minkowski p=6)
              -> per-channel percentile stretch
              -> per-channel midtone gamma (nonlinear dye-fading correction)
              -> damped LAB neutralization (only if residual cast remains)
              -> RGB JPG.

No sharpening, no inpainting, no upscaling -- restoration only.
"""

import argparse
import csv
import fnmatch
import os
import sys
from dataclasses import dataclass, asdict, field
from functools import partial
from multiprocessing import Pool, cpu_count
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from tqdm import tqdm


# ---------- per-image stats / report row ----------

@dataclass
class Stats:
    filename: str
    w: int = 0
    h: int = 0
    classified_as: str = ""
    chroma_p95: float = 0.0  # post-stretch chroma 95th percentile -- primary B&W classifier
    std_a: float = 0.0
    std_b: float = 0.0
    mean_a_in: float = 0.0
    mean_b_in: float = 0.0
    black_pt_R: float = 0.0
    white_pt_R: float = 0.0
    black_pt_G: float = 0.0
    white_pt_G: float = 0.0
    black_pt_B: float = 0.0
    white_pt_B: float = 0.0
    illum_R: float = 1.0
    illum_G: float = 1.0
    illum_B: float = 1.0
    dye_gamma_R: float = 1.0
    dye_gamma_G: float = 1.0
    dye_gamma_B: float = 1.0
    mean_a_out: float = 0.0
    mean_b_out: float = 0.0
    neutralization_applied: bool = False
    shadow_p5_pre: float = 0.0  # luminance 5th percentile after stretch (before lift)
    shadow_lift_gamma: float = 1.0  # gamma applied (1.0 == no lift)
    despeckle_applied: bool = False
    despeckle_mask_pixels: int = 0
    despeckle_mask_components: int = 0
    despeckle_largest_component: int = 0
    error: str = ""


# ---------- analysis ----------

def lab_stats(rgb_u8):
    """Mean and std of a*/b* (signed, neutral=0). OpenCV LAB stores a/b
    as uint8 with neutral=128, so subtract 128 to get the standard signed range."""
    lab = cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2LAB)
    a = lab[..., 1].astype(np.float32) - 128.0
    b = lab[..., 2].astype(np.float32) - 128.0
    return float(a.mean()), float(b.mean()), float(a.std()), float(b.std())


def post_stretch_chroma_p95(rgb_u8, p_lo, p_hi):
    """Tentatively per-channel-stretch the image, then return the 95th percentile
    of LAB chroma. This is the primary B&W classifier: a stained B&W has its three
    channels collapse to near-identical luminance after stretch (low chroma); a
    faded color photo's color separation re-emerges (high chroma)."""
    f = rgb_u8.astype(np.float32) / 255.0
    out = np.empty_like(f)
    for c in range(3):
        lo = np.percentile(f[..., c], p_lo)
        hi = np.percentile(f[..., c], p_hi)
        if hi > lo:
            out[..., c] = np.clip((f[..., c] - lo) / (hi - lo), 0.0, 1.0)
        else:
            out[..., c] = f[..., c]
    u8 = (out * 255.0 + 0.5).astype(np.uint8)
    lab = cv2.cvtColor(u8, cv2.COLOR_RGB2LAB).astype(np.float32)
    a = lab[..., 1] - 128.0
    b = lab[..., 2] - 128.0
    chroma = np.sqrt(a * a + b * b)
    return float(np.percentile(chroma, 95))


# ---------- correction primitives ----------

def percentile_stretch_channel(ch_f, p_lo, p_hi):
    """Linear stretch of a single float channel in [0,1] using percentiles."""
    lo = float(np.percentile(ch_f, p_lo))
    hi = float(np.percentile(ch_f, p_hi))
    if hi <= lo:
        return ch_f, lo, hi
    out = (ch_f - lo) / (hi - lo)
    return np.clip(out, 0.0, 1.0), lo, hi


def shades_of_gray_illuminant(rgb_f, p=6):
    """Per-channel illuminant via Minkowski p-norm (Finlayson & Trezzi 2004).
    Generalizes gray-world (p=1) and white-patch (p=inf). Returned vector is
    normalized so its mean is 1 -- preserves the photo's overall exposure."""
    flat = rgb_f.reshape(-1, 3).astype(np.float64)
    illum = np.power(np.mean(np.power(flat, p), axis=0), 1.0 / p)
    illum = illum / illum.mean()
    return np.where(illum < 1e-6, 1e-6, illum)


def per_channel_midtone_gamma(rgb_f, strength):
    """Per-channel power-law correction for nonlinear dye fading.

    White balance and percentile stretch are linear, so they cannot reshape a
    channel's tone curve. Photographic dye layers fade nonlinearly and at
    different rates, leaving the midtones of a faded print color-skewed even
    after the endpoints are fixed. After WB + stretch, each channel's median
    should match a common neutral midtone if no layer-specific fading occurred.

    The geometric mean of the three channel medians is taken as that neutral
    target; each channel gets a gamma g = log(target)/log(median) that maps its
    median onto the target. The gamma is damped toward 1.0 by `strength` so an
    intentionally warm/cool scene is only partially neutralized, never forced
    gray. A near-grayscale image has near-equal medians, so all gammas land at
    ~1.0 (effective no-op).

    Returns (corrected float array in [0,1], [g_R, g_G, g_B]).
    """
    medians = np.array([float(np.percentile(rgb_f[..., c], 50)) for c in range(3)])
    target = float(np.exp(np.mean(np.log(np.clip(medians, 1e-6, None)))))
    target = float(np.clip(target, 0.15, 0.85))

    out = rgb_f.copy()
    gammas = []
    for c in range(3):
        m = float(medians[c])
        if m < 0.02 or m > 0.98:
            gammas.append(1.0)
            continue
        g = np.log(target) / np.log(m)
        g = 1.0 + (g - 1.0) * strength
        g = float(np.clip(g, 0.4, 2.5))
        out[..., c] = np.power(np.clip(rgb_f[..., c], 1e-6, 1.0), g)
        gammas.append(g)
    return out, gammas


def adaptive_shadow_gamma(p5, target, min_gamma):
    """Compute a gamma that lifts the 5th-percentile luminance to `target`.
    Returns 1.0 (no-op) when no lift is needed or the image is too dark to
    lift safely. min_gamma caps how aggressive the lift can be -- very dark
    photos (p5 ~ 0) would otherwise demand a tiny gamma that washes the
    photo out."""
    if target <= 0 or p5 >= target or p5 < 1e-3:
        return 1.0
    g = float(np.log(target) / np.log(p5))
    return max(g, min_gamma) if g < 1.0 else 1.0


def lift_luminance_bw(L_u8, target, min_gamma):
    """Single-channel shadow lift. Returns (uint8 image, gamma_used, p5_pre)."""
    L_f = L_u8.astype(np.float32) / 255.0
    p5 = float(np.percentile(L_f, 5))
    g = adaptive_shadow_gamma(p5, target, min_gamma)
    if g == 1.0:
        return L_u8, 1.0, p5
    out = np.clip(np.power(L_f, g) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return out, g, p5


def lift_luminance_color(rgb_u8, target, min_gamma):
    """Color shadow lift via LAB L*. Applies gamma only to the perceptual
    lightness channel so hue is preserved. Returns (uint8 image, gamma_used,
    p5_pre)."""
    lab = cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2LAB).astype(np.float32)
    L_f = lab[..., 0] / 255.0
    p5 = float(np.percentile(L_f, 5))
    g = adaptive_shadow_gamma(p5, target, min_gamma)
    if g == 1.0:
        return rgb_u8, 1.0, p5
    lab[..., 0] = np.clip(np.power(L_f, g) * 255.0, 0, 255)
    out = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2RGB)
    return out, g, p5


def damped_lab_neutralize(rgb_u8, alpha):
    """If residual |mean(a*)| or |mean(b*)| > 3, shift them toward 0 by alpha
    of their value. Conservative on purpose -- a sunset *should* be warm."""
    lab = cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2LAB).astype(np.float32)
    a = lab[..., 1] - 128.0
    b = lab[..., 2] - 128.0
    ma, mb = float(a.mean()), float(b.mean())
    if abs(ma) <= 3.0 and abs(mb) <= 3.0:
        return rgb_u8, ma, mb, False
    a -= alpha * ma
    b -= alpha * mb
    lab[..., 1] = np.clip(a + 128.0, 0, 255)
    lab[..., 2] = np.clip(b + 128.0, 0, 255)
    out = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2RGB)
    lab2 = cv2.cvtColor(out, cv2.COLOR_RGB2LAB).astype(np.float32)
    return out, float(lab2[..., 1].mean() - 128.0), float(lab2[..., 2].mean() - 128.0), True


def _oriented_line_medians(L_u8, length):
    """Return the four directional median images (uint8) at 0/45/90/135 deg
    using a 1-D line of `length` pixels centered on each pixel.

    A scratch of any orientation is collinear with exactly one of these four
    line kernels. The median taken along the *parallel* direction is
    dominated by scratch pixels (no smoothing); the median taken along an
    *orthogonal* direction is dominated by background. The caller picks the
    most-different-from-pixel direction to identify scratch pixels.
    """
    half = length // 2
    H, W = L_u8.shape
    pad = cv2.copyMakeBorder(L_u8, half, half, half, half, cv2.BORDER_REFLECT_101)

    def med(offsets):
        stack = np.empty((length, H, W), dtype=np.uint8)
        for i, (dy, dx) in enumerate(offsets):
            stack[i] = pad[half + dy:half + dy + H, half + dx:half + dx + W]
        return np.median(stack, axis=0).astype(np.uint8)

    rng = list(range(-half, half + 1))
    return [
        med([(0, k)  for k in rng]),  # 0 deg, horizontal
        med([(k, 0)  for k in rng]),  # 90 deg, vertical
        med([(-k, k) for k in rng]),  # 45 deg
        med([(k, k)  for k in rng]),  # 135 deg
    ]


def despeckle(img_u8, mode, min_size, max_size, threshold, scratch_len, edge_protect, stats):
    """Selective oriented-median dust/scratch removal (Photoshop "Dust &
    Scratches" family with directional kernels).

    Two-stage detection on luminance (LAB L* in color, the image itself in
    B&W):
      1. 3x3 isotropic median catches isolated round dust.
      2. 1-D medians of length `scratch_len` at 0/45/90/135 deg catch thin
         linear scratches. For each pixel, the orientation whose median
         differs MOST from the pixel value is the one most likely
         orthogonal to a passing scratch -- its median is dominated by
         background, so it is the right replacement.

    A pixel is replaced only if (a) it disagrees with the chosen median by
    more than `threshold`, AND (b) it lies in a low-gradient region (the
    gradient is taken on the 3x3-median-filtered L, so isolated specks
    don't self-protect).

    Cannot invent features: every replacement is a real neighboring pixel
    value (the median IS one of its inputs).
    """
    is_color = (mode == "RGB")
    if is_color:
        lab = cv2.cvtColor(img_u8, cv2.COLOR_RGB2LAB)
        L_u8 = lab[..., 0]
    else:
        L_u8 = img_u8

    # Stage 1: cheap 3x3 isotropic pass — kills 1-px dust, and feeds the
    # edge-protect gradient with a speck-free signal.
    M3_u8 = cv2.medianBlur(L_u8, 3)

    # Stage 2: oriented medians on M3 (so any leftover speck doesn't bias
    # the line median through it).
    if scratch_len >= 3:
        # Force odd length (median of an even-length line is interpolated
        # and breaks the "real-pixel-value" guarantee).
        if scratch_len % 2 == 0:
            scratch_len += 1
        dir_meds = _oriented_line_medians(M3_u8, scratch_len)
        diffs = np.stack(
            [np.abs(L_u8.astype(np.int16) - m.astype(np.int16))
             for m in dir_meds], axis=0
        )  # (4, H, W) int16
        best_dir = np.argmax(diffs, axis=0).astype(np.uint8)
        max_diff = diffs.max(axis=0).astype(np.float32) / 255.0
        replacement_L = np.choose(best_dir, dir_meds)
    else:
        replacement_L = M3_u8
        max_diff = np.abs(
            L_u8.astype(np.int16) - M3_u8.astype(np.int16)
        ).astype(np.float32) / 255.0

    # Edge-protect on M3-smoothed L (specks gone, real edges intact).
    if edge_protect > 0:
        Mf = M3_u8.astype(np.float32) / 255.0
        gx = cv2.Sobel(Mf, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(Mf, cv2.CV_32F, 0, 1, ksize=3)
        gmag = np.sqrt(gx * gx + gy * gy) / 8.0  # 3x3 Sobel max sum is 8
        protect = gmag > edge_protect
    else:
        protect = np.zeros_like(L_u8, dtype=bool)

    apply_mask = (max_diff > threshold) & ~protect

    apply_u8 = apply_mask.astype(np.uint8)
    n_labels, labels, comp_stats, _ = cv2.connectedComponentsWithStats(apply_u8, 8)
    keep = np.zeros(n_labels, dtype=bool)
    n_comp = 0
    largest = 0
    for i in range(1, n_labels):
        area = int(comp_stats[i, cv2.CC_STAT_AREA])
        if min_size <= area <= max_size:
            keep[i] = True
            n_comp += 1
            if area > largest:
                largest = area
    apply_mask = keep[labels]

    mask_pixels = int(apply_mask.sum())
    stats.despeckle_applied = True
    stats.despeckle_mask_pixels = mask_pixels
    stats.despeckle_mask_components = n_comp
    stats.despeckle_largest_component = largest

    if mask_pixels == 0:
        return img_u8

    if is_color:
        out_lab = lab.copy()
        out_lab[..., 0] = np.where(apply_mask, replacement_L, lab[..., 0])
        # Chroma channels: cheap 3x3 isotropic median is enough — chroma
        # acuity is much lower than luminance, so directional precision
        # doesn't help, and a*/b* on a scratch are usually noise anyway.
        for c in (1, 2):
            m3c = cv2.medianBlur(lab[..., c], 3)
            out_lab[..., c] = np.where(apply_mask, m3c, lab[..., c])
        return cv2.cvtColor(out_lab, cv2.COLOR_LAB2RGB)
    return np.where(apply_mask, replacement_L, img_u8).astype(np.uint8)


# ---------- pipelines ----------

def restore_color(rgb_u8, p_lo, p_hi, alpha, shadow_target, shadow_min_gamma,
                  dye_strength, stats):
    rgb_f = rgb_u8.astype(np.float32) / 255.0

    illum = shades_of_gray_illuminant(rgb_f, p=6)
    stats.illum_R, stats.illum_G, stats.illum_B = (
        float(illum[0]), float(illum[1]), float(illum[2])
    )
    wb = np.clip(rgb_f / illum, 0.0, 1.0)

    out = np.empty_like(wb)
    pts = []
    for c in range(3):
        out[..., c], lo, hi = percentile_stretch_channel(wb[..., c], p_lo, p_hi)
        pts.append((lo, hi))
    (stats.black_pt_R, stats.white_pt_R) = pts[0]
    (stats.black_pt_G, stats.white_pt_G) = pts[1]
    (stats.black_pt_B, stats.white_pt_B) = pts[2]

    if dye_strength > 0:
        out, dye_gammas = per_channel_midtone_gamma(out, dye_strength)
    else:
        dye_gammas = [1.0, 1.0, 1.0]
    stats.dye_gamma_R, stats.dye_gamma_G, stats.dye_gamma_B = dye_gammas

    out_u8 = (out * 255.0 + 0.5).astype(np.uint8)

    out_u8, gamma_used, p5_pre = lift_luminance_color(
        out_u8, shadow_target, shadow_min_gamma
    )
    stats.shadow_p5_pre = p5_pre
    stats.shadow_lift_gamma = gamma_used

    if alpha > 0:
        out_u8, ma_out, mb_out, applied = damped_lab_neutralize(out_u8, alpha)
    else:
        lab2 = cv2.cvtColor(out_u8, cv2.COLOR_RGB2LAB).astype(np.float32)
        ma_out = float(lab2[..., 1].mean() - 128.0)
        mb_out = float(lab2[..., 2].mean() - 128.0)
        applied = False
    stats.mean_a_out = ma_out
    stats.mean_b_out = mb_out
    stats.neutralization_applied = applied
    return out_u8, "RGB"


def restore_bw(rgb_u8, p_lo, p_hi, shadow_target, shadow_min_gamma, stats):
    rgb_f = rgb_u8.astype(np.float32) / 255.0
    y = 0.2126 * rgb_f[..., 0] + 0.7152 * rgb_f[..., 1] + 0.0722 * rgb_f[..., 2]
    stretched, lo, hi = percentile_stretch_channel(y, p_lo, p_hi)
    stats.black_pt_R = stats.black_pt_G = stats.black_pt_B = lo
    stats.white_pt_R = stats.white_pt_G = stats.white_pt_B = hi
    out_u8 = (stretched * 255.0 + 0.5).astype(np.uint8)
    out_u8, gamma_used, p5_pre = lift_luminance_bw(
        out_u8, shadow_target, shadow_min_gamma
    )
    stats.shadow_p5_pre = p5_pre
    stats.shadow_lift_gamma = gamma_used
    stats.mean_a_out = 0.0
    stats.mean_b_out = 0.0
    return out_u8, "L"


# ---------- worker ----------

def process_one(input_path, output_dir, args):
    name = os.path.basename(input_path)
    stats = Stats(filename=name)
    try:
        with Image.open(input_path) as im:
            dpi = im.info.get("dpi")
            if im.mode != "RGB":
                im = im.convert("RGB")
            rgb = np.array(im)
        h, w = rgb.shape[:2]
        stats.w, stats.h = w, h

        forced_bw = any(fnmatch.fnmatch(name, g) for g in args.force_bw)
        forced_color = any(fnmatch.fnmatch(name, g) for g in args.force_color)
        mean_a, mean_b, std_a, std_b = lab_stats(rgb)
        stats.mean_a_in, stats.mean_b_in = mean_a, mean_b
        stats.std_a, stats.std_b = std_a, std_b
        stats.chroma_p95 = post_stretch_chroma_p95(rgb, args.black_point, args.white_point)
        # Combined classifier. B&W if EITHER signal indicates monochrome:
        #   (a) input chroma is already very low (post-stretch p95 would just be
        #       amplified noise from a near-grayscale signal), OR
        #   (b) even after per-channel stretch, chroma stays low.
        # Both must fail to classify as color. This catches a faded color photo
        # whose input chroma is mild but real (signal grows under stretch) while
        # keeping a stained B&W photo (input nearly grayscale) on the B&W path.
        input_low = max(std_a, std_b) < args.bw_input_std
        post_low = stats.chroma_p95 < args.bw_threshold
        is_bw = input_low or post_low
        if forced_bw:
            is_bw = True
        elif forced_color:
            is_bw = False
        stats.classified_as = "bw" if is_bw else "color"

        if is_bw:
            out_u8, mode = restore_bw(
                rgb, args.black_point, args.white_point,
                args.shadow_target, args.shadow_min_gamma, stats,
            )
        else:
            out_u8, mode = restore_color(
                rgb, args.black_point, args.white_point,
                args.neutralize_strength,
                args.shadow_target, args.shadow_min_gamma,
                args.dye_strength, stats,
            )

        if args.despeckle:
            out_u8 = despeckle(
                out_u8, mode,
                args.despeckle_min_size,
                args.despeckle_max_size,
                args.despeckle_threshold,
                args.despeckle_scratch_length,
                args.despeckle_edge_protect,
                stats,
            )

        if not args.dry_run:
            stem = Path(name).stem
            out_path = os.path.join(output_dir, f"{stem}{args.suffix}.jpg")
            save_kwargs = {"quality": args.quality, "optimize": True, "subsampling": 0}
            if dpi:
                save_kwargs["dpi"] = dpi
            # Pillow infers mode from array shape/dtype: 2-D uint8 -> "L", 3-D -> "RGB"
            assert (mode == "L" and out_u8.ndim == 2) or (mode == "RGB" and out_u8.ndim == 3)
            Image.fromarray(out_u8).save(out_path, format="JPEG", **save_kwargs)
    except Exception as e:
        stats.error = f"{type(e).__name__}: {e}"
    return stats


# ---------- CLI ----------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Adaptive batch photo restoration (TIFF -> JPG)."
    )
    p.add_argument("input_dir")
    p.add_argument("output_dir")
    p.add_argument("--quality", type=int, default=92,
                   help="JPG quality 1-100 (default 92)")
    p.add_argument("--jobs", type=int, default=max(1, cpu_count() - 1),
                   help="parallel workers (default: cpu_count - 1)")
    p.add_argument("--black-point", dest="black_point", type=float, default=0.5,
                   help="low percentile for level stretch (default 0.5)")
    p.add_argument("--white-point", dest="white_point", type=float, default=99.5,
                   help="high percentile for level stretch (default 99.5)")
    p.add_argument("--bw-threshold", dest="bw_threshold", type=float, default=8.0,
                   help="post-stretch LAB chroma p95 threshold for B&W classification "
                        "(default 8.0). Below this, photo treated as B&W.")
    p.add_argument("--bw-input-std", dest="bw_input_std", type=float, default=4.0,
                   help="input max(std a*, std b*) below which photo is forced to "
                        "B&W path regardless of post-stretch chroma (default 4.0). "
                        "This catches stained B&W photos where the per-channel "
                        "stretch amplifies noise into apparent chroma.")
    p.add_argument("--neutralize-strength", dest="neutralize_strength",
                   type=float, default=0.5,
                   help="alpha for residual LAB neutralization, 0 disables (default 0.5)")
    p.add_argument("--dye-strength", dest="dye_strength", type=float, default=0.5,
                   help="strength of per-channel midtone gamma correction for nonlinear "
                        "dye fading, in [0,1] (default 0.5). Aligns each RGB channel's "
                        "median toward the geometric mean of the three, damped by this "
                        "factor. 0 disables (restores old linear-only color behavior); "
                        "1.0 fully neutralizes midtones. Color path only.")
    p.add_argument("--shadow-target", dest="shadow_target", type=float, default=0.10,
                   help="target value for the 5th-percentile luminance after stretch "
                        "(default 0.10). When the stretched p5 is below this, an "
                        "adaptive gamma is applied to lift shadows up to the target. "
                        "Set 0 to disable. Higher values = brighter shadows.")
    p.add_argument("--shadow-min-gamma", dest="shadow_min_gamma", type=float, default=0.7,
                   help="floor on the shadow-lift gamma (default 0.7). Caps how "
                        "aggressive the lift can be when the input has very crushed "
                        "shadows; lower = stronger lift allowed.")
    p.add_argument("--despeckle", action="store_true",
                   help="enable optional dust/scratch removal stage (default off). "
                        "Two-stage selective median filter: a 3x3 isotropic pass "
                        "for round dust, then 1-D oriented medians at 0/45/90/135 "
                        "deg of length --despeckle-scratch-length for thin "
                        "scratches. A pixel is replaced only where it disagrees "
                        "with the best-orientation median by more than "
                        "--despeckle-threshold AND lies in a low-gradient region. "
                        "Cannot invent features: every replacement value is the "
                        "median of nearby real pixels.")
    p.add_argument("--despeckle-min-size", dest="despeckle_min_size", type=int, default=4,
                   help="minimum connected-component size in pixels to correct "
                        "(default 4). Single-pixel and tiny components below this "
                        "are film grain, not dust, and are left alone.")
    p.add_argument("--despeckle-max-size", dest="despeckle_max_size", type=int, default=2000,
                   help="maximum connected-component size in pixels to correct "
                        "(default 2000). Larger blobs are kept untouched — they "
                        "are likely real image content, not dust. Long thin "
                        "scratches form components in the hundreds-to-low-"
                        "thousands range, so this default is intentionally "
                        "generous; the per-pixel edge-protect gate is the main "
                        "safeguard against touching real content.")
    p.add_argument("--despeckle-threshold", dest="despeckle_threshold",
                   type=float, default=0.06,
                   help="absolute |L - median| deviation threshold in [0,1] image "
                        "units (default 0.06). The deviation is taken against the "
                        "best of four oriented medians, not the isotropic median, "
                        "so this can be lower than a comparable isotropic "
                        "threshold. Lower to catch fainter scratches at the cost "
                        "of touching more grain.")
    p.add_argument("--despeckle-scratch-length", dest="despeckle_scratch_length",
                   type=int, default=7,
                   help="length of the oriented 1-D median windows at 0/45/90/135 "
                        "deg (default 7 px, must be odd). Longer windows catch "
                        "wider scratches but risk touching real low-contrast lines "
                        "of similar width. Set <3 to skip the oriented stage and "
                        "only run the 3x3 isotropic pass.")
    p.add_argument("--despeckle-edge-protect", dest="despeckle_edge_protect",
                   type=float, default=0.08,
                   help="Sobel gradient magnitude (normalized to [0,1]) above which "
                        "pixels are protected from despeckle correction (default "
                        "0.08). Belt-and-braces detail preservation: the median "
                        "filter already preserves edges, this gate adds a hard "
                        "no-touch boundary on real fine detail. Set 0 to disable.")
    p.add_argument("--report", default=None,
                   help="path to write per-image decision CSV")
    p.add_argument("--dry-run", action="store_true",
                   help="analyze + report only, do not write JPGs")
    p.add_argument("--suffix", default="",
                   help="optional suffix appended to output filename stem")
    p.add_argument("--force-bw", action="append", default=[], metavar="GLOB",
                   help="filename glob to force B&W (repeatable)")
    p.add_argument("--force-color", action="append", default=[], metavar="GLOB",
                   help="filename glob to force color (repeatable)")
    p.add_argument("--ext", default="tif,tiff,TIF,TIFF",
                   help="comma-separated input extensions (no dot)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.dry_run:
        os.makedirs(args.output_dir, exist_ok=True)
    exts = tuple("." + e for e in args.ext.split(","))
    files = sorted(
        os.path.join(args.input_dir, f)
        for f in os.listdir(args.input_dir)
        if f.endswith(exts)
    )
    if not files:
        print(f"No input files found in {args.input_dir} with extensions {exts}",
              file=sys.stderr)
        sys.exit(1)
    print(f"Found {len(files)} files. Workers: {args.jobs}. Dry-run: {args.dry_run}")

    worker = partial(process_one, output_dir=args.output_dir, args=args)

    results = []
    if args.jobs == 1:
        for f in tqdm(files, total=len(files)):
            results.append(worker(f))
    else:
        with Pool(processes=args.jobs) as pool:
            for r in tqdm(pool.imap_unordered(worker, files), total=len(files)):
                results.append(r)

    n_bw = sum(1 for r in results if r.classified_as == "bw")
    n_color = sum(1 for r in results if r.classified_as == "color")
    n_err = sum(1 for r in results if r.error)
    print(f"Done. B&W: {n_bw}  Color: {n_color}  Errors: {n_err}")

    if args.report and results:
        results.sort(key=lambda r: r.filename)
        with open(args.report, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(asdict(results[0]).keys()))
            w.writeheader()
            for r in results:
                w.writerow(asdict(r))
        print(f"Wrote report: {args.report}")


if __name__ == "__main__":
    main()
