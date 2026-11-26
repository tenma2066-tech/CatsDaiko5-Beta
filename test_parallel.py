"""Exercise concurrent jobs in separate spawned processes without a game server."""
import os
import fcntl
import hashlib
import hmac
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


import app as A


def fake_job(job, transfer, confirm, *args):
    if transfer == "CRASH":
        os._exit(7)
    A._get_bcsfe()
    version = "15.9.0" if transfer == "FIRST" else "15.10.0"
    A._learn_version(SimpleNamespace(game_version=version))
    A._set_names({0: [transfer]}, "fixture", version)
    data_dir = Path(A.engine.core.data_dir_path.to_str())
    (data_dir / "worker.txt").write_text(transfer, encoding="utf-8")
    job["result"] = {"transfer": transfer, "confirm": confirm, "pid": os.getpid()}
    A._log(job, "fixture running")
    deadline = time.monotonic() + 10
    while not Path(os.environ["PARALLEL_TEST_GATE"]).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("test gate timed out")
        time.sleep(0.01)
    job["status"] = "done"
    job["finished"] = time.time()
    A._publish_job(job)


def fixture_worker(pipe, job, arguments, slot, learned_version, names):
    A._run_job = fake_job
    A._worker_entry(pipe, job, arguments, slot, learned_version, names)


def fixture_real_job_worker(pipe, job, arguments, slot, learned_version, names):
    class Handler:
        def __init__(self, transfer):
            self.save_file = SimpleNamespace(game_version=None, xp={"FIRST": 10, "SECOND": 20, "THIRD": 30, "FOURTH": 40}[transfer])

        @staticmethod
        def from_codes(transfer, confirm, cc, gv):
            Path(os.environ["PARALLEL_TEST_GATE"] + "." + transfer).touch()
            deadline = time.monotonic() + 10
            while not Path(os.environ["PARALLEL_TEST_GATE"]).exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("test gate timed out")
                time.sleep(0.01)
            return Handler(transfer), None

        def get_codes(self, upload_managed_items=False, tries=1):   # 試運転は廃止したので、コード発行まで進む
            return ("T-" + str(self.save_file.xp), "1234")

    audit = SimpleNamespace(ok=True, cats_checked=0, critical_count=0,
                            warning_count=0, findings=[], as_dict=lambda: {"ok": True})
    A.engine.core.ServerHandler = Handler
    A.save_audit.audit_save = lambda *args, **kwargs: audit
    A.save_repair.plan_known_error_character_repairs = lambda *args: ()
    A.engine.ensure_cat_roster = lambda save: None
    A._harvest_names = lambda save: None
    A._snap = lambda save: {"XP": save.xp}
    A._apply_edits = lambda job, save, *args: setattr(save, "xp", save.xp + 1)
    A._worker_entry(pipe, job, arguments, slot, learned_version, names)


class ParallelJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gate = Path(self.temp.name) / "go"
        self.env = patch.dict(os.environ, {
            "PARALLEL_TEST_GATE": str(self.gate),
            "BCSFE_PARALLEL_ROOT": str(Path(self.temp.name) / "workers"),
        })
        self.env.start()
        self.auth_store = patch.object(A, "PAYMENTS_ENABLED", False)
        self.auth_store.start()
        self.max_jobs = patch.object(A, "MAX_CONCURRENT_JOBS", 4)
        self.slots = patch.object(A, "_slots", [None] * 4)
        self.max_jobs.start()
        self.slots.start()
        self.worker = patch.object(A, "_worker_entry", fixture_worker)
        self.worker.start()
        self.names_file = patch.object(A, "_NAMES_FILE", str(Path(self.temp.name) / "names.json"))
        self.version_file = patch.object(A, "_VERSION_FILE", str(Path(self.temp.name) / "version.json"))
        self.names_file.start()
        self.version_file.start()
        self.old_names, self.old_version = dict(A._names), A._learned["version"]
        A._names.update(state="none", items=[], version=None, updated=0)
        A._learned["version"] = None
        self.client = A.app.test_client()

    def tearDown(self):
        self.gate.touch()
        self.wait_for(lambda: all(slot is None for slot in A._slots))
        self.worker.stop()
        self.names_file.stop()
        self.version_file.stop()
        self.env.stop()
        self.auth_store.stop()
        self.slots.stop()
        self.max_jobs.stop()
        A._names.clear()
        A._names.update(self.old_names)
        A._learned["version"] = self.old_version
        with A._jobs_lock:
            A._jobs.clear()
        self.temp.cleanup()

    def wait_for(self, predicate):
        end = time.monotonic() + 12
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail("Timed out waiting for worker")

    def start(self, transfer, confirm="1234", dry=False):
        return self.client.post("/run", json={"transfer": transfer, "confirm": confirm, "items": ["XP"], "dry": dry})

    def status(self, job_id):
        data = self.client.get("/status/" + job_id).get_json()
        # 実行の途中の結果は、/status では返さない（複製の元コードが評価の前に見えないように）。
        # 試験用のワーカーが実行中に書く印（pid など）は、内部の記録から読む。
        if data.get("result") is None and data.get("status") in ("running", "queued"):
            with A._jobs_lock:
                data["result"] = (A._jobs.get(job_id) or {}).get("result")
        return data

    def test_default_and_configured_limits(self):
        for configured, expected in ((None, 4), ("3", 3)):
            env = os.environ.copy()
            env.pop("MAX_CONCURRENT_JOBS", None)
            if configured is not None:
                env["MAX_CONCURRENT_JOBS"] = configured
            output = subprocess.check_output(
                [sys.executable, "-c", "import json, app; print(json.dumps([app.MAX_CONCURRENT_JOBS, len(app._slots)]))"],
                env=env, text=True,
            )
            self.assertEqual(json.loads(output), [expected, expected])

    def test_distinct_jobs_overlap_and_keep_their_results_separate(self):
        transfers = ("FIRST", "SECOND", "THIRD", "FOURTH")
        responses = [self.start(transfer) for transfer in transfers]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        jobs = [response.get_json()["job"] for response in responses]
        self.wait_for(lambda: all(self.status(job)["result"] for job in jobs))
        self.assertTrue(all(self.status(job)["status"] == "running" for job in jobs))
        self.assertEqual(len({self.status(job)["result"]["pid"] for job in jobs}), 4)
        self.assertEqual(self.start("FIRST", "9999").status_code, 409)
        fifth = self.start("FIFTH")
        self.assertEqual(fifth.status_code, 200)
        fifth_job = fifth.get_json()["job"]
        self.assertEqual(self.status(fifth_job)["status"], "queued")
        self.assertEqual(self.status(fifth_job)["queue_position"], 1)
        self.assertEqual(self.start("FIFTH").status_code, 409)
        state = self.client.get("/api/state").get_json()
        self.assertEqual((state["active_jobs"], state["max_concurrent_jobs"]), (4, 4))
        self.assertEqual(state["queued_jobs"], 1)
        root = Path(self.temp.name) / "workers"
        self.assertEqual({(root / str(i) / "data" / "worker.txt").read_text() for i in range(4)}, set(transfers))
        self.gate.touch()
        self.wait_for(lambda: all(self.status(job)["status"] == "done" for job in jobs))
        self.wait_for(lambda: self.status(fifth_job)["status"] == "done")
        self.assertEqual([self.status(job)["result"]["transfer"] for job in jobs], list(transfers))
        self.assertEqual(A._learned["version"], "15.10.0")
        self.assertEqual(A._names["version"], "15.10.0")
        self.assertIn(A._names["items"][0]["name"], (*transfers[1:], "FIFTH"))

    def test_worker_crash_releases_its_slot_and_transfer(self):
        response = self.start("CRASH")
        self.assertEqual(response.status_code, 200)
        job_id = response.get_json()["job"]
        self.wait_for(lambda: self.status(job_id)["status"] == "error")
        self.wait_for(lambda: all(slot is None for slot in A._slots))
        self.gate.touch()
        retry = self.start("CRASH")
        self.assertEqual(retry.status_code, 200)
        self.wait_for(lambda: self.status(retry.get_json()["job"])["status"] == "error")

    def test_queued_job_can_be_cancelled_and_code_reused(self):
        jobs = [self.start(t).get_json()["job"] for t in ("FIRST", "SECOND", "THIRD", "FOURTH")]
        self.wait_for(lambda: all(self.status(job)["result"] for job in jobs))
        queued = self.start("FIFTH").get_json()["job"]
        self.assertEqual(self.client.post('/cancel/' + queued).status_code, 200)
        self.assertEqual(self.status(queued)["status"], "error")
        retry = self.start("FIFTH").get_json()["job"]
        self.assertEqual(self.status(retry)["status"], "queued")
        self.client.post('/cancel/' + retry)

    def test_waiting_and_completed_jobs_expire(self):
        jobs = [self.start(t).get_json()["job"] for t in ("FIRST", "SECOND", "THIRD", "FOURTH")]
        self.wait_for(lambda: all(self.status(job)["result"] for job in jobs))
        queued = self.start("FIFTH").get_json()["job"]
        with A._jobs_lock:
            A._jobs[queued]["created"] = time.time() - A.QUEUE_TTL - 1
        A._gc_jobs()
        self.assertEqual(self.status(queued)["status"], "error")
        retry = self.start("FIFTH").get_json()["job"]
        self.client.post('/cancel/' + retry)
        with A._jobs_lock:
            A._jobs[retry]["finished"] = time.time() - A.JOB_TTL - 1
        self.assertEqual(self.client.get('/status/' + retry).status_code, 404)

    def test_real_job_flow_runs_in_parallel_with_fake_server(self):
        self.worker.stop()
        self.worker = patch.object(A, "_worker_entry", fixture_real_job_worker)
        self.worker.start()
        transfers = ("FIRST", "SECOND", "THIRD", "FOURTH")
        responses = [self.start(transfer, dry=True) for transfer in transfers]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        jobs = [response.get_json()["job"] for response in responses]
        self.wait_for(lambda: all(Path(str(self.gate) + "." + transfer).exists() for transfer in transfers))
        self.assertTrue(all(self.status(job)["status"] == "running" for job in jobs))
        self.gate.touch()
        self.wait_for(lambda: all(self.status(job)["status"] != "running" for job in jobs))
        for job, before in zip(jobs, (10, 20, 30, 40)):
            status = self.status(job)
            self.assertEqual(status["status"], "done", status["error"])
            self.assertEqual(status["report"], [{"label": "XP", "before": before, "after": before + 1}])
            self.assertTrue(status["result"])                      # 試運転は廃止：コードまで発行される

    def test_transfer_lock_prevents_overlap_with_another_server_process(self):
        lock_dir = Path(self.temp.name) / "workers" / "locks"
        lock_dir.mkdir(parents=True)
        digest = hmac.new(A.app.secret_key.encode(), b"FIRST", hashlib.sha256).hexdigest()
        with (lock_dir / ("transfer-" + digest)).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            response = self.start("FIRST")
            self.assertEqual(response.status_code, 200)
            job_id = response.get_json()["job"]
            self.wait_for(lambda: self.status(job_id)["status"] == "error")
            self.assertIn("まだ実行中", self.status(job_id)["error"])
            self.assertIsNone(self.status(job_id)["result"])
        self.wait_for(lambda: all(slot is None for slot in A._slots))
        self.gate.touch()
        retry = self.start("FIRST")
        self.assertEqual(retry.status_code, 200)
        self.wait_for(lambda: self.status(retry.get_json()["job"])["status"] == "done")

    def test_running_job_is_not_expired(self):
        response = self.start("FIRST")
        job_id = response.get_json()["job"]
        self.wait_for(lambda: self.status(job_id)["result"])
        with A._jobs_lock:
            A._jobs[job_id]["created"] = time.time() - A.JOB_TTL - 1
        A._gc_jobs()
        self.assertEqual(self.client.get("/status/" + job_id).status_code, 200)


    def test_state_separates_running_jobs_from_warmup_slots(self):
        from collections import deque
        with patch.object(A, "_slots", ["job-a", "warmup", None, "job-b"]), \
             patch.object(A, "_pending_jobs", deque([("queued", (), None)])):
            state = self.client.get("/api/state").get_json()
        self.assertEqual(state["running_jobs"], 2)
        self.assertEqual(state["active_jobs"], 2)
        self.assertTrue(state["busy"])
        self.assertEqual(state["warmup_slots"], 1)
        self.assertEqual(state["occupied_slots"], 3)
        self.assertEqual(state["available_slots"], 1)
        self.assertEqual(state["queued_jobs"], 1)
        self.assertEqual([slot["state"] for slot in state["slot_states"]],
                         ["running", "warmup", "free", "running"])

    def test_job_status_reports_its_real_slot_and_global_usage(self):
        response = self.start("FIRST")
        self.assertEqual(response.status_code, 200)
        job_id = response.get_json()["job"]
        self.wait_for(lambda: self.status(job_id)["status"] == "running")
        status = self.status(job_id)
        self.assertEqual(status["slot"], 1)
        self.assertEqual(status["max_concurrent_jobs"], 4)
        self.assertEqual(status["active_jobs"], 1)
        self.assertEqual(status["occupied_slots"], 1)
        self.assertEqual(status["available_slots"], 3)
        self.assertEqual(status["queued_jobs"], 0)


if __name__ == "__main__":
    unittest.main()
