"""SQLite-backed persistence for scrape jobs.

Why this exists:
  When the server runs on Railway (or any host where the container can restart),
  the in-memory `JOBS` dict in main.py is lost. The user submits a 10k-lead job,
  closes their browser, and may come back hours later — or after a redeploy.
  We need their job's metadata and final results to survive both.

Storage layout:
  data/jobs.db                              — this SQLite file
  data/{ts}_{kw}_{shortid}.json             — full results JSON (one per job)
  data/{ts}_{kw}_{shortid}.csv              — flat CSV for download

The DB stores only metadata + file paths. The actual results live in the JSON/CSV
files written by main._persist_results(). This keeps DB row size small and avoids
duplicating large result payloads.

Status values: pending | running | done | error | cancelled | interrupted
  - `interrupted` is assigned at startup to any job that was still `running` when
    the previous server process died (Railway restart, OOM, deploy, etc.).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


# Single connection guarded by a lock. SQLite under WAL mode handles concurrent
# reads/writes, but to keep this simple and fully predictable we serialise all
# writes through a thread lock. Scrape throughput is browser-bound, not DB-bound,
# so the lock isn't on any hot path.
_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None
_DB_PATH: Optional[Path] = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    keywords TEXT NOT NULL,
    locations TEXT NOT NULL,
    max_results INTEGER NOT NULL,
    fetch_emails INTEGER NOT NULL DEFAULT 1,
    auto_grid INTEGER NOT NULL DEFAULT 1,
    restrict_to_location INTEGER NOT NULL DEFAULT 1,
    radius_km REAL,
    grid_size INTEGER,
    zoom INTEGER,
    tile_workers INTEGER,
    card_workers INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    results_count INTEGER NOT NULL DEFAULT 0,
    json_path TEXT,
    csv_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_started_at ON jobs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
"""


def init_db(db_path: Path) -> None:
    """Open the DB connection and ensure the schema exists. Call once on startup."""
    global _CONN, _DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _DB_PATH = db_path
    _CONN = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    _CONN.row_factory = sqlite3.Row
    # WAL gives us concurrent readers + non-blocking writers, which matters when
    # the dashboard polls /api/jobs while a scrape is updating its row.
    _CONN.execute("PRAGMA journal_mode=WAL;")
    _CONN.execute("PRAGMA synchronous=NORMAL;")
    _CONN.executescript(SCHEMA)


def _conn() -> sqlite3.Connection:
    if _CONN is None:
        raise RuntimeError("storage.init_db() was not called")
    return _CONN


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # JSON columns
    for k in ("keywords", "locations"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else []
        except Exception:
            d[k] = []
    # Cast bools
    for k in ("fetch_emails", "auto_grid", "restrict_to_location"):
        d[k] = bool(d.get(k))
    return d


def insert_job(job: dict) -> None:
    """Persist a newly-created job. `job` should carry every column we store
    except json_path / csv_path / finished_at / error (those land later)."""
    with _LOCK:
        _conn().execute(
            """
            INSERT INTO jobs (
                id, keywords, locations, max_results, fetch_emails, auto_grid,
                restrict_to_location, radius_km, grid_size, zoom,
                tile_workers, card_workers, status, started_at, results_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                job["id"],
                json.dumps(job.get("keywords") or []),
                json.dumps(job.get("locations") or []),
                int(job.get("max_results") or 0),
                1 if job.get("fetch_emails", True) else 0,
                1 if job.get("auto_grid", True) else 0,
                1 if job.get("restrict_to_location", True) else 0,
                float(job.get("radius_km") or 0.0) if job.get("radius_km") is not None else None,
                int(job["grid_size"]) if job.get("grid_size") else None,
                int(job.get("zoom") or 14),
                int(job.get("tile_workers") or 3),
                int(job.get("card_workers") or 5),
                job.get("status") or "pending",
                float(job.get("started_at") or time.time()),
            ),
        )


_UPDATABLE = {
    "status",
    "error",
    "finished_at",
    "results_count",
    "json_path",
    "csv_path",
}


def update_job(job_id: str, **fields: Any) -> None:
    """Patch any subset of `_UPDATABLE` columns on a job row. Unknown keys are
    ignored so callers can blindly forward state — keeps update sites simple."""
    cols = [(k, v) for k, v in fields.items() if k in _UPDATABLE]
    if not cols:
        return
    with _LOCK:
        set_clause = ", ".join(f"{k} = ?" for k, _ in cols)
        params = [v for _, v in cols] + [job_id]
        _conn().execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", params)


def get_job(job_id: str) -> Optional[dict]:
    cur = _conn().execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def list_jobs(limit: int = 200) -> list[dict]:
    cur = _conn().execute(
        "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?",
        (int(limit),),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def delete_job(job_id: str) -> tuple[bool, list[str]]:
    """Remove the row and best-effort delete the JSON/CSV files.

    Returns (removed_row, removed_files). Missing files are tolerated — they
    aren't an error condition (the job may never have produced any).
    """
    row = get_job(job_id)
    if not row:
        return False, []
    removed_files: list[str] = []
    for key in ("json_path", "csv_path"):
        p = row.get(key)
        if not p:
            continue
        try:
            if os.path.exists(p):
                os.remove(p)
                removed_files.append(p)
        except Exception:
            # A locked or read-only file shouldn't block deleting the DB row.
            # The dashboard will still show the job as gone.
            pass
    with _LOCK:
        _conn().execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    return True, removed_files


def mark_running_as_interrupted() -> int:
    """Called on server startup. Any job that was `running` (or `pending`) when
    the previous process died is now a zombie — mark it interrupted so the
    dashboard surfaces it correctly. Returns count flipped."""
    with _LOCK:
        cur = _conn().execute(
            "UPDATE jobs SET status = 'interrupted', finished_at = COALESCE(finished_at, ?) "
            "WHERE status IN ('running', 'pending')",
            (time.time(),),
        )
        return cur.rowcount or 0


def read_results_from_disk(job_id: str) -> list[dict]:
    """Load the persisted results JSON for `job_id`. Empty list if missing.

    Used by the export and detail endpoints when the job is no longer in the
    in-memory JOBS dict (i.e. completed before this server process started).
    """
    row = get_job(job_id)
    if not row or not row.get("json_path"):
        return []
    p = row["json_path"]
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return list(data.get("results") or [])
        if isinstance(data, list):
            return data
    except Exception:
        return []
    return []
