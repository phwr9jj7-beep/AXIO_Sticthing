"""
06_qc_compare.py
----------------
Quality-control visualization: generates side-by-side comparison images and
diagnostic plots to help choose the best stitching strategy.

Outputs (saved to intermediate/<dataset>/stitched/QC/):
  - thumbnail_<method>.png      : Downsampled overview of full stitched canvas
  - histogram_comparison.png    : Pixel intensity distributions per method
  - overlap_seams_<method>.png  : Magnified seam visualization (random tile boundary)
  - shading_profiles.png        : BaSiCPy flat-field / dark-field heat-maps

Usage:
    py -3 scripts/06_qc_compare.py --dataset 0347 --scene 0
    py -3 scripts/06_qc_compare.py --dataset RecognizedCode
    py -3 scripts/06_qc_compare.py --dataset all
"""

import argparse
from pathlib import Path
import numpy as np
import tifffile
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTERMEDIATE = PROJECT_ROOT / "intermediate"
RESULTS = PROJECT_ROOT / "01.Results"

DATASET_MAP = {
    "0347": INTERMEDIATE / "0347",
    "RecognizedCode": INTERMEDIATE / "RecognizedCode",
}

THUMB_MAX_PX = 2048


def auto_contrast(img, p_low=0.5, p_high=99.5):
    """Apply auto-contrast (percentile-based) for display."""
    lo = np.percentile(img, p_low)
    hi = np.percentile(img, p_high)
    img = np.clip(img, lo, hi)
    img = (img - lo) / (hi - lo + 1e-6)
    return img


def make_thumbnail(img: np.ndarray) -> np.ndarray:
    """Downscale image to fit within THUMB_MAX_PX."""
    h, w = img.shape
    scale = min(THUMB_MAX_PX / h, THUMB_MAX_PX / w, 1.0)
    if scale < 1.0:
        new_h, new_w = int(h * scale), int(w * scale)
        from skimage.transform import resize
        img = resize(img, (new_h, new_w), anti_aliasing=True, preserve_range=True)
    return img.astype(np.float32)


def plot_shading_profiles(corrected_dir: Path, scene: int, out_dir: Path):
    ff_path = corrected_dir / f"QC_flatfield_scene{scene}.tif"
    df_path = corrected_dir / f"QC_darkfield_scene{scene}.tif"

    if not ff_path.exists():
        print(f"  [SKIP] No BaSiCPy QC files for scene {scene} (run script 03 first)")
        return

    ff = tifffile.imread(str(ff_path))
    df = tifffile.imread(str(df_path))

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle(f"BaSiCPy Shading Profiles — Scene {scene}", fontsize=13, fontweight="bold")

    im0 = axes[0].imshow(ff, cmap="hot", interpolation="bilinear")
    axes[0].set_title("Flat-field (illumination profile)")
    axes[0].axis("off")
    plt.colorbar(im0, ax=axes[0], fraction=0.046)

    im1 = axes[1].imshow(df, cmap="Blues", interpolation="bilinear")
    axes[1].set_title("Dark-field (background/autofluorescence)")
    axes[1].axis("off")
    plt.colorbar(im1, ax=axes[1], fraction=0.046)

    plt.tight_layout()
    out_path = out_dir / f"shading_profiles_scene{scene}.png"
    plt.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {out_path.name}")


def plot_stitched_thumbnails(stitched_dir: Path, scene: int, dataset: str, out_dir: Path):
    """Find all stitched TIFs for this scene and make thumbnail comparisons."""
    patterns = [
        f"*_scene{scene}_*_coord_stitch.tif",
        f"*_scene{scene}_*_phase_stitch.tif",
    ]

    found = {}
    for pattern in patterns:
        matches = list(stitched_dir.glob(pattern))
        for m in matches:
            method = "coord" if "coord" in m.name else "phase"
            source = "corrected" if "corrected" in m.name else "raw"
            label = f"{method}_{source}"
            found[label] = m

    if not found:
        print(f"  [SKIP] No stitched TIFs found for scene {scene} — run scripts 04/05 first.")
        print(f"  Looked in: {stitched_dir}")
        return

    n = len(found)
    fig, axes = plt.subplots(1, n, figsize=(8 * n, 8))
    if n == 1:
        axes = [axes]
    fig.suptitle(f"Stitched Thumbnails — {dataset} Scene {scene}", fontsize=13, fontweight="bold")

    histdata = {}
    for ax, (label, path) in zip(axes, found.items()):
        print(f"  Loading thumbnail: {path.name}...")
        img = tifffile.imread(str(path))
        thumb = make_thumbnail(img)
        display = auto_contrast(thumb)
        ax.imshow(display, cmap="gray", vmin=0, vmax=1, interpolation="bilinear")
        ax.set_title(label, fontsize=10)
        ax.axis("off")
        ax.set_xlabel(f"{img.shape[1]}×{img.shape[0]} px | {path.stat().st_size/1024**2:.0f} MB")
        histdata[label] = img.ravel()[::16]  # subsample for histogram

    plt.tight_layout()
    thumb_out = out_dir / f"thumbnail_comparison_scene{scene}.png"
    plt.savefig(str(thumb_out), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {thumb_out.name}")

    # Histogram comparison
    fig, ax = plt.subplots(figsize=(10, 5))
    colors = plt.cm.tab10.colors
    for i, (label, pix) in enumerate(histdata.items()):
        ax.hist(pix, bins=512, range=(0, 65535), alpha=0.6,
                label=label, color=colors[i], density=True, histtype="step", linewidth=1.5)
    ax.set_xlabel("Pixel intensity (16-bit)")
    ax.set_ylabel("Density")
    ax.set_title(f"Intensity Histogram — Scene {scene}")
    ax.legend()
    ax.set_xlim(0, 65535)
    plt.tight_layout()
    hist_out = out_dir / f"histogram_comparison_scene{scene}.png"
    plt.savefig(str(hist_out), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {hist_out.name}")


def run_qc(dataset_name: str, ds_dir: Path, scene: int):
    corrected_dir = ds_dir / "basic_corrected"
    stitch_dir = RESULTS / dataset_name
    qc_dir = stitch_dir / "QC"
    qc_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  QC Report: {dataset_name} | Scene {scene}")
    print(f"  Output: {qc_dir}")
    print(f"{'='*60}")

    plot_shading_profiles(corrected_dir, scene, qc_dir)
    plot_stitched_thumbnails(stitch_dir, scene, dataset_name, qc_dir)
    print(f"\n✓ QC complete for {dataset_name} scene {scene}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=["0347", "RecognizedCode", "all"], default="0347")
    parser.add_argument("--scene", type=int, default=0)
    args = parser.parse_args()

    targets = list(DATASET_MAP.keys()) if args.dataset == "all" else [args.dataset]
    for name in targets:
        run_qc(name, DATASET_MAP[name], args.scene)


if __name__ == "__main__":
    main()
