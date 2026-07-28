"""engine/sfx_library.py -- reine Discovery-Schicht fuer das Shorts-SFX-System.

Scannt `Sounds/*` zur Laufzeit: jeder Unterordner wird eine Kategorie, seine
Dateien werden Varianten (dasselbe Rotationsmuster wie das bestehende, statische
SFX_LIBRARY in engine/audio.py -- siehe _select_sfx_variant). Kein ffmpeg, kein
LLM-Call hier, nur Dateisystem + kleine, editierbare Metadaten-Tabelle.

Neue Kategorien (z.B. spaeter "Cash", "Danger") werden ohne Codeaenderung
gefunden, sobald ihr Ordner unter Sounds/ existiert -- Metadaten (description/
loud/placement_window) sind optional und fallen auf neutrale Defaults zurueck,
wenn eine Kategorie hier noch nicht eingetragen ist.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from core.paths import HERE

SOUNDS_DIR = os.path.join(HERE, "Sounds")

_AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".flac")


@dataclass(frozen=True)
class CategoryMeta:
    description: str
    loud: bool = False
    placement_window: str = "anywhere"  # "hook" | "hook_and_reengagement" | "anywhere"


# Handkuratierte Metadaten fuer die aktuell vorhandenen mechanischen Kategorien
# (Bing/Plop/Woosh -- werden bereits deterministisch ueber TRANSITION_LIBRARY
# angesteuert, siehe engine/shorts_sfx_analyzer.py, tauchen der LLM daher nie als
# Auswahloption vor). Kuenftige semantische Kategorien (SUCCESS/DANGER/REVELATION/
# SOCIAL_HOOK o.ae.) werden hier ergaenzt, sobald ihr Sounds/<Name>/-Ordner existiert
# -- bis dahin bekommen unbekannte Ordner neutrale Defaults (siehe _DEFAULT_META).
CATEGORY_META: dict[str, CategoryMeta] = {
    "Bing": CategoryMeta(
        description="Bing = heller Signal-Ton fuer eine genannte Zahl, ein Ergebnis "
                     "oder einen Reveal-Moment.",
        loud=False,
        placement_window="anywhere",
    ),
    "Plop": CategoryMeta(
        description="Plop = weicher Auftauch-Sound, wenn nach einem harten Cut ein "
                     "neues Bild ins Bild ploppt.",
        loud=False,
        placement_window="anywhere",
    ),
    "Woosh": CategoryMeta(
        description="Woosh = energischer Bewegungs-Sound fuer einen schnellen "
                     "Bild-/Perspektivwechsel.",
        loud=False,
        placement_window="anywhere",
    ),
}

_DEFAULT_META = CategoryMeta(description="", loud=False, placement_window="anywhere")


def _category_from_dirname(name: str) -> str:
    return name


def discover_categories(sounds_dir: str = SOUNDS_DIR) -> dict[str, list[str]]:
    """Scannt `sounds_dir` nach Unterordnern -- jeder wird eine Kategorie, ihre
    Audiodateien (per Endung gefiltert) werden die Varianten-Liste. Ordner ohne
    brauchbare Audiodateien werden nicht aufgenommen. Lose Dateien direkt in
    `sounds_dir` (z.B. Meme-Sounds ausserhalb einer Kategorie) werden ignoriert --
    nur Unterordner sind Kategorien, das ist die bewusste Konvention dieses Systems."""
    categories: dict[str, list[str]] = {}
    if not os.path.isdir(sounds_dir):
        return categories
    for entry in sorted(os.scandir(sounds_dir), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        files = sorted(
            os.path.join(entry.path, f)
            for f in os.listdir(entry.path)
            if f.lower().endswith(_AUDIO_EXTS)
        )
        if files:
            categories[_category_from_dirname(entry.name)] = files
    return categories


def get_category_meta(category: str) -> CategoryMeta:
    return CATEGORY_META.get(category, _DEFAULT_META)


def semantic_categories(categories: dict[str, list[str]]) -> dict[str, list[str]]:
    """Alle entdeckten Kategorien MINUS der mechanischen (Bing/Plop/Woosh -- werden
    bereits deterministisch ueber TRANSITION_LIBRARY an Uebergaengen platziert, siehe
    engine/shorts_sfx_analyzer.py). Das ist die Liste, die der LLM fuer die semantische
    Schicht tatsaechlich zur Auswahl angeboten bekommt."""
    mechanical = {"Bing", "Plop", "Woosh"}
    return {cat: files for cat, files in categories.items() if cat not in mechanical}
