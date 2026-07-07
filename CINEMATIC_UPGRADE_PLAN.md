# Cinematic-Upgrade-Plan — Gap-Analyse „Simplicissimus-Niveau" (2026-07-07)

Analyse und priorisierter Bauplan, erstellt nach vollständiger Code-Lektüre (nicht nur Doku):
`dashboard.py` (4.657 Z.), `engine_elevenlabs.py`, `render_overlay.py`,
`tests/test_cinematic_e2e.py` (frisch ausgeführt: **54/54 grün**), `assets/CREDITS.txt`,
`STYLE_GUIDE.md`, `ARCHITECTURE.md` §20–§34, `IMPLEMENTATION_PLAN.md`.

Alle Zeilennummern beziehen sich auf den Stand vom 2026-07-07 (Branch `main`, ab b35bb51).

---

## 0. Eigene Position (Schritt 0 — vor allem anderen)

Diese Pipeline hat ein anderes Problem, als die 8-Kriterien-Liste nahelegt. Die cinematische
**Maschinerie ist real, getestet und überraschend vollständig**: frame-exakte Sync-Invariante,
LLM-Phase-Engine mit Hysterese, deterministisches Motion-/Transition-Vokabular, Ducking,
Phase-Volume-Envelope, Phase-Color-Grading, Title-Cards, Counter-Overlays. Das ist kein
Diashow-Generator mehr. Der Engpass liegt nicht in fehlender Mechanik, sondern an zwei ganz
konkreten Stellen: **(a) Das gesamte Sound-Design spielt auf synthetischen
Platzhaltern** — ein `sine`/`anoisesrc`-Bett, das per Phase-Volume moduliert wird, bleibt ein
Sinuston. Kriterium 5 ist der größte Einzelhebel für wahrgenommene Produktionsqualität, seine
Plumbing existiert zu 100 %, sein Material zu 0 %. **(b) Alles Visuelle atmet in exakt einer
Rhythmus-Einheit — der Szene.** Innerhalb einer 5-Sekunden-calm-Szene passiert genau eine
Smoothstep-Bewegung; der Moment, in dem der Zuschauer das Muster bemerkt, ist der Moment, in
dem sich das Video wie eine Slideshow anfühlt. Die Wort-Timestamps, die das lösen würden,
liegen zur Render-Zeit bereits vollständig vor (`adjusted_words` in `_render_worker`) und
werden nach dem Szenen-Alignment weggeworfen.

Wo ich von Abschnitt 2 des Auftrags **explizit abweiche**: Erstens ist Kriterium 3 („Daten
zeigen") für *diesen* Kanal nicht Priorität 1. Simplicissimus ist ein Daten-Essay-Kanal; diese
Pipeline produziert **persönliche Erzählungen im Ink-/Line-Art-Stil** (Yeonmi-Story: Flucht,
Hunger, Menschenhandel — getragen von Figuren und Symbolik, nicht von Statistik). Animierte
Counter/Balken lohnen sich als mittlere Priorität, **Karten lehne ich ab** (Begründung §2,
Kriterium 3). Zweitens **verwerfe ich Kriterium 8 (Tilt-Shift) ganz**: Der Miniatur-Look lebt
von fotografischen Tiefen-Cues (Texturen, Straßenfluchten, kleine Menschen in großen Räumen).
Auf flächiger schwarz-auf-weiß-Line-Art gibt es keine Tiefe zu simulieren — ein
Unschärfe-Gradient auf reinweißem Hintergrund liest sich als Rendering-Fehler, exakt die
Defekt-Klasse, vor der §26.3 warnt. Drittens ist die Skript-Seite **weiter, als der Auftrag
annimmt**: `SCRIPT_SYSTEM` (dashboard.py:458) erzwingt bereits explizit ein
Simplicissimus-Schema mit HOOK als Punkt 1 und „open question" als Closing. Die Lücke bei
Kriterium 1+2 ist nicht Generierung, sondern **Verifikation + Render-Kopplung** — für
Fremd-Skripte (Option A/B) prüft niemand, ob ein Hook existiert, und der Renderer weiß nichts
davon. Das macht Kriterium 1+2 deutlich billiger als geplant. Das kontinuierliche
Scoring-Modell aus Abschnitt 2.1 **baue ich nicht** (Begründung §5).

---

## 1. Ground-Truth-Verifikation (Schritt A): Doku §20–§32 vs. Code

Gegenprobe im Stil von `IMPLEMENTATION_PLAN.md` §1. Ergebnis vorweg: **Die Cinematic-Doku ist
ehrlich.** Anders als die alte Warnung vor Doku-Drift befürchten ließ, deckt sich §20–§32
fast vollständig mit dem Code — ein einziges echtes Drift-Fundstück (Zeile 4 der Tabelle).

| Doku behauptet | Code-Realität | Status |
|---|---|---|
| §20.2: `MOTION_LIBRARY` mit generalisierten Zoom+Fokus-Verläufen, deterministische Auswahl | ✅ dashboard.py:1936 — 11 Einträge, Auswahl `candidates[i % len]`, Intensität skaliert mit Dauer, Sequenz-Fortsetzungen erben Motion | stimmt |
| §24: Phase-Engine, 80 %-Hysterese, `phase_source`-Debugfeld, Cold-Open-fähig („Flash-Forward bei Beat 0 darf CLIMAX sein") | ✅ `_assign_phases` (1206), `PHASE_COVERAGE_THRESHOLD = 0.8` (1152), analyze_script-Prompt instruiert explizit „actual narrative arc, NOT position" (903 ff.) | stimmt |
| §25: `PHASE_PROMPT_ADDITIONS` als harte Injection in den finalen KIE-Prompt | ✅ `_build_image_prompt` (1320) injiziert `STYLE ({phase}): …` unmittelbar vor dem Master | stimmt |
| §29 / §32.1: `PHASE_VOLUME` „moved to engine_elevenlabs.py" (Kommentar dashboard.py:1150) | ⚠ dashboard.py:1172 definiert `PHASE_VOLUME` **erneut** und schattet damit den `import *` aus Zeile 22. Werte identisch → kein Verhaltens-Bug, aber genau die Dead-Code-Klasse, die Phase 33.4.2-prep A jagt. `t_phase_j_no_duplicate_tts_constants` prüft nur TTS-Konstanten, nicht diese | **Drift (klein)** |
| §26.1: `PHASE_COLOR_FILTER` nach zoompan, vor Overlays | ✅ `_render_clip` (2122): `eq_suffix` hängt am `[base]`-Label, Overlays compositen danach | stimmt |
| §27: Title-Cards als eigener Szenentyp, PIL-generiert, Phase-Akzentfarbe | ✅ `render_title_card` (render_overlay.py:154), Shortcut in `_render_clip` (2087) | stimmt |
| §28: Counter-Style bei punchy+callout | ✅ `_overlay_specs_for_scene` (2051) + `render_counter` (render_overlay.py:100) — **statisch**, kein Count-Up | stimmt (mit Grenze) |
| §29.2/29.3: Staircase-Fix + `end_aligned`-Intervallende | ✅ `_phase_modulate_music` (2440): inclusive-start/exclusive-end-Ausdruck, `end_aligned` bevorzugt | stimmt |
| §32.5: `engine_render.py`/`engine_audio.py` „noch nicht extrahiert" | ✅ ehrlich — es existiert nur `engine_elevenlabs.py`; die komplette Render-/Audio-Pipeline lebt in dashboard.py (~Z. 1860–2725) | stimmt |
| §32.6 u. a.: Test-IDs `t_phase_b_*` … `t_phase_j_*` | ✅ alle in `tests/test_cinematic_e2e.py` vorhanden; Suite frisch ausgeführt: **54 passed, 0 failed** | stimmt |
| §14: Sound-Assets = synthetische Platzhalter | ✅ `assets/CREDITS.txt` bestätigt wörtlich: sine/anoisesrc via lavfi, „Aktuell keine echten Einträge" | stimmt — **der Kern-Gap** |
| §16/§19: Whisper-Wörter nur für Sync-Invariante + Pausen-Trim + SFX-Anker | ✅ `align_scenes_to_whisper` (3258) konsumiert die Wortliste per Wortzahl und persistiert nur `start_aligned`/`end_aligned`. Die Wortliste selbst geht nach dem Render verloren (ElevenLabs-Wörter überleben in `audio_meta.json`, Whisper-Wörter gar nicht) | stimmt |

---

## 2. Gap-Analyse (Schritt B) — gegen die Zieldefinition aus §0

### Kurz-Tabelle

| # | Kriterium | Status | Hebel (1–5) | Aufwand (1–5) | Phase |
|---|---|---|---|---|---|
| 5 | Musik + SFX atmen mit dem Bogen | ⚠ Plumbing ✅ / Material ❌ | **5** | 2 | **K** |
| 1+2 | Cold Open + Leitfrage | ⚠ nur im Generator-Prompt, keine Prüfung/Kopplung | 4 | 2 | **L** |
| 4 | Cuts folgen dem Sprechrhythmus | ⚠ Szenengrenzen aligned, Intra-Szene statisch | 4 | 3 | **O** |
| 3 | Daten werden gezeigt | ⚠ statische Counter, keine Animation; Karten ❌ | 3 | 3 | N |
| 6 | Kontrastreiches Grading | ⚠ existiert, aber `eq` auf Line-Art fast wirkungslos | 2–3 | 2 | P |
| 7 | Smoothe, vielfältige Übergänge | ✅ | — | — | — |
| 8 | Tilt-Shift | ❌ — **bewusst nicht bauen** (§0) | 1 | 3 | verworfen |
| — | Modularisierung (Auftrags-Frage 5.6) | Enabler, kein Kriterium | — | 2 | M |

### Details pro Kriterium

**K5 — Musik/SFX (Hebel 5 / Aufwand 2).** Vorhanden: `_build_final_audio` (2486) →
`_phase_modulate_music` (2440) → `_duck_music_under_voice` (2395) → `_place_sfx` (2411),
SFX-Events regelbasiert und frame-genau auf `start_aligned` (2354). Fehlt: echtes Material —
und selbst mit echtem Material bleibt **ein** geloopter neutral_bed über 15–25 Min. flach,
egal welcher Volume-Envelope drüberliegt. „Atmen" heißt: unterschiedliche Betten pro
Intensitätsstufe, mit Crossfade an Phasen-Blockgrenzen. Der Kommentar in dashboard.py:1169
(„wird bedeutungsvoll, sobald Pixabay-Stems da sind") sieht das selbst so. → Phase K.

**K1+2 — Hook/Leitfrage (Hebel 4 / Aufwand 2).** Vorhanden: `SCRIPT_SYSTEM` erzwingt
HOOK-Schema für *generierte* Skripte; die Phase-Engine erkennt Flash-Forward-Cold-Opens
passiv (CLIMAX-Phase bei Beat 0 möglich). Fehlt: (a) `analyze_script` extrahiert weder
`hook` noch `throughline_question` — für hochgeladene/transkribierte Skripte gibt es null
QA-Signal; (b) der Renderer behandelt den Hook-Beat wie jede andere Szene. → Phase L.

**K4 — Sprechrhythmus (Hebel 4 / Aufwand 3).** Vorhanden: Szenengrenzen sitzen auf echten
Wortgrenzen (Whisper/ElevenLabs), Pausen-Trim, punchy-Szenen ≤1.1 s, `MAX_SCENE_SEC = 6.0`.
Das ist die halbe Miete — die Schnittdichte stimmt. Fehlt: jegliche Bewegungsdynamik
*innerhalb* einer Szene. Eine 5-s-calm-Szene ist eine einzige gleichförmige Kamerabewegung.
Die Wortliste, die betonungsgenaue Akzente ermöglichen würde, ist zur Render-Zeit im Scope
(`adjusted_words`, dashboard.py:2589) und wird verworfen. → Phase O.

**K3 — Daten zeigen (Hebel 3 / Aufwand 3).** Vorhanden: `callouts` (LLM-extrahiert, strikte
Nie-Erfinden-Regel, 897 ff.), statischer Counter-/Callout-Overlay. Fehlt: Animation (Count-Up,
wachsender Balken, Zeitleisten-Marker). Machbarkeit ohne Framework-Bruch: **ja** — PIL rendert
statt einem PNG eine Frame-Sequenz, ffmpeg liest sie als `image2`-Input; alles andere
(`overlay`, `fade`) bleibt identisch. **Karten lehne ich ab**: eine echte Karte braucht
Geodaten + Projektion → das ist eine Kartografie-Library durch die Hintertür
(Zero-Framework-Konflikt). Der stilkonforme Weg: eine „ink-style map" ist ein *Bild* — vom
Bildmodell generiert wie jede andere Szene, bei Bedarf mit animiertem Marker-Overlay obendrauf.
→ Phase N.

**K6 — Grading (Hebel 2–3 / Aufwand 2).** Vorhanden und getestet — aber:
`eq=saturation=1.2` auf schwarz-auf-weiß-Tusche-Zeichnungen ist mathematisch fast ein No-Op
(es gibt kaum Sättigung zu verstärken), `contrast=1.3` macht Schwarz schwärzer und Weiß
weißer — das sieht man, aber es „dramatisiert" nicht. Für diesen Stil wirken **Papier-Tönung**
(colorbalance auf die weißen Flächen) und **Vignette** deutlich stärker als eq. Refinement
eines bestehenden Features, kein Neubau. → Phase P.

**K7 — Übergänge: erledigt.** Smoothstep-Easing (2108), kuratierte xfade-Familien mit
variabler Dauer (2255), phasen-priorisierte Auswahl (2262), Richtungs-Alternierung,
SFX-Kopplung per Konstruktion (2354). Einzige echte Restlücke: Übergänge existieren **nur an
Sequenzgrenzen** (`_has_transition_before`, 2299) — Videos ohne erkannte `visual_sequences`
haben ausschließlich harte Schnitte. Das ist eine Daten-, keine Code-Frage (das LLM ist
instruiert, Sequenzen sparsam zu vergeben) und für den Simplicissimus-Look sogar richtig:
harte Schnitte dominieren dort auch. Kein Handlungsbedarf.

**K8 — Tilt-Shift: verworfen.** Siehe §0. Technisch wäre ein Split-Blur (crop in drei
horizontale Bänder → boxblur oben/unten → vstack) pro Frame machbar und bezahlbar — aber auf
diesem Stil ohne dramaturgischen Nutzen. Genau der Fall aus Anti-Pattern-Liste 2.2:
„Effekt, nur weil er technisch möglich ist". Der einzige verwandte Effekt, der auf Line-Art
funktioniert, ist ein **Fokus-Rahmen** (dezente Rand-Unschärfe als CLIMAX-Verstärker) — als
optionales Experiment in Phase P notiert, nicht als eigenes Kriterium.

---

## 3. Bauplan (Schritt C) — Phasen K–P, nach Hebel/Aufwand absteigend

Alle Phasen additiv und rückwärtskompatibel (Vorbild `_normalize_motion`, §20.2): alte
`plan.json` ohne die neuen Felder bleiben ladbar, fehlende Felder degradieren aufs heutige
Verhalten. Kein LLM im Render-Pfad (Resume-Determinismus). Test-Vorschläge jeweils für
`tests/test_cinematic_e2e.py` im etablierten Source-Introspection-Stil.

**Reihenfolge-Begründung:** K vor M, obwohl K Audio-Code anfasst, den M später verschiebt —
K ist der größte wahrnehmbare Sprung und der Move in M ist mechanisch (Funktion zieht mit
ihrer K-Erweiterung um). M vor N/O, weil N und O die Render-Pipeline substanziell erweitern
und nicht in einen 4.700-Zeilen-Monolithen wachsen sollen (Antwort auf Auftrags-Frage 5.6:
**ja, jetzt extrahieren — aber nach K/L, nicht davor**).

### Phase K — Echter Sound-Pool + Intensitäts-Stufen (Hebel 5 / Aufwand 2)

**Scope:** (K.1) Kuratierung echter Assets nach den bestehenden Lizenzregeln
(IMPLEMENTATION_PLAN §10: Pixabay / Freesound-nur-CC0 / Mixkit). Ziel-Pool: **3 Stufen à
2 Betten** (`calm`, `tension`, `climax` — 2 pro Stufe, damit lange Videos nicht monoton
loopen) + Ersatz der 3 Platzhalter-SFX + neu `swell_01.wav` (Klimax-Anlauf).
`CREDITS.txt`-Eintrag pro Datei ist Teil der Definition of Done. **Die Downloads macht der
Nutzer** (Lizenz-Sichtprüfung pro Datei — das ist die „nachfragen statt raten"-Stelle des
Auftrags); der Code-Teil ist unabhängig davon testbar, weil er auf Datei-Existenz prüft.
(K.2) `MUSIC_BEDS`-Mapping Stufe→Dateien, Phase→Stufe (`OPENING`→calm,
`RISING_ACTION`→tension, `CLIMAX`→climax, `RESOLUTION`→calm). (K.3) Musik-Spur als
Segment-Kette pro zusammenhängendem Phasen-Block mit `acrossfade` statt Ein-Bett-Loop —
Details §4.1. `_phase_modulate_music` bleibt als Fein-Gain obendrauf erhalten.

**Betroffene Funktionen:** `_build_final_audio`, neu `_build_music_track(scenes, render_dir)`,
Konstanten `MUSIC_BEDS`. Nebenbei: Duplikat `PHASE_VOLUME` (dashboard.py:1172) entfernen
(Ground-Truth-Fund §1).

**Definition of Done:** `final.mp4` klingt in OPENING hörbar anders als in CLIMAX
(unterschiedliches Bett, nicht nur Lautstärke); fehlt eine Stufe → Fallback auf
`neutral_bed.mp3` → fehlt auch das → reines Voiceover (bestehendes Verhalten, unverändert).

**Tests:** `t_phase_k_bed_mapping_complete` (jede Phase hat eine Stufe),
`t_phase_k_missing_tier_falls_back` (Quelltext-Check: Fallback-Kette vorhanden),
`t_phase_k_segment_durations_cover_audio` (Blockdauern + Crossfade-Kompensation = Audiodauer),
`t_phase_k_no_duplicate_phase_volume` (Regression für den Duplikat-Fix).

### Phase L — Hook-/Leitfragen-Extraktion + QA-Signal + Render-Kopplung (Hebel 4 / Aufwand 2)

**Scope:** (L.1) `analyze_script` additiv erweitern: `hook` + `throughline_question`
(Schema §4.2) — dieselbe Nie-Erfinden-Disziplin wie bei `callouts`. (L.2) QA-Signal:
`_plan_generate_worker` schreibt das Ergebnis in `plan.json`; Frontend zeigt Badge
„⚠ Kein Cold Open in Beat 0–2 erkennbar" bzw. die erkannte Leitfrage als Info. Kein
Auto-Rewrite — der Nutzer entscheidet. (L.3) Render-Kopplung: Hook-Szene bekommt
`is_hook: true` → `_motion_for_scene` wählt `snap_zoom_in`, `_transition_for_scene` erzwingt
harten Schnitt danach (kein weicher Fade aus dem Hook raus), `_build_image_prompt` bekommt
einen Hook-Style-Cue analog `PHASE_PROMPT_ADDITIONS`.

**Betroffene Funktionen:** `analyze_script`, `_assign_phases` (setzt `is_hook`),
`_motion_for_scene`, `_transition_for_scene`, `_build_image_prompt`, Frontend-Plan-Badge.

**Definition of Done:** Ein Skript mit klarem Cold Open erzeugt `plan.json` mit
`analysis.hook.beat == 0..2`; ein flach beginnendes Skript erzeugt das Warn-Badge; die
Hook-Szene hat in `plan.json` nachvollziehbar `motion.name == "snap_zoom_in"`.

**Tests:** `t_phase_l_hook_fields_in_analyze_prompt`, `t_phase_l_is_hook_motion_override`,
`t_phase_l_no_hook_no_behavior_change` (Plan ohne `hook`-Feld rendert identisch zu heute).

### Phase M — Engine-Extract: `engine_render.py` + `engine_audio.py` (Enabler)

**Scope:** Der in §32.5 bereits benannte Move, nach dem bewährten Phase-J-Muster
(`import *` + `__all__`, Lazy-Imports gegen Zyklen). `engine_render.py`: `_render_clip`,
`_assemble_clips`, `_mux_audio`, `_crossfade_clips`, `_render_selfcheck`, Motion-/Transition-
Vokabular + Selektoren, Render-Konstanten. `engine_audio.py`: `_build_final_audio`,
`_build_music_track` (aus K), `_phase_modulate_music`, `_duck_music_under_voice`,
`_place_sfx`, `_build_sfx_events`, `MUSIC_BEDS`/`SFX_FILES`. Orchestratoren
(`_render_worker` etc.) bleiben in dashboard.py.

**Definition of Done:** dashboard.py schrumpft messbar (~800 Z.), alle bestehenden 54 Tests
grün ohne Caller-Änderung.

**Tests:** `t_phase_m_render_engine_globals_intact`, `t_phase_m_audio_engine_globals_intact`
(analog `t_phase_j_engine_refactor_globals_intact`).

### Phase N — Animierte Daten-Overlays: Count-Up, Balken, Zeitleiste (Hebel 3 / Aufwand 3)

**Scope:** (N.1) `render_overlay.py` bekommt einen Sequenz-Modus: rendert für
`counter_anim` / `bar` / `timeline` eine PNG-Frame-Sequenz (Smoothstep-interpolierter Wert,
Line-Art-konforme Optik: schwarze Striche, Phase-Akzentfarbe aus `PHASE_ACCENT`). Bei 1.5 s
× 30 fps sind das 45 PNGs pro Overlay — PIL-seitig trivial. (N.2) `_render_clip` nutzt für
animierte Overlays `-framerate {fps} -i ov_%04d.png` statt `-loop 1 -i ov.png`; `overlay` +
`fade` bleiben unverändert. (N.3) `analyze_script` additiv: `data_visuals` (Schema §4.3),
strikt nur für Zahlen, die wörtlich im Beat stehen. Bestehende statische `callouts` bleiben
der Fallback für alles, was kein `data_visual` ist. **Nicht-Ziel:** Karten (§2, Kriterium 3).

**Definition of Done:** Ein Beat „3,2 Millionen Menschen verhungerten" erzeugt einen
Count-Up 0→3.2M über ~1.2 s im Ink-Stil; Pläne ohne `data_visuals` rendern exakt wie heute.

**Tests:** `t_phase_n_overlay_sequence_mode`, `t_phase_n_data_visual_prompt_never_invents`
(Prompt-Text-Check), `t_phase_n_static_callout_fallback`.

### Phase O — Wort-Akzent-Puls (Mikro-Timing, Hebel 4 / Aufwand 3)

**Scope:** Antwort auf Auftrags-Frage 5.4 — ja, machbar, konservative V1: (O.1) In
`_render_worker` werden nach `align_scenes_to_whisper` die pro Szene konsumierten Wörter
nicht mehr verworfen, sondern pro Szene wird **maximal ein** Akzent-Zeitpunkt berechnet und
additiv als `scene["accent_t"]` (Sekunden relativ zum Clip-Start) persistiert.
Betonungs-Proxy (regelbasiert, deterministisch, kein LLM): das Wort mit der **längsten
Folgepause ≥ 0.25 s** innerhalb der Szene — Sprecher pausieren nach betonten Inhalten; Zahlen
und Wörter ≥ 8 Zeichen gewinnen bei Gleichstand. Nur für Szenen mit `pacing == "punchy"`
oder `is_climax` und Dauer ≥ 2 s (calm-Szenen brauchen Bildruhe — Anti-Pattern 2.2).
(O.2) `_render_clip` addiert einen Gauß-Puls auf den Zoom-Ausdruck (Details §4.4).

**Definition of Done:** Eine punchy-Szene mit `accent_t` zeigt einen ~0.2 s
Snap-Zoom-Impuls exakt auf der Wortgrenze; Szenen ohne `accent_t` rendern byte-identisch
zum heutigen Ausdruck.

**Tests:** `t_phase_o_accent_rule_deterministic`, `t_phase_o_accent_only_punchy_or_climax`,
`t_phase_o_no_accent_expr_unchanged`, `t_phase_o_accent_zoom_bounded` (Puls-Amplitude hält
den Crop im Bild).

### Phase P — Ink-Style-Grading-Feintuning (Hebel 2–3 / Aufwand 2)

**Scope:** `PHASE_COLOR_FILTER` um stilgerechte Bausteine erweitern: Papier-Tönung
(`colorbalance` auf Mitteltöne/Lichter — kühl in OPENING, warm-gebrochen in CLIMAX) +
`vignette` nur für CLIMAX (dezent, `PI/5`-Bereich). Vorab am lokalen ffmpeg-Build
verifizieren, welche Filter kompiliert sind (Lehre aus dem `drawtext`-Fund, §18.1).
Optional-Experiment, klar als solches markiert: Fokus-Rahmen (Rand-Unschärfe via
Band-Split + boxblur) als CLIMAX-Verstärker — nur wenn der A/B-Vergleich auf echtem Material
überzeugt, sonst verwerfen.

**Definition of Done:** Screenshot-A/B OPENING vs. CLIMAX zeigt einen benennbaren
Unterschied (Papierton + Vignette), ohne dass ein Einzel-Frame „kaputt" aussieht (§26.3-Regel).

**Tests:** `t_phase_p_filter_map_all_phases`, `t_phase_p_climax_has_vignette`,
`t_phase_p_legacy_plan_identity` (Szene ohne Phase → kein Filter, wie heute).

---

## 4. Vertiefung der Top-3-Hebel (Schritt D)

### 4.1 Phase K — Musik-Segment-Kette (der eine neue ffmpeg-Baustein)

Datenmodell (`plan.json`, additiv, reine Nachvollziehbarkeit analog `transition_type`):

```json
"music_plan": [
  {"tier": "calm",    "file": "bed_calm_01.mp3",    "start": 0.0,   "end": 62.4},
  {"tier": "tension", "file": "bed_tension_01.mp3", "start": 62.4,  "end": 205.4},
  {"tier": "climax",  "file": "bed_climax_02.mp3",  "start": 205.4, "end": 293.6}
]
```

Blöcke = zusammenhängende Szenen gleicher Stufe (Phase→Stufe-Mapping aus K.2), Grenzen aus
`start_aligned`/`end_aligned` wie in `_phase_modulate_music`. Bett-Auswahl pro Block
deterministisch: `MUSIC_BEDS[tier][block_index % len]` — dasselbe Muster wie
`_motion_for_scene`. **Crossfade-Kompensation wie beim Video-xfade** (§15/`_crossfade_clips`):
jedes Segment außer dem letzten wird um `XFADE_D` länger getrimmt, `acrossfade` konsumiert
genau diesen Überlapp — die Gesamtlänge bleibt exakt die Audiodauer:

```
ffmpeg -y \
  -stream_loop -1 -i assets/music/bed_calm_01.mp3 \
  -stream_loop -1 -i assets/music/bed_tension_01.mp3 \
  -stream_loop -1 -i assets/music/bed_climax_02.mp3 \
  -filter_complex "\
    [0:a]atrim=0:64.4,asetpts=PTS-STARTPTS[b0];\
    [1:a]atrim=0:145.0,asetpts=PTS-STARTPTS[b1];\
    [2:a]atrim=0:88.2,asetpts=PTS-STARTPTS[b2];\
    [b0][b1]acrossfade=d=2.0:c1=tri:c2=tri[x1];\
    [x1][b2]acrossfade=d=2.0:c1=tri:c2=tri[a]" \
  -map "[a]" render_tmp/_music_track.mp3
```

Einbau in `_build_final_audio` **vor** `_phase_modulate_music` — der bestehende
Phase-Volume-Envelope und `sidechaincompress` bleiben unverändert und wirken jetzt auf
Material, das die Differenzierung tatsächlich hörbar macht. Fallback-Kette:
`MUSIC_BEDS[tier]` leer → `neutral_bed.mp3` für diesen Block → gar keine Musik-Assets →
reines Voiceover (heutiges Verhalten, Zeile 2498 ff. unverändert).

**Kurations-Vorschlag (Nutzer-Aufgabe, Lizenz pro Datei prüfen):** 6 Betten — calm: 2×
ruhiges Piano/Pad ohne Percussion; tension: 2× Puls/Ostinato mit leiser Percussion; climax:
2× volles Arrangement mit Drums. Kriterien: loopbar (kein hartes Intro/Outro), ≥ 90 s,
gleiche Tonart-Familie pro Video nicht nötig (Crossfade 2 s + Ducking maskiert
Tonart-Sprünge ausreichend; wer es sauberer will, kuratiert alle 6 in a-Moll-Umfeld). SFX:
whoosh/impact/riser durch echte ersetzen + `swell` neu.

### 4.2 Phase L — Hook/Leitfrage: Analyse-Schema + Kopplung

`analyze_script`-Erweiterung (additiv im bestehenden JSON-Objekt, Prompt-Ergänzung nach dem
DRAMATURGY-Block):

```json
"hook": {"beat": 0, "type": "quote" | "scene" | "thesis" | "none", "strength": "strong" | "weak"},
"throughline_question": "Wie konnte ein 13-jähriges Mädchen …?"
```

Prompt-Regel im Stil der CALLOUTS-Regel: *„hook.type='none' wenn die Beats 0–2 mit
Kontext/Definitionen statt einer konkreten Person, Szene, Zahl oder These beginnen. NIEMALS
eine Leitfrage erfinden, die das Skript nicht trägt — leerer String ist valide."*
Szenen-Feld in `_assign_phases`: `is_hook = (beat == hook.beat and strength == "strong")`.
Kopplung (alle drei Stellen sind Ein-Zeilen-Prioritäten vor der bestehenden Auswahl):

- `_motion_for_scene`: `if scene.get("is_hook"): return _build_motion("snap_zoom_in", 1.2)`
- `_transition_for_scene` der Folgeszene: hard cut erzwingen (Hook endet auf Schnitt, nicht Fade)
- `_build_image_prompt`: `HOOK_PROMPT_ADDITION` („single striking focal subject, maximum
  negative space, poster-like composition") analog `PHASE_PROMPT_ADDITIONS` in
  `engine_elevenlabs.py`

QA-Rückkanal: `plan.json` → bestehendes Plan-Status-Polling → Badge im Stepper (Step
„Skript"). Kein neuer Endpoint nötig.

### 4.3 Phase N — Datenmodell Daten-Overlays

```json
"data_visuals": [
  {"beat": 14, "kind": "counter", "from": 0, "to": 3200000, "format": "3,2 Mio.", "label": "verhungert 1994–1998"},
  {"beat": 22, "kind": "timeline", "points": [{"t": "1994", "label": "Hungersnot"}, {"t": "2007", "label": "Flucht"}], "highlight": 1}
]
```

Szenen-Feld: `s["data_visual"]` (vom Plan-Worker zugeordnet wie `callout`).
`_overlay_specs_for_scene` prüft `data_visual` **vor** `callout` (Callout bleibt Fallback).
Render: `render_overlay.py --sequence`-Modus schreibt `ov_%04d.png` in ein Temp-Verzeichnis;
in `_render_clip` ersetzt für diesen Overlay-Input `-framerate 30 -i {dir}/ov_%04d.png` das
bisherige `-loop 1 -i {png}` — Rest des Filtergraphen (fade/overlay/enable) unverändert.
Wert-Interpolation im PIL-Skript mit derselben Smoothstep-Formel wie `_render_clip`
(3t²−2t³), damit Zahlen-Animation und Kamera dieselbe Bewegungssprache sprechen.

### 4.4 Phase O — Akzent-Puls: der konkrete zoompan-Ausdruck

Heute (dashboard.py:2108 f.):

```python
smoothstep = f"(3*pow(on/{frames},2)-2*pow(on/{frames},3))"
z_expr = f"{z0}+({z1}-{z0})*{smoothstep}"
```

Neu, nur wenn `accent_t` gesetzt (sonst byte-identischer Ausdruck — Rückwärtskompatibilität
per Konstruktion):

```python
f_a   = round(accent_t * fps)          # Akzent-Frame
sigma = max(2, round(0.06 * fps))      # ~0.2s sichtbare Puls-Breite
amp   = 0.05                           # +5% Zoom auf dem Peak — unter der 1.2x-Jitter-Grenze
z_expr = (f"{z0}+({z1}-{z0})*{smoothstep}"
          f"+{amp}*exp(-pow((on-{f_a})/{sigma},2))")
```

Der Gauß-Term ist auf beiden Seiten glatt (kein Knick → kein Ruckeln), symmetrisch
(Zoom-in + Zoom-out in einem), und addiert maximal `amp` auf `z` — mit `z1 ≤ 1.25`
(MOTION_LIBRARY-Maximum) bleibt der Crop sicher im Bild. Fokus-Ausdrücke unverändert.
Persistenz: `accent_t` wird im bestehenden Plan-Write-Back des `_render_worker`
(Zeile 2689 ff.) mitgeschrieben, damit ein Resume-Render denselben Puls reproduziert.

---

## 5. Warum ich das kontinuierliche Scoring-Modell (Auftrag §2.1) nicht baue

Die vier vorgeschlagenen Scores (Spannung, Informationsdichte, emotionale Intensität,
visuelle Neuheit) lösen Probleme, die nach dieser Analyse anders gelöst sind oder (noch)
nicht nachweisbar existieren: Musik-„Atmung" löst Phase K über Material-Stufen; Mikro-Rhythmus
löst Phase O über Wort-Timestamps — beides orthogonal zum Phasen-Modell. Visuelle Neuheit ist
durch `i % len(candidates)` + Sequenz-Vererbung bereits gegen direkte Wiederholung geschützt.
Informationsdichte ist der einzige Score mit eigenständigem Wert (dichte Passagen → mehr
Bildruhe), aber `pacing: calm` ist heute genau dieser Proxy. Vier kontinuierliche
LLM-Scores pro Szene wären zudem vier neue nicht-deterministische Eingänge in einen bewusst
deterministischen Render-Pfad. **Watch-Item statt Bauauftrag:** Wenn nach K+O ein konkretes
Video zeigt, dass zwei gleichphasige Szenen systematisch falsch behandelt werden, ist das der
Beleg, den §2.1 selbst als Voraussetzung nennt — vorher nicht.

---

## 6. Post-Render-Review-Checkliste (Schritt E)

Manuelle Checkliste für den Nutzer nach jedem Produktions-Render — Ergänzung zum technischen
`_render_selfcheck`, bewusst **kein** LLM-Call im Render-Pfad. Prüfzeit: ~10 Min. pro Video.

1. **Hook-Test (0:00–0:05):** Würdest du beim Scrollen stoppen? Steht in den ersten 5 s eine
   Person, Szene, Zahl oder These — oder Kontext-Erklärung?
2. **Leitfrage (bis 1:00):** Kannst du nach der ersten Minute die Frage des Videos in einem
   Satz sagen?
3. **Slideshow-Radar:** 3 zufällige 30-s-Abschnitte — bewegt sich in jedem etwas *außer* der
   gleichförmigen Kamerafahrt? Haben 3 aufeinanderfolgende Szenen dieselbe Bewegungsrichtung?
4. **Schnitt-Stichprobe:** 5 zufällige Schnitte bei 0.5× Geschwindigkeit — sitzt der Schnitt
   auf einer Wort-/Satzgrenze oder mitten im Wort?
5. **Musik-Bogen:** Augen zu, nur hören: Kannst du OPENING/CLIMAX/RESOLUTION am Klang
   unterscheiden? Gibt es irgendwo > 5 s echte Stille, die nicht dramaturgisch gewollt ist?
6. **SFX-Disziplin:** Whooshes nur an echten Wechseln? Kein Impact-Spam in ruhigen Passagen?
7. **Grading-A/B:** Screenshot aus OPENING neben Screenshot aus CLIMAX — benennbarer
   Unterschied (Ton, Kontrast, Vignette), ohne dass einer „defekt" wirkt?
8. **Stil-Konsistenz:** 5 zufällige Frames nebeneinander — dieselbe Hand, Linienstärke,
   Weißgrad? (Ausreißer → Szene mit angepasstem Prompt neu generieren.)
9. **Klimax-Probe:** Ist der `climax_beat`-Moment auch ohne Ton als Höhepunkt erkennbar —
   und mit Ton doppelt?
10. **Ende:** Letztes gesprochenes Wort vs. letztes Bild — kein abgeschnittenes Wort, kein
    eingefrorenes Standbild > 2 s nach Sprechende.

Optional, klar als **experimentell / nur manuell auslösbar** markiert (nie Teil des
Render-Pfads): ein LLM-Scoring-Call, der 6–8 Frame-Grabs + das Transkript gegen diese 10
Punkte bewertet — als eigener Button, mit sichtbaren Kosten, Ergebnis rein informativ.

---

## 7. Zusammenfassung der Entscheidungen (wer hier entschieden hat und warum)

1. **Sound-Material vor allem anderen** (Phase K) — größter Hebel, kleinster Aufwand, die
   gesamte Infrastruktur wartet seit Phase G darauf.
2. **Hook/Leitfrage als Verifikations- und Kopplungs-Problem** (Phase L), nicht als
   Generierungs-Problem — `SCRIPT_SYSTEM` kann es schon, nur geprüft und gerendert wird es nicht.
3. **Extract jetzt, aber nach K/L** (Phase M) — Antwort auf Auftrags-Frage 5.6.
4. **Tilt-Shift verworfen** — Stil-Mismatch mit Line-Art; Fokus-Rahmen als P-Experiment.
5. **Karten verworfen, Charts/Counter/Zeitleisten ja** (Phase N) — Geodaten wären ein
   Framework durch die Hintertür; Ink-Karten sind Bilder, keine Renderings.
6. **Kein kontinuierliches Scoring-Modell** — Watch-Item mit klarem Auslöse-Kriterium (§5).
7. **Kriterium 7 (Übergänge) ist fertig** — keine Phase, kein Selbstzweck-Polish.

---

## 8. Integration mit der Produkt-Roadmap (Nutzer-TODO, ergänzt 2026-07-07)

Dieser Abschnitt verzahnt die bestehende Produkt-Roadmap des Nutzers (UI-Rebuild 33.x,
Description-Generator 37.x, Asset-Phasen 8.x, Multi-Speaker 9.x, Engine-Refactor J.2–J.4)
mit den Phasen K–P aus §3. **§1–§7 bleiben unverändert gültig** — hier steht nur, was wie
zusammenpasst und in welcher Reihenfolge beide Stränge laufen sollten.

### 8.1 Stand-Korrekturen (gegen Code/Git verifiziert, 2026-07-07)

Der in die TODO eingeflossene Review-Text ist an drei Stellen veraltet:

- **PR 2 (33.4.2 Thema-Card + Script-First Flow) ist bereits gemerged** — HEAD auf `main`
  ist `413456b`, der Test `t_phase33_4_2_thema_card_restructured` existiert und ist grün.
  → Von der TODO streichen; die Zeilennummern in diesem Dokument beziehen sich auf genau
  diesen Stand (`413456b`, nicht `b35bb51` wie im Kopf vermerkt — `b35bb51` war der
  Eltern-Commit).
- **Testzahl: 54, nicht 53** (der PR-2-Test kam hinzu; frisch ausgeführt: 54/54 grün).
- **`produceCard`-Beobachtung bestätigt:** kein `data-step-section`-Attribut, lebt außerhalb
  der Step-Visibility. Entscheidung („gehört sie zu einem Step?") wird in 33.5
  (Live-Preview) fällig — bis dahin bewusst so lassen, sie ist als Step-übergreifender
  Orchestrator korrekt modelliert.

### 8.2 Überlappungs-Auflösung: Roadmap-Item ↔ Phase K–P

| Roadmap-Item (Nutzer) | Dieses Dokument | Verhältnis / Entscheidung |
|---|---|---|
| **Phase 8.x Pixabay + Asset-Curation** (8.1 Pool-Struktur, 8.2 Sound-Variant-Datenmodell, 8.3 Deterministic-Picker, 8.4 Audit-UI, 8.5 CREDITS-Auto-Generator, 8.6 Pixabay-API, 8.7 4-Stem-Crossfade) | **Phase K** | Kein Konflikt — **K ist der manuelle Schnellpfad, 8.x die spätere Automatisierungs-Ausbaustufe.** K liefert in ~1 Tag: 6 handkuratierte Betten + 4 SFX + `MUSIC_BEDS` + Segment-Kette (§4.1). 8.2/8.3/8.7 bauen exakt auf `MUSIC_BEDS`/`music_plan` auf (8.3 = das `block_index % len`-Muster aus §4.1, ist in K schon drin; 8.7 = Verallgemeinerung der K-Segment-Kette auf 4 parallele Stems). Nichts aus K wird weggeworfen. 8.6 (Pixabay-API): mit stdlib-`urllib` Zero-Framework-konform, aber die **Lizenz-Sichtprüfung pro Datei bleibt beim Nutzer** — 8.5 füllt CREDITS-Felder automatisch aus, ersetzt die Prüfung nicht. Reihenfolge: **K zuerst, 8.x erst wenn der manuelle Pool nachweislich zu klein wird.** |
| **Engine-Refactor J.2 (`engine_render.py`) + J.3 (`engine_audio.py`)** | **Phase M** | Identisch — M **ist** J.2+J.3 in einem PR (Begründung für den Zeitpunkt: §3, „nach K/L, vor N/O"). J.4 (`engine_scenes.py`) ist zusätzlich und danach ein eigener, isolierter Move (Kandidaten: `split_units`, `segment_by_pacing`, `analyze_script`, `_assign_phases`, `visual_prompts`). |
| **Phase 9.x Multi-Speaker** | orthogonal | Kein Konflikt mit K–P. Einziger Berührungspunkt: Phase O nutzt Wort-Timestamps — bei Multi-Speaker-Audio müssen die Timestamps sprecherübergreifend eine Liste bleiben (sind sie bei ElevenLabs/Whisper ohnehin). Keine Abhängigkeit in beide Richtungen. |
| **Phase 37.x Description-Generator** | orthogonal, ein Synergie-Punkt | 37.7 (Timestamp-Generator aus `plan.json`) sollte `act_breaks`/`card_title` als Kapitelmarken lesen — existiert schon. **Nach Phase L zusätzlich `throughline_question` als Description-Hook nutzen** (die Leitfrage ist die beste erste Description-Zeile). 37.1–37.6/37.8 unberührt. |
| **35 Test-Coverage** | zahlt aufeinander ein | Die Test-Vorschläge der Phasen K–P (je 3–4 Tests, §3) sind Teil des 35-Ziels — bei der 35-Planung nicht doppelt einplanen. |
| **36 ARCHITECTURE-Lint** | Seed vorhanden | Die Ground-Truth-Tabelle aus §1 ist die erste Lint-Regelliste. Der `PHASE_VOLUME`-Duplikat-Fund (§1, Zeile 4) ist genau die Fund-Klasse, die 36 automatisieren soll — als ersten Lint-Testfall verwenden. |
| **33.4.3 / 33.5 / 33.6 / PR 3 (37.1)** | orthogonal (UI/Feature-Track) | Kein Berührungspunkt mit K–P außer dem Plan-Badge aus L.2 (ein Badge im Skript-Step — trivial, kollidiert nicht mit 33.5-Live-Preview, kann aber gemeinsam mit ihr gebaut werden, wenn 33.5 zuerst drankommt). |

### 8.3 Kombinierte Reihenfolge (Empfehlung — zwei Tracks, minimale Kollision)

Oberstes Ziel laut Nutzer: **das System wirklich perfekt hinbekommen.** Daraus folgt: Der
UI-Track verbessert die Bedienung, der Cinematic-Track (K–P) verbessert **jedes gerenderte
Video** — letzterer darf nicht hinter UI-Politur zurückfallen. K/L sind backend-only und
kollidieren mit keinem UI-PR; sie können interleaved laufen.

```
SOFORT (vor jedem weiteren PR):
  0. Visuelle Verifikation des Wizard-Umbaus   ~10 min
     (Checkliste aus dem Review: 1 Card pro Step, Scroll, Ring-Highlight,
      Mobile-Drawer, 0 Console-Errors — PR 1 UND PR 2 sind ja schon auf main,
      d.h. es wird der KOMBINIERTE Stand verifiziert, nicht nur PR 1)
  0b. K.1-Kuration anstoßen (Nutzer-Aufgabe, §4.1: 6 Betten + 4 SFX)
      — läuft parallel zu allem, blockiert nichts, gated Phase K

WOCHE 1 (interleaved):
  1. PR 3 (37.1 Description-Gen Backend+UI)    ~3h     [UI-Track]
  2. Phase K (Code: MUSIC_BEDS + Segment-Kette) ~3h    [Cinematic — sobald Assets da]
  3. Phase L (Hook/Leitfrage + Badge)           ~3h    [Cinematic]
  4. 33.4.3 Reset-Buttons                       ~1h    [UI-Track]

WOCHE 2:
  5. Phase M (= J.2 + J.3 Engine-Extract)       ~1 Tag [Enabler — VOR N/O]
  6. 33.5 Live-Preview (+ L-Badge-Platzierung)  ~3h    [UI-Track]
  7. Phase O (Wort-Akzent-Puls)                 ~4h    [Cinematic]
  8. 33.6 Keyboard-Nav                          ~2h    [UI-Track]

WOCHE 3:
  9. Phase N (animierte Daten-Overlays)         ~1 Tag [Cinematic]
 10. Phase P (Ink-Grading)                      ~3h    [Cinematic]
 11. 35 Test-Coverage (inkl. K–P-Tests)         ~2 Tage
 12. 36 ARCHITECTURE-Lint (Seed: §1-Tabelle)    ~3h

DANACH (Ausbaustufen, in dieser Reihenfolge):
 13. Phase 8.x (Pixabay-Automatisierung — nur falls manueller K-Pool zu klein)
 14. J.4 engine_scenes.py
 15. Phase 9.x Multi-Speaker
 16. Phase 37.2–37.8 Description-Vertiefung
 17. Nice-to-have: 40 GPU-Whisper / 41 Cloud-Storage / 42 Embedding-Cache / 43 i18n
```

**Erster Qualitäts-Meilenstein:** Nach Schritt 3 (K+L fertig) ein echtes Produktions-Video
rendern und gegen die Checkliste aus §6 reviewen — **bevor** N/O/P gebaut werden. Wenn K+L
den wahrgenommenen Sprung liefern, den §0 vorhersagt, bestätigt das die Priorisierung; wenn
nicht, ist das der Moment, N/O/P neu zu gewichten statt blind weiterzubauen.

### 8.4 Handoff-Regeln für die Coding-IDE

Wer dieses Dokument als Bauauftrag ausführt (IDE-Agent oder Mensch), hält sich an:

1. **Vor jedem PR und nach jedem Merge:** `python3 tests/test_cinematic_e2e.py` — 54/54
   (bzw. + neue Tests) ist die Baseline, kein PR ohne grüne Suite.
2. **Zeilennummern in diesem Dokument gelten für Commit `413456b`.** Nach jedem Merge neu
   greppen (Funktionsnamen sind die stabilen Anker), nie blind auf Zeilennummern patchen.
3. **Harte Constraints aus dem Original-Auftrag gelten für jede Phase:** Zero-Framework
   (nur Stdlib + ffmpeg-subprocess), additive `plan.json`-Felder, kein LLM im Render-Pfad,
   Background-Thread + Status-Dict + `_start`/`_status`-Polling für Langlaufendes,
   Sound-Lizenzen nur Pixabay / Freesound-CC0 / Mixkit mit CREDITS-Eintrag.
4. **Ein PR = eine Phase** (K, L, M, …) im etablierten Format: Code + Tests +
   ARCHITECTURE.md-Abschnitt + ggf. IMPLEMENTATION_PLAN-Statuszeile. Die
   Definition-of-Done-Blöcke aus §3 sind die Abnahmekriterien.
5. **Phase K gated auf Assets:** Der Code-Teil ist ohne echte Betten baubar und testbar
   (Datei-Existenz-Fallbacks), aber die DoD („OPENING klingt anders als CLIMAX") ist erst
   mit echten Assets abnehmbar. Nicht als „fertig" melden, solange `CREDITS.txt` leer ist.
6. **Bei Abweichung vom Plan** (anderer Ansatz besser, Annahme stellt sich als falsch
   heraus): abweichen ist erlaubt, aber dokumentieren — kurzer Absatz im PR + Ergänzung
   hier in §8, nicht stillschweigend.

---

## 9. UI/UX-Konzept (Nutzer-Feedback 2026-07-07, Screenshot-Review + Code-Verifikation)

Grundlage: Nutzer-Feedback zum aktuellen Wizard-Stand (Screenshot ①Thema-Step) + Verifikation
jedes Kritikpunkts am Code. Ergänzt §8 um vier neue Arbeitspakete: **33.7** (Theme + Stepper),
**33.8** (Einstiegs-Flow), **Bug-Ticket B-1** (Charakter-Upload), **Phase 38**
(Preset-Prompt-Library) und **Phase 39** (T2V raus / I2V rein).

### 9.1 Befunde (jeder Punkt am Code verifiziert, nicht nur am Screenshot)

| # | Befund | Code-Beleg | Schwere |
|---|---|---|---|
| F1 | **Stepper unlesbar:** inaktive Steps sind heller Text auf hell-lila Hintergrund — WCAG-AA-Fail; horizontale Anordnung quetscht 5 Steps + Untertitel in eine Zeile | dashboard.html Stepper-Markup (Phase 33.2) | hoch |
| F2 | **Lila als Grundfarbe** ist ein hart codiertes Token, kein Zufall: `'app-acc': '#4f46e5'` (dashboard.html:32) + `--acc:#4f46e5` (Z. 50). Nutzer lehnt Lila explizit ab | dashboard.html:32/50 | hoch (Geschmack), trivial (Fix — zwei Token-Stellen) |
| F3 | **Brand-Farbe hat keine Funktion in der Pipeline** — sie färbt ausschließlich den Sidebar-Akzent. Ohne gesetzten Wert generiert `nameToHsl(ch.name)` eine **zufällige** HSL-Farbe aus dem Kanalnamen (dashboard.html:751) — das ist die „random Farbe beim Anlegen". Sie beeinflusst weder Bilder noch Render noch Prompts | dashboard.html:748–765, dashboard.py:3860 | mittel (Verwirrung) |
| F4 | **Einstieg landet in Kanal-Settings** statt in der Arbeit (Video-Liste / letztes Video) — der erste Klick jedes Besuchs ist Verwaltung, nicht Produktion | Frontend-Flow (Phase 33.3) | mittel |
| F5 | **Charakter-Referenz-Upload defekt** (Nutzer-Report). Endpoint existiert (`/api/upload_charref`, dashboard.py:4301) — Runtime-Bug, statisch nicht final diagnostizierbar → Bug-Ticket B-1 mit Repro-Anleitung unten | dashboard.py:4301 ff. | hoch (Kernfeature Charakter-Konsistenz) |
| F6 | **Doppelte/verstreute Farbwahl:** Brand-Color-Picker im Settings-Modal (dashboard.html:272 ff.) + Farb-Ableitung beim Anlegen — zwei Stellen für eine funktionslose Entscheidung | dashboard.html:272, 748 | niedrig |
| F7 | **Default-Master-Prompts sind Minimal-Platzhalter:** `IMAGE_MASTER_DEFAULT` (dashboard.py:245) beschreibt ein karges Stick-Figure ohne Kompositions-/Safety-Regeln. Neue Kanäle starten mit dem schwächsten denkbaren Stil | dashboard.py:245–268 | hoch (Output-Qualität neuer Kanäle) |
| F8 | **T2V-Modus-Toggle prominent im ①Thema-Step,** obwohl die gesamte Cinematic-Pipeline (Phasen A–P) Bild-first ist. Wichtig: `gen_veo()` akzeptiert bereits `image_urls` (dashboard.py:3054) — **der I2V-Pfad existiert im Backend schon**, nur der UI-Modus zeigt T2V | dashboard.py:3054, UI ①Thema | mittel |

### 9.2 Ziel-Konzept: neutrale Werkstatt-UI statt SaaS-Lila

**Design-These:** Die UI ist die Werkstatt, das Video ist das Werkstück — die Werkstatt
bleibt neutral und konkurriert farblich nie mit den Video-Frames im Preview. Das löst
F1/F2/F3/F6 mit einem Konzept:

- **Farbsystem (ersetzt die Lila-Token, zwei Stellen: dashboard.html:32 + :50):**
  Papierweiß `#fafaf9` (Flächen), Tiefschwarz `#111827` (Text/aktive Elemente), Graustufen
  für Sekundäres. **Ein** Akzent, sparsam: das Tusche-Rot `#c13838` — bewusst die bereits
  existierende `PHASE_ACCENT["CLIMAX"]`-Farbe (engine_elevenlabs.py:103): die App hat schon
  eine Farbidentität, sie nutzt sie nur nicht in der UI. (Alternative siehe §10.5 Punkt 5.)
  Kein Lila mehr, nirgends.
- **Kontrast-Regel (fixt F1):** Jede Text/Hintergrund-Kombination ≥ 4.5:1 (WCAG AA).
  Inaktive Steps: dunkelgrauer Text auf Papierweiß mit grauem Kreis — nie heller Text auf
  hellem Grund. Aktiver Step: schwarzer Kreis, weiße Ziffer. Erledigt: ✓ statt Ziffer.
- **Stepper vertikal in die Sidebar (fixt F1-Layout):** Die 5 Steps untereinander in der
  bestehenden Sidebar — pro Zeile: Status-Kreis, Label, einzeilige Substatus-Info
  („3/12 Bilder", „TTS fertig 4:32"). Damit übernimmt der Stepper die Status-Pills, die
  33.5 (Live-Preview) geplant hätte — **33.7 und 33.5 zusammen planen**, sonst wird
  Fortschritt zweimal gebaut. Hauptspalte rechts: nur die aktive Step-Card (die zentrale
  `updateStepVisibility()` aus 33.4.2-prep bleibt unverändert nutzbar — nur der
  Klick-Geber wandert von der Top-Leiste in die Sidebar). Mobile: kompakte horizontale
  Leiste oben (nur Kreise), Sidebar bleibt Drawer.
- **Einstiegs-Flow (fixt F4):** App-Start = Video-Liste des zuletzt aktiven Kanals mit
  „Weiterarbeiten an ‚…'"-Karte ganz oben (letztes Video + sein aktueller Step). Kanal-
  Settings ausschließlich hinter dem Zahnrad. Erst-Start (kein Kanal): Mini-Onboarding mit
  genau zwei Pflichtfeldern — Kanalname + Stil-Preset (Dropdown aus Phase 38) — **keine
  Farbwahl im Pflichtpfad**.
- **Brand-Farbe entschärfen (fixt F3/F6):** `nameToHsl`-Zufallsfallback ersetzen durch
  neutrales Graphit; Picker bleibt als rein optionales Settings-Feld mit ehrlichem Label
  („Sidebar-Akzentfarbe — rein kosmetisch"). Keine zweite Farbwahl-Stelle.

**Arbeitspakete:** **33.7** = Farbsystem + Kontrast + vertikaler Stepper (~4h, ersetzt
Teile von 33.5-Scope). **33.8** = Einstiegs-Flow + Onboarding + Brand-Color-Entschärfung
(~2h). Tests: `t_phase33_7_no_purple_tokens` (kein `#4f46e5`/`#4338ca` mehr im HTML),
`t_phase33_7_stepper_vertical_in_sidebar`, `t_phase33_8_default_view_is_videos`,
`t_phase33_8_no_color_in_onboarding`.

### 9.3 Bug-Ticket B-1 — Charakter-Referenz-Upload (SOFORT, vor allen UI-Phasen)

**Symptom (Nutzer):** Upload der Charakter-Bild-Referenz schlägt fehl. **Verdachtspunkte
für die Diagnose** (in dieser Reihenfolge prüfen): (1) Browser-Netzwerk-Tab: was antwortet
`POST /api/upload_charref` — 4xx/5xx oder 200 mit leerem Effekt? (2) Multipart-/Base64-
Parsing im Handler (dashboard.py:4301 ff.) gegen das tatsächlich gesendete Frontend-Format.
(3) Schreibpfad `charsheets/`-Verzeichnis + `char_ref_url.txt` (dashboard.py:149 f.) —
existiert das Verzeichnis bei frischen Kanälen? (4) Silent-Fail im Frontend: Fehler-Response
muss als Toast sichtbar werden, nie stumm verschluckt. **Akzeptanzkriterium:** PNG/JPG
hochladen → erscheint sofort als Referenz-Kachel → nächster Batch-Gen hängt sie nachweislich
an (`char_ref_applied: true` in `plan.json`, dashboard.py:1745). Plus Regressionstest
`t_bug_b1_charref_upload_roundtrip`.

### 9.4 Phase 38 — Preset-Prompt-Library (fixt F7; Hebel 4 / Aufwand 2)

Neue Kanäle (und der ①Thema-Step) bekommen ein **Stil-Preset-Dropdown** statt des kargen
Defaults. Backend: Konstante `PRESET_MASTERS: dict[str, str]`; bei Kanal-Anlage wird das
gewählte Preset nach `channels/<cid>/master_prompt.txt` kopiert (bestehender Mechanismus,
`write_master`) — danach frei editierbar, kein neues Datenmodell. **Das Default-Preset ist
`flat_cartoon_doc` (§10.4 — der tatsächliche Ziel-Stil).** Die folgenden drei Presets sind
Alternativen für andere Kanal-Typen (Universal-Tool), ebenfalls fertig vorformuliert —
1:1 so implementieren (Englisch, weil die Bildmodelle darauf am zuverlässigsten reagieren):

**Preset — `ink_documentary` (schwarz-weiße Tusche, für Kanäle die diesen Look wollen):**

```
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
```

**Preset — `charcoal_noir` (True Crime / düstere Stoffe):**

```
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
```

**Preset — `editorial_minimal` (Erklär-/Essay-Formate, Daten-freundlich):**

```
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
```

Der bestehende `IMAGE_MASTER_DEFAULT` (Stick-Figure) bleibt als Legacy-Preset
`stick_minimal` erhalten (Rückwärtskompatibilität — bestehende Kanäle behalten ihren Text
ohnehin, `master_prompt.txt` wird nie überschrieben). **Definition of Done:** Neuer Kanal →
Dropdown mit 5 Presets + Kurzbeschreibung → Auswahl füllt den Master-Prompt-Editor vor →
Nutzer passt höchstens Details an. **Tests:** `t_phase38_preset_masters_dict`,
`t_phase38_new_channel_gets_preset`, `t_phase38_existing_master_never_overwritten`,
`t_phase38_presets_contain_safety_rules` (Symbolik-Absatz in jedem Preset vorhanden).

### 9.5 Phase 39 — T2V-Modus entfernen, I2V-Pfad an die Standard-Pipeline (fixt F8)

**Entscheidung:** Der Modus-Toggle „Video (T2V)" verschwindet aus ①Thema — die gesamte
Cinematic-Pipeline (Phasen A–P, Sync-Invariante, Phase-Engine, Sound-Design) ist Bild-first;
ein paralleler T2V-Pfad, der all das umgeht, verwirrt und produziert schlechtere Videos.
**Bewegtbild kommt stattdessen als I2V-Veredelung EINZELNER Szenen** in die Standard-
Pipeline: (39.1) `mode`-Feld und Veo-Backend bleiben unangetastet (Rückwärtskompatibilität,
alte Video-Modus-Projekte bleiben ladbar) — nur der Toggle in der UI entfällt. (39.2) Pro
Szene im Szenen-Grid ein „🎬 Animieren (I2V)"-Button: ruft das **bereits existierende**
`gen_veo(video_prompt, image_urls=[<generiertes Szenenbild>])` (dashboard.py:3054) mit dem
Szenenbild als Start-Frame auf — kein neuer API-Client nötig. Ergebnis additiv als
`scene["i2v_file"]`. (39.3) `_render_worker` bevorzugt `i2v_file` vor dem Ken-Burns-Clip
(auf Ziel-fps/-Auflösung normalisiert, `_frames`-Budget der Szene bleibt bindend — die
Sync-Invariante gilt unverändert). Empfehlung: sparsam einsetzen — 2–4 animierte
Schlüsselszenen (Hook, Klimax) pro Video, nicht flächendeckend (Kosten + Stil-Konsistenz).
**Definition of Done:** Toggle weg; eine Szene mit `i2v_file` landet als Bewegtbild im
`final.mp4`, alle anderen rendern unverändert; Plan ohne `i2v_file` rendert byte-identisch
zu heute. **Tests:** `t_phase39_no_t2v_toggle_in_html`, `t_phase39_i2v_file_preferred`,
`t_phase39_legacy_video_mode_still_loads`.

### 9.6 Einordnung in die §8.3-Reihenfolge (Ergänzung, ändert die bestehende Liste nicht)

- **B-1 (Charakter-Upload-Bug): sofort**, noch vor der visuellen Verifikation — ein kaputtes
  Kernfeature schlägt jede neue Funktion.
- **33.7 (Theme + vertikaler Stepper): vor 33.5** einplanen und beide zusammen entwerfen
  (der Stepper übernimmt die Status-Anzeige, die 33.5 sonst separat gebaut hätte).
- **33.8 (Einstiegs-Flow) + Phase 38 (Presets):** direkt nach 33.7 — beides kleine,
  in sich geschlossene PRs mit sofort spürbarem Effekt.
- **Phase 39 (T2V→I2V):** nach dem ersten Qualitäts-Meilenstein (§8.3) — I2V ist Veredelung
  und lohnt erst, wenn Sound (K) und Hook (L) sitzen.

---

## 10. Grafikstil — Ist-Analyse, Evaluation & Kodifizierung (Nutzer-Klarstellung 2026-07-07)

**Klarstellung des Nutzers:** `yeonmi_storyboard/` stammt aus dem allerersten Video-Versuch
und hat mit dem Dashboard nichts mehr zu tun; der Weiß-Line-Art-Stil aus `STYLE_GUIDE.md`
ist nicht mehr aktuell. Das Dashboard soll ein **Universal-Tool** sein: Charakter-Konsistenz
über Referenzbilder, Grafikstil über den Master-Prompt **des jeweiligen Kanals**. Analysiert
wurden die ~50 Referenz-Frames in `assets/*.jpg` (`0-00` … `1-34`) sowie die tatsächliche
Pipeline-Ausgabe (`channels/default/videos/video_1/generated/`).

### 10.1 Befund: Es gibt DREI Stil-Schichten im Repo — nur eine ist gewollt

| Schicht | Wo sie lebt | Status |
|---|---|---|
| **Yeonmi-Ink-Stil** (schwarz-weiße Tusche-Line-Art) | `STYLE_GUIDE.md`, `gen.py` (`MASTER`), `scenes.tsv`, `run_batch.sh`, `yeonmi_storyboard/` | **Legacy** — erster Video-Versuch, vom Dashboard entkoppelt, aber dokumentarisch so präsent, dass er jede Analyse fehlleitet (hat auch §0/§2/§9 dieses Dokuments anfangs fehlkalibriert — Korrekturen in §10.5) |
| **Stick-Figure-Default** (`IMAGE_MASTER_DEFAULT`, dashboard.py:245) | Aktiv in der Pipeline: **kein einziger Kanal hat einen `master_prompt.txt`** (verifiziert per find über `channels/`), beide Kanäle fallen auf den Default zurück. Das generierte `000.jpg` ist wörtlich ein Strichmännchen auf Weiß | **Aktiv, aber ungewollt** — der schwächste denkbare Output |
| **Ziel-Stil** (flacher 2D-Cartoon, siehe 10.2) | Ausschließlich als ~50 JPG-Referenzframes in `assets/` — **in keinem Prompt, keiner Konstante, keinem Dokument kodifiziert** | **Gewollt, aber nirgendwo aufgeschrieben** |

Das ist der eigentliche Stil-Gap: nicht „falscher Stil im Prompt", sondern **der gewollte
Stil existiert nur als Bilder**. Jeder neue Kanal, jedes neue Video startet beim
Strichmännchen.

### 10.2 Ziel-Stil-Analyse (aus den Referenz-Frames destilliert, promptbare Attribute)

Konsistent über die gesichteten Frames (`0-02`: Studio-Mikrofon, `0-30`: Politiker im
Arbeitszimmer, `1-15`: Mann am Schreibtisch bei Nacht):

1. **Flache 2D-Cartoon-Illustration** mit sauberem, fast vektorartigem Finish — kein
   Skizzen-/Tusche-Charakter, keine sichtbare „Handschrift".
2. **Dicke, gleichmäßige dunkelbraun-schwarze Outlines** um Figuren und Objekte —
   das stärkste Wiedererkennungsmerkmal.
3. **Flat Fills + dezentes Cel-Shading**: pro Fläche ein Schatten- und ein Lichtton;
   weiche Verläufe nur für Lichtkegel (Lampe, Fenster, Bildschirm).
4. **Gedeckte, warme, leicht entsättigte Palette** — Erdbraun, warmes Grau, Amber;
   Nachtszenen kippen in kühles entsättigtes Blau. Nie grelle Vollfarben.
5. **Licht trägt die Stimmung**: genau eine dominante Lichtquelle pro Szene, sichtbare
   Lichtkegel, Umgebung dunkler als das Subjekt — der cineastische Kern des Stils (und
   der Grund, warum `PHASE_COLOR_FILTER` auf diesem Material **gut** funktioniert, 10.5).
6. **Figuren**: vereinfachte, rundliche Proportionen, große Köpfe, minimale Gesichtszüge
   (Punkt-/Strich-Augen, ausdrucksstarke dicke Brauen) — Emotion über Haltung + Licht.
7. **Environments mit echter Tiefe**: eingerichtete Räume, Stadt-Silhouetten,
   Bücherregale — detailliert, aber vereinfacht. Kein leerer weißer Hintergrund.
8. **16:9-Kino-Framing**: Profile, Over-Shoulder, Drittel-Regel, dramatische Nähe.

Kurz: der Stil liegt sehr nah an der tatsächlichen Simplicissimus-Bildsprache — was die
Zieldefinition des Gesamtauftrags zusätzlich bestätigt.

### 10.3 Evaluation des Vorgehens „Charakter per Referenzbild, Stil per Kanal-Prompt"

**Die Architektur ist richtig, die Arbeitsteilung muss nur sauber benannt werden:**

- **Identität (WER) → Referenzbilder.** Charsheets/`char_ref_url` (bereits gebaut,
  dashboard.py:1652 ff.) sind der richtige Mechanismus für „dieselbe Figur in jedem Bild" —
  ein Prompt allein kann Gesichter/Kleidung nicht über 50 Bilder stabil halten. (Deshalb
  ist Bug B-1 aus §9.3 so kritisch: ohne funktionierenden Upload fällt die WER-Hälfte aus.)
- **Stil (WIE) → kodifizierter Master-Prompt, NICHT ein globales Stil-Referenzbild.**
  Die Erkenntnis aus dem ersten Projekt gilt stiltunabhängig weiter (`STYLE_GUIDE.md`:
  „Referenz würde Kompositionen aneinander angleichen → immer dasselbe Bild"): hängt man
  dasselbe Stil-Referenzbild an jede Generierung, erbt jedes Bild dessen **Komposition**,
  nicht nur dessen Stil. Der Stil muss deshalb als **Text** in den Master-Prompt — präzise
  genug, dass er ohne Bildanker reproduzierbar ist (deshalb die Destillat-Liste in 10.2).
- **Konsequenz:** „Grafikstil pro Kanal spezifizieren" (Nutzer-Frage) → ja, genau so, und
  der Mechanismus existiert schon (`master_prompt.txt` pro Kanal + Phase-38-Presets als
  Startpunkt). Was fehlte, war ausschließlich der kodifizierte Text — der steht jetzt
  in 10.4.

### 10.4 Preset 0 — `flat_cartoon_doc` (NEUES Default-Preset, fertig formuliert)

Wird in Phase 38 als **erstes und vorausgewähltes** Preset implementiert und ersetzt
`IMAGE_MASTER_DEFAULT` als Default für neue Kanäle:

```
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
```

**Abnahme-Kriterium:** 3 Testbilder (Innenraum warm / Nacht kühl / Außen-Totale) müssen
neben den Frames in `assets/` liegen können, ohne als Fremdkörper aufzufallen — das ist
der Stil-Regressionstest per Sichtprüfung des Nutzers.

### 10.5 Errata — Korrekturen an §0, §2, §3 und §9 (Ink-Fehlkalibrierung)

Die folgenden Aussagen dieses Dokuments waren auf den Legacy-Ink-Stil kalibriert und werden
hiermit korrigiert (die Original-Abschnitte bleiben zur Nachvollziehbarkeit unverändert —
bei Widerspruch gilt §10):

1. **§0/§2 „persönliche Erzählungen im Ink-/Line-Art-Stil":** Der Kanal-Stil ist der flache
   Farb-Cartoon aus 10.2. Die **Priorisierung K > L > O ändert sich nicht** (Sound, Hook,
   Mikro-Rhythmus sind stilunabhängig), wohl aber zwei Detail-Bewertungen (Punkte 2+3).
2. **§2 K6 / Phase P „eq auf Line-Art fast wirkungslos":** Auf dem echten Voll-Farb-Stil
   ist das Gegenteil richtig — `eq contrast/saturation` wirkt auf der gedeckten warmen
   Palette **gut sichtbar**; das bestehende `PHASE_COLOR_FILTER` (Phase D) ist wertvoller
   als in §2 angenommen. Phase P wird dadurch **kleiner**: Vignette für CLIMAX + optional
   warme/kühle Tönung als Verstärkung, kein Ersatz der eq-Kette. Hebel bleibt 2–3,
   Aufwand sinkt auf 1–2.
3. **§2 K8 Tilt-Shift:** Begründung ändert sich, Ergebnis nicht. Auf dem Farb-Cartoon mit
   echter Raumtiefe wäre ein dezenter Tiefen-Blur technisch plausibler als auf Line-Art —
   aber ein Miniatur-Effekt hat weiterhin keinen dramaturgischen Nutzen für
   Doku-Erzählungen, und Unschärfe auf harten Vektor-Kanten liest sich schnell als
   Kompressionsartefakt. Bleibt verworfen; Fokus-Rahmen bleibt P-Experiment.
4. **§3 Phase N „Line-Art-konforme Optik: schwarze Striche":** Daten-Overlays müssen zum
   Farb-Cartoon passen: dicke dunkelbraune Outlines, Flächenfarben aus der warmen Palette,
   Akzent weiterhin aus `PHASE_ACCENT`. Die Technik (PIL-Frame-Sequenz) ist unverändert.
5. **§9.2 UI-These:** Die ursprüngliche Begründung („App produziert schwarz-weiße Tusche")
   ist hinfällig — das Farbkonzept selbst (Papierweiß, Graphit, kein Lila, ein sparsamer
   Akzent) bleibt richtig, jetzt als bewusst neutrale Werkstatt-UI, die mit den warmen
   Video-Frames im Preview nicht konkurriert. Alternativ darf 33.7 den Akzent statt
   Tusche-Rot in warmes Amber legen — Entscheidung beim Nutzer, beides erfüllt die
   Kontrast-Regeln.
6. **§9.4 Preset-Reihenfolge:** `flat_cartoon_doc` (10.4) ist **Preset 0 und Default**.
   `ink_documentary`, `charcoal_noir`, `editorial_minimal` bleiben als Alternativen
   (Universal-Tool: andere Kanäle dürfen andere Stile fahren), `stick_minimal` wird
   sichtbar als „Legacy" gelabelt.

### 10.6 Phase Q — Legacy-Bereinigung + Stil-Kodifizierung (Hebel 4 / Aufwand 1–2)

**Scope:** (Q.1) Legacy-Artefakte des ersten Video-Versuchs klar markieren und aus dem
Wissenspfad künftiger Agenten/Entwickler nehmen: `yeonmi_storyboard/`, `gen.py`,
`scenes.tsv`, `run_batch.sh`, `STYLE_GUIDE.md` in ein `legacy/`-Verzeichnis verschieben
(nicht löschen — Historie), `STYLE_GUIDE.md` bekommt einen Kopf-Banner „LEGACY — Stil des
ersten Videos, NICHT der aktuelle Kanal-Stil, siehe CINEMATIC_UPGRADE_PLAN.md §10".
(Q.2) Stil kodifizieren: Preset 0 aus 10.4 in `PRESET_MASTERS` (Phase 38) + als neuer
Inhalt von `IMAGE_MASTER_DEFAULT`. (Q.3) Die Stil-Referenzframes von `assets/*.jpg` nach
`assets/style_reference/` verschieben — sie liegen aktuell unsortiert neben `music/`/`sfx/`
und sind vom Sound-Asset-Pool (Phase K) nicht unterscheidbar. (Q.4) Bestandskanäle:
einmalig `master_prompt.txt` aus dem neuen Preset anlegen — **nur wo keiner existiert,
nie überschreiben**.

**Definition of Done:** Ein frisch angelegter Kanal generiert ohne manuelle Prompt-Arbeit
Bilder im 10.2-Stil (Sichtprüfung gegen `assets/style_reference/`); kein Dokument im
Repo-Root behauptet mehr, der aktuelle Stil sei Weiß-Line-Art; `grep -ri yeonmi` trifft
außerhalb von `legacy/` nichts mehr.

**Tests:** `t_phase_q_default_master_is_flat_cartoon`,
`t_phase_q_no_yeonmi_refs_outside_legacy` (Quelltext-Grep),
`t_phase_q_existing_master_untouched`.

**Einordnung in §8.3:** Q gehört **in Woche 1, direkt neben Phase 38** (beide zusammen ein
halber Tag) — denn solange der Stick-Figure-Default aktiv ist, testet jeder
Produktions-Render der Phasen K/L gegen den falschen Stil, und der erste
Qualitäts-Meilenstein (§8.3) würde ein Strichmännchen-Video bewerten.

---

## 11. Sequenz-Ketten (Doppel-Anker) — Bestandsschutz & Tiefenprüfung (2026-07-07)

**Nutzer-Anforderung:** Die Logik, mit der zusammenhängende Szenen aus mehreren Bildern
entstehen („Das Auto **ist kaputt**" → Bild 1: das ganze Auto; Bild 2: **dasselbe** Auto,
aber kaputt — gleiche Identität, gleicher Hintergrund, neuer Zustand), muss über alle
geplanten Umbauten hinweg **geschützt** werden. Sie ist gefunden, vollständig gelesen und
tiefengeprüft — hier der Ablauf, die Befunde und die verbindlichen Schutzregeln.

### 11.1 Die Logik, wie sie heute im Code steht (Fundstellen)

Das „Das Auto ist kaputt"-Beispiel läuft durch genau diese Kette:

1. **Erkennung (LLM, einmal pro Skript):** `analyze_script` → `visual_sequences`
   (dashboard.py:890 ff.) — gruppiert ≥ 2 **aufeinanderfolgende** Beats, die dasselbe
   konkrete Subjekt kontinuierlich zeigen. Bewusst konservativ instruiert: „When in doubt,
   do NOT form a sequence" — Einzelbilder sind der sichere Default.
2. **Zuordnung:** Audio-Pfad `_apply_visual_sequences_direct` (821, direkter Index 1:1);
   manueller Pfad über `segment_by_pacing`. Beide enden in `_renumber_seq_pos` (806):
   `seq_pos` wird in **finaler Szenen-Reihenfolge** neu gezählt — der Anker ist immer
   `seq_pos == 0`.
3. **Doppel-Anker-Referenzierung (der Kern):** `_resolve_chain_refs` (1552) — jede
   Fortsetzung (`seq_pos ≥ 1`) referenziert **sowohl das Anker-Bild als auch das
   unmittelbare Vorgängerbild** (dedupliziert bei `seq_pos == 1`). Begründung im Code:
   nur am Vorgänger zu ketten würde Drift pro Generation akkumulieren; der fixe Anker
   ist das visuelle Fundament. Dazu additiv die Charakter-Referenz — aber **konditional**
   (1652): nur wenn `concrete_entity` der Szene wirklich ein `char_*` aus der Analyse ist.
4. **Nebenläufigkeits-Schutz:** `_wait_for_chain_scene` (1524) — weil der Batch-Worker
   bis zu 8 Szenen parallel dispatcht, kann eine Fortsetzung im selben Batch wie ihr
   Anker landen; sie **blockiert** (Timeout 170 s > 150 s Anker-Poll-Maximum), bis der
   Anker eine `source_url` hat oder gescheitert ist, und fällt dann sauber auf
   „keine Ketten-Referenz" zurück statt zu crashen. Bewusst **außerhalb aller Locks**
   aufgerufen (1642 ff.), damit eine wartende Szene keine fremden Szenen blockiert.
5. **Continuity-Prompt:** Fortsetzungen bekommen den STRICT-CONTINUITY-Zusatz (1656 ff.) —
   **nur positive Constraints** („You MUST perfectly match identity, outfit, background …
   Change ONLY the camera angle/framing or the specific action"), weil negierte
   Anweisungen von Bildmodellen schwächer gewichtet werden (Pink-Elephant-Effekt).
   Genau dieser Satz erzeugt „dasselbe Auto, aber kaputt".
6. **Selbstheilung:** Läuft eine KIE-Referenz-URL ab (Temp-Hosting), wird einmalig aus den
   lokalen Dateien neu hochgeladen und der Submit wiederholt (1679 ff.).
7. **Nachvollziehbarkeit:** `chain_anchor_file`/`chain_prev_file`/`char_ref_applied`
   werden pro Szene in `plan.json` persistiert (1739 ff.).
8. **Render-Seite (die Kette wirkt bis ins Video):** Fortsetzungen **erben die Motion**
   ihres Vorgängers (`_motion_for_scene`, 2019 ff. — eine Sequenz liest sich als eine
   durchgehende Kamerafahrt) und Crossfades/Whooshes sitzen **nur an Sequenzgrenzen**
   (`_has_transition_before`, 2299 / `_build_sfx_events`, 2354).

### 11.2 Tiefenprüfungs-Befunde

**Solide:** Doppel-Anker gegen Drift, Warten-statt-Crashen, Lock-freies Warten,
Stale-URL-Retry, konditionale Char-Referenz, Motion-Vererbung — das Design ist
durchdacht und die Race-Klasse ist sogar getestet (`t_round5_image_job_worker_race_detect`).

**Vier Schwachstellen gefunden:**

| # | Befund | Risiko |
|---|---|---|
| S1 | **Docstring von `_batch_generate_worker` (1577–1582) ist falsch** — behauptet wörtlich „Image scenes have no ordering dependency on each other … never from another scene's generated output", während dieselbe Funktion 70 Zeilen später `_resolve_chain_refs` aufruft. Exakt die Drift-Klasse, die Phase 36 (Lint) jagen soll | Fehlleitung künftiger Änderungen — jemand „optimiert" die Dispatch-Reihenfolge im Vertrauen auf den Docstring |
| S2 | **Implizite Ordnungs-Invariante ohne Schutz:** Kein Deadlock heute, weil `todo` die Szenen-Reihenfolge erhält und der ThreadPool FIFO abarbeitet — ein Anker wird immer vor seinen Fortsetzungen dispatcht. **Aber:** Sortiert irgendjemand `todo` jemals um (z. B. „Hook-Szenen zuerst" in Phase L, „Fehler-Szenen zuerst" in einem Retry-Feature), können alle 8 Worker gleichzeitig in `_wait_for_chain_scene` hängen, deren Anker noch in der Queue stecken → 170 s × Kettenlänge Stillstand, danach Fortsetzungen **ohne** Referenzen | Die Invariante existiert nur als Zufall der aktuellen Implementierung, nirgendwo als Regel oder Test |
| S3 | **Phase-Style-Injection kann innerhalb einer Sequenz kippen:** Wechselt die Story-Phase mitten in einer Sequenz (RISING_ACTION→CLIMAX), bekommt die Fortsetzung einen **anderen** `STYLE ({phase})`-Block als ihr Anker — der Continuity-Zusatz sagt „match the references", die Phase-Injection sagt „maximum contrast, dynamic angle". Zwei widersprüchliche Instruktionen im selben Prompt | Sichtbare Stil-Sprünge innerhalb einer Kette; bisher unbemerkt, weil Sequenzen kurz und Phasenwechsel selten mitten hineinfallen |
| S4 | **Null direkte Testabdeckung:** Kein einziger der 54 Tests prüft `_resolve_chain_refs`, `_wait_for_chain_scene`, `_renumber_seq_pos` oder den Continuity-Prompt | Jede der geplanten Phasen (L, M, O, 38, 39) kann die Kette lautlos brechen |

### 11.3 Schutzregeln (verbindlich für ALLE Phasen dieses Dokuments)

1. **Dispatch-Reihenfolge ist heilig:** `todo` in `_batch_generate_worker` behält die
   Szenen-Reihenfolge. Wer je priorisieren will, muss Sequenz-Mitglieder als
   unteilbare Blöcke behandeln (Anker zuerst). → abgesichert durch Test T2 unten.
2. **Motion-Vererbung schlägt jede neue Motion-Regel:** Die Sequenz-Fortsetzungs-Prüfung
   in `_motion_for_scene` (seq_pos ≥ 1 erbt) steht **vor** allen Overrides — konkret
   für Phase L: `is_hook` darf die Motion nur setzen, wenn die Szene **nicht**
   Fortsetzung einer Sequenz ist (praktisch irrelevant, weil der Hook fast immer Beat 0
   und damit Anker ist — aber die Regel gehört in den Code, nicht in die Hoffnung).
3. **Continuity-Zusatz bleibt die LETZTE Prompt-Komponente** (nach Master + Phase-Cue) —
   Phase 38/Q dürfen Master-Prompts austauschen, aber die Append-Reihenfolge in
   `_batch_generate_worker` (1655–1665) nicht verändern.
4. **S3-Fix (klein, in Phase L miterledigen):** Fortsetzungs-Szenen erben den
   Phase-Style-Cue ihres **Ankers** statt ihres eigenen — eine Zeile in der
   `full_prompt`-Konstruktion (`phase=anchor_phase if seq_pos>=1 else scene.phase`).
   Die Render-Seite (Color-Grading, Musik) bleibt bei der echten Szenen-Phase — nur die
   **Bild**-Instruktion muss innerhalb einer Kette konsistent sein.
5. **Phase 39 (I2V) respektiert Ketten:** Animieren bevorzugt **Anker-Szenen**; eine
   I2V-animierte Fortsetzung mitten in einer Kette bricht die visuelle Kontinuität
   zwischen den Standbildern davor/danach. UI-Hinweis am Button, keine harte Sperre.
6. **Phase M (Engine-Extract) verschiebt die Kette als Ganzes:** `_resolve_chain_refs` +
   `_wait_for_chain_scene` + `_renumber_seq_pos` + `_apply_visual_sequences_direct`
   wandern gemeinsam (nach `engine_scenes.py` in J.4 bzw. bleiben bis dahin in
   dashboard.py) — niemals auf zwei Module verteilen, sie teilen sich die
   plan.json-Polling-Semantik.
7. **Additive Felder bleiben stabil:** `seq_id`, `seq_pos`, `seq_reason`,
   `chain_anchor_file`, `chain_prev_file`, `char_ref_applied` sind öffentliche
   plan.json-Verträge — umbenennen/entfernen ist ein Breaking Change.

### 11.4 Regressionstests (schließt S4; im Stil der bestehenden Source-Introspection-Tests)

- `t_seq_double_anchor_refs` — `_resolve_chain_refs`: seq_pos 1 → genau 1 Ref (dedupliziert),
  seq_pos 2 → genau 2 Refs (Anker + Vorgänger), seq_pos 0 / keine Sequenz → leer.
- `t_seq_todo_preserves_scene_order` — Quelltext-Check: kein `sort`/`sorted`/`reverse`
  auf `todo` in `_batch_generate_worker` (Schutzregel 1 / S2).
- `t_seq_wait_timeout_exceeds_poll_max` — `_wait_for_chain_scene`-Timeout (170 s) bleibt
  größer als `IMAGE_JOB_MAX_POLLS × 3 s` (150 s); wer die Poll-Zahl erhöht, wird hier
  gestoppt, bevor Ketten stillschweigend ohne Referenzen laufen.
- `t_seq_continuity_prompt_last` — der CONTINUITY-Block steht im finalen Prompt **nach**
  dem Master-Text (Schutzregel 3).
- `t_seq_motion_inheritance_precedence` — `_motion_for_scene`: Sequenz-Vererbung gewinnt
  gegen alle nachgelagerten Overrides, inkl. künftigem `is_hook` (Schutzregel 2).
- `t_seq_renumber_assigns_anchor_zero` — `_renumber_seq_pos` vergibt pro seq_id lückenlos
  0,1,2… in Szenen-Reihenfolge.
- **Mini-Fix (mit dem ersten dieser Tests einchecken):** Docstring von
  `_batch_generate_worker` korrigieren (S1) — der Satz „never from another scene's
  generated output" fällt, Verweis auf `_resolve_chain_refs`/Doppel-Anker kommt rein.

**Einordnung:** Die Tests aus 11.4 gehören in **Woche 1, vor Phase L** (L fasst
`_motion_for_scene` an — Schutzregel 2 muss vorher als Test existieren). Der S3-Fix läuft
als Teil von Phase L mit. Aufwand gesamt: ~2 h.
