"""SQLite-Schicht für Shorts/Upload/Control — stdlib `sqlite3`, keine neue Abhängigkeit.

Ein geteilter Connection-Handle (`check_same_thread=False`, `journal_mode=WAL`),
Schreibzugriffe unter `WRITE_LOCK` — dieselbe Konvention wie die bestehenden
Job-Dicts in dashboard.py (`_BATCH_JOBS_LOCK`, `_RENDER_JOBS_LOCK`, …), nur auf
DB-Schreibvorgänge angewendet statt auf Dict-Mutationen.

`privacy_status` in `upload_queue` trägt eine harte `CHECK`-Constraint auf
`'private'` — das ist Absicht, nicht ein Tippfehler: dieses System darf laut
Produktprinzip NIEMALS ein Video direkt/öffentlich hochladen, jeder Upload läuft
über `status.publishAt` + `privacyStatus=private`. Die Constraint ist eine
zweite, unabhängige Absicherungsebene zusätzlich zur Code-Logik in
`youtube/upload.py` (Structural Constraint > Prosa-Regel — dieselbe Lehre wie
beim `concrete_entity`-Enum in der Bild-Pipeline).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "pipeline.db")

WRITE_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS youtube_oauth (
    cid TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at REAL NOT NULL,
    scope TEXT,
    obtained_at REAL NOT NULL,
    youtube_channel_id TEXT,
    youtube_channel_title TEXT
);

-- Nur für Upload — kein Research-Verbraucher (mehr) in diesem System.
CREATE TABLE IF NOT EXISTS quota_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    pacific_day TEXT NOT NULL,   -- 'YYYY-MM-DD', America/Los_Angeles — Googles echte Reset-Grenze
    caller TEXT NOT NULL DEFAULT 'upload',
    endpoint TEXT NOT NULL,
    units INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quota_day ON quota_usage(pacific_day);

CREATE TABLE IF NOT EXISTS upload_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cid TEXT NOT NULL,
    vid TEXT NOT NULL,
    render_target TEXT NOT NULL,           -- 'longform' | 'short_vertical'
    short_id TEXT,
    file_path TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    tags TEXT,                              -- json list
    category_id TEXT,
    privacy_status TEXT NOT NULL DEFAULT 'private' CHECK (privacy_status = 'private'),
    scheduled_at REAL NOT NULL,              -- entspricht 1:1 status.publishAt
    status TEXT NOT NULL DEFAULT 'queued',    -- queued|uploading|uploaded|failed|canceled
    youtube_video_id TEXT,
    resumable_upload_url TEXT,
    upload_offset_bytes INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON upload_queue(status, scheduled_at);
"""

_conn: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """Einmalig erzeugter, geteilter Connection-Handle. `init_db()` muss vorher
    einmal beim App-Start gelaufen sein (siehe dashboard.py main())."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.row_factory = sqlite3.Row
    return _conn


def backup_db(keep: int = 5) -> None:
    """Rotierendes Startup-Backup (`pipeline.db.bak.1` = neuestes). WAL-Mode ist
    bereits an -- das hier ist eine zweite, unabhängige Absicherungsebene gegen
    Write-Korruption bei Crash (Refactor Phase 0). No-Op beim allerersten Start,
    solange noch keine pipeline.db existiert. Muss vor get_connection()/init_db()
    laufen (dashboard.py main()), sonst würde die offene Connection mitkopiert."""
    if not os.path.exists(DB_PATH):
        return
    oldest = f"{DB_PATH}.bak.{keep}"
    if os.path.exists(oldest):
        os.remove(oldest)
    for i in range(keep - 1, 0, -1):
        src = f"{DB_PATH}.bak.{i}"
        if os.path.exists(src):
            os.replace(src, f"{DB_PATH}.bak.{i + 1}")
    shutil.copy2(DB_PATH, f"{DB_PATH}.bak.1")


def init_db() -> None:
    """Idempotent — CREATE TABLE IF NOT EXISTS überall. Einmaliger Aufruf beim
    App-Start (dashboard.py main()), vor den mount()-Aufrufen."""
    conn = get_connection()
    with WRITE_LOCK:
        conn.executescript(_SCHEMA)
        conn.commit()


# ── youtube_oauth ────────────────────────────────────────────────────────────────
def save_tokens(cid: str, access_token: str, refresh_token: str, expires_at: float,
                scope: str, youtube_channel_id: str | None = None,
                youtube_channel_title: str | None = None) -> None:
    """Erster Verbindungsaufbau (Authorization-Code-Tausch) -- immer BEIDE Tokens.
    Für einen reinen Access-Token-Refresh siehe update_access_token() unten (Google
    liefert bei einem Refresh meist KEIN neues refresh_token zurück)."""
    conn = get_connection()
    with WRITE_LOCK:
        conn.execute(
            """INSERT OR REPLACE INTO youtube_oauth
               (cid, access_token, refresh_token, expires_at, scope, obtained_at,
                youtube_channel_id, youtube_channel_title)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, access_token, refresh_token, expires_at, scope, time.time(),
             youtube_channel_id, youtube_channel_title),
        )
        conn.commit()


def update_access_token(cid: str, access_token: str, expires_at: float) -> None:
    """Nach einem Refresh -- lässt refresh_token/youtube_channel_* unangetastet."""
    conn = get_connection()
    with WRITE_LOCK:
        conn.execute(
            "UPDATE youtube_oauth SET access_token=?, expires_at=? WHERE cid=?",
            (access_token, expires_at, cid),
        )
        conn.commit()


def get_tokens(cid: str) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM youtube_oauth WHERE cid=?", (cid,)).fetchone()
    return dict(row) if row else None


def delete_tokens(cid: str) -> None:
    conn = get_connection()
    with WRITE_LOCK:
        conn.execute("DELETE FROM youtube_oauth WHERE cid=?", (cid,))
        conn.commit()


# ── quota_usage ──────────────────────────────────────────────────────────────────
def _pacific_day(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts, tz=ZoneInfo("America/Los_Angeles")) if ts is not None \
        else datetime.now(tz=ZoneInfo("America/Los_Angeles"))
    return dt.strftime("%Y-%m-%d")


def record_usage(units: int, endpoint: str, caller: str = "upload") -> None:
    conn = get_connection()
    now = time.time()
    with WRITE_LOCK:
        conn.execute(
            "INSERT INTO quota_usage (ts, pacific_day, caller, endpoint, units) VALUES (?, ?, ?, ?, ?)",
            (now, _pacific_day(now), caller, endpoint, units),
        )
        conn.commit()


def get_uploads_today() -> int:
    """Anzahl videos.insert-Aufrufe am heutigen Pacific-Tag -- die bindende Grenze
    ist die separate 100-Uploads/Tag-Zuteilung, nicht die allgemeine Punktekosten-Regel
    (siehe Plan, Abschnitt Quota)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM quota_usage WHERE pacific_day=? AND endpoint='videos.insert'",
        (_pacific_day(),),
    ).fetchone()
    return row["n"] if row else 0


# ── upload_queue ─────────────────────────────────────────────────────────────────
def queue_add(cid: str, vid: str, render_target: str, file_path: str, title: str,
              scheduled_at: float, short_id: str | None = None, description: str = "",
              tags: list | None = None, category_id: str | None = None) -> int | None:
    conn = get_connection()
    now = time.time()
    with WRITE_LOCK:
        cur = conn.execute(
            """INSERT INTO upload_queue
               (cid, vid, render_target, short_id, file_path, title, description, tags,
                category_id, scheduled_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, vid, render_target, short_id, file_path, title, description,
             json.dumps(tags or []), category_id, scheduled_at, now, now),
        )
        conn.commit()
        return cur.lastrowid


def queue_list(status: str | None = None) -> list[dict]:
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM upload_queue WHERE status=? ORDER BY scheduled_at", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM upload_queue ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def queue_get(queue_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM upload_queue WHERE id=?", (queue_id,)).fetchone()
    return dict(row) if row else None


def queue_update(queue_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = get_connection()
    with WRITE_LOCK:
        conn.execute(f"UPDATE upload_queue SET {cols} WHERE id=?", (*fields.values(), queue_id))
        conn.commit()


def queue_cancel(queue_id: int) -> None:
    queue_update(queue_id, status="canceled")


def queue_earliest_scheduled_at(cid: str, vid: str) -> float | None:
    """Frühester scheduled_at über ALLE Warteschlangen-Einträge dieses Videos (Longform
    + jeder Short), egal ob schon hochgeladen oder noch wartend -- nur canceled/failed
    ausgeschlossen (die zählen nicht als "hat den Tag schon festgelegt"). Grundlage für
    next_publish_slot() in youtube/upload.py: wer für ein Video zuerst gequeued wird,
    legt den Kalendertag für alle folgenden fest, unabhängig von der Reihenfolge."""
    conn = get_connection()
    row = conn.execute(
        "SELECT MIN(scheduled_at) AS m FROM upload_queue "
        "WHERE cid=? AND vid=? AND status NOT IN ('canceled','failed')",
        (cid, vid),
    ).fetchone()
    return row["m"] if row and row["m"] is not None else None


def queue_pending() -> list[dict]:
    """Einträge, die der Upload-Worker (youtube/upload.py) aufgreifen soll: queued
    (noch nie gestartet) oder uploading (Prozess starb mitten im Resumable-Upload,
    siehe Risiko-Abschnitt im Plan). scheduled_at ist NUR status.publishAt bei
    YouTube, keine Startzeit für den Upload-VORGANG selbst -- der darf sofort
    beginnen, unabhängig davon wie weit scheduled_at in der Zukunft liegt."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM upload_queue WHERE status IN ('queued','uploading') ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]
