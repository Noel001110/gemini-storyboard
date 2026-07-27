"""youtube/metadata.py — Packaging: Titel (reuse generate_titles) + Description/Tags/
Kategorie, aus dem fertigen Skript, ohne externen Research-Teil (siehe Plan-Abschnitt
"Packaging"). Titel-Generierung selbst bleibt komplett unangetastet in engine/prompts.py
(generate_titles) -- dieses Modul ergänzt nur die drei fehlenden Felder.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_CATEGORIES_CACHE_PATH = os.path.join(HERE, "_categories_cache.json")
_CATEGORIES_CACHE_TTL_SEC = 24 * 3600

# Fallback, solange kein Kanal per OAuth verbunden ist (Phase 3) -- videoCategories.list
# selbst braucht immer mindestens ein OAuth-Token oder einen API-Key, beides existiert
# vor Phase 3 nicht in diesem System. Diese IDs sind der seit über einem Jahrzehnt
# stabile, gut dokumentierte Kern der offiziellen YouTube-Taxonomie (nur die praktisch
# für neue Uploads "assignable"-Kategorien, keine der Film/Show-spezifischen IDs 30+,
# die in den meisten Regionen ohnehin nicht zuweisbar sind). Sobald ein Kanal verbunden
# ist, ersetzt _fetch_categories_live() unten diese Liste durch den echten, live
# abgerufenen Stand -- Plan-Vorgabe: "nicht aus dem Gedächtnis hartkodiert".
_FALLBACK_CATEGORIES = [
    {"id": "1",  "title": "Film & Animation"},
    {"id": "2",  "title": "Autos & Vehicles"},
    {"id": "10", "title": "Music"},
    {"id": "15", "title": "Pets & Animals"},
    {"id": "17", "title": "Sports"},
    {"id": "19", "title": "Travel & Events"},
    {"id": "20", "title": "Gaming"},
    {"id": "22", "title": "People & Blogs"},
    {"id": "23", "title": "Comedy"},
    {"id": "24", "title": "Entertainment"},
    {"id": "25", "title": "News & Politics"},
    {"id": "26", "title": "Howto & Style"},
    {"id": "27", "title": "Education"},
    {"id": "28", "title": "Science & Technology"},
    {"id": "29", "title": "Nonprofits & Activism"},
]

PACKAGING_SYSTEM = """\
You write the YouTube description/tags/category block for an ALREADY-FINISHED video,
using ONLY the finished script and its chosen title as source material -- never invent
a claim the script doesn't support (same honesty rule as the title generator).

DESCRIPTION: 2-4 short paragraphs. The first 1-2 sentences must work as a standalone
hook on their own (that's the part shown before mobile's "Show more" truncation,
roughly the first 100 characters) -- restate the core hook, don't just repeat the
title verbatim. Weave in 3-5 relevant keyword phrases naturally, never keyword-stuffed.
No links, no hashtags, no calls-to-action placeholders -- the channel adds those
manually afterward.

TAGS: 8-15 short tags (single words or short phrases), ordered from the broadest
genre/topic match down to the most specific/niche angle of this exact video --
mirror how an interested viewer would actually search for this content.

CATEGORY: category_id must be EXACTLY one id from the ALLOWED CATEGORIES list given
below, chosen as the closest real match -- never invent an id outside that list, an
invalid categoryId fails the entire upload.
"""


def _read_categories_cache() -> list | None:
    try:
        data = json.load(open(_CATEGORIES_CACHE_PATH))
        if time.time() - data.get("ts", 0) < _CATEGORIES_CACHE_TTL_SEC:
            return data.get("categories") or None
    except Exception:
        pass
    return None


def _write_categories_cache(categories: list) -> None:
    try:
        with open(_CATEGORIES_CACHE_PATH, "w") as f:
            json.dump({"ts": time.time(), "categories": categories}, f)
    except OSError:
        pass


def _fetch_categories_live(cid: str, region_code: str) -> list | None:
    """Echter videoCategories.list-Aufruf über das per-OAuth verbundene Kanal-Token
    (youtube/oauth.py, Phase 3). Vor Phase 3 existiert dieses Modul/Token noch nicht --
    das ist hier kein Fehler, sondern der erwartete Zustand, deshalb breites except."""
    try:
        from youtube.oauth import get_valid_access_token  # Phase 3, evtl. noch nicht vorhanden
        token = get_valid_access_token(cid)
    except Exception:
        return None
    if not token:
        return None
    url = ("https://www.googleapis.com/youtube/v3/videoCategories"
           f"?part=snippet&regionCode={region_code}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.load(r)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  [YouTube] videoCategories.list live-Abruf fehlgeschlagen, "
              f"Fallback-Liste bleibt aktiv: {e}", flush=True)
        return None
    out = [{"id": item["id"], "title": item["snippet"]["title"]}
           for item in resp.get("items", [])
           if item.get("snippet", {}).get("assignable")]
    return out or None


def get_categories(cid: str | None = None, region_code: str = "US") -> list:
    """Gültige videoCategory-Liste (id+title). Live via OAuth-Token wenn ein Kanal
    verbunden ist, sonst der dokumentierte Fallback oben. 24h lokal gecacht, damit
    Packaging nicht bei jedem Aufruf erneut fragt."""
    cached = _read_categories_cache()
    if cached is not None:
        return cached
    live = _fetch_categories_live(cid, region_code) if cid else None
    categories = live if live else list(_FALLBACK_CATEGORIES)
    _write_categories_cache(categories)
    return categories


def _enforce_tag_char_limit(tags: list, limit: int = 500) -> list:
    """YouTube begrenzt ALLE Tags zusammen (inkl. Trennzeichen) auf ~500 Zeichen --
    Code-seitige Kürzung NACH dem LLM-Call, da das Modell selbst keine verlässliche
    Zeichen-Buchhaltung über eine ganze Liste hinweg garantieren kann."""
    kept = []
    total = 0
    for t in tags:
        t = t.strip()
        if not t:
            continue
        add = len(t) + (1 if kept else 0)  # +1 fürs Komma-Trennzeichen zwischen Tags
        if total + add > limit:
            break
        kept.append(t)
        total += add
    return kept


CHAPTER_TITLE_SYSTEM = """\
You write short YouTube chapter titles (2-6 words each) for the story segments given
below, in order. Each segment is one act of a video's real content -- write a title
that reflects what actually happens/is discussed in THAT segment's text, grounded only
in what's given. NEVER use a generic label like the segment's internal name (e.g. never
write "Climax", "Opening", "Rising Action", "Resolution", or similar meta-terms as a
title) -- viewers see these in the YouTube player as real chapter names, they must read
like a real title, not an internal category. Return exactly one title per segment, in
the same order.
"""


def _format_chapter_timestamp(seconds: float, total_duration: float) -> str:
    seconds = max(0, round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if total_duration >= 3600:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def generate_chapters(scenes: list, min_chapter_sec: float = 10.0) -> list | None:
    """YouTube-Kapitel für die Longform-Description. Gruppiert Szenen (chronologisch,
    nach scene['i']) in aufeinanderfolgende Blöcke gleicher `phase` (reale Taxonomie:
    OPENING/RISING_ACTION/CLIMAX/RESOLUTION -- siehe shorts/clip_select.py). Erzwingt
    YouTubes offizielle Vorgaben (support.google.com/youtube/answer/9884579, per
    WebFetch verifiziert): >= 3 Kapitel, erstes bei 00:00, jedes >= 10s -- zu kurze
    Blöcke werden mit dem nächsten (bzw. beim letzten Block: dem vorherigen) verschmolzen.

    Gibt None zurück, wenn am Ende < 3 gültige Blöcke übrig bleiben (kein Kapitel-
    Feature für dieses Video, kein Fehler). Die Titel selbst kommen aus EINEM LLM-Call
    (generate_chapter_titles-Schema) -- NIE der rohe Phasenname als Titel."""
    valid = [s for s in scenes
             if s.get("start_aligned") is not None and s.get("end_aligned") is not None]
    if not valid:
        return None
    valid.sort(key=lambda s: s["i"])

    blocks: list = []
    for s in valid:
        phase = s.get("phase")
        if blocks and blocks[-1]["phase"] == phase:
            blocks[-1]["scenes"].append(s)
        else:
            blocks.append({"phase": phase, "scenes": [s]})

    for b in blocks:
        b["start"] = b["scenes"][0]["start_aligned"]
        b["end"] = b["scenes"][-1]["end_aligned"]
        b["text"] = " ".join(sc.get("text", "") for sc in b["scenes"])

    # Zu kurze Blöcke verschmelzen -- vorwärts, außer beim letzten Block (dort rückwärts,
    # sonst bliebe ein winziger Rest am Ende übrig).
    merged: list = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if (b["end"] - b["start"]) < min_chapter_sec and i + 1 < len(blocks):
            nxt = blocks[i + 1]
            nxt["scenes"] = b["scenes"] + nxt["scenes"]
            nxt["start"] = b["start"]
            nxt["text"] = b["text"] + " " + nxt["text"]
            i += 1
            continue
        if (b["end"] - b["start"]) < min_chapter_sec and merged:
            prev = merged[-1]
            prev["end"] = b["end"]
            prev["scenes"] += b["scenes"]
            prev["text"] += " " + b["text"]
            i += 1
            continue
        merged.append(b)
        i += 1

    if len(merged) < 3:
        return None

    total_duration = merged[-1]["end"]
    titles = _generate_chapter_titles([b["text"] for b in merged])
    if len(titles) != len(merged):
        titles = [f"Part {idx + 1}" for idx in range(len(merged))]

    chapters = []
    for idx, (b, title) in enumerate(zip(merged, titles)):
        start = 0.0 if idx == 0 else b["start"]  # erstes Kapitel MUSS bei 00:00 stehen
        chapters.append({
            "seconds": start,
            "timestamp": _format_chapter_timestamp(start, total_duration),
            "title": title,
        })
    return chapters


def _generate_chapter_titles(block_texts: list) -> list:
    from dashboard import post_gemini_native  # lazy, wie generate_titles in engine/prompts.py

    schema = {
        "type": "object",
        "properties": {
            "titles": {"type": "array", "items": {"type": "string"}, "minItems": len(block_texts),
                       "maxItems": len(block_texts)},
        },
        "required": ["titles"],
    }
    segments_str = "\n\n".join(f"SEGMENT {i + 1}:\n{t.strip()[:1500]}"
                                for i, t in enumerate(block_texts))
    try:
        txt = post_gemini_native([
            {"role": "system", "content": CHAPTER_TITLE_SYSTEM},
            {"role": "user", "content": segments_str},
        ], json_mode=True, temp=0.7, response_schema=schema, thinking_level="low")
        data = json.loads(txt)
        titles = [str(t).strip() for t in data.get("titles", [])]
        if len(titles) == len(block_texts):
            return titles
    except Exception as e:
        print(f"  [Chapters] Titel-Generierung fehlgeschlagen: {e}", flush=True)
    return []


def format_chapters_block(chapters: list) -> str:
    """Formatiert generate_chapters()-Ergebnis als anhängbaren Description-Block --
    'MM:SS Titel' (bzw. 'H:MM:SS' ab 1h) pro Zeile, aufsteigend, wie von YouTube
    verlangt."""
    return "\n".join(f"{c['timestamp']} {c['title']}" for c in chapters)


def _srt_timestamp(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600); sec -= h * 3600
    m = int(sec // 60); sec -= m * 60
    s = int(sec)
    ms = round((sec - s) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(words: list, max_words_per_cue: int = 10, max_cue_sec: float = 5.0) -> str:
    """Baut eine echte SRT-Untertitelspur aus wortgenauen Timestamps (dieselbe Form
    wie voiceover_word_timestamps: [{"word","start","end"}]) -- getrennt von den
    eingebrannten 1-Wort-Captions im Video selbst (word_caption_seq in engine/render.py),
    die für eine echte Untertitelspur (captions.insert) viel zu kleinteilig geschnitten
    wären. Gruppiert stattdessen zu lesbaren Phrasen: neue Cue nach max_words_per_cue
    Wörtern, nach max_cue_sec Sekunden, oder am Satzende (. ! ?) -- je nachdem, was
    zuerst eintritt."""
    if not words:
        return ""
    cues = []
    cur, cur_start = [], None
    for w in words:
        if cur_start is None:
            cur_start = w["start"]
        cur.append(w)
        ends_sentence = str(w.get("word", "")).rstrip().endswith((".", "!", "?"))
        if len(cur) >= max_words_per_cue or (w["end"] - cur_start) >= max_cue_sec or ends_sentence:
            cues.append((cur_start, w["end"], " ".join(x["word"] for x in cur).strip()))
            cur, cur_start = [], None
    if cur:
        cues.append((cur_start, cur[-1]["end"], " ".join(x["word"] for x in cur).strip()))

    lines = []
    for i, (start, end, text) in enumerate(cues, 1):
        if not text:
            continue
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(start)} --> {_srt_timestamp(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def generate_packaging(full_script: str, chosen_title: str, categories: list) -> dict:
    """EIN post_gemini_native(..., response_schema=...)-Call -> {description, tags,
    category_id}. category_id ist enum-constrained auf die tatsächlich gültigen IDs
    aus `categories` (siehe get_categories) -- dieselbe Lehre wie beim concrete_entity-
    enum: harte Auswahl statt Prosa-Anweisung, wo Ungültigkeit einen echten Fehler
    (kompletter Upload-Fehlschlag) statt nur schlechter Qualität produziert."""
    from dashboard import post_gemini_native  # lazy, wie generate_titles in engine/prompts.py

    category_ids = [c["id"] for c in categories]
    schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "minLength": 50},
            "tags": {"type": "array", "items": {"type": "string"}},
            "category_id": {"type": "string", "enum": category_ids},
        },
        "required": ["description", "tags", "category_id"],
    }
    cat_list_str = "\n".join(f"- {c['id']}: {c['title']}" for c in categories)
    user_msg = (
        f"TITLE: {chosen_title}\n\n"
        f"ALLOWED CATEGORIES (category_id must be exactly one of these ids):\n{cat_list_str}\n\n"
        f"SCRIPT:\n{full_script.strip()[:6000]}"
    )
    txt = post_gemini_native([
        {"role": "system", "content": PACKAGING_SYSTEM},
        {"role": "user", "content": user_msg},
    ], json_mode=True, temp=0.7, response_schema=schema, thinking_level="low")
    data = json.loads(txt)
    return {
        "description": str(data.get("description", "")).strip(),
        "tags": _enforce_tag_char_limit(list(data.get("tags", []))),
        "category_id": str(data.get("category_id", "")),
    }
