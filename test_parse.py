import json, re
d = {"cid":"thestick", "vid":"why_you_will_never_get_rich_an", "text":"hello world"}
vid = re.sub(r"[^\w\-]", "_", d.get("vid", ""))
print(f"vid is '{vid}'")
if not vid:
    print("Kein Video ausgewählt")
