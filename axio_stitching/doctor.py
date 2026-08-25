"""
doctor.py — environment diagnostics for the AXIO Stitching pipeline.

This is the first thing an agent (or a confused user) should run. Stitching failures are
overwhelmingly environmental — an optional package that the chosen algorithm needs is not
importable, the output volume is out of space, or the machine simply does not have the RAM
for a gigapixel canvas — and every one of those is cheaper to catch here than 40 minutes
into a run.

Each check reports ``ok`` / ``warn`` / ``fail`` plus a concrete ``fix`` string, so the
caller can act rather than merely relay.
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__

# ---------------------------------------------------------------------------
# Check model
# ---------------------------------------------------------------------------

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail, "fix": self.fix}


@dataclass
class DoctorReport:
    ok: bool
    checks: list[Check] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "checks": [c.to_dict() for c in self.checks],
            "info": self.info,
        }

    def summary(self) -> str:
        if self.failures:
            return f"{len(self.failures)} blocking problem(s); {len(self.warnings)} warning(s)"
        if self.warnings:
            return f"ready, with {len(self.warnings)} warning(s)"
        return "ready"


# ---------------------------------------------------------------------------
# System probes
# ---------------------------------------------------------------------------

def memory_bytes() -> tuple[int | None, int | None]:
    """
    ``(total, available)`` physical memory in bytes, or ``(None, None)`` when it cannot be
    determined. Uses ``psutil`` when present, else per-OS system calls — deliberately no
    hard dependency, since a missing psutil must not make the doctor itself fail.
    """
    try:
        import psutil  # type: ignore[import-not-found]

        vm = psutil.virtual_memory()
        return int(vm.total), int(vm.available)
    except Exception:
        pass

    if sys.platform.startswith("win"):
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullTotalPhys), int(status.ullAvailPhys)
        except Exception:
            return None, None
        return None, None

    try:  # Linux
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
        values: dict[str, int] = {}
        for line in meminfo.splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                values[key] = int(parts[0]) * 1024
        total = values.get("MemTotal")
        available = values.get("MemAvailable", values.get("MemFree"))
        if total:
            return total, available
    except Exception:
        pass

    try:  # POSIX generic (macOS included, available is approximated by total)
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return int(total), None
    except (ValueError, OSError, AttributeError):
        return None, None


def disk_free_bytes(path: str | os.PathLike[str]) -> int | None:
    """Free bytes on the volume holding ``path`` (walking up to the nearest existing parent)."""
    candidate = Path(path)
    for _ in range(64):
        if candidate.exists():
            try:
                return shutil.disk_usage(candidate).free
            except OSError:
                return None
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    return None


def human_bytes(value: float | None) -> str:
    if value is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024 or unit == "PB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} PB"


def _module_version(name: str) -> str | None:
    try:
        module = importlib.import_module(name)
    except Exception:
        return None
    return str(getattr(module, "__version__", "installed"))


# ---------------------------------------------------------------------------
# Dependency tables
# ---------------------------------------------------------------------------

#: (import name, pip name, what breaks without it)
REQUIRED_PACKAGES: tuple[tuple[str, str, str], ...] = (
    ("numpy", "numpy", "array maths - nothing runs"),
    ("scipy", "scipy", "optimisation used by the global position solver"),
    ("tifffile", "tifffile", "reading tiles and writing stitched TIFFs"),
    ("skimage", "scikit-image", "phase correlation and resampling"),
    ("networkx", "networkx", "tile adjacency graph"),
    ("pydantic", "pydantic", "configuration models"),
)

OPTIONAL_PACKAGES: tuple[tuple[str, str, str], ...] = (
    ("basicpy", "basicpy", 'required by correction="basicpy"; use correction="median" without it'),
    ("cv2", "opencv-python", 'required by algorithm="sift"; use algorithm="phase" without it'),
    ("PySide6", "PySide6", "required to launch the desktop GUI"),
    ("mcp", "mcp", "required to serve this pipeline as MCP tools"),
    ("typer", "typer", "required for the `axio` command-line interface"),
    ("rich", "rich", "pretty CLI output"),
    ("matplotlib", "matplotlib", "QC plots"),
    ("psutil", "psutil", "accurate memory reporting in estimates"),
)


# ---------------------------------------------------------------------------
# The doctor
# ---------------------------------------------------------------------------

def run_doctor(out_dir: str | os.PathLike[str] | None = None) -> DoctorReport:
    """
    Diagnose the environment.

    ``out_dir`` — when given, the free space and writability of that specific output
    location are checked instead of the current working directory.
    """
    checks: list[Check] = []
    info: dict[str, Any] = {
        "axio_stitching": __version__,
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }

    # ---- Interpreter -------------------------------------------------------
    if sys.version_info < (3, 10):
        checks.append(
            Check(
                "python",
                FAIL,
                f"Python {platform.python_version()} is below the required 3.10",
                "Install Python 3.10 or newer and recreate the environment.",
            )
        )
    else:
        checks.append(Check("python", OK, f"Python {platform.python_version()} at {sys.executable}"))

    # ---- Required packages -------------------------------------------------
    for import_name, pip_name, why in REQUIRED_PACKAGES:
        version = _module_version(import_name)
        if version is None:
            checks.append(
                Check(
                    f"package:{pip_name}",
                    FAIL,
                    f"{pip_name} is not importable ({why})",
                    f"{sys.executable} -m pip install {pip_name}",
                )
            )
        else:
            checks.append(Check(f"package:{pip_name}", OK, f"{pip_name} {version}"))

    # ---- Optional packages -------------------------------------------------
    for import_name, pip_name, why in OPTIONAL_PACKAGES:
        version = _module_version(import_name)
        if version is None:
            checks.append(
                Check(
                    f"optional:{pip_name}",
                    WARN,
                    f"{pip_name} is not installed - {why}",
                    f"{sys.executable} -m pip install {pip_name}",
                )
            )
        else:
            checks.append(Check(f"optional:{pip_name}", OK, f"{pip_name} {version}"))

    # ---- MCP SDK flavour ---------------------------------------------------
    flavour = detect_mcp_flavour()
    info["mcp_sdk"] = flavour
    if flavour["available"]:
        checks.append(
            Check("mcp-sdk", OK, f"MCP SDK {flavour['version']} ({flavour['api']} API)")
        )
    else:
        checks.append(
            Check(
                "mcp-sdk",
                WARN,
                "no usable MCP server API found in the installed `mcp` package",
                f'{sys.executable} -m pip install "mcp[cli]>=1.0"',
            )
        )

    # ---- CPU / memory ------------------------------------------------------
    cpu_count = os.cpu_count() or 1
    total_ram, available_ram = memory_bytes()
    info["cpu_count"] = cpu_count
    info["ram_total_bytes"] = total_ram
    info["ram_available_bytes"] = available_ram
    checks.append(Check("cpu", OK, f"{cpu_count} logical core(s)"))

    if total_ram is None:
        checks.append(Check("memory", WARN, "physical memory could not be determined",
                            "Install psutil for accurate memory reporting."))
    elif total_ram < 8 * 1024**3:
        checks.append(
            Check(
                "memory",
                WARN,
                f"{human_bytes(total_ram)} total RAM ({human_bytes(available_ram)} available) - "
                "large mosaics will need scene-by-scene stitching",
                'Stitch one scene at a time (`scene=N`) and prefer z_mode="mip_output_only".',
            )
        )
    else:
        checks.append(
            Check("memory", OK, f"{human_bytes(total_ram)} total, {human_bytes(available_ram)} available")
        )

    # ---- Output volume -----------------------------------------------------
    target = Path(out_dir) if out_dir else Path.cwd()
    info["out_dir"] = str(target)
    free = disk_free_bytes(target)
    info["disk_free_bytes"] = free
    if free is None:
        checks.append(Check("disk", WARN, f"free space on {target} could not be determined"))
    elif free < 20 * 1024**3:
        checks.append(
            Check(
                "disk",
                WARN,
                f"only {human_bytes(free)} free on {target} - stitching writes corrected tiles "
                "as well as the final mosaic",
                "Free space, or point the output directory at a larger volume.",
            )
        )
    else:
        checks.append(Check("disk", OK, f"{human_bytes(free)} free on {target}"))

    writable, reason = _check_writable(target)
    if writable:
        checks.append(Check("output-writable", OK, f"{target} is writable"))
    else:
        checks.append(
            Check("output-writable", FAIL, f"{target} is not writable: {reason}",
                  "Choose a different output directory, or fix the permissions.")
        )

    # ---- Desktop app -------------------------------------------------------
    from .agent_runner import find_app_path

    app = find_app_path()
    info["app_path"] = app
    if app:
        checks.append(Check("desktop-app", OK, f"AXIO Stitching Studio at {app}"))
    else:
        checks.append(
            Check(
                "desktop-app",
                WARN,
                "the desktop app could not be located; `axio_launch_gui` will not work",
                "Set AXIO_STITCHING_APP to the AXIO Stitching Studio executable.",
            )
        )

    ok = not any(c.status == FAIL for c in checks)
    return DoctorReport(ok=ok, checks=checks, info=info)


def _check_writable(directory: Path) -> tuple[bool, str]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".axio_write_test"
        probe.touch()
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, str(exc)


def detect_mcp_flavour() -> dict[str, Any]:
    """
    Which MCP server API is available in the installed ``mcp`` package.

    The SDK renamed ``mcp.server.fastmcp.FastMCP`` to ``mcp.server.MCPServer`` in 2.0, and
    both are in the wild, so the server module supports either and this reports which one
    will be used.
    """
    try:
        import mcp  # noqa: F401
    except Exception:
        return {"available": False, "api": None, "version": None}

    version = None
    try:
        from importlib.metadata import version as _dist_version

        version = _dist_version("mcp")
    except Exception:
        version = getattr(sys.modules.get("mcp"), "__version__", None)

    try:
        from mcp.server import MCPServer  # noqa: F401

        return {"available": True, "api": "MCPServer (SDK >= 2.0)", "version": version}
    except Exception:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401

        return {"available": True, "api": "FastMCP (SDK 1.x)", "version": version}
    except Exception:
        pass
    return {"available": False, "api": None, "version": version}
