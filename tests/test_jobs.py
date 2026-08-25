"""
test_jobs.py — background execution for long stitching runs.

The job manager is what makes the MCP surface usable on real data, so the properties that
matter are: a job never blocks the caller, its state is always truthful (including for a job
whose owning process is gone), and cancellation stops the run without pretending the failure
was the pipeline's fault.
"""

import time
from pathlib import Path

import pytest

from axio_stitching.jobs import Job, JobManager, _pid_alive
from axio_stitching.models import PipelineStage, ProgressEvent, StitchConfig


def wait_for(predicate, timeout: float = 60.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture()
def manager(tmp_path: Path) -> JobManager:
    return JobManager(jobs_dir=tmp_path / "jobs")


@pytest.fixture()
def config(small_dataset: Path, tmp_path: Path) -> StitchConfig:
    return StitchConfig(
        xml_path=small_dataset,
        out_dir=tmp_path / "out",
        correction="none",
        algorithm="coordinate",
        scene=0,
    )


class TestStart:
    def test_returns_immediately_with_a_job_id(self, manager: JobManager, config: StitchConfig):
        job = manager.start(config)
        assert job.id
        assert job.state in {"running", "succeeded", "failed"}

    def test_job_ids_are_unique(self, manager: JobManager, config: StitchConfig):
        ids = {manager.start(config).id for _ in range(3)}
        assert len(ids) == 3

    def test_runs_to_completion(self, manager: JobManager, config: StitchConfig):
        job = manager.start(config)
        assert wait_for(lambda: job.done), f"job did not finish: {job.state} {job.message}"
        assert job.state == "succeeded", job.error
        assert job.result is not None and job.result.success
        assert job.percent == 100
        assert job.stage == PipelineStage.DONE.value

    def test_records_progress_and_a_log_tail(self, manager: JobManager, config: StitchConfig):
        job = manager.start(config)
        wait_for(lambda: job.done)
        assert job.log_tail(), "a finished job should have produced log lines"
        assert all(line.startswith("[") for line in job.log_tail())

    def test_writes_output_the_result_points_at(self, manager: JobManager, config: StitchConfig):
        job = manager.start(config)
        wait_for(lambda: job.done)
        assert job.result is not None
        assert job.result.output_paths
        assert all(Path(p).exists() for p in job.result.output_paths)


class TestFailure:
    def test_a_bad_config_fails_the_job_rather_than_the_caller(
        self, manager: JobManager, small_dataset: Path, tmp_path: Path
    ):
        config = StitchConfig(
            xml_path=small_dataset, out_dir=tmp_path / "out", correction="none",
            algorithm="phase", scene=99,
        )
        job = manager.start(config)
        assert wait_for(lambda: job.done)
        # Scene 99 does not exist: the engine skips it and reports zero scenes processed.
        assert job.state in {"succeeded", "failed"}
        if job.state == "succeeded":
            assert job.result is not None and job.result.scenes_processed == 0


class TestCancel:
    def test_reports_cancellation_rather_than_a_pipeline_failure(
        self, manager: JobManager, config: StitchConfig
    ):
        job = manager.start(config)
        manager.cancel(job.id)
        assert wait_for(lambda: job.done)
        # Cancellation may land after completion on a tiny dataset; either outcome is honest,
        # but a cancelled job must never be reported as "failed".
        assert job.state in {"cancelled", "succeeded"}
        if job.state == "cancelled":
            assert job.error == "cancelled by request"

    def test_cancelling_an_unknown_job_is_reported_not_raised(self, manager: JobManager):
        result = manager.cancel("no-such-job")
        assert result["cancelled"] is False and "no such job" in result["reason"]

    def test_cancelling_a_finished_job_says_so(self, manager: JobManager, config: StitchConfig):
        job = manager.start(config)
        wait_for(lambda: job.done)
        result = manager.cancel(job.id)
        assert result["cancelled"] is False and "already" in result["reason"]

    def test_cancel_explains_that_partial_output_stays(self, manager: JobManager, config: StitchConfig):
        job = manager.start(config)
        result = manager.cancel(job.id)
        if result["cancelled"]:
            assert "left in place" in result["reason"]
        wait_for(lambda: job.done)


class TestDescribe:
    def test_describes_a_live_job(self, manager: JobManager, config: StitchConfig):
        job = manager.start(config)
        payload = manager.describe(job.id)
        assert payload["job_id"] == job.id
        assert set(payload) >= {"state", "percent", "stage", "elapsed_seconds", "config", "log_tail"}
        wait_for(lambda: job.done)

    def test_log_lines_can_be_suppressed(self, manager: JobManager, config: StitchConfig):
        job = manager.start(config)
        wait_for(lambda: job.done)
        assert manager.describe(job.id, log_lines=0)["log_tail"] == []

    def test_an_unknown_job_is_reported_as_unknown(self, manager: JobManager):
        payload = manager.describe("nope")
        assert payload["state"] == "unknown"

    def test_reads_a_finished_job_back_from_the_journal(self, manager: JobManager, config: StitchConfig, tmp_path: Path):
        job = manager.start(config)
        wait_for(lambda: job.done)

        fresh = JobManager(jobs_dir=tmp_path / "jobs")
        payload = fresh.describe(job.id)
        assert payload["from_journal"] is True
        assert payload["state"] == job.state

    def test_a_job_whose_process_is_gone_is_orphaned_not_running(self, tmp_path: Path):
        import json

        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()
        (jobs_dir / "ghost.json").write_text(
            json.dumps({"job_id": "ghost", "state": "running", "pid": 999_999_999}),
            encoding="utf-8",
        )
        payload = JobManager(jobs_dir=jobs_dir).describe("ghost")
        assert payload["state"] == "orphaned"
        assert "never recorded" in payload["error"]


class TestListing:
    def test_lists_live_jobs_newest_first(self, manager: JobManager, config: StitchConfig):
        first = manager.start(config)
        wait_for(lambda: first.done)
        second = manager.start(config)
        wait_for(lambda: second.done)
        assert [j.id for j in manager.list()][0] == second.id

    def test_journal_listing_survives_a_new_manager(self, manager: JobManager, config: StitchConfig, tmp_path: Path):
        job = manager.start(config)
        wait_for(lambda: job.done)
        records = JobManager(jobs_dir=tmp_path / "jobs").list_journalled()
        assert any(r["job_id"] == job.id for r in records)

    def test_journal_listing_is_empty_when_nothing_ran(self, tmp_path: Path):
        assert JobManager(jobs_dir=tmp_path / "never-used").list_journalled() == []


class TestJobRecord:
    def test_elapsed_time_advances_while_running(self, config: StitchConfig):
        job = Job(id="x", config=config)
        first = job.elapsed_seconds
        time.sleep(0.05)
        assert job.elapsed_seconds > first

    def test_elapsed_time_freezes_once_finished(self, config: StitchConfig):
        job = Job(id="x", config=config)
        job.finished_at = job.started_at + 3
        assert job.elapsed_seconds == pytest.approx(3, abs=0.001)

    def test_log_tail_is_bounded(self, config: StitchConfig):
        job = Job(id="x", config=config)
        for i in range(500):
            job._log.append(str(i))
        assert len(job.log_tail(1000)) <= 200

    def test_to_dict_summarises_the_config(self, config: StitchConfig):
        payload = Job(id="x", config=config).to_dict()
        assert payload["config"]["algorithm"] == "coordinate"
        assert payload["config"]["correction"] == "none"


class TestPidAlive:
    def test_this_process_is_alive(self):
        import os

        assert _pid_alive(os.getpid())

    def test_a_nonsense_pid_is_not_alive(self):
        assert not _pid_alive(999_999_999)

    def test_a_non_integer_is_not_alive(self):
        assert not _pid_alive("not a pid")
        assert not _pid_alive(None)
