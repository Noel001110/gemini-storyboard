# SFX-Integration & Cut-Stil – Review-Vorlage

**Kontext:** gemini-storyboard Pipeline (Zero-Framework, Python stdlib + FFmpeg, KIE.ai, ElevenLabs TTS, Whisper Word-Timing, Ken Burns Rendering)

## Ausgangslage

- Word-für-Wort Captions: bereits implementiert
- SFX-System existiert schon (58-Type Transition-Library, SFX auf Cut-Punkten), aber Platzierung fühlt sich nicht "on point" zum Skript an
- Audio-Assets für SFX bisher synthetische Sinuston-Platzhalter
- Cuts hängen aktuell an Szenengrenzen, nicht an Wortbetonung – obwohl Whisper-Timing dafür verfügbar wäre

## Entscheidung 1: Cut-Stil NICHT ändern

Recherche bestätigt: Hard Cuts sind der professionelle Standard, kein Kompromiss. Mehr visuelle Transitions gelten als Anfängerfehler ("over-produced", "disjointed"). Empfehlung überall: Transitions gezielt auf Story-Beats mappen (Kapitelwechsel, Ton-Shift), nicht die Frequenz erhöhen.

→ **Keine Änderung am Cut-Verhältnis.** Bestehender Mix aus überwiegend Hard Cuts + gelegentlicher Transition bleibt.

## Entscheidung 2: SFX über bestehenden ElevenLabs-Account, textgetriggert

**Warum:** ElevenLabs SFX (V2, 48kHz, bis 30s) generiert Sounds aus Textbeschreibung statt aus einer Library zu ziehen – kein "Vine-Boom"-Wiederverwendungsproblem, kein neuer Vendor (gleicher Account wie TTS).

**Was die API NICHT löst:** Trigger-Logik (wann/wo ein SFX sitzt) bleibt eigene Pipeline-Arbeit.

### Implementierungsschritte

1. Script-Analyse-Stufe (die schon `core_statement → concrete_entity → callback_check` ausgibt) bekommt zusätzliches optionales Feld:
   - `sfx_trigger_phrase` (exaktes Wort/Phrase aus dem Skript)
   - `sfx_prompt` (kurze SFX-Beschreibung für die API)
   - Nur gesetzt, wenn Szene es hergibt – nicht jede Szene braucht SFX
2. Whisper-Word-Timing (läuft schon für Captions) matcht `sfx_trigger_phrase` auf exakten Audio-Timestamp
3. ElevenLabs Sound-Effects-API-Call mit `sfx_prompt` → Audiofile
4. FFmpeg-Overlay am Timestamp (analog zur bestehenden SFX-Overlay-Logik bei Transitions, jetzt textgetriggert statt cut-getriggert)

### Guardrails

- Sparsam: 3–6 echte SFX-Momente pro Video, LLM explizit auf Zurückhaltung instruieren
- Sound-Design an Cut-Punkten ist der eigentliche Hebel für "Flow"-Gefühl bei Hard-Cut-lastigem Editing (Cutting-to-the-beat, J-/L-Cut-Prinzip) – nicht mehr visuelle Übergänge
- Zero-Framework-konform: kein neues Modell, kein neuer Vendor, nur zusätzliches Feld + ein API-Endpoint, den ihr schon nutzt

## Offene Fragen für Review

- Sitzt `sfx_trigger_phrase`-Erkennung zuverlässig genug in der bestehenden LLM-Analyse-Stufe, oder braucht's einen eigenen Call?
- Kosten-/Latenz-Impact durch zusätzliche SFX-API-Calls pro Video abschätzen
