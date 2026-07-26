"""core/jobs.py — JobRegistry: ein thread-sicherer Ersatz für die 8 einzelnen
(dict + threading.Lock)-Paare in dashboard.py (JOBS, BATCH_JOBS, PLAN_JOBS,
THUMB_JOBS, RENDER_JOBS, VOICE_JOBS, PRODUCE_JOBS, ACTIVE_SCENE_JOBS).

EIN gemeinsames Lock statt 8 getrennter ist bewusst, kein Kompromiss: mehrere
bestehende Call-Sites prüfen/ändern Einträge aus ZWEI verschiedenen Job-Kinds
atomar in derselben kritischen Sektion -- z.B. hält dashboard.py's
`_ACTIVE_SCENE_JOBS_LOCK` beim Start eines Szenen-Jobs sowohl den
ACTIVE_SCENE_JOBS-Dedup-Eintrag als auch den zugehörigen JOBS[job_id]["status"]
gemeinsam konsistent. Mit 8 getrennten Locks wäre das nicht mehr ausdrückbar,
ohne Aufrufer von zwei Locks gleichzeitig abhängig zu machen (Deadlock-Risiko
bei falscher Erwerbsreihenfolge). Für das Traffic-Volumen eines lokalen
Single-User-Tools ist ein Lock für den gesamten Job-State performant genug --
die kritischen Sektionen sind Mikrosekunden (dict-Operationen), nie I/O.

Migration der 8 Dicts in dashboard.py auf diese Registry passiert schrittweise,
Kind für Kind (siehe Refactoring-Plan Phase 2) -- dieses Modul allein ändert
noch kein Aufrufer-Verhalten.
"""
from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any


class JobRegistry:
    """Key-Value-Store für Job-State, partitioniert nach `kind` (z.B. "batch",
    "render", "active_scene"). Innerhalb eines kind ist der Key beliebig hashbar
    (job_id-String, (cid, vid)-Tupel, (cid, vid, scene_i)-Tupel, ...)."""

    def __init__(self) -> None:
        self._store: dict[str, dict[Any, Any]] = {}
        self._lock = threading.Lock()

    def get(self, kind: str, key: Any, default: Any = None) -> Any:
        with self._lock:
            return self._store.get(kind, {}).get(key, default)

    def set(self, kind: str, key: Any, value: Any) -> None:
        with self._lock:
            self._store.setdefault(kind, {})[key] = value

    def update(self, kind: str, key: Any, **fields: Any) -> dict[Any, Any]:
        """Merged `fields` in den bestehenden Eintrag (legt ihn an, falls er fehlt).
        Gibt den aktualisierten Eintrag zurück."""
        with self._lock:
            bucket = self._store.setdefault(kind, {})
            entry: dict[Any, Any] = bucket.setdefault(key, {})
            entry.update(fields)
            return entry

    def delete(self, kind: str, key: Any) -> None:
        with self._lock:
            self._store.get(kind, {}).pop(key, None)

    def contains(self, kind: str, key: Any) -> bool:
        with self._lock:
            return key in self._store.get(kind, {})

    def items(self, kind: str) -> list[tuple[Any, Any]]:
        with self._lock:
            return list(self._store.get(kind, {}).items())

    def keys(self, kind: str) -> list[Any]:
        with self._lock:
            return list(self._store.get(kind, {}).keys())

    @contextmanager
    def locked(self) -> Generator[dict[str, dict[Any, Any]]]:
        """Escape-Hatch für Call-Sites, die mehrere Operationen -- auch über
        verschiedene `kind`s hinweg -- atomar zusammen brauchen (z.B. "wenn Szene X
        noch als running markiert ist, nicht nochmal starten"). Gibt direkten
        Zugriff auf die interne {kind: {key: value}}-Struktur innerhalb des Locks;
        Aufrufer sind selbst dafür verantwortlich, sie nicht widersprüchlich zu
        mutieren (gleiche Verantwortung wie bei den bisherigen `with _X_LOCK:`-
        Blöcken in dashboard.py)."""
        with self._lock:
            yield self._store
