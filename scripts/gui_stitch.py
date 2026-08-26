"""
gui_stitch.py
-------------
Main entry point for the AXIO microscopy stitching studio. Provides a native,
standalone PySide6 GUI interface that allows non-programmers to drag & drop Zeiss
info/meta XML files, adjust shading correction and stitching parameters, run the
pipeline in the background, and view results.

Supports Consensus-Channel Alignment and Z-Stack Stitching.
"""

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict
import numpy as np

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QProgressBar,
    QPlainTextEdit, QTabWidget, QFileDialog, QScrollArea, QGroupBox,
    QSpinBox
)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPixmap, QFont, QColor

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from gui_worker import StitchWorker

# Re-use parsing logic for local UI metadata display
def parse_info_xml(xml_path: Path):
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        images = root.findall("Image")
        if not images:
            return {}
        scenes = defaultdict(list)
        for img in images:
            fn = img.findtext("Filename")
            if not fn: continue
            b = img.find("Bounds")
            if b is None: continue
            s = int(b.attrib.get("StartS", 0))
            scenes[s].append(fn)
        return dict(scenes)
    except Exception:
        return {}

def parse_meta_xml(xml_path: Path):
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        scale_m = None
        for d in root.findall('.//Scaling/Items/Distance'):
            if d.get('Id') == 'X':
                scale_m = float(d.findtext('Value'))
                break
        scenes = {}
        for i, tr in enumerate(root.findall('.//TileRegion')):
            cols = int(tr.findtext('Columns'))
            rows = int(tr.findtext('Rows'))
            scenes[i] = {"cols": cols, "rows": rows}
        return scale_m, scenes
    except Exception:
        return None, {}


class DropArea(QLabel):
    file_dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setText("📥\n\nDrag & Drop Zeiss XML here\n\n(or click Browse below)")
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #2d2d34;
                border-radius: 8px;
                background-color: #16161a;
                color: #a1a1aa;
                font-size: 14px;
                padding: 40px;
            }
        """)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet("""
                QLabel {
                    border: 2px dashed #0a84ff;
                    border-radius: 8px;
                    background-color: #1d2a3a;
                    color: #0a84ff;
                    font-size: 14px;
                    padding: 40px;
                }
            """)

    def dragLeaveEvent(self, event):
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #2d2d34;
                border-radius: 8px;
                background-color: #16161a;
                color: #a1a1aa;
                font-size: 14px;
                padding: 40px;
            }
        """)

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            if file_path.endswith(".xml"):
                self.file_dropped.emit(file_path)
            self.dragLeaveEvent(None)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AXIO Stitching Studio")
        self.setMinimumSize(QSize(1000, 700))
        self.worker = None
        self.current_xml_path = None
        self.setup_ui()
        self.setup_styles()
        # When an AI agent (or `axio_launch_gui`) opened this window, adopt its context so
        # the user sees the dataset, the parameters that were used, and the finished preview
        # instead of a blank form.
        self._apply_launch_context()

    def setup_ui(self):
        # Main Widget and layout
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(15)

        # ----------------- LEFT COLUMN: Configuration -----------------
        left_layout = QVBoxLayout()
        left_layout.setSpacing(12)

        # App Header
        header_label = QLabel("AXIO Stitching Studio")
        header_label.setStyleSheet("font-size: 20px; font-weight: bold; color: #ffffff;")
        left_layout.addWidget(header_label)

        desc_label = QLabel("Zeiss microscopy whole-brain mosaic reconstructor.")
        desc_label.setStyleSheet("color: #71717a; font-size: 12px; margin-top: -8px;")
        left_layout.addWidget(desc_label)

        # Group 1: Files Input/Output
        io_group = QGroupBox("Files Input / Output")
        io_layout = QVBoxLayout(io_group)
        
        self.drop_area = DropArea()
        self.drop_area.file_dropped.connect(self.on_file_loaded)
        io_layout.addWidget(self.drop_area)

        browse_h_layout = QHBoxLayout()
        self.xml_input = QLineEdit()
        self.xml_input.setPlaceholderText("Select _info.xml or _meta.xml...")
        self.xml_input.textChanged.connect(self.on_xml_text_changed)
        
        btn_browse_xml = QPushButton("Browse...")
        btn_browse_xml.clicked.connect(self.browse_xml)
        browse_h_layout.addWidget(self.xml_input)
        browse_h_layout.addWidget(btn_browse_xml)
        io_layout.addLayout(browse_h_layout)

        output_h_layout = QHBoxLayout()
        self.out_input = QLineEdit()
        self.out_input.setPlaceholderText("Select output directory...")
        
        btn_browse_out = QPushButton("Browse...")
        btn_browse_out.clicked.connect(self.browse_output)
        output_h_layout.addWidget(self.out_input)
        output_h_layout.addWidget(btn_browse_out)
        io_layout.addLayout(output_h_layout)

        left_layout.addWidget(io_group)

        # Group 2: Metadata Display
        self.meta_group = QGroupBox("Dataset Details")
        meta_layout = QVBoxLayout(self.meta_group)
        self.metadata_label = QLabel("No dataset loaded yet. Please drop an XML file.")
        self.metadata_label.setWordWrap(True)
        self.metadata_label.setStyleSheet("color: #a1a1aa; line-height: 1.4;")
        meta_layout.addWidget(self.metadata_label)
        left_layout.addWidget(self.meta_group)

        # Group 3: Parameters Settings
        param_group = QGroupBox("Stitching Parameters")
        param_layout = QVBoxLayout(param_group)

        correction_h = QHBoxLayout()
        correction_h.addWidget(QLabel("Shading Correction:"))
        self.correction_combo = QComboBox()
        self.correction_combo.addItem("BaSiCPy Flatfield", "basicpy")
        self.correction_combo.addItem("Median Profile", "median")
        self.correction_combo.addItem("Spatial Background Subtraction", "spatial")
        self.correction_combo.addItem("None (Raw Tiles)", "none")
        correction_h.addWidget(self.correction_combo)
        param_layout.addLayout(correction_h)

        algo_h = QHBoxLayout()
        algo_h.addWidget(QLabel("Stitching Algorithm:"))
        self.algo_combo = QComboBox()
        self.algo_combo.addItem("Bounded Phase Correlation", "phase")
        self.algo_combo.addItem("SIFT Feature Matching", "sift")
        self.algo_combo.addItem("Stage Coordinates Only", "coordinate")
        algo_h.addWidget(self.algo_combo)
        param_layout.addLayout(algo_h)

        scene_h = QHBoxLayout()
        scene_h.addWidget(QLabel("Select Scene:"))
        self.scene_combo = QComboBox()
        self.scene_combo.addItem("All Scenes", None)
        scene_h.addWidget(self.scene_combo)
        param_layout.addLayout(scene_h)

        left_layout.addWidget(param_group)

        # Group 4: Multi-Channel Settings
        mc_group = QGroupBox("Multi-Channel Settings")
        mc_layout = QVBoxLayout(mc_group)

        ref_chan_h = QHBoxLayout()
        ref_chan_h.addWidget(QLabel("Reference Channel:"))
        self.ref_chan_combo = QComboBox()
        self.ref_chan_combo.addItems(["Channel 0 (DAPI)", "Channel 1", "Channel 2", "Channel 3", "Channel 4"])
        ref_chan_h.addWidget(self.ref_chan_combo)
        mc_layout.addLayout(ref_chan_h)

        align_mode_h = QHBoxLayout()
        align_mode_h.addWidget(QLabel("Alignment Mode:"))
        self.align_mode_combo = QComboBox()
        self.align_mode_combo.addItem("Single Reference Channel", "reference")
        self.align_mode_combo.addItem("All-Channel Average", "average")
        self.align_mode_combo.addItem("All-Channel Max Projection", "max_projection")
        align_mode_h.addWidget(self.align_mode_combo)
        mc_layout.addLayout(align_mode_h)

        split_ref_h = QHBoxLayout()
        split_ref_h.addWidget(QLabel("Split Channel Ref Tag:"))
        self.split_ref_input = QLineEdit()
        self.split_ref_input.setPlaceholderText("e.g. _c1_ (leave empty for stack)")
        split_ref_h.addWidget(self.split_ref_input)
        mc_layout.addLayout(split_ref_h)

        split_targets_h = QHBoxLayout()
        split_targets_h.addWidget(QLabel("Split Target Tags:"))
        self.split_targets_input = QLineEdit()
        self.split_targets_input.setPlaceholderText("e.g. _c2_,_c3_ (comma separated)")
        split_targets_h.addWidget(self.split_targets_input)
        mc_layout.addLayout(split_targets_h)

        left_layout.addWidget(mc_group)

        # Group 5: Z-Stack Settings
        z_group = QGroupBox("Z-Stack Settings")
        z_layout = QVBoxLayout(z_group)

        z_mode_h = QHBoxLayout()
        z_mode_h.addWidget(QLabel("Z-Stack Mode:"))
        self.z_mode_combo = QComboBox()
        self.z_mode_combo.addItem("Disabled (2D only)", "none")
        self.z_mode_combo.addItem("Stitch 3D Stack (MIP Alignment)", "mip_align_3d")
        self.z_mode_combo.addItem("Stitch 3D Stack (Reference Slice)", "ref_slice_3d")
        self.z_mode_combo.addItem("Output 2D MIP Only", "mip_output_only")
        z_mode_h.addWidget(self.z_mode_combo)
        z_layout.addLayout(z_mode_h)

        ref_z_h = QHBoxLayout()
        ref_z_h.addWidget(QLabel("Reference Z-Slice:"))
        self.ref_z_spin = QSpinBox()
        self.ref_z_spin.setRange(0, 999)
        self.ref_z_spin.setValue(0)
        self.ref_z_spin.setEnabled(False)
        ref_z_h.addWidget(self.ref_z_spin)
        z_layout.addLayout(ref_z_h)

        self.z_mode_combo.currentIndexChanged.connect(self.on_z_mode_changed)

        left_layout.addWidget(z_group)

        # Action Buttons Layout
        actions_h = QHBoxLayout()
        self.btn_run = QPushButton("Run Stitching Pipeline")
        self.btn_run.clicked.connect(self.start_stitching)
        self.btn_run.setStyleSheet("background-color: #0a84ff; padding: 12px; font-size: 14px;")
        
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.cancel_stitching)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setStyleSheet("background-color: #27272a; padding: 12px; font-size: 14px; color: #f43f5e;")

        actions_h.addWidget(self.btn_run, 3)
        actions_h.addWidget(self.btn_cancel, 1)
        left_layout.addLayout(actions_h)

        main_layout.addLayout(left_layout, 1)

        # ----------------- RIGHT COLUMN: Live Output Tabs -----------------
        self.tab_widget = QTabWidget()
        
        # Tab 1: Terminal Log
        self.log_viewer = QPlainTextEdit()
        self.log_viewer.setReadOnly(True)
        self.log_viewer.setFont(QFont("Courier New", 10))
        self.log_viewer.setStyleSheet("""
            QPlainTextEdit {
                background-color: #0b0b0d;
                border: 1px solid #2d2d34;
                border-radius: 6px;
                color: #e4e4e7;
                padding: 10px;
            }
        """)
        self.tab_widget.addTab(self.log_viewer, "Console Output")

        # Tab 2: Canvas Preview
        preview_scroll = QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setStyleSheet("background-color: #0b0b0d; border: none;")
        
        self.preview_label = QLabel("Stitched mosaic output will be displayed here after completion.")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("color: #71717a; font-size: 13px; padding: 20px;")
        preview_scroll.setWidget(self.preview_label)
        self.tab_widget.addTab(preview_scroll, "Stitched Canvas Preview")

        right_layout = QVBoxLayout()
        right_layout.addWidget(self.tab_widget)

        # Status & Progress Footer
        self.status_bar_label = QLabel("Ready")
        self.status_bar_label.setStyleSheet("color: #a1a1aa; font-weight: bold;")
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(12)
        
        footer_layout = QHBoxLayout()
        footer_layout.addWidget(self.status_bar_label, 1)
        footer_layout.addWidget(self.progress_bar, 2)
        right_layout.addLayout(footer_layout)

        main_layout.addLayout(right_layout, 2)

    def setup_styles(self):
        # Theme styling configuration using premium dark HSL shades
        self.setStyleSheet("""
            QMainWindow {
                background-color: #0b0b0d;
            }
            QWidget {
                font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Arial, sans-serif;
                color: #e4e4e7;
            }
            QGroupBox {
                border: 1px solid #27272a;
                border-radius: 8px;
                margin-top: 12px;
                font-weight: bold;
                background-color: #18181b;
                padding: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
                color: #0a84ff;
            }
            QLineEdit {
                background-color: #09090b;
                border: 1px solid #27272a;
                border-radius: 6px;
                padding: 8px;
                color: #ffffff;
            }
            QLineEdit:focus {
                border: 1px solid #0a84ff;
            }
            QPushButton {
                background-color: #27272a;
                color: #ffffff;
                border: 1px solid #3f3f46;
                border-radius: 6px;
                padding: 8px 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3f3f46;
            }
            QPushButton:pressed {
                background-color: #18181b;
            }
            QPushButton:disabled {
                background-color: #1c1c1e;
                color: #52525b;
                border-color: #27272a;
            }
            QComboBox {
                background-color: #09090b;
                border: 1px solid #27272a;
                border-radius: 6px;
                padding: 8px;
                color: #ffffff;
                min-width: 150px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox:focus {
                border: 1px solid #0a84ff;
            }
            QProgressBar {
                background-color: #09090b;
                border: 1px solid #27272a;
                border-radius: 6px;
                text-align: center;
                color: #ffffff;
                font-weight: bold;
                font-size: 11px;
            }
            QProgressBar::chunk {
                background-color: #0a84ff;
                border-radius: 5px;
            }
            QTabWidget::panel {
                border: 1px solid #27272a;
                border-radius: 8px;
                background-color: #09090b;
            }
            QTabBar::tab {
                background-color: #18181b;
                border: 1px solid #27272a;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 8px 16px;
                margin-right: 2px;
                font-weight: bold;
                color: #a1a1aa;
            }
            QTabBar::tab:selected {
                background-color: #09090b;
                border-color: #27272a;
                color: #0a84ff;
            }
        """)

    def browse_xml(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Zeiss XML Metadata", "", "Zeiss Metadata (*_info.xml *_meta.xml);;XML (*.xml)"
        )
        if file_path:
            self.xml_input.setText(file_path)

    def browse_output(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if dir_path:
            self.out_input.setText(dir_path)

    def on_xml_text_changed(self, text):
        if not text:
            return
        path = Path(text)
        if path.exists() and path.is_file():
            self.on_file_loaded(text)

    # -- Launch context (agent handoff) ------------------------------------------------

    def _apply_launch_context(self):
        """
        Adopt the context an external launcher passed via environment variables.

        The MCP tool `axio_launch_gui` (and any script) can set:
            AXIO_STITCHING_XML         - dataset to load (fills Dataset Details + scenes)
            AXIO_STITCHING_OUT_DIR     - output directory
            AXIO_STITCHING_CORRECTION  - correction the run used   (basicpy|median|spatial|none)
            AXIO_STITCHING_ALGORITHM   - algorithm the run used    (phase|sift|coordinate)
            AXIO_STITCHING_SCENE       - scene index the run used  (integer)

        Without these the window opens blank, which reads to the user as "the app ignored
        what the agent just did" - the handoff has to carry the work, not just the binary.
        """
        out_dir = os.environ.get("AXIO_STITCHING_OUT_DIR", "").strip()
        xml = os.environ.get("AXIO_STITCHING_XML", "").strip()

        # Output dir BEFORE the XML: on_file_loaded only autofills an EMPTY output field.
        if out_dir:
            self.out_input.setText(out_dir)
        if xml and Path(xml).is_file():
            # setText fires on_xml_text_changed -> on_file_loaded, which populates the
            # Dataset Details panel and the scene dropdown.
            self.xml_input.setText(xml)

        correction = os.environ.get("AXIO_STITCHING_CORRECTION", "").strip()
        if correction:
            idx = self.correction_combo.findData(correction)
            if idx >= 0:
                self.correction_combo.setCurrentIndex(idx)
        algorithm = os.environ.get("AXIO_STITCHING_ALGORITHM", "").strip()
        if algorithm:
            idx = self.algo_combo.findData(algorithm)
            if idx >= 0:
                self.algo_combo.setCurrentIndex(idx)
        scene_raw = os.environ.get("AXIO_STITCHING_SCENE", "").strip()
        if scene_raw:
            try:
                idx = self.scene_combo.findData(int(scene_raw))
                if idx >= 0:
                    self.scene_combo.setCurrentIndex(idx)
            except ValueError:
                pass

        if out_dir:
            self._show_existing_preview(Path(out_dir))

    def _show_existing_preview(self, out_dir: Path):
        """
        Display the newest stitched preview a previous run left in ``out_dir`` and switch to
        the preview tab - so opening the app on finished work SHOWS the work.
        """
        try:
            previews = sorted(
                out_dir.glob("stitched_*_preview.png"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        if not previews:
            return
        target = previews[0]
        try:
            pixmap = QPixmap(str(target))
            if pixmap.isNull():
                return
            self.preview_label.setPixmap(pixmap)
            self.preview_label.setText("")
            self.log_viewer.appendPlainText(
                f"[PREVIEW] Loaded existing output: {target.name} "
                f"(from a previous run in {out_dir})"
            )
            self.tab_widget.setCurrentIndex(1)
        except Exception as e:
            self.log_viewer.appendPlainText(f"[PREVIEW] Could not load {target.name}: {e}")

    def on_z_mode_changed(self):
        mode = self.z_mode_combo.currentData()
        self.ref_z_spin.setEnabled(mode == "ref_slice_3d")

    def on_file_loaded(self, file_path):
        self.current_xml_path = file_path
        self.xml_input.setText(file_path)
        
        path = Path(file_path)
        
        # Autofill output directory to parent + "/Results"
        if not self.out_input.text():
            self.out_input.setText(str(path.parent / "Stitched_Output"))

        self.metadata_label.setText("Reading XML dataset layout...")
        QApplication.processEvents()

        # Parse XML
        is_meta = "_meta.xml" in path.name.lower() or path.name.endswith("meta.xml")
        scenes = {}
        if not is_meta:
            scenes = parse_info_xml(path)
            
        if is_meta or not scenes:
            meta_path = path if is_meta else path.parent / path.name.replace("_info.xml", "_meta.xml")
            if meta_path.exists():
                scale_m, meta_scenes = parse_meta_xml(meta_path)
                for s_idx, s_info in meta_scenes.items():
                    scenes[s_idx] = [None] * (s_info["cols"] * s_info["rows"])

        if scenes:
            n_scenes = len(scenes)
            total_tiles = sum(len(v) for v in scenes.values() if v)
            
            # Probe channels/Z-slices
            channels_text = "Unknown"
            z_slices_text = "Unknown"
            try:
                first_tile_fn = None
                for s_idx, t_list in scenes.items():
                    for t in t_list:
                        if t:
                            if isinstance(t, dict) and "filename" in t:
                                first_tile_fn = t["filename"]
                            elif isinstance(t, str):
                                first_tile_fn = t
                            break
                    if first_tile_fn:
                        break
                        
                if not first_tile_fn:
                    tifs = list(path.parent.glob("*.tif"))
                    if tifs:
                        first_tile_fn = tifs[0].name
                        
                if first_tile_fn:
                    tile_path = path.parent / first_tile_fn
                    if tile_path.exists():
                        from lib_shared import detect_tile_axes
                        info = detect_tile_axes(tile_path)
                        
                        import re
                        z_pattern = re.compile(r'_z(\d+)_')
                        z_indices = []
                        try:
                            raw_files = os.listdir(path.parent)
                            prefix = first_tile_fn.split('_ORG.tif')[0]
                            for f in raw_files:
                                if f.startswith(prefix.split('_c')[0]):
                                    m = z_pattern.search(f)
                                    if m:
                                        z_indices.append(int(m.group(1)))
                            z_indices = sorted(list(set(z_indices)))
                        except Exception:
                            pass
                            
                        num_channels = info['num_channels']
                        num_z = len(z_indices) if z_indices else info['num_z']
                        
                        channels_text = str(num_channels)
                        z_slices_text = str(num_z)
                        if num_z > 1:
                            z_slices_text += " (⚠️ Z-stack detected)"
            except Exception:
                pass

            self.metadata_label.setText(
                f"ℹ️ <b>Dataset Loaded Successfully:</b><br><br>"
                f"📂 <b>Directory:</b> {path.parent.name}<br>"
                f"🎬 <b>Scenes:</b> {n_scenes}<br>"
                f"🎴 <b>Total Tiles:</b> {total_tiles} (approx)<br>"
                f"📊 <b>Channels:</b> {channels_text}<br>"
                f"📐 <b>Z-Slices:</b> {z_slices_text}"
            )
            
            # Populate scenes dropdown
            self.scene_combo.clear()
            self.scene_combo.addItem("All Scenes", None)
            for s_idx in sorted(scenes.keys()):
                tiles_count = len(scenes[s_idx]) if scenes[s_idx] else 0
                self.scene_combo.addItem(f"Scene {s_idx} ({tiles_count} tiles)", s_idx)
        else:
            self.metadata_label.setText("⚠️ Failed to parse scene grid layouts from XML. Please verify file content.")

    def start_stitching(self):
        xml_path = self.xml_input.text().strip()
        out_dir = self.out_input.text().strip()

        if not xml_path:
            self.log_viewer.appendPlainText("[ERROR] Please specify input XML file path.")
            return
        if not out_dir:
            self.log_viewer.appendPlainText("[ERROR] Please specify output directory.")
            return

        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.log_viewer.clear()
        
        # Retrieve settings
        correction = self.correction_combo.currentData()
        algorithm = self.algo_combo.currentData()
        scene = self.scene_combo.currentData()
        
        ref_channel = self.ref_chan_combo.currentIndex()
        ref_tag = self.split_ref_input.text().strip()
        target_tags = self.split_targets_input.text().strip()
        alignment_mode = self.align_mode_combo.currentData()
        z_mode = self.z_mode_combo.currentData()
        ref_z_slice = self.ref_z_spin.value()

        # Initialize background worker
        self.worker = StitchWorker(
            xml_path=xml_path,
            out_dir=out_dir,
            correction=correction,
            algorithm=algorithm,
            scene=scene,
            ref_channel=ref_channel,
            ref_tag=ref_tag,
            target_tags=target_tags,
            alignment_mode=alignment_mode,
            z_mode=z_mode,
            ref_z_slice=ref_z_slice
        )

        # Wire worker signals to main UI threads
        self.worker.status_signal.connect(self.on_status_updated)
        self.worker.progress_signal.connect(self.on_progress_updated)
        self.worker.log_signal.connect(self.on_log_received)
        self.worker.finished_signal.connect(self.on_stitching_finished)

        # Move to preview tab in preparation, reset preview pane
        self.preview_label.setText("Stitching active. Thumbnail preview will load upon successful completion.")
        
        self.worker.start()

    def cancel_stitching(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)

    def on_status_updated(self, status):
        self.status_bar_label.setText(status)

    def on_progress_updated(self, progress):
        self.progress_bar.setValue(progress)

    def on_log_received(self, log_line):
        self.log_viewer.appendPlainText(log_line.rstrip('\r\n'))

    def on_stitching_finished(self, success, message):
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress_bar.setValue(100 if success else 0)
        self.status_bar_label.setText("Complete" if success else "Failed")

        if success:
            self.log_viewer.appendPlainText(f"\n[SUCCESS] {message}")
            self.load_stitched_preview()
        else:
            self.log_viewer.appendPlainText(f"\n[FAILED] {message}")
            self.preview_label.setText(f"Stitching failed:\n{message}")

    def load_stitched_preview(self):
        # Locate the pre-rendered preview PNG in the output directory
        out_dir = Path(self.out_input.text().strip())
        algorithm = self.algo_combo.currentData()
        
        # Grab first preview PNG found in results
        previews = list(out_dir.glob(f"stitched_scene*_{algorithm}_preview.png"))
        if not previews:
            previews = list(out_dir.glob(f"stitched_scene*_*_{algorithm}_preview.png"))
            
        if not previews:
            self.preview_label.setText("Success! Stitched image saved. Could not find preview PNG thumbnail.")
            return

        target_preview = previews[0]
        self.log_viewer.appendPlainText(f"[PREVIEW] Loading pre-rendered thumbnail: {target_preview.name}...")
        
        try:
            pixmap = QPixmap(str(target_preview))
            self.preview_label.setPixmap(pixmap)
            self.preview_label.setText("")
            
            # Automatically toggle tabs to let users admire their results!
            self.tab_widget.setCurrentIndex(1)
                
        except Exception as e:
            self.preview_label.setText(f"Stitched successfully! (Preview loading failed: {str(e)})")


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    # NOTE: the packaged executable's entry point is scripts/axio_launcher.py, which
    # dispatches --mcp-serve / --cli BEFORE Qt is imported. This block keeps the historical
    # `python scripts/gui_stitch.py --xml ...` behaviour working in a source checkout.
    if "--xml" in sys.argv:
        from gui_runner import main as runner_main
        runner_main()
    else:
        main()
