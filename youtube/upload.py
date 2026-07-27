"""youtube/upload.py — YouTube Data API v3 Resumable-Upload-Protokoll (handgebaut,
stdlib urllib) + Retry/Backoff + die harte assert_schedulable()-Absicherung.

Alle unten verwendeten Protokoll-Details sind gegen die offizielle Doku verifiziert
(developers.google.com/youtube/v3/guides/using_resumable_upload_protocol,
videos-Ressourcen-Referenz) -- siehe Plan, Abschnitt "Risiken":
- Vollständigkeits-Statuscode ist 201 Created (Status-Abfrage), nicht 200.
- 308 Resume Incomplete + Range-Header zeigt den TATSÄCHLICH empfangenen Bereich --
  das ist die Wahrheit, nicht der lokale upload_offset_bytes-Wert.
- 404 Not Found bei der Status-Abfrage heißt: Session abgelaufen, neue Session bei
  Byte 0 beginnen -- kein Absturz, kein unerwarteter Fehler.
- Init-Request braucht X-Upload-Content-Length/-Type zusätzlich zum Content-Type
  auf dem Metadaten-Body.
- Retriable: 500/502/503/504, exponentielles Backoff + Jitter, MAX_RETRIES=10
  (aus dem offiziellen Python-Referenzskript).
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import store.db as db
from youtube import quota
from youtube.oauth import get_valid_access_token

UPLOAD_INIT_URL = ("https://www.googleapis.com/upload/youtube/v3/videos"
                    "?uploadType=resumable&part=snippet,status")

RETRIABLE_STATUS_CODES = (500, 502, 503, 504)
MAX_RETRIES = 10

# Sicherheitsabstand gegen Uhr-/Zeitzonen-Abweichungen -- YouTube veröffentlicht ein
# Video SOFORT, wenn publishAt in der Vergangenheit liegt (offizielle Doku, wörtlich
# verifiziert). Zweite, unabhängige Absicherungsebene neben der DB-CHECK-Constraint
# auf privacy_status (dieselbe Lehre: Structural Constraint > Prosa-Regel).
MIN_SCHEDULE_LEAD_SEC = 15 * 60

# User-Vorgabe: feste, kanal-/videounabhängige Werte für JEDEN Upload (Longform, Klimax-
# Short, alle Parts) -- kein Kids-Content, aber auch nicht 18+, "einfach normal".
# Per WebFetch gegen die offizielle videos-Ressourcen-Doku verifiziert: das SCHREIBBARE
# Feld ist status.selfDeclaredMadeForKids (status.madeForKids ist read-only, YouTubes
# eigene Einschätzung -- Verwechslungsgefahr).
DEFAULT_MADE_FOR_KIDS = False
DEFAULT_LANGUAGE = "en"  # nur noch Fallback, siehe _video_language() -- war vorher hart
# User-Vorgabe Juli 2026: status.containsSyntheticMedia explizit auf true setzen ("Made
# with AI"-Offenlegung, Feld seit Okt. 2024 in der API, Pflicht-Durchsetzung seit Jan.
# 2026). Per WebFetch gegen die offizielle Policy-Seite geprüft: stilisierte/animierte
# Inhalte sind von der PFLICHT eigentlich ausgenommen (nur "realistisch wirkender"
# Alter/Synthetic Content muss offengelegt werden) -- der Kanal ist trotzdem zu 100%
# KI-generiert (Bilder + Stimme), daher bewusst freiwillig transparent statt sich auf
# die Ausnahme zu verlassen.
DEFAULT_CONTAINS_SYNTHETIC_MEDIA = True


def _video_language(entry: dict) -> str:
    """Sprache DIESES Videos (für defaultLanguage/defaultAudioLanguage), aus dem
    gespeicherten script.json des Longform-Videos -- Shorts/Parts teilen sich Sprache
    und Stimme mit ihrem Longform, haben kein eigenes script.json.

    Juli 2026 (User-Report, gleiche Bug-Klasse wie die Hook-Shorts-Sprache): vorher
    ein hart kodiertes DEFAULT_LANGUAGE = "en" für JEDEN Upload, unabhängig von der
    tatsächlichen Sprache des Videos. Best-effort: bei jedem Fehler (kein Skript,
    Kanal nicht ladbar) Fallback auf DEFAULT_LANGUAGE statt den Upload zu blockieren."""
    try:
        import dashboard  # lazy, siehe _add_to_playlist-Konvention weiter unten
        return (dashboard.load_v_script(entry["cid"], entry["vid"]) or {}).get(
            "language", DEFAULT_LANGUAGE)
    except Exception:
        return DEFAULT_LANGUAGE


def next_publish_slot(cid: str, vid: str, day_offset: int, hour: int = 20) -> float:
    """Timestamp für 'Anker-Tag + day_offset Tage, HH:00 Uhr lokale Zeit'.

    Anker-Tag = Datum des FRÜHESTEN bereits vorhandenen Warteschlangen-Eintrags für
    dieses Video (Longform ODER irgendein Short, siehe queue_earliest_scheduled_at) --
    Longform wird manuell gequeued (⑦), Shorts automatisch beim Bauen (⑥/⑦), die
    Reihenfolge zwischen beiden ist nicht festgelegt. Wer für ein Video zuerst gequeued
    wird, legt den Kalendertag für den Rest fest: Longform ruft dies mit day_offset=0
    auf, Hook-Short n (1-indiziert) mit day_offset=n-1 -- Short 1 landet dadurch immer
    auf demselben Tag wie das Longform.

    Ohne bestehenden Eintrag ist der Anker 'heute', außer HH:00 heute läge bereits zu
    knapp an/vor 'jetzt' (MIN_SCHEDULE_LEAD_SEC) -- dann 'morgen', damit day_offset=0
    (Longform) niemals von assert_schedulable() abgelehnt wird."""
    earliest = db.queue_earliest_scheduled_at(cid, vid)
    if earliest is not None:
        anchor_date = datetime.fromtimestamp(earliest).date()
    else:
        now = datetime.now()
        today_slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if today_slot.timestamp() < time.time() + MIN_SCHEDULE_LEAD_SEC:
            anchor_date = (now + timedelta(days=1)).date()
        else:
            anchor_date = now.date()
    target = datetime(anchor_date.year, anchor_date.month, anchor_date.day, hour, 0, 0)
    return (target + timedelta(days=day_offset)).timestamp()


def assert_schedulable(publish_at: float) -> None:
    if publish_at < time.time() + MIN_SCHEDULE_LEAD_SEC:
        raise ValueError(
            "scheduled_at zu nah an/vor 'jetzt' — würde YouTube dazu bringen, das Video "
            f"SOFORT zu veröffentlichen (weniger als {MIN_SCHEDULE_LEAD_SEC // 60} Minuten "
            "Vorlauf). Upload wird verweigert statt mit einem vergangenheitsnahen Wert "
            "abgeschickt."
        )


def _backoff_sleep(retry: int) -> None:
    # Exponentiell + Jitter, 1:1 aus dem offiziellen Google-Referenzskript.
    time.sleep(random.random() * (2 ** retry))


def _iso8601(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _init_session(access_token: str, entry: dict) -> str:
    size = os.path.getsize(entry["file_path"])
    lang = _video_language(entry)
    body = {
        "snippet": {
            "title": entry["title"],
            "description": entry.get("description") or "",
            "tags": json.loads(entry.get("tags") or "[]"),
            "categoryId": entry.get("category_id") or None,
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            # Harte Invariante, kein Codepfad in diesem System weicht davon ab --
            # dieselbe Spalte trägt zusätzlich die CHECK-Constraint in store/db.py.
            "privacyStatus": "private",
            "publishAt": _iso8601(entry["scheduled_at"]),
            "selfDeclaredMadeForKids": DEFAULT_MADE_FOR_KIDS,
            "containsSyntheticMedia": DEFAULT_CONTAINS_SYNTHETIC_MEDIA,
        },
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(UPLOAD_INIT_URL, data=data, method="POST", headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(size),
        "X-Upload-Content-Type": "video/mp4",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        location = r.headers.get("Location")
    if not location:
        raise RuntimeError("Resumable-Session-Init lieferte keinen Location-Header zurück.")
    return location


def _check_status(location: str, total: int) -> tuple:
    """Leere PUT mit Content-Range: bytes */{total} -- offizielles Protokoll für 'wie
    viel wurde bisher wirklich empfangen'. Rückgabe: (done, video_id|None, resume_from)."""
    req = urllib.request.Request(location, data=b"", method="PUT", headers={
        "Content-Range": f"bytes */{total}",
        "Content-Length": "0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r) if r.status == 201 else {}
            return True, resp.get("id"), total
    except urllib.error.HTTPError as e:
        if e.code == 308:
            rng = e.headers.get("Range")  # z.B. "bytes=0-12345"
            resume_from = int(rng.split("-")[1]) + 1 if rng else 0
            return False, None, resume_from
        raise  # inkl. 404 (Session abgelaufen) -- Aufrufer entscheidet, wie zu reagieren


def _put_whole_file(location: str, file_path: str, start: int, total: int) -> str:
    with open(file_path, "rb") as f:
        f.seek(start)
        chunk = f.read()
    req = urllib.request.Request(location, data=chunk, method="PUT", headers={
        "Content-Length": str(len(chunk)),
        "Content-Range": f"bytes {start}-{total - 1}/{total}",
    })
    with urllib.request.urlopen(req, timeout=1800) as r:
        resp = json.load(r)
        return resp["id"]


def _do_upload(entry: dict) -> str:
    """Ein vollständiger Upload-Versuch für `entry` (ein upload_queue-Row-Dict) --
    Resume falls eine Session schon existiert, sonst neue Session; Single-Shot-PUT
    (ganze Datei in einer Anfrage -- bei den hier üblichen Dateigrößen unkritisch,
    das 256-KB-Vielfaches-Erfordernis gilt laut offizieller Doku nur für mehrteiliges
    Hochladen); Retry auf 500/502/503/504 mit Backoff. Gibt die echte
    youtube_video_id zurück."""
    access_token = get_valid_access_token(entry["cid"])
    if not access_token:
        raise RuntimeError(f"Kanal '{entry['cid']}' ist nicht per OAuth verbunden.")

    total = os.path.getsize(entry["file_path"])
    location = entry.get("resumable_upload_url")
    start = 0

    if location:
        # Fortsetzung nach einem früheren Absturz -- IMMER zuerst den echten Stand
        # abfragen; die Range-Antwort von Google ist die Wahrheit, nicht der lokal
        # gespeicherte upload_offset_bytes-Wert (siehe Plan-Risiko-Abschnitt).
        try:
            done, video_id, resume_from = _check_status(location, total)
            if done:
                return video_id
            start = resume_from
        except urllib.error.HTTPError as e:
            if e.code == 404:
                location = None  # Session abgelaufen -- unten neu initialisieren
            else:
                raise

    if not location:
        location = _init_session(access_token, entry)
        db.queue_update(entry["id"], resumable_upload_url=location, upload_offset_bytes=0,
                         status="uploading")
        start = 0

    last_err: Exception | None = None
    for retry in range(MAX_RETRIES):
        try:
            video_id = _put_whole_file(location, entry["file_path"], start, total)
            db.record_usage(1, "videos.insert", caller="upload")
            return video_id
        except urllib.error.HTTPError as e:
            if e.code in RETRIABLE_STATUS_CODES:
                last_err = e
                _backoff_sleep(retry)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            _backoff_sleep(retry)
            continue
    raise RuntimeError(f"Upload nach {MAX_RETRIES} Versuchen fehlgeschlagen: {last_err}")


PLAYLIST_INSERT_URL = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"


def _add_to_playlist(entry: dict, video_id: str) -> None:
    """Best-effort: fügt das frisch hochgeladene Video der pro Kanal hinterlegten
    Playlist hinzu (channels/<cid>/youtube_playlist_id.txt, siehe dashboard.py:
    ch_youtube_playlist_id -- leer/fehlend = keine Zuordnung, kein Fehler). Gilt
    identisch für Longform und alle Shorts-Varianten (derselbe entry["cid"]).
    Ein Fehlschlag hier (z.B. ungültige Playlist-ID) darf den bereits erfolgreichen
    Video-Upload NICHT rückgängig machen oder als Fehler in der Queue erscheinen --
    nur loggen, status bleibt 'uploaded'."""
    import dashboard  # lazy, siehe youtube/metadata.py-Konvention
    try:
        playlist_id = open(dashboard.ch_youtube_playlist_id(entry["cid"])).read().strip()
    except OSError:
        return
    if not playlist_id:
        return
    try:
        access_token = get_valid_access_token(entry["cid"])
        body = {"snippet": {"playlistId": playlist_id,
                             "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
        req = urllib.request.Request(PLAYLIST_INSERT_URL, data=json.dumps(body).encode(),
                                      method="POST", headers={
                                          "Authorization": f"Bearer {access_token}",
                                          "Content-Type": "application/json; charset=UTF-8",
                                      })
        with urllib.request.urlopen(req, timeout=15):
            pass
        db.record_usage(50, "playlistItems.insert", caller="upload")
        print(f"  [Upload] Queue #{entry['id']}: zu Playlist {playlist_id} hinzugefügt.",
              flush=True)
    except Exception as e:
        print(f"  [Upload] Queue #{entry['id']}: Playlist-Zuordnung fehlgeschlagen "
              f"(Upload selbst war erfolgreich): {e}", flush=True)


THUMBNAIL_SET_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024  # harte API-Grenze, per WebFetch verifiziert
CAPTIONS_INSERT_URL = ("https://www.googleapis.com/upload/youtube/v3/captions"
                        "?uploadType=multipart&part=snippet")


def _set_thumbnail(entry: dict, video_id: str) -> None:
    """Best-effort: lädt das bereits generierte thumbnail.jpg (siehe
    dashboard._thumbnail_generate_worker) als Custom-Thumbnail hoch -- vorher ging das
    Video ohne jedes eigene Thumbnail raus (nur YouTubes Auto-Frame). Rohbytes, kein
    JSON/Multipart (per WebFetch gegen die offizielle thumbnails.set-Doku verifiziert).
    Gilt für jeden render_target, für den eine thumbnail.jpg existiert -- Shorts haben
    aktuell keine eigene, also läuft das faktisch nur für 'longform'."""
    import dashboard  # lazy, siehe _add_to_playlist-Konvention
    thumb_path = os.path.join(dashboard.v_out(entry["cid"], entry["vid"]), "thumbnail.jpg")
    if not os.path.exists(thumb_path):
        return
    size = os.path.getsize(thumb_path)
    if size > THUMBNAIL_MAX_BYTES:
        print(f"  [Upload] Queue #{entry['id']}: thumbnail.jpg zu groß ({size} Bytes > "
              f"{THUMBNAIL_MAX_BYTES}) -- übersprungen.", flush=True)
        return
    try:
        access_token = get_valid_access_token(entry["cid"])
        with open(thumb_path, "rb") as f:
            data = f.read()
        req = urllib.request.Request(
            f"{THUMBNAIL_SET_URL}?videoId={video_id}", data=data, method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "image/jpeg"})
        with urllib.request.urlopen(req, timeout=30):
            pass
        db.record_usage(50, "thumbnails.set", caller="upload")
        print(f"  [Upload] Queue #{entry['id']}: Thumbnail gesetzt.", flush=True)
    except Exception as e:
        print(f"  [Upload] Queue #{entry['id']}: Thumbnail-Upload fehlgeschlagen "
              f"(Video-Upload selbst war erfolgreich): {e}", flush=True)


def _words_for_entry(entry: dict) -> list | None:
    """Lädt die wortgenauen Timings (ElevenLabs voiceover_word_timestamps) für diesen
    Queue-Eintrag -- longform liest uploads/audio_meta.json, jede Shorts-Variante
    uploads/{render_target}_audio_meta.json (gleiche Namenskonvention wie
    shorts/api.py:_hooks_worker). Gibt None zurück, wenn keine wortgenauen Timings
    vorliegen (z.B. ein über Whisper statt ElevenLabs erzeugtes Video) -- Aufrufer
    überspringt den Untertitel-Upload dann still, kein Fehler."""
    import dashboard  # lazy, siehe _add_to_playlist-Konvention
    uploads_dir = dashboard.v_uploads(entry["cid"], entry["vid"])
    fname = "audio_meta.json" if entry["render_target"] == "longform" else f"{entry['render_target']}_audio_meta.json"
    try:
        meta = json.load(open(os.path.join(uploads_dir, fname)))
    except Exception:
        return None
    return meta.get("voiceover_word_timestamps") or None


def _upload_captions(entry: dict, video_id: str) -> None:
    """Best-effort: baut aus den bereits vorhandenen wortgenauen Timings eine echte
    SRT-Untertitelspur (youtube.metadata.build_srt) und lädt sie hoch -- getrennt von
    den im Video eingebrannten 1-Wort-Captions.

    WICHTIG: braucht den Scope youtube.force-ssl (siehe SCOPES in youtube/oauth.py).
    Ein Kanal, der VOR dieser Erweiterung schon verbunden war, muss einmalig über
    Control neu verbunden werden -- ein bestehender Refresh-Token bekommt einen neu
    hinzugefügten Scope nicht automatisch (Google-OAuth-Verhalten, kein Bug hier)."""
    from youtube.metadata import build_srt
    words = _words_for_entry(entry)
    if not words:
        return
    srt = build_srt(words)
    if not srt.strip():
        return
    try:
        access_token = get_valid_access_token(entry["cid"])
        lang = _video_language(entry)
        meta_body = json.dumps({"snippet": {
            "videoId": video_id, "language": lang, "name": "", "isDraft": False,
        }}).encode("utf-8")
        boundary = "yt_caption_upload_boundary"
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + meta_body + (
            f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + srt.encode("utf-8") + f"\r\n--{boundary}--".encode("utf-8")
        req = urllib.request.Request(
            CAPTIONS_INSERT_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": f"multipart/related; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30):
            pass
        db.record_usage(400, "captions.insert", caller="upload")
        print(f"  [Upload] Queue #{entry['id']}: Untertitel hochgeladen.", flush=True)
    except Exception as e:
        print(f"  [Upload] Queue #{entry['id']}: Untertitel-Upload fehlgeschlagen "
              f"(Video-Upload selbst war erfolgreich): {e}", flush=True)


VIDEOS_UPDATE_URL = "https://www.googleapis.com/youtube/v3/videos?part=snippet,status"


def _update_video_language(entry: dict, video_id: str) -> None:
    """Setzt defaultLanguage/defaultAudioLanguage + status.containsSyntheticMedia
    NACHTRÄGLICH für ein bereits hochgeladenes Video (Backfill, siehe
    scripts/backfill_youtube_metadata.py) -- nur für schon-live Videos nötig,
    _init_session() setzt alles bei JEDEM neuen Upload direkt mit.

    videos.update verlangt laut Doku das KOMPLETTE Objekt für jeden Teil, den man mit
    `part` angibt -- kein Partial-Patch. Ein Request mit nur defaultLanguage würde
    Titel/Description/Tags/Kategorie also mit leeren Werten überschreiben, einer mit
    nur containsSyntheticMedia würde privacyStatus/publishAt zurücksetzen. Deshalb
    werden snippet UND status hier komplett aus unseren eigenen, beim Queuen
    gespeicherten Werten rekonstruiert (title/description/tags/category_id,
    scheduled_at) statt aus einem zusätzlichen Live-Read von YouTube -- ein Video-Read
    würde selbst wieder Quota kosten und könnte theoretisch abweichen, wenn jemand
    manuell auf YouTube editiert hat; für private, automatisiert hochgeladene Videos
    ist unsere eigene Queue die Wahrheit."""
    lang = _video_language(entry)
    body = {
        "id": video_id,
        "snippet": {
            "title": entry["title"],
            "description": entry.get("description") or "",
            "tags": json.loads(entry.get("tags") or "[]"),
            "categoryId": entry.get("category_id") or None,
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": _iso8601(entry["scheduled_at"]),
            "selfDeclaredMadeForKids": DEFAULT_MADE_FOR_KIDS,
            "containsSyntheticMedia": DEFAULT_CONTAINS_SYNTHETIC_MEDIA,
        },
    }
    try:
        access_token = get_valid_access_token(entry["cid"])
        req = urllib.request.Request(
            VIDEOS_UPDATE_URL, data=json.dumps(body).encode(), method="PUT",
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json; charset=UTF-8"})
        with urllib.request.urlopen(req, timeout=30):
            pass
        db.record_usage(50, "videos.update", caller="backfill")
        print(f"  [Upload] Queue #{entry['id']}: Sprache nachträglich gesetzt ({lang}).", flush=True)
    except Exception as e:
        print(f"  [Upload] Queue #{entry['id']}: Sprach-Update fehlgeschlagen: {e}", flush=True)


def process_one(entry: dict) -> None:
    """Ein upload_queue-Eintrag, ein Versuch. Transiente Fehler (Netzwerk, 5xx,
    Kontingent für heute erschöpft) bleiben in Status queued/uploading -- der nächste
    Worker-Durchlauf versucht automatisch erneut. Ein zu naher scheduled_at wird
    dagegen NIE von selbst besser (der Abstand zu 'jetzt' schrumpft nur), deshalb
    terminal als 'failed' markiert statt endlos zu retryen."""
    try:
        assert_schedulable(entry["scheduled_at"])
    except ValueError as e:
        db.queue_update(entry["id"], status="failed", error=str(e))
        print(f"  [Upload] Queue #{entry['id']}: dauerhaft fehlgeschlagen: {e}", flush=True)
        return

    try:
        quota.assert_quota_available()
        video_id = _do_upload(entry)
        db.queue_update(entry["id"], status="uploaded", youtube_video_id=video_id, error=None)
        print(f"  [Upload] Queue #{entry['id']} ({entry['cid']}/{entry['vid']}): fertig -> "
              f"https://youtu.be/{video_id}", flush=True)
        _add_to_playlist(entry, video_id)
        _set_thumbnail(entry, video_id)
        _upload_captions(entry, video_id)
    except Exception as e:
        db.queue_update(entry["id"], attempts=entry.get("attempts", 0) + 1, error=str(e))
        print(f"  [Upload] Queue #{entry['id']}: Fehler: {e}", flush=True)


def worker_loop(poll_interval_sec: float = 5.0) -> None:
    """Dauerschleife für den Upload-Worker-Thread (dashboard.py main(), nur gestartet
    wenn youtube.oauth.client_configured())."""
    while True:
        try:
            for entry in db.queue_pending():
                if entry.get("platform") == "tiktok":
                    # Lazy import to avoid circular dependency
                    from tiktok.upload import process_one as tiktok_process_one
                    tiktok_process_one(entry)
                else:
                    process_one(entry)
        except Exception as e:
            print(f"  [Upload] Worker-Loop-Fehler: {e}", flush=True)
        time.sleep(poll_interval_sec)
