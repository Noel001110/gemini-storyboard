# Phase X Report: Bugfixes (Script Persistence & Character Extraction)

## What was done?
Fixed two critical bugs reported by the user affecting the storyboard workflow:
1. **Frontend Script Disappearance:** The manually typed narrator script often disappeared after clicking "Planen" and reloading the page.
2. **Backend Character Extraction:** Characters like "Jake" were completely ignored by the script planner if they lacked an explicit visual description in the text.

## What bugs were fixed and how?
- **Bug 1: `script.json` Debounce Skip**
  - **Issue:** The frontend used a 2.5-second debounce timer (`_SCRIPT_DEBOUNCE_MS`) to save the Option B script. If the user pasted text and instantly clicked "Planen", the script was sent to the pipeline but never actually persisted via `/api/save_script`. A subsequent page reload would fetch an empty script from the server and overwrite the local state.
  - **Fix:** Modified `makePlan()` in `dashboard.html` to force a synchronous save. Added `if(_scriptSaveTimer) { clearTimeout(_scriptSaveTimer); await _flushScriptToServer(); }` at the beginning of the function. This guarantees the script is safely stored on the server *before* plan generation begins.

- **Bug 2: "Invent Nothing" Character Ignorance**
  - **Issue:** The LLM prompt for `analyze_script` in `dashboard.py` instructed the model to "invent nothing" and extract only explicit facts. It also required a `visual_description` for each character. When a character had no visual description (like Jake in the cafe), the LLM strictly followed the rule, failed to provide the description, and completely omitted the character from the output.
  - **Fix:** Added a targeted exception in the JSON schema prompt in `dashboard.py`: `"visual_description": "string (EXCEPTION to the invent nothing rule: if no look is described, invent a generic basic look, e.g. 'young man, casual clothes')"`. Now the AI will infer a basic look rather than skipping the character entirely, allowing them to appear in the UI so the user can generate reference sheets.

## What is the working state?
- The frontend now instantly flushes user script input to the server upon clicking "Planen", eliminating data loss on fast navigation/reloads.
- The pipeline correctly identifies and lists characters even when the script narration focuses purely on action and dialogue without physical descriptions. The user is now successfully prompted to generate character reference sheets.
