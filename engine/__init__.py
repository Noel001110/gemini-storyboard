"""Engine package — alle Sub-Systeme der Render-Pipeline.

Layout:
    audio.py    — _build_final_audio, _phase_modulate_music, _duck_music_under_voice,
                  _place_sfx, _build_sfx_events, MUSIC_BEDS, SFX_FILES, _build_music_track
    render.py   — _render_clip, _assemble_clips, _mux_audio, _crossfade_clips,
                  _render_selfcheck, MOTION_LIBRARY, TRANSITION_LIBRARY
    scenes.py   — _resolve_chain_refs, _wait_for_chain_scene, _renumber_seq_pos,
                  _apply_visual_sequences_direct, _assign_phases, analyze_script,
                  split_units, segment_by_pacing, visual_prompts
    prompts.py  — IMAGE_MASTER_DEFAULT, PRESET_MASTERS, PHASE_PROMPT_ADDITIONS,
                  HOOK_PROMPT_ADDITION, _build_image_prompt

Alle Module exportieren über __all__; dashboard.py nutzt lazy imports gegen Zyklen.
Reihenfolge der Extraktion: scenes → render → audio → prompts (siehe Phase M.2–M.5).
"""