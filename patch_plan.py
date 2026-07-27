import re

with open("workers/plan.py", "r") as f:
    content = f.read()

# Add core.paths to imports if not there (it's likely there, but let's check)
if "from core.paths import" not in content:
    content = content.replace("import dashboard", "import dashboard\nfrom core.paths import ch_master")

# Replace brand vibe extraction to also extract master prompt
old_vibe = """        # Load channel brand_vibe if configured
        brand_vibe = ""
        try:
            channels = dashboard.load_channels()
            cur_ch = next((c for c in channels if c.get("id") == cid), {})
            brand_vibe = cur_ch.get("brand_vibe", "")
        except Exception:
            pass

        with dashboard._PLAN_JOBS_LOCK:
            dashboard.PLAN_JOBS[key]["step"] = f"Schreibe Bild-Prompts für {len(scenes)} Szenen …"
        prompts = visual_prompts(scenes, analysis, brand_vibe=brand_vibe)"""

new_vibe = """        # Load channel brand_vibe and master_prompt if configured
        brand_vibe = ""
        master_prompt = ""
        try:
            channels = dashboard.load_channels()
            cur_ch = next((c for c in channels if c.get("id") == cid), {})
            brand_vibe = cur_ch.get("brand_vibe", "")
            
            from core.paths import ch_master
            import os
            mp_path = ch_master(cid)
            if os.path.exists(mp_path):
                with open(mp_path, "r", encoding="utf-8") as f:
                    master_prompt = f.read().strip()
        except Exception:
            pass

        with dashboard._PLAN_JOBS_LOCK:
            dashboard.PLAN_JOBS[key]["step"] = f"Schreibe Bild-Prompts für {len(scenes)} Szenen …"
        prompts = visual_prompts(scenes, analysis, brand_vibe=brand_vibe, master_prompt=master_prompt)"""

content = content.replace(old_vibe, new_vibe)

with open("workers/plan.py", "w") as f:
    f.write(content)
