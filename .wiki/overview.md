# AXIO Stitching Project Overview

The **AXIO Stitching** project is a high-throughput, high-resolution spatial microscopy data processing pipeline designed for Zeiss Axio Microscope tile scans. It incorporates flatfield shading corrections and robust global stitching algorithms to assemble large-scale, multi-tile tissue images (up to 5,000+ tiles per scene) into single contiguous mosaics.

---

## 🔬 Core Datasets

The project handles two primary categories of imaging sessions:

1. **Benchmark Datasets (2026-04-17)**
   - **`0347`**: Raw tile scans (3,660 files, ~7.27 GB) and associated XML coordinates.
   - **`RecognizedCode`**: Raw tile scans (923 files, ~8.04 GB) and associated XML coordinates.
   - Used to compare flatfield corrections and stitching methods.
2. **Mouse Whole Brain Dataset (`MouseTestRawdata20260421`)**
   - High-throughput dataset consisting of 16 wells/samples (`A1`, `A2-Image Export-03`, `B2-Image Export-01`, etc.).
   - Each well contains up to 5 individual scenes, with 400 to 1,200+ tiles per scene.
   - Combined size of raw datasets exceeds 150+ GB.

---

## 🖥️ AXIO Stitching Studio Desktop App

To make the advanced stitching workflow accessible to non-programmers (such as biology lab technicians), a standalone graphical desktop application has been developed:

1.  **MainWindow (`gui_stitch.py`)**: A premium dark-mode Qt interface that supports drag-and-drop ingestion of Zeiss XML coordinates, dynamic parameter selects, live logging, and on-the-fly 8-bit visual preview rendering of stitched results.
2.  **Async StitchWorker (`gui_worker.py`)**: A background thread running a decoupled command line interface (`gui_runner.py`) using `subprocess.Popen`, which protects the interface from lockups and crashes.
3.  **Bootstrapped Environment (`environment.yml`, `construct.yaml`)**: Spec sheets configures Conda Constructor to compile a standalone, local-running python bundle containing all heavy scientific libraries (PyTorch, PySide6, OpenCV, SciPy).
4.  **Launchers**: Double-clickable `launcher.bat` (Windows) and `launcher.sh` (Linux/macOS) wrappers configure absolute virtual environment pathings to insulate runtime execution from system python environmental conflicts.

### 🌈 Multi-Channel & Split-Channel Ingestion Support
To accommodate multi-spectral microscopy data, the desktop GUI and backend runner support both stacked and split-channel TIFF formats:
*   **Reference-Guided Alignment**: BaSiCPy flatfield corrections and phase-correlation calculations are performed strictly on the designated **Reference Channel** (e.g. DAPI/index 0 or filename tag `_c1_`) to capture high-contrast overlapping features. The solved spatial offsets are then mapped identically to all other target channels to guarantee perfect channel registration.
*   **Multi-Page TIFF Stacks**: For single files containing stacked frames, the runner slices and fits flatfield corrections for each channel index, registers using the reference frame, and compiles the final stitched canvases back into a single multi-page TIFF output with proper channel metadata.
*   **Split-Channel TIFFs**: For split files with tag suffixes (e.g. `_c1_` DAPI, `_c2_` GFP), the runner parses reference files matching the reference tag to compute the stitching layout, processes target tag files using mapped coordinates, and outputs separate channel-specific stitched images (e.g., `stitched_scene0_c1_phase.tif`, `stitched_scene0_c2_phase.tif`).

### 📐 Consensus-Channel Alignment & 3D Z-Stack Stitching (New in Phase 4)
To support advanced multi-channel focus and 3D volumetric scans, Phase 4 introduces:
*   **Consensus Alignment**: Instead of aligning using a single reference channel, users can choose to align by **All-Channel Average** or **All-Channel Max Intensity Projection (MIP)**. The pipeline aggregates signals across all spectral channels to construct a consensus 2D registration frame, capturing overlapping features that might be dim or absent in any single channel.
*   **3D Z-Stack Stitching**: To support 3D imaging, the studio implements a projection-solve-reassemble workflow:
    1. A representative 2D frame (MIP or reference Z-slice) is created for each tile.
    2. The solver computes the global XY translation offsets using phase correlation on these 2D frames.
    3. The solved offsets are applied independently to stitch each individual Z-plane.
    4. The output is assembled and saved as a unified, ImageJ-compatible **multi-page 3D TIFF stack** (`ZCYX` or `ZYX` axes) to keep results clean and organized.
*   **Metadata-Aware Dimension Detection**: Replaced fragile shape heuristics with `tifffile.TiffFile.series[0].axes` mapping, allowing robust detection of `'YX'`, `'CYX'`, `'ZYX'`, and `'ZCYX'` layouts.
*   **Split-Channel Z-Slice Regex Detection**: Automatically detects split Z-slice files on disk using the `_z\d+` regex pattern matching.
*   **BaSiCPy uint16 Correction Fix**: Fixed a clipping bug in the split-channel BaSiCPy flow that previously coerced corrected uint16 tile images to uint8.

---

## 🛠️ Pipeline Stages

The pipeline consists of a sequential execution flow controlled by [run_pipeline.py](file:///e:/Oohashi3DWholeBrainProj/AXIO_Sticthing/scripts/run_pipeline.py):

1. **Inspection (`01_inspect_data.py`)**: Parses ZEN-compatible metadata XMLs to identify scenes and nominal stage coordinate grids.
2. **Flatfield Shading Correction**:
   - **BaSiCPy (`basicpy`)**: Retrospective flatfield fitting using low-rank sparse decomposition.
   - **Median (`median`)**: Median tile calculation over the entire scene.
   - **Spatial (`spatial`)**: Gaussian-smoothed average intensity profile.
3. **Stitching & Alignment**:
   - **Stage Coordinates (`coord`)**: Naive stitching using motor coordinates.
   - **Phase Correlation (`phase`)**: Fourier-based translation estimation for overlapping tile boundaries.
   - **SIFT (`sift`)**: OpenCV-based feature keypoint matching. Integrated with a Tikhonov-regularized global least-squares optimization solver to eliminate BFS drift.
4. **QC Report Generation (`06_qc_compare.py`)**: Computes quantitative metrics:
   - **MAD (Median Absolute Difference)** in overlap regions.
   - **NCC (Normalized Cross-Correlation)**.
   - **Gamma** (Intensity contrast consistency).

---

## 🧠 Algorithmic Breakthrough: Origin-Anchored Global Optimization

During high-throughput stitching, a critical translation invariance vulnerability was identified:
- The Levenberg-Marquardt global solver (`least_squares`) failed to anchor the origin when given only relative shifts (for both phase correlation and SIFT).
- Floating-point noise caused the canvas origin to drift towards arithmetic infinity, causing massive memory allocation failures.

### The Solution: Tikhonov Regularization Anchor
A regularizer was introduced (first in `11_mouse_stitch_optimal.py` and then ported to `gui_runner.py` and the SIFT library `lib_stitch_sift.py`) to bind the global solver to nominal motor positions:
```python
lambda_anchor = 0.5
drift = (pos - init_pos) * lambda_anchor
res.extend(drift.flatten())
```
This forces the optimizer to respect hardware limits (motor coordinates) while allowing local sub-pixel adjustments, ensuring stable convergence and perfect alignment across all 16 wells and 5 scenes.

---

## 📂 Results Summary
- Stitched high-resolution outputs are written to [02.Results/](file:///e:/Oohashi3DWholeBrainProj/AXIO_Sticthing/02.Results/) (e.g., `scene0_optimal.tif` to `scene4_optimal.tif` for each sample).
- Comparative metrics and manuscript drafts are archived in [01.Results/Report/](file:///e:/Oohashi3DWholeBrainProj/AXIO_Sticthing/01.Results/Report/).

---

## 📖 Additional Resources
- [[system/lessons-learned]]: System logs and project insights.
