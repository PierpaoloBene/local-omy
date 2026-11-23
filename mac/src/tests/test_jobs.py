"""The job queue: priorities, deduplication, claiming, retries."""

import time
from contextlib import closing

import pytest

from omy import jobs


@pytest.fixture(autouse=True)
def clean(archive):
    yield


def test_priorities_order():
    jobs.enqueue("index", "all")
    jobs.enqueue("organize", "s1")
    jobs.enqueue("render", "people")
    jobs.enqueue("diarize", "s2")
    jobs.enqueue("transcribe", "s3")
    order = []
    while j := jobs.next_job(list(jobs.KINDS)):
        order.append(j.kind)
        jobs.finish(j.id)
    assert order == ["transcribe", "diarize", "render", "organize", "index"]
    assert [jobs.PRIORITY[k] for k in order] == [10, 20, 30, 40, 50]


def test_enqueue_dedup_and_force():
    a = jobs.enqueue("render", "people")
    assert jobs.enqueue("render", "people") == a
    assert jobs.summary()["queued"] == 1
    o = jobs.enqueue("organize", "s1")
    assert jobs.enqueue("organize", "s1", force=True) == o
    assert jobs.get(o).force
    with pytest.raises(ValueError):
        jobs.enqueue("nope", "x")


def test_running_blocks_duplicates_except_render():
    t = jobs.enqueue("transcribe", "s1")
    assert jobs.next_job(["transcribe"]).id == t
    assert jobs.enqueue("transcribe", "s1") == t  # running: no-op
    r = jobs.enqueue("render", "people")
    assert jobs.next_job(["render"]).id == r
    r2 = jobs.enqueue("render", "people")  # a change that the running one may have missed
    assert r2 != r
    assert jobs.next_job(["render"]) is None  # waits for the running one
    jobs.finish(r)
    assert jobs.next_job(["render"]).id == r2


def test_claim_is_filtered_and_exclusive():
    jobs.enqueue("organize", "s1")
    assert jobs.next_job(["transcribe", "diarize"]) is None
    j = jobs.next_job(["organize"])
    assert j.attempts == 1 and jobs.state_of(j.id) == "running"
    assert jobs.next_job(["organize"]) is None


def test_forced_kinds_only_for_forced_jobs():
    jobs.enqueue("organize", "plain")
    assert jobs.next_job([], forced_kinds=["organize"]) is None
    jobs.enqueue("organize", "forced", force=True)
    assert jobs.next_job([], forced_kinds=["organize"]).key == "forced"


def test_fail_retries_later_then_gives_up():
    jid = jobs.enqueue("diarize", "s1")
    for n in (1, 2, 3):
        j = jobs.next_job(["diarize"], now=time.time() + 10_000 * n)
        assert j.attempts == n
        assert jobs.fail(j.id, "boom", retry_after=30) is (n < jobs.MAX_ATTEMPTS)
        if n < jobs.MAX_ATTEMPTS:
            assert jobs.next_job(["diarize"]) is None  # not due yet
    assert jobs.state_of(jid) == "failed"
    assert jobs.summary()["failed"][0]["error"] == "boom"
    assert jobs.pending(["diarize"]) == 0
    assert jobs.enqueue("diarize", "s1") != jid  # a failed job doesn't block a new one


def test_default_backoff_doubles():
    jobs.enqueue("diarize", "s1")
    j = jobs.next_job(["diarize"])
    t0 = time.time()
    jobs.fail(j.id, "x")
    with closing(jobs._connect()) as db:
        not_before = db.execute("SELECT not_before FROM jobs WHERE id=?", (j.id,)).fetchone()[0]
    assert 55 < not_before - t0 < 65


def test_requeue_keeps_attempts_and_reset_running():
    jid = jobs.enqueue("organize", "s1")
    assert jobs.next_job(["organize"]).attempts == 1
    jobs.requeue(jid)  # preemption: as if never started
    assert jobs.get(jid).attempts == 0 and jobs.state_of(jid) == "queued"
    jobs.next_job(["organize"])
    assert jobs.reset_running() == 1  # worker restart: the attempt stays counted
    assert jobs.state_of(jid) == "queued" and jobs.get(jid).attempts == 1


def test_a_job_that_crashes_the_worker_runs_out_of_attempts():
    jid = jobs.enqueue("diarize", "s1")
    for n in range(1, jobs.MAX_ATTEMPTS + 1):
        assert jobs.next_job(["diarize"]).attempts == n
        jobs.reset_running()  # the worker died, again
    assert jobs.state_of(jid) == "failed" and jobs.next_job(["diarize"]) is None


def test_gave_up_only_on_the_same_input():
    jid = jobs.enqueue("organize", "s1", input_hash="h1")
    for _ in range(jobs.MAX_ATTEMPTS):
        j = jobs.next_job(["organize"], now=time.time() + 1e6)
        jobs.fail(j.id, "x", retry_after=0)
    assert jobs.state_of(jid) == "failed"
    assert jobs.gave_up("organize", "s1", "h1")
    assert not jobs.gave_up("organize", "s1", "h2")  # the input changed
    assert not jobs.gave_up("organize", "other", "h1")
    jobs.finish(jobs.enqueue("organize", "s1", input_hash="h2"))
    assert not jobs.gave_up("organize", "s1", "h1")  # done since


def test_ignore_window_and_force_flags_upgrade_queued_and_running():
    o = jobs.enqueue("organize", "s1", ignore_window=True)
    j = jobs.get(o)
    assert j.ignore_window and not j.force
    assert jobs.next_job([], forced_kinds=["organize"]).key == "s1"  # running, plain
    assert jobs.enqueue("organize", "s1", force=True, ignore_window=True) == o  # upgrade a running job
    assert jobs.get(o).force
    jobs.finish(o)  # a forced re-run follows
    again = jobs.next_job([], forced_kinds=["organize"])
    assert again is not None and again.id != o and again.force
    jobs.finish(again.id)
    assert jobs.next_job(["organize"]) is None  # no endless re-runs


def test_pending_counts_queued_and_running():
    assert jobs.pending(["transcribe", "diarize"]) == 0
    a = jobs.enqueue("transcribe", "a")
    jobs.enqueue("diarize", "b")
    jobs.enqueue("organize", "c")
    assert jobs.pending(["transcribe", "diarize"]) == 2
    jobs.next_job(["transcribe"])
    assert jobs.pending(["transcribe", "diarize"]) == 2
    jobs.finish(a)
    assert jobs.pending(["transcribe", "diarize"]) == 1


def test_recent():
    jid = jobs.enqueue("transcribe", "s1")
    jobs.next_job(["transcribe"])
    jobs.finish(jid)
    assert jobs.recent("transcribe", "s1", "done", 60)
    assert not jobs.recent("transcribe", "s1", "failed", 60)
    assert not jobs.recent("transcribe", "other", "done", 60)


def test_demand_wake_summary():
    assert jobs.demand_at("chat") is None
    jobs.touch_demand("chat", at=100.0)
    assert jobs.demand_at("chat") == 100.0
    jobs.touch_demand("chat")
    assert jobs.demand_at("chat") > 100.0
    before = jobs.wake_time()
    time.sleep(0.01)
    jobs.wake()
    assert jobs.wake_time() > before
    assert set(jobs.summary()) == {"queued", "by_kind", "running", "failed"}
