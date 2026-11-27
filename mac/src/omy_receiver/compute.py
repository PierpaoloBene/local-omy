"""The compute API: what the Mac's compute worker calls on the hub (docs/hub-migration.md, 2).

Mounted by the receiver when OMY_COMPUTE_TOKEN is set, under /compute/v1/, with its own token (the iPhone's
token is no good here, and this one is no good on /v1/). The Mac pulls: it asks for a job with a lease,
downloads the inputs, runs the unchanged pipeline, uploads the outputs and completes the job. The hub commits
atomically (omy.hubjobs). Nothing is ever pushed to the Mac, so the Mac needs no open port.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

PROTOCOL = 1
MAX_JSON = 4 * 1024 * 1024
MAX_OUTPUT = 256 * 1024 * 1024
MAX_WAIT = 25.0
WORKER_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
OUT_NAME = re.compile(r"[A-Za-z0-9._/*-]{1,200}")


def _err(status: int, detail: str, **extra) -> JSONResponse:
    return JSONResponse({"error": detail, **extra}, status_code=status)


def build_router(config, authorized, log, live=None) -> APIRouter:
    from omy import asks, hubjobs, hubstate, jobs

    router = APIRouter(prefix="/compute/v1")
    archive: Path = config.archive
    staging = archive / "tmp" / "compute"

    def gate(request: Request) -> JSONResponse | None:
        if not authorized(request, config.compute_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if request.headers.get("x-omy-compute-protocol") != str(PROTOCOL):
            return _err(426, "unsupported compute protocol", protocol=PROTOCOL)
        return None

    async def body_json(request: Request, limit: int = MAX_JSON) -> dict:
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > limit:
                raise ValueError("body too large")
        doc = json.loads(data or b"{}")
        if not isinstance(doc, dict):
            raise ValueError("body is not an object")
        return doc

    # --- lease --------------------------------------------------------------------------------

    def take(worker_id: str, boot_id: str, kinds: list[str], forced: list[str], caps, state):
        prev = hubstate.read()
        if prev.get("worker_id") == worker_id and prev.get("boot_id") not in (None, boot_id):
            n = jobs.release_worker(worker_id)  # it restarted: whatever it held is not running
            if n:
                log.warning("compute worker %s restarted: %d leases released", worker_id, n)
        hubstate.record(worker_id, boot_id, caps, state, kinds)
        ok = [k for k in kinds if k in jobs.COMPUTE_KINDS and hubstate.capable(caps, k) is None]
        ok_forced = [k for k in forced if k in jobs.COMPUTE_KINDS and hubstate.capable(caps, k) is None]
        while True:
            job = jobs.next_job(ok, forced_kinds=ok_forced, lease_for=worker_id)
            if job is None:
                return None
            try:
                prep = hubjobs.inputs_for(job)
            except Exception as e:  # noqa: BLE001 - one broken session must not stop the queue
                log.error("compute: inputs of %s %s: %s", job.kind, job.key, e)
                jobs.fail(job.id, f"inputs: {e}")
                continue
            if prep is None:
                jobs.finish(job.id)  # nothing left to do (the session changed, the note is up to date)
                continue
            return job, prep

    @router.post("/lease")
    async def lease(request: Request):
        if (r := gate(request)) is not None:
            return r
        try:
            b = await body_json(request)
            worker_id, boot_id = b["worker_id"], b["boot_id"]
            kinds, forced = b.get("kinds", []), b.get("forced_kinds", [])
            wait = min(max(float(b.get("wait_s", 0)), 0.0), MAX_WAIT)
            if (not isinstance(worker_id, str) or not WORKER_ID.fullmatch(worker_id) or not isinstance(boot_id, str)
                    or not isinstance(kinds, list) or not isinstance(forced, list)
                    or not all(isinstance(k, str) for k in (*kinds, *forced))):
                raise ValueError
            caps, state = b.get("capabilities"), b.get("state")
            if not isinstance(caps, dict) or (state is not None and not isinstance(state, dict)):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return _err(400, "invalid lease request")
        deadline = time.monotonic() + wait
        while True:
            got = await asyncio.to_thread(take, worker_id, boot_id, kinds, forced, caps, state)
            if got is not None:
                job, prep = got
                return {"job": {"id": job.id, "kind": job.kind, "key": job.key, "lease_id": job.lease_id,
                                "lease_s": jobs.LEASE_S, "attempt": job.attempts, "force": job.force},
                        "inputs": prep["inputs"], "outputs": prep["outputs"], "params": prep.get("params", {}),
                        "payload": prep.get("payload")}
            left = deadline - time.monotonic()
            if left <= 0:
                return Response(status_code=204)
            await asyncio.sleep(min(2.0, left))

    # --- inputs and outputs --------------------------------------------------------------------

    def held(job_id: int, lease_id: str | None, reclaim: bool = False):
        if not lease_id:
            return None
        return jobs.lease_of(job_id, lease_id) or (jobs.reclaimable(job_id, lease_id) if reclaim else None)

    @router.get("/jobs/{job_id}/inputs/{path:path}")
    async def get_input(job_id: int, path: str, request: Request):
        if (r := gate(request)) is not None:
            return r
        if held(job_id, request.headers.get("x-lease-id")) is None:
            return _err(409, "no valid lease")
        f = hubjobs.can_read(path)
        if f is None:
            return _err(404, "not an input")
        headers = {"X-Content-Path": path}
        if f.suffix == ".bin":
            try:
                headers["X-Sha256"] = json.loads(f.with_suffix(".json").read_text())["sha256"]
            except (OSError, ValueError, KeyError):
                pass
        if "X-Sha256" not in headers:
            headers["X-Sha256"] = await asyncio.to_thread(hubjobs.sha256_file, f)
        # FileResponse: Range, If-Range, ETag, Last-Modified
        return FileResponse(f, media_type="application/octet-stream", headers=headers)

    def stage_dir(job_id: int, lease_id: str) -> Path:
        return staging / f"{job_id}-{lease_id}"

    def stage_path(job_id: int, lease_id: str, name: str) -> Path:
        return stage_dir(job_id, lease_id) / name.replace("/", "~")

    @router.put("/jobs/{job_id}/outputs/{name:path}")
    async def put_output(job_id: int, name: str, request: Request):
        if (r := gate(request)) is not None:
            return r
        lease_id = request.headers.get("x-lease-id")
        job = held(job_id, lease_id, reclaim=True)
        if job is None:
            return _err(409, "no valid lease")
        sha = request.headers.get("x-sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return _err(400, "invalid X-Sha256")
        if not OUT_NAME.fullmatch(name) or not hubjobs.output_allowed(job.kind, job.key, name):
            return _err(400, "not an output of this job")
        d = stage_dir(job_id, lease_id)
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".part.")
        try:
            h, size = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_OUTPUT:
                        return _err(413, "output too large")
                    h.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            if h.hexdigest() != sha:
                return _err(400, "body sha256 mismatch")
            os.replace(tmp, stage_path(job_id, lease_id, name))  # same (name, hash) again: same file
            return {"name": name, "sha256": sha, "size": size}
        finally:
            Path(tmp).unlink(missing_ok=True)

    # --- finishing -----------------------------------------------------------------------------

    def do_complete(job_id: int, b: dict):
        lease_id = b.get("lease_id")
        job = held(job_id, lease_id, reclaim=True)
        if job is None:
            if isinstance(lease_id, str) and jobs.completed(job_id, lease_id):
                return 200, {"status": "duplicate"}
            return 409, {"error": "stale", "detail": "the lease is gone and the job was given to somebody else"}
        versions = b.get("versions")
        if isinstance(versions, dict):
            why = hubstate.capable({"kinds": [job.kind], "versions": versions}, job.kind)
            if why:
                return 409, {"error": "version", "detail": why}
        staged: dict[str, Path] = {}
        for o in b.get("outputs", []):
            name, sha = o.get("name"), o.get("sha256")
            path = stage_path(job_id, lease_id, name) if isinstance(name, str) else None
            if (path is None or not path.is_file() or hubjobs.sha256_file(path) != sha
                    or not hubjobs.output_allowed(job.kind, job.key, name)):
                return 400, {"error": f"output {name!r} was not uploaded or does not match its hash"}
            staged[name] = path
        try:
            done = hubjobs.commit(job, staged, b)
        except hubjobs.Stale as e:
            jobs.requeue(job.id)  # a new lease will plan the work again from what the hub has now
            shutil.rmtree(stage_dir(job_id, lease_id), ignore_errors=True)
            return 409, {"error": "stale", "detail": str(e)}
        except hubjobs.Invalid as e:
            jobs.fail(job.id, f"invalid output: {e}", retry_after=300.0)
            shutil.rmtree(stage_dir(job_id, lease_id), ignore_errors=True)
            return 422, {"error": "invalid", "detail": str(e)}
        jobs.finish(job.id)
        shutil.rmtree(stage_dir(job_id, lease_id), ignore_errors=True)
        return 200, {"status": "done", **done}

    @router.post("/jobs/{job_id}/complete")
    async def complete(job_id: int, request: Request):
        if (r := gate(request)) is not None:
            return r
        try:
            b = await body_json(request)
        except ValueError:
            return _err(400, "invalid body")
        code, doc = await asyncio.to_thread(do_complete, job_id, b)
        return JSONResponse(doc, status_code=code)

    @router.post("/jobs/{job_id}/heartbeat")
    async def heartbeat(job_id: int, request: Request):
        if (r := gate(request)) is not None:
            return r
        try:
            b = await body_json(request)
        except ValueError:
            return _err(400, "invalid body")
        lease_id = b.get("lease_id")
        ok = isinstance(lease_id, str) and await asyncio.to_thread(jobs.heartbeat, job_id, lease_id)
        if not ok:
            return _err(409, "lease lost", cancel=True)
        return {"ok": True, "cancel": False}

    @router.post("/jobs/{job_id}/fail")
    async def fail(job_id: int, request: Request):
        if (r := gate(request)) is not None:
            return r
        try:
            b = await body_json(request)
            err, retry = str(b.get("error", "failed"))[:1000], b.get("retry_after")
            retry = float(retry) if retry is not None else None
        except (ValueError, TypeError):
            return _err(400, "invalid body")
        if await asyncio.to_thread(held, job_id, b.get("lease_id"), True) is None:
            return _err(409, "stale")
        again = await asyncio.to_thread(jobs.fail, job_id, err, retry, bool(b.get("permanent")))
        return {"retry": again}

    @router.post("/jobs/{job_id}/release")
    async def release(job_id: int, request: Request):
        if (r := gate(request)) is not None:
            return r
        try:
            b = await body_json(request)
        except ValueError:
            return _err(400, "invalid body")
        lease_id = b.get("lease_id")
        ok = isinstance(lease_id, str) and await asyncio.to_thread(jobs.release, job_id, lease_id)
        return {"released": ok}

    # --- status, context, settings, profile -------------------------------------------------------

    @router.post("/status")
    async def status(request: Request):
        """The worker's state (the JSON of worker.write_status): brain/status, the widget and the Mac app read it."""
        if (r := gate(request)) is not None:
            return r
        try:
            b = await body_json(request)
            st = b["state"]
            if not isinstance(st, dict):
                raise ValueError
        except (ValueError, KeyError):
            return _err(400, "invalid body")

        def write():
            from omy import worker
            hubstate.record(str(b.get("worker_id", "mac")), str(b.get("boot_id", "")), b.get("capabilities"), st,
                            b.get("kinds"))
            doc = {**st, "heartbeat": time.time(), "queue": jobs.summary(),
                   "version_problems": hubstate.version_problems()}
            worker.STATUS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = worker.STATUS.with_name(f".{worker.STATUS.name}.{os.getpid()}")
            tmp.write_text(json.dumps(doc) + "\n")
            os.replace(tmp, worker.STATUS)

        await asyncio.to_thread(write)
        return {"ok": True}

    @router.put("/context/{day}")
    async def put_context(day: str, request: Request):
        """context/audio-DAY.jsonl, written on the Mac (who uses the microphone and the speakers): diarize
        needs it. It only grows: an older, shorter file never replaces a newer one."""
        if (r := gate(request)) is not None:
            return r
        try:
            date.fromisoformat(day)
        except ValueError:
            return _err(400, "invalid day")
        sha = request.headers.get("x-sha256", "")
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > 16 * 1024 * 1024:
                return _err(413, "body too large")
        if hashlib.sha256(data).hexdigest() != sha:
            return _err(400, "body sha256 mismatch")
        dest = archive / "context" / f"audio-{day}.jsonl"

        def store() -> str:
            if dest.is_file():
                cur = dest.read_bytes()
                if cur == data:
                    return "same"
                if len(cur) > len(data):
                    return "kept"
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".ctx.")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
            return "stored"

        return {"status": await asyncio.to_thread(store)}

    @router.get("/settings")
    async def get_settings(request: Request):
        if (r := gate(request)) is not None:
            return r
        from omy_receiver import features
        return {"features": await asyncio.to_thread(features.load, archive)}

    @router.put("/profile")
    async def put_profile(request: Request):
        """voices/me.npy, built on the Mac (omy voice enroll needs pyannote)."""
        if (r := gate(request)) is not None:
            return r
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > 1024 * 1024:
                return _err(413, "body too large")
        try:
            import numpy as np
            v = np.load(io.BytesIO(data), allow_pickle=False)
            if v.ndim != 1 or not np.all(np.isfinite(v)) or not 0 < v.size <= 4096:
                raise ValueError
        except Exception:  # noqa: BLE001
            return _err(400, "not a voice profile")
        dest = archive / "voices" / "me.npy"
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".me.")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return {"ok": True}

    # --- "Chiedi": the questions the iPhone asked (omy.asks) -----------------------------------------------

    def publish(ask_id: str, event: dict) -> None:
        if live is not None:
            live.publish(ask_id, event)

    @router.get("/ask/next")
    async def ask_next(request: Request, worker_id: str = "mac", wait: float = 0.0):
        """Long-poll: the oldest question nobody is answering, leased to this worker, or 204."""
        if (r := gate(request)) is not None:
            return r
        if not WORKER_ID.fullmatch(worker_id):
            return _err(400, "invalid worker_id")
        deadline = time.monotonic() + min(max(wait, 0.0), MAX_WAIT)
        while True:
            await asyncio.to_thread(hubstate.record, worker_id, hubstate.read().get("boot_id", ""), None, None)
            a = await asyncio.to_thread(asks.next_for, worker_id)
            if a is not None:
                from omy.brain import ask as ask_mod
                return {"ask": {"id": a["id"], "lease_id": a["lease_id"], "question": a["question"],
                                "history": a["history"], "now": a["asked_at"],
                                "embed_text": ask_mod.retrieval_query(a["question"], a["history"])}}
            left = deadline - time.monotonic()
            if left <= 0:
                return Response(status_code=204)
            await asyncio.sleep(min(1.0, left))

    @router.post("/ask/{ask_id}/retrieve")
    async def ask_retrieve(ask_id: str, request: Request):
        """The hub searches (dates, people, BM25 + the query vector the Mac made) and answers with the messages to give
        to the chat model, or finishes the question itself when nothing relevant was found."""
        if (r := gate(request)) is not None:
            return r
        try:
            b = await body_json(request)
            lease_id, qvec = b["lease_id"], b.get("qvec")
            if qvec is not None and (not isinstance(qvec, list) or len(qvec) > 4096):
                raise ValueError
        except (ValueError, KeyError):
            return _err(400, "invalid body")
        a = await asyncio.to_thread(asks.get, ask_id)
        if a is None or not await asyncio.to_thread(asks.holds, ask_id, lease_id):
            return _err(409, "no valid lease")
        from datetime import datetime
        from omy.brain import ask as ask_mod
        from omy_receiver.localtime import local_tz
        try:
            now = datetime.fromisoformat(a["asked_at"]) if a["asked_at"] else datetime.now(local_tz())
            if now.tzinfo is None:
                now = now.replace(tzinfo=local_tz())
        except ValueError:
            now = datetime.now(local_tz())
        publish(ask_id, {"type": "status", "stage": "searching"})
        found = await asyncio.to_thread(ask_mod.retrieve, a["question"], a["history"], now, qvec)
        if found["not_found"]:
            await asyncio.to_thread(asks.finish, ask_id, lease_id, citations=[], used=[], not_found=True,
                                    answer=ask_mod.NOT_FOUND)
            for ev in ({"type": "citations", "items": []}, {"type": "delta", "text": ask_mod.NOT_FOUND},
                       {"type": "done", "not_found": True, "used": [], "timings": {}}):
                publish(ask_id, ev)
            return {"not_found": True}
        await asyncio.to_thread(asks.set_citations, ask_id, lease_id, found["citations"])
        publish(ask_id, {"type": "citations", "items": found["citations"]})
        publish(ask_id, {"type": "status", "stage": "answering"})
        return {"not_found": False, "citations": found["citations"], "messages": found["messages"]}

    @router.post("/ask/{ask_id}/events")
    async def ask_events(ask_id: str, request: Request):
        """The Mac's generation, as the events of /v1/ask: delta (text pieces), then done or error."""
        if (r := gate(request)) is not None:
            return r
        try:
            b = await body_json(request)
            lease_id, events = b["lease_id"], b["events"]
            if not isinstance(events, list) or len(events) > 200:
                raise ValueError
        except (ValueError, KeyError):
            return _err(400, "invalid body")
        if not await asyncio.to_thread(asks.holds, ask_id, lease_id):
            return _err(409, "no valid lease")
        for ev in events:
            kind = ev.get("type") if isinstance(ev, dict) else None
            if kind == "delta" and isinstance(ev.get("text"), str):
                await asyncio.to_thread(asks.append, ask_id, lease_id, ev["text"])
                publish(ask_id, {"type": "delta", "text": ev["text"]})
            elif kind == "done":
                used = [n for n in ev.get("used", []) if isinstance(n, int)]
                await asyncio.to_thread(asks.finish, ask_id, lease_id, used=used)
                publish(ask_id, {"type": "done", "not_found": False, "used": used,
                                 "timings": ev.get("timings") if isinstance(ev.get("timings"), dict) else {}})
            elif kind == "error":
                msg = str(ev.get("message", "error"))[:200]
                await asyncio.to_thread(asks.fail, ask_id, lease_id, msg)
                publish(ask_id, {"type": "error", "message": msg})
        await asyncio.to_thread(asks.renew, ask_id, lease_id)
        return {"ok": True}

    @router.get("/last-segment")
    async def last_segment(request: Request):
        """When the newest segment started: the Mac needs it to know if the iPhone app was reopened."""
        if (r := gate(request)) is not None:
            return r
        from omy import hubstats
        return {"start": await asyncio.to_thread(hubstats.last_segment_start)}

    return router
