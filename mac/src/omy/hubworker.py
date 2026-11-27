"""The hub's scheduler (`omy hub-worker`, next to the receiver in the hub container).

The same Worker as on the Mac, with the part that needs the Mac's models left out: it scans for work (closed
sessions to transcribe or diarize, transcripts to organize: Worker.scan), reaps the leases the Mac let expire,
queues the embedding of new chunks, and runs the jobs that are plain Python (render, index: chunks and full text,
no vectors). transcribe, diarize, organize and embed are leased to the Mac (omy_receiver/compute.py) and
committed by omy.hubjobs. No models, no power or time-of-day conditions: those are the Mac's business.
"""

from __future__ import annotations

import signal
import threading
import time

from omy import hubjobs, hubstate, jobs
from omy.worker import SCAN_EVERY, Worker, log

REAP_EVERY = 30.0
HUB_KINDS = ["render", "index"]


class HubWorker(Worker):
    def __init__(self) -> None:
        super().__init__()
        self.last_reap = 0.0

    # What may run: the pure-Python jobs, always. The vault batch waits for the Mac's organize jobs while it is there.
    def allowed(self) -> tuple[list[str], list[str]]:
        return list(HUB_KINDS), []

    def organize_ok(self) -> bool:
        return hubstate.online()

    def write_status(self) -> None:  # cache/worker.json is the Mac's status (POST /compute/v1/status)
        pass

    def scan(self) -> dict:
        out = super().scan()
        # new chunks without a vector: the Mac embeds them (a job of its own, batch by batch)
        if hubstate.can("embed") and hubjobs.missing_vectors():
            jobs.enqueue("embed", "all")
        return out

    def step(self) -> bool:
        now = time.time()
        if now - self.last_reap >= REAP_EVERY:
            self.last_reap = now
            lost = jobs.reap()
            if lost:
                log(f"leases expired: {len(lost)} jobs back in the queue ({lost})")
        if self.scan_due or now - self.last_scan >= SCAN_EVERY:
            try:
                self.scan()
            except Exception as e:  # noqa: BLE001
                log(f"scan failed: {e}")
        self.finalize_batch()
        job = jobs.next_job(*self.allowed()[:1])
        if job is None:
            return False
        self.run_job(job)
        return True

    def run_index(self, job: jobs.Job) -> None:
        from omy.brain import index
        sessions = None if job.key == "all" else [x for x in job.key.split(",") if x]
        stats = index.update(sessions)
        if stats.get("pending"):
            jobs.enqueue("embed", "all")

    def run(self) -> None:
        signal.signal(signal.SIGUSR1, lambda *_: (setattr(self, "scan_due", True), self.wake_event.set()))
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log(f"hub-worker started, {jobs.reset_running()} running jobs back in the queue")
        while not self.stopping:
            try:
                if self.step():
                    continue
            except Exception as e:  # noqa: BLE001
                log(f"step failed: {e}")
            self.wake_event.wait(2.0)
            self.wake_event.clear()
            wt = jobs.wake_time()
            if wt > self.wake_seen:  # the receiver (an upload, "Trascrivi ora") or the compute API woke us
                self.wake_seen, self.scan_due = wt, True
        log("hub-worker stopped")


def main() -> int:
    HubWorker().run()
    return 0
