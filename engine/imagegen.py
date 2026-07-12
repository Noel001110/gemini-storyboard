"""engine.imagegen — Bild-Generierungs-Provider hinter einem Interface.

Enthält (Evaluation Juli 2026, Änderung 1 — Umbau-Vorschlag "mehr Konsistenz +
Modell-Flexibilität"):
    Öffentliches Interface:
        generate_image                — submit + poll + download, provider-agnostisch
    KIE-Provider (verschoben aus dashboard.py, Verhalten unverändert):
        kie_key, KIE_API, VALID_IMAGE_MODELS
        _kie_rate_limit_wait, _kie_circuit_status, _kie_record_failure,
        _kie_record_success, _kie_retry_with_backoff  — Rate-Limit + Circuit-Breaker
        _kie_submit_image              — Submit-Task, gibt task_id zurück
        _kie_poll_and_download         — Poll-Loop + Download (NEU, konsolidiert das
                                          bisher 3x duplizierte Poll-Muster aus
                                          dashboard.py/engine.prompts)
    Referenz-Hosting (verschoben aus dashboard.py, Änderung 2 bereits umgesetzt):
        upload_image_public, get_public_charsheet_url, _multipart_upload,
        KIE_UPLOAD_URL

BLEIBT in dashboard.py (Orchestrator, tief mit JOBS/Semaphore/plan.json verzahnt):
    _image_job_worker(_inner), _mark_scene_error, der Batch-Worker-Loop selbst —
    diese importieren die Provider-Bausteine jetzt von hier statt sie zu definieren.

Grund für den Umbau: engine/prompts.py und engine/scenes.py griffen zirkulär auf
dashboard.py zurück (`from dashboard import _kie_submit_image, kie_key, KIE_API`),
weil der KIE-Client im Monolithen lebte. Damit war kein zweites Bild-Modell testbar,
ohne im Monolithen zu wühlen. `generate_image(..., provider=...)` löst das — ein
zweiter Provider (z.B. FLUX Kontext, siehe Umbau-Dokument Phase 2a) braucht nur einen
neuen Eintrag in `_PROVIDERS`, keine Änderung an den Aufrufern.

`post_kie_text`/`post_gemini_native`/`analyze_script` (Text-LLM-Pfad) bleiben bewusst
in dashboard.py — andere Baustelle, nicht Teil dieser Änderung (siehe Umbau-Dokument).

Lazy-Import-Konvention (siehe routes/__init__.py): keine Top-Level-Imports von
dashboard.py oder anderen engine-Modulen hier — dieses Modul ist absichtlich das
Blatt der Abhängigkeits-Kette (nichts importiert wieder zurück).
"""

from __future__ import annotations

import collections
import json
import os
import threading
import time
import urllib.error
import urllib.request

KIE_KEY_FILE = os.path.expanduser("~/.kie_key")


def kie_key() -> str:
    return open(KIE_KEY_FILE).read().strip()


# KIE.ai — image generation (Job-Queue-API, geteilt mit gen_video in dashboard.py,
# deshalb bleibt der Name/Wert identisch zum bisherigen dashboard.KIE_API)
KIE_API = "https://api.kie.ai/api/v1/jobs"
VALID_IMAGE_MODELS = ("nano-banana-2", "nano-banana-2-lite")

# KIE's real documented limit is 20 submissions per 10s account-wide. Concurrent batch
# dispatch hat keine natürliche Taktung — dieser Sliding-Window-Limiter verhindert,
# dass mehrere gleichzeitig freiwerdende Slots über die reale Grenze bursten.
_KIE_SUBMIT_TIMES = collections.deque()
_KIE_SUBMIT_LOCK = threading.Lock()
KIE_SUBMIT_RATE_LIMIT = 12
KIE_SUBMIT_RATE_WINDOW = 10.0


def _kie_rate_limit_wait():
    """Sliding-window Rate-Limit. Bewegt Calls in 10s-Fenster auf max 12."""
    while True:
        with _KIE_SUBMIT_LOCK:
            now = time.time()
            while _KIE_SUBMIT_TIMES and now - _KIE_SUBMIT_TIMES[0] > KIE_SUBMIT_RATE_WINDOW:
                _KIE_SUBMIT_TIMES.popleft()
            if len(_KIE_SUBMIT_TIMES) < KIE_SUBMIT_RATE_LIMIT:
                _KIE_SUBMIT_TIMES.append(now)
                return
            wait_for = KIE_SUBMIT_RATE_WINDOW - (now - _KIE_SUBMIT_TIMES[0]) + 0.05
        time.sleep(max(wait_for, 0.05))


# Exponential Backoff + Circuit Breaker für externe API-Calls: nach
# KIE_FAILURES_THRESHOLD Fehlern in KIE_FAILURE_WINDOW_S Sekunden →
# KIE_CIRCUIT_OPEN_DURATION_S Sekunden keine Calls mehr (verhindert Thundering-Herd).
_KIE_FAILURE_TIMES = collections.deque(maxlen=20)
KIE_FAILURE_WINDOW_S = 60.0
KIE_FAILURES_THRESHOLD = 10
KIE_CIRCUIT_OPEN_DURATION_S = 60.0
_KIE_CIRCUIT_OPENED_AT = 0.0  # 0 = closed; sonst time.time() der Öffnung


def _kie_circuit_status():
    """True wenn Circuit geschlossen (Calls erlaubt), False wenn offen (Calls blockiert)."""
    global _KIE_CIRCUIT_OPENED_AT
    if _KIE_CIRCUIT_OPENED_AT == 0.0:
        return True
    if time.time() - _KIE_CIRCUIT_OPENED_AT > KIE_CIRCUIT_OPEN_DURATION_S:
        return True
    return False


def _kie_record_failure():
    """Zählt Fehler. Bei Threshold → Circuit öffnen."""
    global _KIE_CIRCUIT_OPENED_AT
    now = time.time()
    while _KIE_FAILURE_TIMES and now - _KIE_FAILURE_TIMES[0] > KIE_FAILURE_WINDOW_S:
        _KIE_FAILURE_TIMES.popleft()
    _KIE_FAILURE_TIMES.append(now)
    if len(_KIE_FAILURE_TIMES) >= KIE_FAILURES_THRESHOLD and _KIE_CIRCUIT_OPENED_AT == 0.0:
        _KIE_CIRCUIT_OPENED_AT = now
        print(f"  [WARNING] kie_circuit_opened threshold={KIE_FAILURES_THRESHOLD} "
              f"window_s={KIE_FAILURE_WINDOW_S} duration_s={KIE_CIRCUIT_OPEN_DURATION_S}", flush=True)


def _kie_record_success():
    """Schließt Circuit bei Erfolg."""
    global _KIE_CIRCUIT_OPENED_AT
    if _KIE_CIRCUIT_OPENED_AT != 0.0:
        print("  [INFO] kie_circuit_closed", flush=True)
        _KIE_CIRCUIT_OPENED_AT = 0.0
    _KIE_FAILURE_TIMES.clear()


def _kie_retry_with_backoff(fn, max_attempts: int = 4, base_sleep_s: float = 2.0):
    """Exponential Backoff für einen KIE-Call.

    fn: callable() → (success_bool, result_or_error)
    Wirft RuntimeError nach max_attempts-Versuchen. Respektiert Circuit-Breaker: wenn
    offen, wirft sofort."""
    if not _kie_circuit_status():
        raise RuntimeError(f"KIE Circuit Breaker ist offen — kein Call erlaubt (warte {KIE_CIRCUIT_OPEN_DURATION_S}s)")
    last_err = None
    for attempt in range(max_attempts):
        _kie_rate_limit_wait()
        try:
            ok, result = fn()
            if ok:
                _kie_record_success()
                return result
            last_err = result
        except Exception as e:
            last_err = str(e)
            _kie_record_failure()
        if attempt < max_attempts - 1:
            wait = base_sleep_s ** (attempt + 1)   # 2s, 4s, 8s
            err_preview = last_err[:80] if last_err else "-"
            print(f"  [KIE Backoff] Versuch {attempt+1}/{max_attempts} fehlgeschlagen ({err_preview}); "
                  f"warte {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"KIE nach {max_attempts} Versuchen aufgegeben: {last_err}")


def _kie_submit_image(full_prompt: str, model: str = "nano-banana-2", ref_urls: list | None = None,
                       *, aspect_ratio: str = "16:9", resolution: str = "2K",
                       output_format: str = "jpg", seed: int | None = None) -> str:
    """Submit image task to KIE, return task_id.

    ref_urls: reference image URL(s) for visual consistency. IMPORTANT — the correct
    field per KIE's actual docs is "image_input" for nano-banana-2 (up to 14 images) and
    "image_urls" for nano-banana-2-lite (up to 10). Using the wrong field name silently
    does nothing — KIE accepts the request (200 OK) but the reference has zero effect on
    the output, which is exactly the bug this fixes (verified empirically: submitting
    "image_urls" against nano-banana-2 produced a result with no resemblance at all to
    the reference image).

    seed: Evaluation Juli 2026 (Änderung 3, verworfen) — KIEs offizielle OpenAPI-Spec
    für nano-banana-2 kennt KEINEN seed-Parameter. Wird hier nur akzeptiert (statt einen
    TypeError zu werfen), damit generate_image() ein provider-übergreifend einheitliches
    Interface bleibt -- ein späterer zweiter Provider (fal.ai/Vertex) kann ihn nutzen."""
    if model not in VALID_IMAGE_MODELS:
        model = "nano-banana-2"
    hdrs = {"Authorization": f"Bearer {kie_key()}", "Content-Type": "application/json"}
    input_body = {
        "prompt": full_prompt, "aspect_ratio": aspect_ratio,
        "resolution": resolution, "output_format": output_format,
    }
    if ref_urls:
        ref_field = "image_input" if model == "nano-banana-2" else "image_urls"
        input_body[ref_field] = ref_urls[:14 if model == "nano-banana-2" else 10]
    body = {"model": model, "input": input_body}
    req_data = json.dumps(body).encode()

    last_err = None
    for attempt in range(4):
        _kie_rate_limit_wait()
        req = urllib.request.Request(f"{KIE_API}/createTask", data=req_data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            # HTTP 429 (Too Many Requests) is transient — the account exceeded the
            # per-window limit. Retry with backoff instead of letting the caller mark
            # the scene as failed.
            if e.code == 429 and attempt < 3:
                wait = 2 ** (attempt + 1)   # 2s, 4s, 8s
                print(f"  [WARNING] kie_429 attempt={attempt+1} wait_s={wait}", flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"KIE HTTP {e.code}: {e.read().decode()[:200]}")
        if resp.get("code") == 200:
            return resp["data"]["taskId"]
        msg = resp.get("msg", str(resp))
        last_err = msg
        # "Your call frequency is too high" is transient and self-inflicted by our own
        # burst — worth a short backoff + retry instead of giving up the whole scene.
        if "frequency" in msg.lower() and attempt < 3:
            print(f"  [WARNING] kie_frequency attempt={attempt+1} wait_s={2*(attempt+1)}", flush=True)
            time.sleep(2 * (attempt + 1))
            continue
        raise RuntimeError(f"KIE: {msg}")
    raise RuntimeError(f"KIE: {last_err}")


def _kie_poll_and_download(task_id: str, out_path: str, *, max_polls: int = 50,
                            poll_interval_s: float = 3.0) -> dict:
    """Poll-Loop + Download, konsolidiert aus dem bisher 3x duplizierten Muster
    (dashboard.py Batch-Worker, gen_image, engine.prompts.gen_thumbnail_image).
    Rückgabe: {"ok": bool, "file": str|None, "source_url": str|None, "error": str|None}."""
    poll_url = f"{KIE_API}/recordInfo?taskId={task_id}"
    poll_hdrs = {"Authorization": f"Bearer {kie_key()}"}
    for poll_i in range(max_polls):
        time.sleep(poll_interval_s)
        try:
            with urllib.request.urlopen(urllib.request.Request(poll_url, headers=poll_hdrs), timeout=15) as r:
                info = json.load(r).get("data", {})
        except Exception:
            continue
        state = info.get("state", "")
        if state == "success":
            try:
                urls = json.loads(info.get("resultJson", "{}")).get("resultUrls", [])
            except Exception:
                urls = []
            if not urls:
                return {"ok": False, "file": None, "source_url": None, "error": "KIE: kein Bild in resultUrls"}
            try:
                dl_req = urllib.request.Request(urls[0],
                    headers={"Referer": "https://kie.ai/", "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(dl_req, timeout=60) as img_r:
                    open(out_path, "wb").write(img_r.read())
            except Exception as e:
                return {"ok": False, "file": None, "source_url": urls[0], "error": f"Download fehlgeschlagen: {e}"}
            return {"ok": True, "file": os.path.basename(out_path), "source_url": urls[0], "error": None}
        if state == "fail":
            return {"ok": False, "file": None, "source_url": None,
                     "error": f"KIE fehlgeschlagen: {info.get('failMsg', 'unbekannt')}"}
    return {"ok": False, "file": None, "source_url": None, "error": f"KIE Timeout (>{max_polls * poll_interval_s:.0f}s)"}


def _provider_kie(prompt: str, ref_urls: list | None, *, model: str, aspect: str,
                   resolution: str, seed: int | None, out_path: str) -> dict:
    try:
        task_id = _kie_submit_image(prompt, model=model, ref_urls=ref_urls,
                                     aspect_ratio=aspect, resolution=resolution, seed=seed)
    except RuntimeError as e:
        return {"ok": False, "url": None, "path": None, "error": str(e)}
    result = _kie_poll_and_download(task_id, out_path)
    return {"ok": result["ok"], "url": result.get("source_url"),
            "path": out_path if result["ok"] else None, "error": result.get("error")}


_PROVIDERS = {
    "kie": _provider_kie,
    # "flux": _provider_flux_kontext,   # Phase 2a (Umbau-Dokument), noch nicht gebaut
}


def generate_image(
    prompt: str,
    ref_urls: list | None = None,
    *,
    out_path: str,
    model: str = "nano-banana-2",
    provider: str = "kie",
    aspect: str = "16:9",
    resolution: str = "2K",
    seed: int | None = None,
) -> dict:
    """Einheitliches Bild-Provider-Interface (Evaluation Juli 2026, Änderung 1).
    Submit + Poll + Download in einem synchronen Call. Rückgabe:
    {"ok": bool, "url": str|None, "path": str|None, "error": str|None}.

    `out_path` ist Pflicht (anders als im ursprünglichen Umbau-Vorschlag) -- der
    Download braucht ein Ziel, und implizite Pfad-Konventionen würden dieses Modul
    wieder an dashboard.py's v_out()/Pfad-Layout koppeln, genau die Kopplung, die
    dieser Umbau auflösen soll."""
    fn = _PROVIDERS.get(provider)
    if fn is None:
        return {"ok": False, "url": None, "path": None, "error": f"Unbekannter Provider: {provider!r}"}
    return fn(prompt, ref_urls, model=model, aspect=aspect, resolution=resolution, seed=seed, out_path=out_path)


# ── Referenz-Hosting (Änderung 2, bereits umgesetzt — hierher verschoben) ────────────

KIE_UPLOAD_URL = "https://api.kie.ai/api/file-stream-upload"

# KIE-URLs sind laut docs.kie.ai 24h gültig. 20h Marge statt voller 24h.
_CHARSHEET_UPLOAD_TTL_SEC = 20 * 3600
_CHARSHEET_UPLOAD_CACHE = {}   # local_path -> (url, uploaded_ts)
_CHARSHEET_UPLOAD_LOCK = threading.Lock()


def _multipart_upload(url: str, field: str, filename: str, data: bytes, mime: str,
                       extra_fields: dict | None = None, extra_headers: dict | None = None) -> str:
    """Generic multipart/form-data upload, returns response body."""
    boundary = b"----upload-" + str(int(time.time())).encode()
    body = b""
    if extra_fields:
        for k, v in extra_fields.items():
            body += b"--" + boundary + b"\r\n"
            body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (b"--" + boundary + b"\r\n"
             + f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
             + f"Content-Type: {mime}\r\n\r\n".encode()
             + data + b"\r\n--" + boundary + b"--\r\n")
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode().strip()


def get_public_charsheet_url(local_path: str) -> str:
    """Gets or uploads a local charsheet image. Caches per server session, but
    re-uploads once the cached URL's TTL (siehe _CHARSHEET_UPLOAD_TTL_SEC) abläuft."""
    with _CHARSHEET_UPLOAD_LOCK:
        cached = _CHARSHEET_UPLOAD_CACHE.get(local_path)
        if cached and (time.time() - cached[1]) < _CHARSHEET_UPLOAD_TTL_SEC:
            return cached[0]

    url = upload_image_public(local_path)

    with _CHARSHEET_UPLOAD_LOCK:
        _CHARSHEET_UPLOAD_CACHE[local_path] = (url, time.time())
    return url


def upload_image_public(local_path: str) -> str:
    """Upload local image to a public host, return URL.

    Primär: KIEs eigene File-Upload-API (selbe Domain wie image_input erwartet,
    garantiert kein Self-Block, Dateien automatisch nach 24h gelöscht -- kein
    permanentes Leak der Charsheets mehr). catbox/litterbox bleiben als Fallback-Kette
    (KIE-Ausfall/Netzproblem)."""
    with open(local_path, "rb") as f:
        data = f.read()
    ext   = os.path.splitext(local_path)[1].lower() or ".png"
    mime  = "image/png" if ext == ".png" else "image/jpeg"
    fname = "image" + ext

    try:
        resp_raw = _multipart_upload(
            KIE_UPLOAD_URL, "file", fname, data, mime,
            extra_fields={"uploadPath": "storyboard-refs", "fileName": fname},
            extra_headers={"Authorization": f"Bearer {kie_key()}"},
        )
        resp = json.loads(resp_raw)
        url = (resp.get("data") or {}).get("downloadUrl", "")
        if resp.get("success") and url.startswith("http"):
            print(f"  [Upload] kie → {url}", flush=True)
            return url
        print(f"  [Upload] kie lieferte kein gültiges Ergebnis ({resp_raw[:200]}) — versuche catbox …", flush=True)
    except Exception as e:
        print(f"  [Upload] kie fehlgeschlagen: {e} — versuche catbox …", flush=True)

    # Fallback: catbox.moe (permanent, reliable, returns raw image)
    try:
        url = _multipart_upload(
            "https://catbox.moe/user/api.php",
            "fileToUpload", fname, data, mime,
            extra_fields={"reqtype": "fileupload"}
        )
        if url.startswith("http"):
            print(f"  [Upload] catbox → {url}", flush=True)
            return url
    except Exception as e:
        print(f"  [Upload] catbox fehlgeschlagen: {e} — versuche litterbox …", flush=True)

    # Fallback: litterbox.catbox.moe (72h temp)
    try:
        url = _multipart_upload(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            "fileToUpload", fname, data, mime,
            extra_fields={"reqtype": "fileupload", "time": "72h"}
        )
        if url.startswith("http"):
            print(f"  [Upload] litterbox → {url}", flush=True)
            return url
    except Exception as e:
        print(f"  [Upload] litterbox fehlgeschlagen: {e}", flush=True)
        raise ValueError(f"Upload fehlgeschlagen: {e}")
