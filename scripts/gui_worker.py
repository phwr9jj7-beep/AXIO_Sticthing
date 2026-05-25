"""
gui_worker.py
-------------
Defines the QThread worker class used to execute the stitching pipeline
asynchronously from the main PySide6 GUI thread. This keeps the UI completely
responsive and allows real-time stdout streaming, progress parsing, and cancellation.
"""

import os
import sys
import subprocess
from PySide6.QtCore import QThread, Signal

class StitchWorker(QThread):
    # Signals for communicating execution updates back to the UI
    status_signal = Signal(str)
    progress_signal = Signal(int)
    log_signal = Signal(str)
    finished_signal = Signal(bool, str)

    def __init__(self, xml_path: str, out_dir: str, correction: str,
                 algorithm: str, scene: int | None = None,
                 ref_channel: int = 0, ref_tag: str = "", target_tags: str = "",
                 alignment_mode: str = "reference", z_mode: str = "none", ref_z_slice: int = 0):
        super().__init__()
        self.xml_path = xml_path
        self.out_dir = out_dir
        self.correction = correction
        self.algorithm = algorithm
        self.scene = scene
        self.ref_channel = ref_channel
        self.ref_tag = ref_tag
        self.target_tags = target_tags
        self.alignment_mode = alignment_mode
        self.z_mode = z_mode
        self.ref_z_slice = ref_z_slice
        self.process = None
        self.is_cancelled = False

    def run(self):
        # Locate gui_runner.py relative to this script directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        runner_path = os.path.join(script_dir, "gui_runner.py")
        
        # Build command list
        cmd = [
            sys.executable,
            runner_path,
            "--xml", self.xml_path,
            "--out-dir", self.out_dir,
            "--correction", self.correction,
            "--algorithm", self.algorithm,
            "--ref-channel", str(self.ref_channel),
            "--ref-tag", self.ref_tag,
            "--target-tags", self.target_tags,
            "--alignment-mode", self.alignment_mode,
            "--z-mode", self.z_mode,
            "--ref-z-slice", str(self.ref_z_slice)
        ]
        
        if self.scene is not None:
            cmd += ["--scene", str(self.scene)]

        # Start process with stdout and stderr redirected into stdout
        # Windows-specific: hide console window
        creation_flags = 0
        if os.name == 'nt':
            # subprocess.CREATE_NO_WINDOW
            creation_flags = 0x08000000

        try:
            self.status_signal.emit("Launching stitching process...")
            self.progress_signal.emit(0)
            
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creation_flags
            )
            
            # Read stdout line by line as it is printed
            for line in self.process.stdout:
                if self.is_cancelled:
                    break
                    
                line_str = line.strip()
                if line_str.startswith("[STATUS]"):
                    status_text = line_str.replace("[STATUS]", "").strip()
                    self.status_signal.emit(status_text)
                    self.log_signal.emit(line)
                elif line_str.startswith("[PROGRESS]"):
                    try:
                        percent = int(line_str.replace("[PROGRESS]", "").strip())
                        self.progress_signal.emit(percent)
                    except ValueError:
                        pass
                else:
                    self.log_signal.emit(line)

            # Wait for process to terminate
            self.process.stdout.close()
            return_code = self.process.wait()

            if self.is_cancelled:
                self.finished_signal.emit(False, "Stitching operation cancelled by user.")
            elif return_code == 0:
                self.finished_signal.emit(True, "Stitching completed successfully!")
            else:
                self.finished_signal.emit(False, f"Stitching process failed with exit code {return_code}.")
                
        except Exception as e:
            self.finished_signal.emit(False, f"Error starting stitching process: {str(e)}")
        finally:
            self.process = None

    def cancel(self):
        """Terminates the active subprocess."""
        self.is_cancelled = True
        if self.process:
            self.status_signal.emit("Cancelling stitching process...")
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
