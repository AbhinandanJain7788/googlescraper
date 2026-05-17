"""Quick correctness check for app/storage.py — DB CRUD + recovery."""
import os
import tempfile
import time
import uuid
from pathlib import Path

from app import storage


def main():
    tmp = Path(tempfile.mkdtemp(prefix="gmscraper-"))
    db = tmp / "jobs.db"
    print(f"[test] DB at {db}")

    storage.init_db(db)

    # 1) Insert
    jid = str(uuid.uuid4())
    storage.insert_job({
        "id": jid,
        "keywords": ["coffee shops"],
        "locations": ["Manhattan, NY"],
        "max_results": 50,
        "fetch_emails": True,
        "auto_grid": True,
        "restrict_to_location": True,
        "radius_km": 20.0,
        "grid_size": 2,
        "zoom": 14,
        "tile_workers": 3,
        "card_workers": 5,
        "status": "pending",
        "started_at": time.time(),
    })

    row = storage.get_job(jid)
    assert row is not None, "get_job after insert returned None"
    assert row["status"] == "pending", f"status={row['status']}"
    assert row["keywords"] == ["coffee shops"], f"keywords={row['keywords']}"
    assert row["locations"] == ["Manhattan, NY"], f"locations={row['locations']}"
    print("[test] insert + get OK")

    # 2) Update status across lifecycle
    storage.update_job(jid, status="running")
    assert storage.get_job(jid)["status"] == "running"

    storage.update_job(jid, results_count=27)
    assert storage.get_job(jid)["results_count"] == 27

    fake_json = tmp / "fake.json"
    fake_json.write_text('{"results": [{"title": "A"}, {"title": "B"}]}', encoding="utf-8")
    storage.update_job(jid, status="done", finished_at=time.time(),
                       json_path=str(fake_json), csv_path=None,
                       results_count=2)
    row = storage.get_job(jid)
    assert row["status"] == "done"
    assert row["finished_at"] is not None
    assert row["json_path"] == str(fake_json)
    print("[test] update lifecycle OK")

    # 3) Read results from disk
    results = storage.read_results_from_disk(jid)
    assert len(results) == 2, f"expected 2 results, got {len(results)}"
    assert results[0]["title"] == "A"
    print("[test] read_results_from_disk OK")

    # 4) List
    jobs = storage.list_jobs()
    assert any(j["id"] == jid for j in jobs)
    print(f"[test] list_jobs returned {len(jobs)} job(s)")

    # 5) Insert a second job, mark as running, then test recovery
    jid2 = str(uuid.uuid4())
    storage.insert_job({
        "id": jid2,
        "keywords": ["restaurants"],
        "locations": ["Brooklyn, NY"],
        "max_results": 100,
        "status": "running",
        "started_at": time.time(),
    })
    flipped = storage.mark_running_as_interrupted()
    assert flipped >= 1, f"expected at least 1 flipped, got {flipped}"
    assert storage.get_job(jid2)["status"] == "interrupted"
    assert storage.get_job(jid)["status"] == "done"  # untouched (was already done)
    print(f"[test] mark_running_as_interrupted flipped {flipped} job(s) OK")

    # 6) Delete the first job + verify its file is gone
    removed, files = storage.delete_job(jid)
    assert removed
    assert str(fake_json) in files, f"expected file in removed list, got {files}"
    assert not fake_json.exists(), "file should be gone after delete"
    assert storage.get_job(jid) is None
    print("[test] delete_job OK")

    # 7) Cleanup
    storage.delete_job(jid2)
    print("\n[test] ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
