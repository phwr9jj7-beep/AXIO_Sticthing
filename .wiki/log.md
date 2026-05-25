# AXIO Stitching Project Log

This log registers daily developer and agent actions in the AXIO Stitching project workspace.

---

### 🟢 2026-04-20: Workspace Initialized
* **Action**: Configured directory hierarchies.
* **Action**: Initialized Git repository and set up Git LFS rules tracking `.tif` microscopy files.
* **Impact**: Prevented repository bloat and established clear separation between raw data (`00.RawData`) and results.

### 🟡 2026-04-21: Shading Correction Fit
* **Action**: Run BaSiCPy flatfield correction on `0347` and `RecognizedCode` datasets.
* **Action**: Saved corrected proxy tiles in `intermediate/` directories.

### 🔴 2026-04-22: Tikhonov Regularizer Implementation
* **Action**: Debugged global LM solver failure. Added origin-anchoring Tikhonov regularizer to `11_mouse_stitch_optimal.py` to prevent grid coordinates from diverging to infinity.
* **Action**: Completed high-resolution stitching of well `A1` through `D6` mouse brains.
* **Impact**: Generated fully aligned high-res `.tif` mosaics inside `02.Results/`.

### 🟢 2026-05-25: Archeological Audit & Wiki Rebuild
* **Action**: Performed recursive folder audit (161,972 files mapped).
* **Action**: Confirmed `.venv` library compatibility and isolated NumPy environment from global Python conflicts.
* **Action**: Created core wiki files (`overview.md`, `timeline.md`, `log.md`, `index.md`).
* **Action**: Detected new experimental data generated today: `RecognizedCode` dataset stitching output (e.g., `stitched_scene0_phase.tif`) in `00.RawData/2026_04_17__RecognizedCode/Stitched_Output/`.

### 🔵 2026-05-25 (Afternoon): Standalone GUI Studio Implementation & Launch Fix
* **Action**: Designed and developed a dark-mode PySide6 desktop GUI dashboard (`gui_stitch.py`), asynchronous background execution worker (`gui_worker.py`), and decoupled CLI pipeline wrapper (`gui_runner.py`).
* **Action**: Structured Conda Constructor installer specifications (`construct.yaml`, `environment.yml`) and bootstrap launcher scripts (`launcher.bat`, `launcher.sh`).
* **Action**: Patched launcher path resolution logic to call local virtual environment Python binaries explicitly, preventing legacy Python 2.7 namespace hijacking and SyntaxError crashes.
* **Action**: Drafted comprehensive software specifications and verification test plans (`SPEC.md`).
* **Impact**: Empowers non-programming lab technicians to perform retrospective illumination corrections and sub-pixel phase stitching on arbitrary Zeiss XML datasets through an intuitive, error-isolated desktop application.

### 🌈 2026-05-25 (Evening): Multi-Channel & Split-Channel Integration
* **Action**: Designed and implemented reference-guided multi-channel alignment and stitching support in the desktop GUI and backend runner.
* **Action**: Extended `lib_shared.py` with the `read_tile_channel` memory-efficient reader and configured `save_tiff` to export ImageJ-compatible 3D CYX stacked TIFFs.
* **Action**: Modified `gui_runner.py` to support multi-page stacks (fitting BaSiC flatfields per channel and assembling multi-page outputs) and split channels (filtering filenames by `--ref-tag`, mapping coordinates, and saving tag-specific stitched outputs).
* **Action**: Integrated multi-channel parameters, widgets, and 3D thumbnail rendering checks into `gui_stitch.py`.
* **Action**: Updated `SPEC.md` and `overview.md` to reflect new parameters, validation workflows, and system capabilities.
* **Impact**: Enables seamless, automated registration and reconstruction of multi-spectral stacked/split Zeiss scan datasets under a unified spatial coordinate model.

### 🔬 2026-05-25 (Night): Comprehensive Test Plan & Architectural Validation
* **Action**: Formulated a comprehensive, multi-tier implementation and test plan linking GUI actions, backend APIs, and E2E pipelines.
* **Action**: Audited all Python scripts across the project to ensure proper module header documentation and cross-platform compatibility.
* **Action**: Verified OS-agnostic pathing logic (via `pathlib.Path`) and decoupled process spawning (via `sys.executable`).
* **Impact**: Establishes a rigorous verification harness for future updates and guarantees stable performance on Windows, Linux, and macOS.

### 🎨 2026-05-25 (Late Night): Shading Correction Options Integration
* **Action**: Generalized shading correction functions in `gui_runner.py` to support `median` and `spatial` correction methods alongside `basicpy` and `none`.
* **Action**: Upgraded `gui_stitch.py` to expose all four tested shading correction options (BaSiCPy, Median Profile, Spatial Background Subtraction, None) via key-value currentData mapping.
* **Action**: Successfully tested Median correction E2E on Scene 0.
* **Impact**: Fully integrates all tested methods into the desktop interface, giving researchers complete flexibility to choose optimal shading strategies.

### 🚀 2026-05-25 (Finalization): Tikhonov-Bounded SIFT Global Optimizer
* **Action**: Replaced the local BFS coordinate propagation chain in the SIFT stitching library (`lib_stitch_sift.py`) with a Tikhonov-anchored global least-squares optimization solver (`scipy.optimize.least_squares`).
* **Action**: Anchored the SIFT displacement equations to nominal Zeiss stage coordinate priors with a regularizer weight of `0.5`, mirroring the phase correlation alignment engine.
* **Action**: Updated `SPEC.md` to document SIFT-based Tikhonov solver verification steps.
* **Action**: Completed successful E2E validation test of SIFT stitching on Scene 0 (430 tiles), outputting a drift-free uncompressed 16-bit TIFF mosaic and thumbnail.
* **Impact**: Eliminates global accumulation drift errors for feature-based stitching, ensuring high-fidelity geometric mosaics across macro distances.

### 🏁 2026-05-25 (Post-Flight): Formal Workflow Finalization
* **Action**: Executed `/science-project-onboarding` re-audit, validating workspace structure.
* **Action**: Executed `/wiki-update` to lint the `.wiki/` framework, checking for orphans and rebuilding indices.
* **Action**: Executed `/lab-commit` to stage and seal all prior GUI, SIFT, and documentation upgrades into the main branch.
* **Impact**: Formalizes the AROS project state, ensuring rigorous documentation compliance prior to federation sync.
