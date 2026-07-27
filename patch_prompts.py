import re

with open("engine/prompts.py", "r") as f:
    content = f.read()

# Update signature
old_sig = 'def visual_prompts(chunk_beats: list, analysis: dict, brand_vibe: str = "") -> list:'
new_sig = 'def visual_prompts(chunk_beats: list, analysis: dict, brand_vibe: str = "", master_prompt: str = "") -> list:'
content = content.replace(old_sig, new_sig)

# Update vibe_text logic
old_vibe = '    vibe_text = f"\\n\\nBRAND TONE & SOUL DIRECTIVE:\\n{brand_vibe}\\nEnforce this overarching vibe and world-building metaphor strictly in every image prompt!" if brand_vibe else ""'
new_vibe = '''    vibe_text = ""
    if brand_vibe or master_prompt:
        vibe_text = "\\n\\nBRAND TONE & AESTHETIC DIRECTIVE:\\n"
        if brand_vibe:
            vibe_text += f"- VIBE / SOUL: {brand_vibe}\\n"
        if master_prompt:
            vibe_text += f"- VISUAL MASTER PROMPT (MUST STRICTLY OBEY): {master_prompt}\\n"
        vibe_text += "Enforce this overarching vibe, world-building metaphor, and visual style strictly in every image prompt!"'''
content = content.replace(old_vibe, new_vibe)

# Update visual_subversion and image_prompt in the JSON Schema string
old_subv = '"visual_subversion": "Subvert the cliché! How do we translate this line into a brutal, tactical, or mechanical metaphor matching the Brand Tone Directive while keeping the concrete anchor visible?",'
new_subv = '"visual_subversion": "Identify the cliché (e.g., a piggy bank). FORBID IT. Instead, create a brutal metaphor strictly using the anatomy/rules of the VISUAL MASTER PROMPT (e.g. if the style is stick-figure, show the character violently welding a black-line cage). Make the POSES extreme.",'
content = content.replace(old_subv, new_subv)

old_img_prompt = '"image_prompt": "The final image text. MUST describe a physical interaction using ACTION VERBS (\'crushing\', \'intercepting\', \'routing\', \'breaching\', \'flanking\'). Avoid passive posture (\'thinking\', \'standing\'). Visually combine concrete_entity + visual_subversion + line_specific_anchor + visual_spike. NO art-style words (line weight, colors - applied by master prompt). Minimum {IMAGE_PROMPT_MIN_LEN} characters."'
new_img_prompt = '"image_prompt": "The final image text. MUST describe a physical interaction using the exact anatomy defined in the VISUAL MASTER PROMPT. Rules: (1) Use ACTION VERBS (\'slamming\', \'shielding\', \'tethering\'). (2) Anchor objects must match the style (e.g. flat outlined icon if minimalist). (3) VISUAL SPIKE: If this is a spike scene, request a Radical Framing Change (e.g. extreme close-up of a hand crushing gears). (4) NO art-style words (colors, camera types - these are applied by the master prompt). Minimum {IMAGE_PROMPT_MIN_LEN} characters."'
content = content.replace(old_img_prompt, new_img_prompt)

with open("engine/prompts.py", "w") as f:
    f.write(content)
