# AXIO Stitching Project Timeline

This timeline documents key milestones, imaging runs, and code iterations of the AXIO Stitching project.

---

## 📅 Project History

### 🔬 2026-04-17: Initial Imaging Run
- **Event**: Acquisition of Zeiss Axio Microscope tile scans for the benchmark datasets.
- **Outputs**:
  - `2026_04_17__RecognizedCode/` (923 tiles)
  - `2026_04_17__18_55__0347/` (3660 tiles)
- **Status**: Raw files stored in [00.RawData/](file:///e:/Oohashi3DWholeBrainProj/AXIO_Sticthing/00.RawData/).

### 🤖 2026-04-20: Project Onboarding & Setup
- **Event**: Initial project onboarding via AI agent.
- **Actions**:
  - Structured the workspace directories.
  - Set up Git repository and configured Git LFS (Large File Storage) tracking for `.tif` images to prevent repository bloat.
  - Documented findings in [lessons-learned.md](file:///e:/Oohashi3DWholeBrainProj/AXIO_Sticthing/.wiki/system/lessons-learned.md).

### 🛠️ 2026-04-21 to 2026-04-22: High-Throughput Processing & Bug Resolution
- **Event**: Batch processing of `MouseTestRawdata20260421` dataset (16 samples, 5 scenes each).
- **Milestones**:
  - **Correction Stage**: Fitted BaSiCPy flatfield on proxy tiles for shading correction.
  - **Stitching Stage**: Resolved a critical translation invariance bug where the Levenberg-Marquardt optimizer drifted to infinity. Introduced a **Tikhonov regularization limit** (lambda = 0.5) to anchor the canvas coordinates back to physical stage coordinates.
  - **Completion**: Successfully stitched and exported all high-resolution scenes (0-4) for samples A1 through D6 into [02.Results/](file:///e:/Oohashi3DWholeBrainProj/AXIO_Sticthing/02.Results/).

### 🔍 2026-05-25: Onboarding Audit & Wiki Consolidation (Current)
- **Event**: Second-phase onboarding audit and wiki consolidation.
- **Actions**:
  - Verified package environment integrity in `.venv`.
  - Audited file modification dates (161,972 files total).
  - Consolidated AROS project wiki, creating `overview.md`, `timeline.md`, `log.md`, and `index.md`.
  - **Identified New Data**: Tracked 2,374 new files from a localized stitching run on the `RecognizedCode` dataset, yielding `stitched_scene0_phase.tif` outputs.

### 🖥️ 2026-05-25: Desktop GUI Dashboard & Run Stability (Current)
- **Event**: Transition from script-level pipeline to desktop GUI application.
- **Actions**:
  - Built a comprehensive PySide6 desktop dashboard with drag-and-drop file target and real-time execution feedback.
  - Isolated heavy processing in an asynchronous `QThread` executing a separate python subprocess, ensuring the GUI remains fully responsive.
  - Patched Windows and Linux launchers to execute using local virtual environment python.exe path explicitly, resolving global Python 2.7 syntax errors.
  - Formulated full software requirements and manual verification steps in `SPEC.md`.

### 🔬 2026-05-25 (Night): Consensus-Channel, 3D Z-Stack, and SIFT Integration
- **Event**: Core engine and GUI upgrade to support multi-dimensional microscopy datasets.
- **Actions**:
  - Integrated SIFT feature matching and RANSAC geometric consensus into the stitching dropdown choices.
  - Added consensus alignment (All-Channel Average and MIP projection) and 3D Z-plane stitching with ImageJ-compatible 3D TIFF serialization.
  - Released comprehensive multi-tier test plans and verified cross-platform code execution.

