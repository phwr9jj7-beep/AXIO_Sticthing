"""
02_install_deps.py
------------------
Installs all Python dependencies required for the stitching pipeline.
Run once before the pipeline scripts.

Usage:
    py -3 scripts/02_install_deps.py
"""

import subprocess
import sys

PACKAGES = [
    # Core image I/O and numerics
    "numpy",
    "tifffile",
    "imageio",
    "pillow",
    "scikit-image",
    "scipy",
    # Shading correction
    "basicpy",          # BaSiCPy: retrospective shading correction
    # Visualization / QC
    "matplotlib",
    # Progress bars
    "tqdm",
    # OME-TIFF / big image support
    "zarr",
    "dask[array]",
]

def pip_install(pkg):
    print(f"  Installing: {pkg}")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"    [ERROR] {result.stderr.strip()}")
    else:
        print(f"    [OK]")

if __name__ == "__main__":
    print("Installing pipeline dependencies...\n")
    for pkg in PACKAGES:
        pip_install(pkg)
    print("\nAll done. Verifying imports...")

    import_checks = {
        "numpy": "numpy",
        "tifffile": "tifffile",
        "sklearn": "scikit-image",
        "basicpy": "basicpy",
        "matplotlib": "matplotlib",
        "tqdm": "tqdm",
        "zarr": "zarr",
        "dask": "dask",
    }
    ok = True
    for mod, pkg in import_checks.items():
        try:
            __import__(mod)
            print(f"  [OK] {mod}")
        except ImportError:
            print(f"  [FAIL] {mod}  (from package: {pkg})")
            ok = False

    if ok:
        print("\n✓ All dependencies available. Ready to run the pipeline.")
    else:
        print("\n✗ Some imports failed. Please check errors above.")
