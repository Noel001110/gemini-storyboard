"""scripts/backfill_youtube_metadata.py — schiebt Thumbnail, Untertitel und
defaultLanguage/defaultAudioLanguage NACHTRÄGLICH in bereits hochgeladene YouTube-
Videos, für alle Uploads die VOR der Juli-2026-Erweiterung liefen (youtube/upload.py:
_set_thumbnail, _upload_captions, _video_language wurden dort erst nachträglich in
process_one() eingehängt -- Uploads von davor haben keins von beidem).

Macht LIVE API-Calls gegen die echten, bereits verbundenen YouTube-Kanäle. Deshalb
bewusst:
  - Standardmäßig NUR ein Trockenlauf (listet, was gemacht würde, ändert nichts).
  - Echte Änderungen nur mit explizitem --execute.
  - Untertitel-Upload braucht den Scope youtube.force-ssl (siehe youtube/oauth.py) --
    ein Kanal, der vor dieser Erweiterung schon verbunden war, muss EINMALIG über
    Control neu verbunden werden, sonst schlägt nur der Untertitel-Teil fehl (Video
    bleibt unangetastet, Thumbnail/Sprache laufen mit dem alten Token trotzdem durch).

Aufruf:
    python3 scripts/backfill_youtube_metadata.py                 # Trockenlauf
    python3 scripts/backfill_youtube_metadata.py --execute        # echte Änderungen
    python3 scripts/backfill_youtube_metadata.py --execute --only=thestick
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

import store.db as db
from youtube.upload import _set_thumbnail, _upload_captions, _update_video_language


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                     help="Ohne dieses Flag: nur auflisten, nichts ändern.")
    ap.add_argument("--only", default=None, help="Nur dieser Kanal (cid).")
    args = ap.parse_args()

    entries = [e for e in db.queue_list(status="uploaded") if e.get("youtube_video_id")]
    if args.only:
        entries = [e for e in entries if e["cid"] == args.only]

    if not entries:
        print("Keine bereits hochgeladenen Videos gefunden.")
        return

    print(f"{len(entries)} bereits hochgeladene Video(s) gefunden"
          f"{' (Kanal: ' + args.only + ')' if args.only else ''}:")
    for e in entries:
        print(f"  #{e['id']}  {e['cid']}/{e['vid']}  [{e['render_target']}]  "
              f"-> https://youtu.be/{e['youtube_video_id']}")

    if not args.execute:
        print("\nTrockenlauf -- nichts geändert. Mit --execute wirklich ausführen.")
        return

    print("\n--execute gesetzt -- schreibe jetzt gegen die echte YouTube-API.\n")
    for e in entries:
        vid_id = e["youtube_video_id"]
        print(f"#{e['id']} {e['cid']}/{e['vid']} ({vid_id}):")
        _set_thumbnail(e, vid_id)
        _upload_captions(e, vid_id)
        _update_video_language(e, vid_id)


if __name__ == "__main__":
    main()
