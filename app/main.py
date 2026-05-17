"""FastAPI backend.

Endpoints:
  GET  /                 -> serves the frontend (index.html)
  POST /api/scrape       -> kicks off a scrape job, returns {job_id}
  GET  /api/jobs/{id}/stream  -> Server-Sent Events: emits each lead as it's scraped, then a 'done' event
  GET  /api/jobs/{id}    -> snapshot of job state + collected results
  POST /api/jobs/{id}/cancel  -> stop the job
  GET  /api/jobs/{id}/export?fmt=csv|json -> download results
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import storage
from .scraper import GoogleMapsScraper

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "app" / "static"
# DATA_DIR can be overridden via env var so Railway can mount a persistent
# volume at /data without us baking in the local path.
DATA_DIR = Path(os.environ.get("DATA_DIR") or (ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "jobs.db"


# ---------- job state ----------

@dataclass
class Job:
    id: str
    keywords: list[str]
    locations: list[str]
    max_results: int
    fetch_emails: bool = True
    auto_grid: bool = True
    restrict_to_location: bool = True
    radius_km: float = 20.0  # fallback only, used when OSM has no bbox
    grid_size: Optional[int] = None
    zoom: int = 14
    tile_workers: int = 3   # how many tiles/contexts to scrape in parallel
    card_workers: int = 5   # how many detail pages to extract in parallel per tile
    status: str = "pending"  # pending | running | done | error | cancelled
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    results: list[dict] = field(default_factory=list)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None

    @property
    def keyword(self) -> str:
        return self.keywords[0] if self.keywords else ""

    @property
    def location(self) -> str:
        return self.locations[0] if self.locations else ""


JOBS: dict[str, Job] = {}


async def _run_scrape(job: Job) -> None:
    job.status = "running"
    storage.update_job(job.id, status="running")
    loop = asyncio.get_running_loop()
    last_db_flush = [time.time()]  # mutable cell for closure access

    def on_result(item: dict) -> None:
        job.results.append(item)
        loop.call_soon_threadsafe(job.queue.put_nowait, {"type": "lead", "data": item})
        # Throttle results_count writes to once every ~3 seconds. The dashboard
        # only needs a rough count while a job is running; the final exact value
        # lands in the `finally` block when the job terminates.
        now = time.time()
        if now - last_db_flush[0] > 3.0:
            last_db_flush[0] = now
            try:
                storage.update_job(job.id, results_count=len(job.results))
            except Exception:
                pass

    def on_status(msg: str) -> None:
        print(f"[scrape {job.id[:8]}] {msg}", flush=True)
        loop.call_soon_threadsafe(job.queue.put_nowait, {"type": "status", "data": msg})

    try:
        async with GoogleMapsScraper(headless=True, fetch_emails=job.fetch_emails) as s:
            await s.scrape(
                keyword=job.keywords,
                location=job.locations or [""],
                max_results=job.max_results,
                on_result=on_result,
                cancel_event=job.cancel_event,
                auto_grid=job.auto_grid,
                radius_km=job.radius_km,
                grid_size=job.grid_size,
                zoom=job.zoom,
                restrict_to_location=job.restrict_to_location,
                on_status=on_status,
                tile_workers=job.tile_workers,
                card_workers=job.card_workers,
            )
        job.status = "cancelled" if job.cancel_event.is_set() else "done"
    except Exception as e:  # surface to client
        job.status = "error"
        job.error = f"{type(e).__name__}: {e}"
        await job.queue.put({"type": "error", "data": job.error})
    finally:
        job.finished_at = time.time()
        # persist results to disk for export endpoints
        try:
            _persist_results(job)
        except Exception as e:
            print("[persist] failed:", e)
        # Push the final state to SQLite so the dashboard reflects it.
        try:
            paths = job_files.get(job.id, {})
            storage.update_job(
                job.id,
                status=job.status,
                error=job.error,
                finished_at=job.finished_at,
                results_count=len(job.results),
                json_path=paths.get("json"),
                csv_path=paths.get("csv"),
            )
        except Exception as e:
            print("[storage] final update failed:", e)
        await job.queue.put({"type": "done", "data": {"count": len(job.results), "status": job.status}})


def _persist_results(job: Job) -> None:
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(job.started_at))
    safe_kw = "".join(c for c in job.keyword if c.isalnum() or c in "-_ ").strip().replace(" ", "_") or "query"
    base = DATA_DIR / f"{ts}_{safe_kw}_{job.id[:8]}"
    json_path = base.with_suffix(".json")
    csv_path = base.with_suffix(".csv")
    with json_path.open("w", encoding="utf-8") as f:
        json.dump({
            "keywords": job.keywords,
            "locations": job.locations,
            "max_results": job.max_results,
            "results": job.results,
        }, f, indent=2, ensure_ascii=False)
    if job.results:
        keys = list(job.results[0].keys())
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for row in job.results:
                w.writerow({k: _csv_safe(v) for k, v in row.items()})
    job_files[job.id] = {"json": str(json_path), "csv": str(csv_path) if job.results else None}


def _csv_safe(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


job_files: dict[str, dict[str, Optional[str]]] = {}


# ---------- API models ----------

class ScrapeRequest(BaseModel):
    # Accept a single string OR a list. The frontend posts newline/comma-separated
    # text that we split before constructing this. Both fields tolerate both forms.
    keyword: str | list[str] = Field(..., description="one keyword or a list")
    location: str | list[str] = Field("", description="one location or a list, may be empty")
    max_results: int = Field(20, ge=1, le=10000)
    fetch_emails: bool = Field(True, description="visit each website to extract emails")
    auto_grid: bool = Field(True, description="auto-tile the search area to break past Google's per-query cap")
    restrict_to_location: bool = Field(True, description="discard places outside the typed city/region's bounding box")
    grid_size: Optional[int] = Field(None, ge=1, le=15, description="NxN grid; None = auto from max_results")
    zoom: int = Field(14, ge=10, le=17, description="map zoom for viewport tiles")
    # `radius_km` is now a fallback only: used when OSM can't return a bbox for the typed location.
    # Hard-capped to 25 km in the scraper regardless of what is sent.
    radius_km: float = Field(20.0, ge=0.5, le=25.0, description="fallback half-side of scan square in km, used when OSM has no bbox")
    tile_workers: int = Field(3, ge=1, le=8, description="number of grid tiles scraped in parallel")
    card_workers: int = Field(5, ge=1, le=10, description="number of detail pages extracted in parallel per tile")


# ---------- app ----------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open the SQLite database and run startup recovery: any job that was
    # `running` when the previous process died (Railway redeploy, OOM, etc.)
    # becomes `interrupted` so the dashboard tells the truth.
    storage.init_db(DB_PATH)
    flipped = storage.mark_running_as_interrupted()
    if flipped:
        print(f"[startup] flipped {flipped} stale running job(s) -> interrupted")
    # Best-effort: rebuild job_files from DB so /export still serves old jobs.
    for row in storage.list_jobs(limit=1000):
        if row.get("json_path") or row.get("csv_path"):
            job_files[row["id"]] = {"json": row.get("json_path"), "csv": row.get("csv_path")}
    yield


app = FastAPI(title="Google Maps Lead Scraper", lifespan=lifespan)


def _normalize_to_list(v: str | list[str]) -> list[str]:
    if isinstance(v, list):
        items = v
    else:
        # split on newline or comma; preserve internal commas in addresses by
        # preferring newline split when both are present.
        s = (v or "").strip()
        if "\n" in s:
            items = s.splitlines()
        elif ";" in s:
            items = s.split(";")
        else:
            items = [s]
    return [i.strip() for i in items if i and i.strip()]


@app.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    keywords = _normalize_to_list(req.keyword)
    locations = _normalize_to_list(req.location) or [""]
    if not keywords:
        raise HTTPException(400, "at least one keyword is required")
    job = Job(
        id=str(uuid.uuid4()),
        keywords=keywords,
        locations=locations,
        max_results=req.max_results,
        fetch_emails=req.fetch_emails,
        auto_grid=req.auto_grid,
        radius_km=req.radius_km,
        grid_size=req.grid_size,
        zoom=req.zoom,
        restrict_to_location=req.restrict_to_location,
        tile_workers=req.tile_workers,
        card_workers=req.card_workers,
    )
    JOBS[job.id] = job
    # Persist BEFORE kicking off the task so a crash mid-startup still leaves
    # a row the user can see on the dashboard (it'll be marked interrupted).
    try:
        storage.insert_job({
            "id": job.id,
            "keywords": job.keywords,
            "locations": job.locations,
            "max_results": job.max_results,
            "fetch_emails": job.fetch_emails,
            "auto_grid": job.auto_grid,
            "restrict_to_location": job.restrict_to_location,
            "radius_km": job.radius_km,
            "grid_size": job.grid_size,
            "zoom": job.zoom,
            "tile_workers": job.tile_workers,
            "card_workers": job.card_workers,
            "status": "pending",
            "started_at": job.started_at,
        })
    except Exception as e:
        print("[storage] insert_job failed:", e)
    job.task = asyncio.create_task(_run_scrape(job))
    return {
        "job_id": job.id,
        "keywords": keywords,
        "locations": locations,
        "max_results": req.max_results,
        "auto_grid": req.auto_grid,
        "restrict_to_location": req.restrict_to_location,
        "grid_size": req.grid_size,
        "zoom": req.zoom,
        "total_text_queries": len(keywords) * len(locations),
    }


@app.get("/api/jobs")
async def list_jobs(limit: int = 200):
    """Return all known jobs (live + persisted), newest first. Used by the
    dashboard so the user can pick up a job they kicked off in a prior session."""
    rows = storage.list_jobs(limit=limit)
    # Overlay live status from JOBS for any job still running in this process —
    # the DB only flushes results_count every ~3s, so live numbers are fresher.
    for row in rows:
        live = JOBS.get(row["id"])
        if live is not None:
            row["status"] = live.status
            row["results_count"] = len(live.results)
            row["error"] = live.error
    return {"jobs": rows}


@app.get("/api/jobs/{job_id}")
async def job_state(job_id: str):
    job = JOBS.get(job_id)
    if job is not None:
        return {
            "id": job.id,
            "keywords": job.keywords,
            "locations": job.locations,
            "max_results": job.max_results,
            "status": job.status,
            "error": job.error,
            "results_count": len(job.results),
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "results": job.results,
        }
    # Fall back to the persisted record (jobs from a prior server life).
    row = storage.get_job(job_id)
    if not row:
        raise HTTPException(404, "job not found")
    results = storage.read_results_from_disk(job_id)
    return {
        "id": row["id"],
        "keywords": row.get("keywords") or [],
        "locations": row.get("locations") or [],
        "max_results": row.get("max_results"),
        "status": row.get("status"),
        "error": row.get("error"),
        "results_count": row.get("results_count") or len(results),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "results": results,
    }


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    """Remove a job record and its on-disk files. If the job is still running we
    cancel it first so we don't orphan a Playwright browser."""
    live = JOBS.get(job_id)
    if live is not None:
        live.cancel_event.set()
        if live.task is not None and not live.task.done():
            # Don't await it — the scrape's finally block writes the final state
            # and the row will be gone anyway. The task will exit when its
            # cancel_event check trips on the next loop iteration.
            pass
        JOBS.pop(job_id, None)
    removed, files = storage.delete_job(job_id)
    job_files.pop(job_id, None)
    if not removed:
        raise HTTPException(404, "job not found")
    return {"ok": True, "removed_files": files}


@app.delete("/api/jobs")
async def delete_all_jobs(confirm: bool = False):
    """Bulk delete. Requires ?confirm=true to avoid accidental wipes from a
    curl typo. Cancels any live jobs first."""
    if not confirm:
        raise HTTPException(400, "pass ?confirm=true to delete all jobs")
    removed = 0
    for row in storage.list_jobs(limit=10_000):
        jid = row["id"]
        live = JOBS.get(jid)
        if live is not None:
            live.cancel_event.set()
            JOBS.pop(jid, None)
        ok, _ = storage.delete_job(jid)
        if ok:
            removed += 1
        job_files.pop(jid, None)
    return {"ok": True, "removed": removed}


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str, request: Request):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")

    async def gen():
        # replay any already-collected leads first (in case client connects late)
        for r in job.results:
            yield _sse("lead", r)
        # then stream new events
        while True:
            if await request.is_disconnected():
                break
            try:
                evt = await asyncio.wait_for(job.queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                if job.status in ("done", "error", "cancelled"):
                    break
                continue
            yield _sse(evt["type"], evt["data"])
            if evt["type"] == "done":
                break

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


def _sse(event: str, data) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job.cancel_event.set()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/export")
async def export_job(job_id: str, fmt: str = "json"):
    if fmt not in ("json", "csv"):
        raise HTTPException(400, "fmt must be 'json' or 'csv'")

    job = JOBS.get(job_id)

    # Prefer the on-disk persisted file when it exists. The export endpoint
    # serves both currently-running jobs (synth from memory) and historical
    # jobs from prior server lives (read from disk via DB-stored path).
    files = job_files.get(job_id, {})
    if not files:
        row = storage.get_job(job_id)
        if row:
            files = {"json": row.get("json_path"), "csv": row.get("csv_path")}

    if fmt == "json":
        path = files.get("json") if files else None
        if path and os.path.exists(path):
            return FileResponse(path, media_type="application/json", filename=os.path.basename(path))
        if job is not None:
            return JSONResponse({"keyword": job.keyword, "location": job.location, "results": job.results})
        if not storage.get_job(job_id):
            raise HTTPException(404, "job not found")
        return JSONResponse({"results": storage.read_results_from_disk(job_id)})

    # CSV
    path = files.get("csv") if files else None
    if path and os.path.exists(path):
        return FileResponse(path, media_type="text/csv", filename=os.path.basename(path))

    # Need to synthesise — gather results from memory or disk.
    if job is not None:
        results = job.results
    elif storage.get_job(job_id):
        results = storage.read_results_from_disk(job_id)
    else:
        raise HTTPException(404, "job not found")
    if not results:
        return StreamingResponse(io.StringIO(""), media_type="text/csv")
    keys = list(results[0].keys())
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=keys)
    w.writeheader()
    for r in results:
        w.writerow({k: _csv_safe(v) for k, v in r.items()})
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]),
                             media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="leads-{job_id[:8]}.csv"'})


# ---------- static frontend ----------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/dashboard")
async def dashboard():
    return FileResponse(str(STATIC_DIR / "dashboard.html"))


@app.get("/api/health")
async def health():
    return {"ok": True, "live_jobs": len(JOBS)}
