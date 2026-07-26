"""Tests für core/jobs.py — JobRegistry (Refactor Phase 2).

Pytest-idiomatisch (test_*, kein t_*-Präfix) statt der hand-gerollten
Konvention der übrigen Testdateien: JobRegistry ist neuer Code ohne
HOME/Channel-Fixture-Bedarf (siehe tests/conftest.py Docstring).
"""
from __future__ import annotations

import threading

from core.jobs import JobRegistry


def test_get_returns_default_when_missing():
    reg = JobRegistry()
    assert reg.get("batch", ("c1", "v1")) is None
    assert reg.get("batch", ("c1", "v1"), default="x") == "x"


def test_set_and_get_roundtrip():
    reg = JobRegistry()
    reg.set("render", ("c1", "v1"), {"running": True, "stage": "clips"})
    assert reg.get("render", ("c1", "v1")) == {"running": True, "stage": "clips"}


def test_different_kinds_do_not_collide_on_same_key():
    reg = JobRegistry()
    key = ("c1", "v1")
    reg.set("render", key, {"stage": "render"})
    reg.set("batch", key, {"stage": "batch"})
    assert reg.get("render", key) == {"stage": "render"}
    assert reg.get("batch", key) == {"stage": "batch"}


def test_update_merges_into_existing_entry():
    reg = JobRegistry()
    reg.set("plan", ("c1", "v1"), {"running": True, "step": "analyze"})
    entry = reg.update("plan", ("c1", "v1"), step="chunk", done=False)
    assert entry == {"running": True, "step": "chunk", "done": False}
    assert reg.get("plan", ("c1", "v1")) == {"running": True, "step": "chunk", "done": False}


def test_update_creates_entry_when_missing():
    reg = JobRegistry()
    entry = reg.update("thumb", ("c1", "v1"), running=True)
    assert entry == {"running": True}
    assert reg.get("thumb", ("c1", "v1")) == {"running": True}


def test_delete_removes_entry_and_is_idempotent():
    reg = JobRegistry()
    reg.set("active_scene", ("c1", "v1", 3), "job-123")
    reg.delete("active_scene", ("c1", "v1", 3))
    assert reg.get("active_scene", ("c1", "v1", 3)) is None
    reg.delete("active_scene", ("c1", "v1", 3))  # no-op, must not raise


def test_contains():
    reg = JobRegistry()
    key = ("c1", "v1", 0)
    assert reg.contains("active_scene", key) is False
    reg.set("active_scene", key, "job-1")
    assert reg.contains("active_scene", key) is True


def test_items_and_keys_snapshot():
    reg = JobRegistry()
    reg.set("voice", ("c1", "v1"), {"running": True})
    reg.set("voice", ("c1", "v2"), {"running": False})
    assert set(reg.keys("voice")) == {("c1", "v1"), ("c1", "v2")}
    assert dict(reg.items("voice")) == {
        ("c1", "v1"): {"running": True},
        ("c1", "v2"): {"running": False},
    }


def test_items_on_unknown_kind_returns_empty():
    reg = JobRegistry()
    assert reg.items("nonexistent") == []
    assert reg.keys("nonexistent") == []


def test_locked_allows_cross_kind_atomic_check():
    # Repliziert das reale Muster aus dashboard.py: "wenn diese Szene laut
    # active_scene noch als running in JOBS markiert ist, nichts Neues starten."
    reg = JobRegistry()
    scene_key = ("c1", "v1", 5)
    reg.set("jobs", "job-abc", {"status": "running"})
    reg.set("active_scene", scene_key, "job-abc")
    with reg.locked() as store:
        existing_job_id = store.get("active_scene", {}).get(scene_key)
        already_running = bool(
            existing_job_id and store.get("jobs", {}).get(existing_job_id, {}).get("status") == "running"
        )
    assert already_running is True


def test_concurrent_updates_are_serialized():
    reg = JobRegistry()
    reg.set("batch", ("c1", "v1"), {"done": 0})
    iterations = 200

    def bump():
        for _ in range(iterations):
            with reg.locked() as store:
                entry = store["batch"][("c1", "v1")]
                entry["done"] += 1

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert reg.get("batch", ("c1", "v1"))["done"] == iterations * 4
