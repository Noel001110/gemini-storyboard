"""engine.presets — Stil-Preset-Bibliothek für Master-Prompts.

Enthält (Phase Q + 38, 2026-07-07):
    Konstanten:
        PRESET_MASTERS           — dict[str, str]: preset-id → Master-Prompt-Text
        PRESET_DESCRIPTIONS      — dict[str, str]: preset-id → Kurzbeschreibung (für UI)
        DEFAULT_PRESET           — "flat_cartoon_doc"
        IMAGE_MASTER_DEFAULT     — = PRESET_MASTERS[DEFAULT_PRESET]
        VIDEO_MASTER_DEFAULT     — Video-Variante von flat_cartoon_doc

Hintergrund (CINEMATIC_UPGRADE_PLAN.md §10):
Der bisherige IMAGE_MASTER_DEFAULT (Stick-Figure) ist ein karger Platzhalter ohne
Kompositions- oder Safety-Regeln. Neue Kanäle starten mit dem schwächsten denkbaren
Stil. Der tatsächliche Ziel-Stil (flacher 2D-Cartoon, dokumentarisch) existierte nur
als ~50 JPG-Referenzframes in assets/*.jpg — nirgendwo als Prompt kodifiziert.

Phase Q + 38 schließt diese Lücke:
  Q.2: PRESET_MASTERS mit 5 fertig formulierten Presets (1:1 aus dem Plan)
  38: PRESET_MASTERS ist die Quelle der Wahrheit für IMAGE_MASTER_DEFAULT
      Bei Kanal-Anlage wird das gewählte Preset in master_prompt.txt kopiert
      (bestehender write_master-Mechanismus) — danach frei editierbar.

Reihenfolge der Presets in der UI (Phase 38 Frontend):
  1. flat_cartoon_doc      — Default, dokumentarisch
  2. editorial_minimal     — Erklär-/Essay-Formate
  3. ink_documentary       — schwarz-weiße Tusche
  4. charcoal_noir         — True Crime / düstere Stoffe
  5. stick_minimal         — Legacy (Rückwärtskompatibilität)

Safety-Regel (alle Presets): sensitive subjects nur symbolisch darstellen —
Kinder, Gewalt, Trafficking etc. NIEMALS explizit, NIEMALS identifizierbar.
"""


# ── Preset 1: flat_cartoon_doc (DEFAULT) ─────────────────────────────────────
# Flacher 2D-Cartoon, dokumentarisch, kodifiziert in §10.4 des Plans.
# Vorher existierte dieser Stil nur als ~50 JPG-Referenzframes in assets/ —
# kein einziger Prompt hat ihn beschrieben.

_FLAT_CARTOON_DOC_MASTER = """\
STYLE (apply to EVERY image, never deviate):
Flat 2D cartoon documentary illustration with a clean, vector-like finish.
Bold, uniform dark-brown outlines around every character and object — the single
strongest style marker; never thin, never sketchy.
Flat color fills with subtle cel shading: one shadow tone and one light tone per
surface. Soft gradients ONLY for light cones and glows (lamps, windows, screens).
Muted, warm, slightly desaturated palette — earthy browns, warm greys, soft ambers;
night scenes shift to cool desaturated blues. Never neon, never fully saturated.
LIGHTING CARRIES THE MOOD: exactly one dominant light source per scene with a
visible light cone; the environment stays darker and moodier than the subject.
Characters: simplified rounded proportions, slightly large heads, minimal facial
features (dot-or-line eyes, thick expressive eyebrows); emotion is carried by
posture, framing and lighting — not by detailed faces.
Environments: detailed but simplified interiors and cityscapes with real depth —
furniture, shelves, windows, skylines. Never an empty white background.
Cinematic 16:9 framing: rule of thirds, profile and over-shoulder shots, dramatic
close-ups on hands and objects where the moment demands it.
NO photorealism, NO text or lettering inside the image, NO speech bubbles,
NO watermarks, NO borders.
Sensitive subjects (children, suffering, death): depict symbolically — silhouettes,
abandoned objects, long shadows — never explicit, never identifiable real victims.
"""

_FLAT_CARTOON_DOC_VIDEO = """\
STYLE (apply to EVERY frame of every video, never deviate):
Flat 2D cartoon documentary illustration with a clean, vector-like finish.
Bold, uniform dark-brown outlines around every character and object.
Flat color fills with subtle cel shading; soft gradients only for light cones.
Muted, warm, slightly desaturated palette — earthy browns, warm greys, soft ambers;
night scenes shift to cool desaturated blues.
LIGHTING CARRIES THE MOOD: exactly one dominant light source per scene with a
visible light cone; the environment stays darker and moodier than the subject.
Characters: simplified rounded proportions, large heads, minimal facial features.
Cinematic 16:9 framing: rule of thirds, profile and over-shoulder shots.
Camera: Ken Burns zoom or slow pan matching scene emotion — no rapid motion.
NO photorealism, NO color drift between scenes, NO mouth movement, NO lip sync.
Sensitive subjects: symbolic depiction only — silhouettes, abandoned objects,
long shadows — never explicit, never identifiable real victims.
"""


# ── Preset 2: editorial_minimal ─────────────────────────────────────────────
# Erklär-/Essay-Formate, Daten-freundlich, zwei Akzentfarben.

_EDITORIAL_MINIMAL_MASTER = """\
STYLE (apply to EVERY image, never deviate):
Flat editorial illustration: bold black outlines, large simple geometric shapes,
pure white #FFFFFF background. Exactly TWO accent colors — muted red #C13838 and
ink blue #1E6BD6 — used sparingly to mark THE key element of each scene, never
decoratively. Isometric or straight-on perspective; diagram-like clarity.
Composition: poster-like, one idea per image, huge negative space, 16:9.
Characters: faceless simplified figures, consistent across ALL images.
Objects and metaphors over literal depiction — a shrinking coin stack instead of
"the economy fell", one figure against a wall of identical figures for conformity.
NO photorealism, NO gradients, NO shading, NO text or numbers inside the image
(numbers are rendered by the pipeline's own counter overlays, never by the model).
Sensitive subjects: symbolic depiction only — outlines, silhouettes, repeated
patterns — never explicit, never identifiable real victims.
"""


# ── Preset 3: ink_documentary ────────────────────────────────────────────────
# Schwarz-weiße Tusche, dokumentarisch. Für Kanäle, die diesen Look wollen.

_INK_DOCUMENTARY_MASTER = """\
STYLE (apply to EVERY image, never deviate):
Hand-drawn black ink line art on a pure white #FFFFFF background.
Confident, varied line weight — thick expressive contour strokes, thin detail lines.
Crisp, sharp, high-contrast, clean white, no grain, no texture, no gradients, no frames.
Composition: ONE strong focal point per image, generous negative space, cinematic 16:9
framing — rule of thirds, low or high angles where the scene's emotion demands it.
Characters: consistent proportions and clothing across ALL images; emotion carried by
body language and posture; minimal facial detail (brows and eyes only when needed).
NO photorealism, NO color, NO text or lettering inside the image, NO watermarks,
NO borders, NO whiteboard/pencil/sketch-paper look.
Sensitive subjects (children, suffering, death, trafficking): depict symbolically —
an empty bowl and reaching hands instead of a starving child; a silhouette behind
frosted glass instead of an identifiable victim; falling papers instead of a body.
"""


# ── Preset 4: charcoal_noir ──────────────────────────────────────────────────
# True Crime / düstere Stoffe. Monochrom mit einem symbolischen Rot-Akzent.

_CHARCOAL_NOIR_MASTER = """\
STYLE (apply to EVERY image, never deviate):
Charcoal and ink illustration on off-white paper #F5F5F0. Deep, rich blacks with
rough charcoal shading; strong chiaroscuro lighting — one dominant light source per
scene, long dramatic shadows. Fog, rain, window light and silhouettes are core motifs.
Composition: film-noir framing — dutch angles, extreme close-ups on hands/eyes/objects,
subjects small against oppressive architecture. 16:9 cinematic.
Characters: consistent silhouettes and coats across ALL images; faces mostly in shadow.
NO photorealism, NO color except ONE symbolic red accent allowed when the script names
blood, danger or a warning — otherwise strictly monochrome. NO text in the image.
Sensitive subjects: symbolic depiction only — chalk outline, abandoned shoe, flickering
streetlamp; never explicit violence, never identifiable real victims.
"""


# ── Preset 5: stick_minimal (LEGACY) ─────────────────────────────────────────
# Der bisherige Default. Bleibt erhalten für Rückwärtskompatibilität — bestehende
# Kanäle behalten ihren master_prompt.txt ohnehin, das wird nie überschrieben.
# Neue Kanäle, die explizit dieses Preset wählen, bekommen weiterhin Strichmännchen.

_STICK_MINIMAL_MASTER = """\
CHARACTER VISUAL (used as reference for every image):
Minimalist 2D stick figure — round circle head, straight line limbs, no facial features drawn.
Pure black thin strokes on pure white #FFFFFF background.
No color, no shading, no fill. Flat line art only.

SCENE STYLE:
Each image shows the stick figure in a pose or action that visually represents the scene moment.
Keep proportions consistent. Simple bold lines. White background always empty.
"""


# ── Public API ────────────────────────────────────────────────────────────────

PRESET_MASTERS: dict[str, str] = {
    "flat_cartoon_doc":  _FLAT_CARTOON_DOC_MASTER,
    "editorial_minimal": _EDITORIAL_MINIMAL_MASTER,
    "ink_documentary":   _INK_DOCUMENTARY_MASTER,
    "charcoal_noir":     _CHARCOAL_NOIR_MASTER,
    "stick_minimal":     _STICK_MINIMAL_MASTER,
}

PRESET_DESCRIPTIONS: dict[str, str] = {
    "flat_cartoon_doc":  "Flacher 2D-Cartoon, dokumentarisch. Warmes Farbkonzept, eine Lichtquelle pro Szene.",
    "editorial_minimal": "Editorial-Illustration, Erklär-/Essay-Formate. Datenfreundlich mit zwei Akzentfarben.",
    "ink_documentary":   "Schwarz-weiße Tusche, dokumentarisch. Hoher Kontrast, viel Negativraum.",
    "charcoal_noir":     "True Crime / düstere Stoffe. Monochrom mit symbolischem Rot-Akzent.",
    "stick_minimal":     "Strichmännchen, monochrom. Legacy-Stil — wird nur noch auf explizite Wahl genutzt.",
}

DEFAULT_PRESET: str = "flat_cartoon_doc"
IMAGE_MASTER_DEFAULT: str = _FLAT_CARTOON_DOC_MASTER
VIDEO_MASTER_DEFAULT: str = _FLAT_CARTOON_DOC_VIDEO


__all__ = [
    "PRESET_MASTERS",
    "PRESET_DESCRIPTIONS",
    "DEFAULT_PRESET",
    "IMAGE_MASTER_DEFAULT",
    "VIDEO_MASTER_DEFAULT",
]