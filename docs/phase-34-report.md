# Phase 34 Report: Skript-UI, Retention Fixes & Viral-Vibe Engine

## Was wurde gemacht?
1. **Retention-Rewrite Error Handling**: Wenn der serverseitige LLM-Call `rewrite_script_for_retention` in `workers/plan.py` fehlschlägt, wird nun explizit ein `retention_error: True` in die `script.json` geschrieben. 
2. **Frontend Script Sync**: Nach Abschluss der Plan-Generierung ruft das Frontend `restoreScriptLocal()` auf.
3. **Skript-Generator in Schritt 2 integriert**: Tab-Switcher im UI zwischen "Direkt eingeben" und "Aus Notizen generieren".
4. **Aufsplittung von Retention und Plan-Generierung**: Der Retention-Rewrite wurde vom Plan-Worker entkoppelt. "Plan aus Skript erstellen" baut Bild-Prompts strikt aus dem Text im Editor ohne verdecktes Umschreiben.
5. **Manueller Optimize-Button**: Ein eigener "✨ Skript optimieren (Retention)"-Button ruft `/api/rewrite_script_retention` synchron auf.
6. **Peak Retention Engine & Visual Subversion**:
   - **Channel Brand Vibe**: Einstellungen-Tab im Dashboard erweitert um `Channel Brand Vibe` Textfeld inkl. **✨ Beispiel laden (Unlisted_Cash)** Button und Endpoint `/api/channels/brand_vibe`.
   - **Preset `unlisted_tactical_v2`**: Neues Master-Preset in `engine/presets.py` für düstere Tactical-Vector-Optik.
   - **Prompt Subversion & Anti-Klischee**: In `engine/prompts.py` wurden `forbidden_visuals_check`, `visual_subversion`, `visual_spike` und die `Metaphor + Anchor Rule` im JSON Schema und System Prompt fest verankert.
   - **Emotional Vulnerability Arc**: Charakter startet isoliert/überfordert in Phase 1 und wandelt sich erst im Climax zum siegreichen Taktiker.
   - **Action-Verben Zwang**: Verbot von passiven Posierungen (`thinking`, `standing`) zugunsten von Action-Verben (`crushing`, `intercepting`, `breaching`).
   - **ElevenLabs Auto-Preset für Alex**: Ein Auto-Trigger in `dashboard.html` wurde hinzugefügt. Wenn eine Voice ausgewählt wird, deren Name "Alex" enthält, snappen die Voice-Settings (Modell, Stability, Similarity, Style, Speed, Boost) automatisch auf das "Business Book Narrator"-Preset (`eleven_multilingual_v2`, `0.4`, `0.75`, `0.0`, `1.1`), welches für Doku-Narration im Juli 2026 perfektioniert wurde.
7. **Bug Fixes (UI)**: 
   - Fehlender schließender `</div>`-Tag im `dashboard.html` (beim Brand-Color-Picker) behoben, der dazu führte, dass die restliche App irrtümlich im Settings-Modal versteckt wurde.
   - **Upload-Queue-Bridge verschoben**: Der Button "Zur Upload-Warteschlange hinzufügen" hing fälschlicherweise oben im Bereich "Description generieren" fest. Er wurde logisch korrekt ans Ende der Seite in die Publish-Übersichtskarte (Schritt 7) verschoben.
8. **TikTok Integration & Shorts Upload Fix**:
   - Die Datenbank `upload_queue` wurde um die Spalte `platform` erweitert und die Tabelle `tiktok_oauth` zur Speicherung von TikTok Access/Refresh-Tokens hinzugefügt.
   - Ein vollständiger OAuth2.0-Flow für TikTok wurde unter `tiktok/oauth.py` und `tiktok/api.py` (Endpunkte `/tiktok/oauth/login` und `/tiktok/oauth/callback`) implementiert.
   - Im Control Panel (`control.html`) wurde ein Button zur Verknüpfung des TikTok-Accounts hinzugefügt.
   - Die TikTok Content Posting API (Direct Post) wurde für den Upload von generierten Shorts unter `tiktok/upload.py` implementiert. Der Worker-Loop checkt nun anhand des `platform`-Flags, an welche Plattform das Video geht.
   - **Upload-Automatisierung gestoppt**: Der Bug, durch den Shorts sofort nach Generierung automatisch hochgeladen wurden, wurde behoben. Sie müssen nun wie das Longform-Video manuell in Schritt 7 via "Zur Warteschlange hinzufügen" in den Queue verschoben werden.
   - Beim manuellen Hinzufügen eines Shorts zur Warteschlange wird dieses fortan automatisch **doppelt** gequeued (1x für YouTube Shorts, 1x für TikTok).

## Working State
Das System verfügt nun über eine hochmoderne Viral-Retention-Pipeline, die Klischees im Keim erstickt, Marken-Vibes per Kanal steuert und visuelle Ermüdung durch gezielte Spikes unterbindet. Es unterstützt nun außerdem den Cross-Upload von Shorts auf YouTube und TikTok über eine integrierte Warteschlange und OAuth-Anbindung. Der UI-Bug bezüglich der verschwundenen Sidebar wurde gefixt.
