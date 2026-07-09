#!/usr/bin/env python3
"""
Test-Script: Vergleich der Bild-Prompt-Generierung über 3 Thinking-Levels
(low / medium / high) am gleichen Skript + gleichen Charakter-Charsheets.

Output: /tmp/thinking_test_<timestamp>/prompts.md mit side-by-side Vergleich.

KEIN Bild-Render — nur Text-Prompts. Spart 100% KIE-Kosten, 95% Zeit.

Verwendung:
  /Users/noel/gemini-storyboard/.venv_whisper/bin/python scripts/test_thinking_levels.py

Hinweis: nutzt die echte Pipeline aus engine/prompts.py + dashboard.py.
Monkey-patcht nur post_gemini_native um thinking_level variabel zu machen.
"""
import json
import os
import sys
import time
from datetime import datetime

# Projekt-Root
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# --- Test-Script: erste 3 Kapitel des Theranos-Skripts -------------------
TEST_SCRIPT = """A nineteen-year-old blonde girl walks into a room and promises to change medicine forever.

One drop of blood. Hundreds of tests. No needles.

Ten years later, her company is worth nine billion dollars. She's the youngest self-made female billionaire in history.

There's just one problem.

The machine never actually worked.

This is the story of how one woman fooled some of the smartest investors in Silicon Valley — and almost got away with it.

[cut to black / title card]

Chapter 1: The Promise

The best scams usually don't start like scams.

They start like miracles.

One tiny drop of blood.
One machine.
Hundreds of tests.
A future where medical diagnostics become faster, cheaper, and easier for everyone.

That was the promise behind Theranos.

And for a while, it sounded like the kind of idea that only seemed impossible to people who were too old to understand the future.

In 2003, Elizabeth Holmes founded the company at just 19 years old.

By 2015, Theranos was valued at $9 billion.

That's not just impressive.
That's the kind of number that makes people stop asking questions.

Because once a company gets that big, belief starts doing the work that evidence should have been doing all along.

Chapter 2: The Image

Elizabeth Holmes knew how to sell the story.

She looked serious.
She sounded serious.
And she built an image that made her seem like the person who could pull off what everyone else thought was impossible.

That matters more than people like to admit.

In Silicon Valley, a compelling story can often move faster than a working product.
A clean pitch can outperform a messy truth.
And if the founder looks certain enough, skepticism starts to feel like a lack of imagination.

Theranos became the perfect example of that dynamic.

It wasn't just selling blood testing.
It was selling the idea of being early on the future.

Chapter 3: The Machine

The core claim was simple and extraordinary.

Theranos said it could perform a wide range of blood tests from just a small finger-prick sample.

That sounds almost elegant.
Almost too elegant to challenge.

If it had worked, it would have changed a lot.

But that was the problem.

The company's technology did not deliver what the public was told it could do.
Investigations later showed that the tests produced inaccurate results, and that the company relied on a system that did not match its public claims.

So while the outside world was being shown a revolution, the inside story was collapsing under its own weight."""

# Beats pro Kapitel (1. Szene jedes Kapitels = repräsentativ)
# Wir nehmen je 1 Eröffnungs-Szene pro Kapitel, um den Vergleich scharf zu halten.
TEST_BEATS = [
    ("Chapter 1: The Promise",
     "The best scams usually don't start like scams. They start like miracles. One tiny drop of blood. One machine. Hundreds of tests. A future where medical diagnostics become faster, cheaper, and easier for everyone."),
    ("Chapter 2: The Image",
     "Elizabeth Holmes knew how to sell the story. She looked serious. She sounded serious. And she built an image that made her seem like the person who could pull off what everyone else thought was impossible."),
    ("Chapter 3: The Machine",
     "The core claim was simple and extraordinary. Theranos said it could perform a wide range of blood tests from just a small finger-prick sample. That sounds almost elegant. Almost too elegant to challenge."),
]

THINKING_LEVELS = ["low", "high"]


def run():
    """Hauptfunktion."""
    from engine.prompts import _image_prompt_chunk, _IMAGE_PROMPT_FEWSHOT, _build_image_prompt
    from dashboard import post_gemini_native, analyze_script, kie_key

    # Hinweis: _image_chunk_schema wird INNERHALB von _image_prompt_chunk gebaut,
    # wir müssen nichts reichen. Wir monkey-patchen nur post_gemini_native.

    if not kie_key():
        print("FEHLER: ~/.kie_key fehlt — KIE.ai-Auth nicht möglich.", file=sys.stderr)
        sys.exit(1)

    # 1) Voll-Skript analysieren (einmal, gibt uns characters/locations für analysis_ctx)
    print("[1/3] Analysiere Skript via analyze_script() …", flush=True)
    all_beats = [b.strip() for b in TEST_SCRIPT.split("\n\n") if b.strip()]
    # Nur die "wichtigen" Beats — keine Stage Directions
    beats_for_analysis = [
        b for b in all_beats
        if not b.startswith("[") and not b.startswith("Chapter ")
        and len(b) > 10
    ]
    analysis = analyze_script(beats_for_analysis)
    analysis_ctx = json.dumps(analysis, ensure_ascii=False, indent=1)
    print(f"  → {len(analysis.get('characters', []))} Charaktere, "
          f"{len(analysis.get('locations', []))} Locations erkannt", flush=True)

    # 2) Für jedes Kapitel × jedes Thinking-Level Prompt generieren
    # Wir monkey-patchen post_gemini_native damit thinking_level variabel ist,
    # behalten aber ALLES andere (Schema, Fewshot, Temperature) identisch.
    results = {}  # (chapter, level) -> list[dict]

    for chapter_label, beat_text in TEST_BEATS:
        for level in THINKING_LEVELS:
            print(f"[2/3] {chapter_label} × thinking_level={level} …", flush=True)
            t0 = time.time()

            # Monkey-patch: _image_prompt_chunk ruft post_gemini_native auf;
            # wir leiten den thinking_level-Parameter durch
            import engine.prompts as ep

            def patched_post_gemini_native(messages, json_mode=False, temp=0.7, model="gemini-3-5-flash",
                                            thinking_level="high", response_schema=None, **kwargs):
                # Override: zwinge den gewünschten level
                return post_gemini_native(
                    messages, json_mode=json_mode, temp=temp, model=model,
                    thinking_level=level, response_schema=response_schema,
                )

            ep.post_gemini_native = patched_post_gemini_native
            try:
                # Eine Szene pro Aufruf (kleinster Chunk = maximales Reasoning pro Level)
                chunk_result = _image_prompt_chunk(
                    [beat_text], 0, 1, analysis_ctx, [None]
                )
                elapsed = time.time() - t0
                results[(chapter_label, level)] = {
                    "elapsed_sec": round(elapsed, 1),
                    "scenes": chunk_result,
                }
                print(f"    ✓ {elapsed:.1f}s", flush=True)
            except Exception as e:
                results[(chapter_label, level)] = {"error": str(e)}
                print(f"    ✗ ERROR: {e}", flush=True)
            finally:
                # Monkey-patch zurück (für saubere Iteration)
                if hasattr(ep, '__wrapped_post_gemini_native'):
                    ep.post_gemini_native = ep.__wrapped_post_gemini_native
                else:
                    # Reset to lazy re-import
                    del ep.__dict__['post_gemini_native']

    # 3) Markdown-Report schreiben
    out_dir = f"/tmp/thinking_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, "prompts.md")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Thinking-Level-Test: Prompt-Vergleich\n\n")
        f.write(f"**Datum**: {datetime.now().isoformat()}\n")
        f.write(f"**Skript**: Theranos-Eröffnung + 3 Kapitel (siehe TEST_SCRIPT im Script)\n")
        f.write(f"**Levels getestet**: {', '.join(THINKING_LEVELS)}\n")
        f.write(f"**Methode**: Direktaufruf von `_image_prompt_chunk()` mit Monkey-Patch "
                f"auf `post_gemini_native` für variablen `thinking_level`. "
                f"Response-Schema, Few-Shot, Temperature bleiben identisch zur echten Pipeline.\n\n")

        # Analyse-Output
        f.write("## Skript-Analyse (von `analyze_script()`)\n\n")
        f.write(f"- Charaktere: {len(analysis.get('characters', []))}\n")
        for ch in analysis.get('characters', []):
            f.write(f"  - `{ch.get('id')}` = **{ch.get('name_or_role')}**: "
                    f"{ch.get('visual_description', '')[:200]}\n")
        f.write(f"\n- Locations: {len(analysis.get('locations', []))}\n")
        for loc in analysis.get('locations', []):
            f.write(f"  - `{loc.get('id')}` = **{loc.get('name')}**\n")
        f.write("\n---\n\n")

        # Pro Kapitel: Side-by-side Vergleich
        for chapter_label, _ in TEST_BEATS:
            f.write(f"## {chapter_label}\n\n")

            for level in THINKING_LEVELS:
                f.write(f"### thinking_level = `{level}`\n\n")
                r = results.get((chapter_label, level), {})
                if "error" in r:
                    f.write(f"**ERROR**: `{r['error']}`\n\n")
                    continue
                f.write(f"*Generiert in {r['elapsed_sec']}s*\n\n")
                for scene in r["scenes"]:
                    f.write(f"**Scene {scene.get('scene')}**\n\n")
                    f.write(f"- **concrete_entity**: `{scene.get('concrete_entity', '')}`\n")
                    f.write(f"- **line_specific_anchor**: {scene.get('line_specific_anchor', '')}\n")
                    f.write(f"- **character_consistency**: {scene.get('character_consistency', '')[:300]}\n")
                    f.write(f"\n**image_prompt**:\n```\n{scene.get('image_prompt', '')}\n```\n\n")
                f.write("---\n\n")

            # Side-by-side der image_prompts
            f.write("### Side-by-side Vergleich (image_prompts)\n\n")
            for level in THINKING_LEVELS:
                r = results.get((chapter_label, level), {})
                if "error" in r:
                    f.write(f"**{level}**: ERROR\n\n")
                    continue
                prompt = r["scenes"][0].get("image_prompt", "") if r["scenes"] else ""
                f.write(f"**{level}** ({len(prompt)} Zeichen):\n```\n{prompt}\n```\n\n")
            f.write("\n---\n\n")

        # Konsistenz-Analyse: Welche Charakter-Adjektive wiederholen sich?
        f.write("## Konsistenz-Analyse (Welche Charakter-Adjektive tauchen in allen Kapiteln auf?)\n\n")
        import re
        for level in THINKING_LEVELS:
            all_prompts = []
            for chapter_label, _ in TEST_BEATS:
                r = results.get((chapter_label, level), {})
                if "scenes" in r:
                    for s in r["scenes"]:
                        all_prompts.append(s.get("image_prompt", ""))
            joined = " ".join(all_prompts).lower()
            # Typische Charakter-Marker
            char_terms = ["blonde", "turtleneck", "black", "blue eyes", "messy bun", "wide eyes",
                          "unblinking", "intense", "serious", "young woman", "nineteen"]
            counts = {t: joined.count(t) for t in char_terms if joined.count(t) > 0}
            f.write(f"### `{level}`\n\n")
            for term, cnt in sorted(counts.items(), key=lambda x: -x[1]):
                f.write(f"- `{term}`: {cnt}×\n")
            f.write("\n")

    print(f"\n[3/3] Report geschrieben: {md_path}", flush=True)
    print(f"        Auch Roh-Daten: {out_dir}/raw_results.json", flush=True)

    # Raw JSON
    with open(os.path.join(out_dir, "raw_results.json"), "w", encoding="utf-8") as f:
        json.dump({
            "analysis": analysis,
            "results": {f"{c}|{l}": r for (c, l), r in results.items()},
        }, f, ensure_ascii=False, indent=2)

    return md_path


if __name__ == "__main__":
    md = run()
    print(f"\nFERTIG. Öffne: {md}")