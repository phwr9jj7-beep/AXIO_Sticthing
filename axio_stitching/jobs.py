"""
jobs.py — background execution for long stitching runs.

A real scene takes minutes to hours. An MCP tool call that blocks for that long simply times
out, and the caller loses the handle to a job that is still consuming the machine — so the
agent-facing surface starts a job, gets an id back immediately, and polls.

Each job runs :meth:`axio_stitching.engine.StitchingEngine.run` on a worker thread with a
progress callback that both updates the in-process record and appends to a bounded log tail.
Cancellation is cooperative: the flag is checked at every progress event, which is the
pipeline's natural stage boundary, so a cancelled job stops within one step rather than
being killed mid-write.

Records are also journalled to ``~/.axio_stitching/jobs/<id>.json`` so a *finished* job can
still be reported after the server process restarts. A record whose state is ``running`` but
whose owning process is gone is reported as ``orphaned`` — never as still running.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import PipelineStage, ProgressEvent, StitchConfig, StitchResult

#: How many log lines are kept per job.
LOG_TAIL_LINES = 200

#: Where finished jobs are journalled.
JOBS_DIR = Path.home() / ".axio_stitching" / "jobs"


class JobCancelled(RuntimeError):
    """Raised inside the worker thread when the caller has requested cancellation."""


@dataclass
class Job:
    id: str
    config: StitchConfig
    state: str = "pending"  # pending | running | succeeded | failed | cancelled
    percent: int = 0
    stage: str = PipelineStage.INIT.value
    message: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: StitchResult | None = None
    error: str | None = None
    pid: int = field(default_factory=os.getpid)
    _log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL_LINES), repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    # -- derived -------------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return end - self.started_at

    @property
    def done(self) -> bool:
        return self.state in {"succeeded", "failed", "cancelled"}

    def log_tail(self, lines: int = 30) -> list[str]:
        """
        The last ``lines`` log entries. ``0`` (or less) means none — a poller that only wants
        state should not be charged for the log. The buffer holds at most
        :data:`LOG_TAIL_LINES`, so any larger number returns everything retained.
        """
        if lines <= 0:
            return []
        return list(self._log)[-lines:]

    def to_dict(self, log_lines: int = 30) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "state": self.state,
            "percent": self.percent,
            "stage": self.stage,
            "message": self.message,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at)),
            "finished_at": (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.finished_at))
                if self.finished_at
                else None
            ),
            "cancel_requested": self._cancel.is_set(),
            "config": {
                "xml_path": str(self.config.xml_path),
                "out_dir": str(self.config.out_dir),
                "correction": self.config.correction.value,
                "algorithm": self.config.algorithm.value,
                "scene": self.config.scene,
                "z_mode": self.config.z_mode.value,
            },
            "log_tail": self.log_tail(log_lines),
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
        }


class JobManager:
    """
    Process-wide registry of stitching jobs.

    One instance per server process (:data:`MANAGER`). Thread-safe; every mutation of a
    job's public fields happens under the manager lock so a poller never observes a
    half-updated record.
    """

    def __init__(self, jobs_dir: Path | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._jobs_dir = jobs_dir if jobs_dir is not None else JOBS_DIR

    # -- lifecycle -----------------------------------------------------------

    def start(self, config: StitchConfig) -> Job:
        """Launch ``config`` on a worker thread and return the job record immediately."""
        job = Job(id=_new_job_id(), config=config, state="running")
        with self._lock:
            self._jobs[job.id] = job
        self._journal(job)

        thread = threading.Thread(target=self._run, args=(job,), name=f"axio-stitch-{job.id}", daemon=True)
        job._thread = thread
        thread.start()
        return job

    def _run(self, job: Job) -> None:
        from .engine import StitchingEngine

        def progress(event: ProgressEvent) -> None:
            if job._cancel.is_set():
                raise JobCancelled("cancelled by request")
            with self._lock:
                job.percent = event.percent
                job.stage = event.stage.value
                job.message = event.status_message
                if event.status_message:
                    job._log.append(f"[{event.percent:3d}%] {event.status_message}")

        try:
            engine = StitchingEngine(job.config, progress_callback=progress)
            result = engine.run()
        except JobCancelled:
            with self._lock:
                job.state = "cancelled"
                job.error = "cancelled by request"
                job.finished_at = time.time()
                job._log.append("[----] cancelled by request")
                self._journal(job)
            return
        except BaseException as exc:  # noqa: BLE001 - a worker thread must never die silently
            with self._lock:
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = time.time()
                job._log.append(f"[FAIL] {job.error}")
                self._journal(job)
            return

        with self._lock:
            job.result = result
            job.finished_at = time.time()
            if job._cancel.is_set():
                # The engine swallows the cancellation into a failed StitchResult; report
                # what actually happened rather than the symptom.
                job.state = "cancelled"
                job.error = "cancelled by request"
            elif result.success:
                job.state = "succeeded"
                job.percent = 100
                job.stage = PipelineStage.DONE.value
            else:
                job.state = "failed"
                job.error = result.error_message
                job.stage = PipelineStage.FAILED.value
            # Inside the lock, so `done` and the journalled record become visible together.
            self._journal(job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        """
        Ask a running job to stop at its next stage boundary.

        Returns a dict describing what happened; cancelling an already-finished job is not
        an error, it just reports that there was nothing to stop.
        """
        job = self.get(job_id)
        if job is None:
            return {"job_id": job_id, "cancelled": False, "reason": "no such job in this process"}
        if job.done:
            return {"job_id": job_id, "cancelled": False, "reason": f"job already {job.state}", "state": job.state}
        job._cancel.set()
        return {
            "job_id": job_id,
            "cancelled": True,
            "reason": "cancellation requested; it takes effect at the next pipeline stage. "
                      "Output already written is left in place.",
            "state": job.state,
        }

    # -- queries -------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def describe(self, job_id: str, log_lines: int = 30) -> dict[str, Any]:
        """
        A job's status — from this process when it owns the job, otherwise from the journal.

        A journalled record left in ``running`` by a process that is gone is reported as
        ``orphaned``: we genuinely do not know whether it finished, and saying "running"
        would be a lie the caller would poll forever on.
        """
        job = self.get(job_id)
        if job is not None:
            return job.to_dict(log_lines)

        record = self._read_journal(job_id)
        if record is None:
            return {"job_id": job_id, "state": "unknown", "error": "no such job"}
        if record.get("state") == "running" and not _pid_alive(record.get("pid")):
            record["state"] = "orphaned"
            record["error"] = (
                "the process that started this job is gone; its outcome was never recorded. "
                "Check the output directory for partial results."
            )
        record["from_journal"] = True
        return record

    # -- journal -------------------------------------------------------------

    def _journal_path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.json"

    def _journal(self, job: Job) -> None:
        try:
            self._jobs_dir.mkdir(parents=True, exist_ok=True)
            payload = job.to_dict(log_lines=LOG_TAIL_LINES)
            payload["pid"] = job.pid
            path = self._journal_path(job.id)
            tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
            tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # journalling is a convenience; never fail a run because of it

    def _read_journal(self, job_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._journal_path(job_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def list_journalled(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent jobs from disk, newest first — including ones this process did not start."""
        try:
            files = sorted(self._jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for path in files[:limit]:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("state") == "running" and not _pid_alive(record.get("pid")):
                record["state"] = "orphaned"
            record.pop("log_tail", None)
            out.append(record)
        return out


def _new_job_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int):
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    except AttributeError:  # pragma: no cover - os.kill exists on Windows in CPython
        return False
    return True


#: The process-wide manager the CLI and MCP server share.
MANAGER = JobManager()
