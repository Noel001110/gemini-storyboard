import os
import glob
import subprocess
import re

def get_non_silent_chunks(file_path):
    cmd = [
        'ffmpeg', '-i', file_path,
        '-af', 'silencedetect=noise=-40dB:d=0.3',
        '-f', 'null', '-'
    ]
    
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    output = result.stderr
    
    silence_starts = []
    silence_ends = []
    
    for line in output.split('\n'):
        match_start = re.search(r'silence_start:\s+([\d\.]+)', line)
        if match_start:
            silence_starts.append(float(match_start.group(1)))
            
        match_end = re.search(r'silence_end:\s+([\d\.]+)', line)
        if match_end:
            silence_ends.append(float(match_end.group(1)))
            
    # ffmpeg output gives us the silent segments. We want the NON-silent segments.
    # To get duration, we can use ffprobe
    duration_cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
    duration_result = subprocess.run(duration_cmd, stdout=subprocess.PIPE, text=True)
    try:
        total_duration = float(duration_result.stdout.strip())
    except:
        return [(0, None)] # Fallback
        
    non_silent_chunks = []
    current_time = 0.0
    
    for i in range(len(silence_starts)):
        # If silence starts right away, the first chunk starts after the silence ends
        if silence_starts[i] > current_time + 0.1: 
            non_silent_chunks.append((current_time, silence_starts[i]))
            
        if i < len(silence_ends):
            current_time = silence_ends[i]
            
    # Add the last chunk
    if current_time < total_duration - 0.1:
        non_silent_chunks.append((current_time, total_duration))
        
    return non_silent_chunks

def main():
    sounds_dir = "/Users/noel/gemini-storyboard/Sounds"
    mp3_files = glob.glob(os.path.join(sounds_dir, "*.mp3"))
    
    for file_path in mp3_files:
        filename = os.path.basename(file_path)
        
        # Skip the original compilation download and already split files
        if filename == "compilation_full.mp3" or "_" in filename and filename.split("_")[-1].replace(".mp3","").isdigit():
            continue
            
        print(f"Analyzing {filename}...")
        
        chunks = get_non_silent_chunks(file_path)
        
        if len(chunks) > 1:
            print(f"  -> Found {len(chunks)} separate sounds in {filename}! Splitting...")
            name_without_ext = os.path.splitext(filename)[0]
            
            # Export each chunk
            for i, (start, end) in enumerate(chunks):
                out_name = f"{name_without_ext}_{i+1}.mp3"
                out_path = os.path.join(sounds_dir, out_name)
                
                # Extract chunk
                duration = end - start
                cmd = [
                    'ffmpeg', '-y',
                    '-ss', str(max(0, start - 0.1)), # padding
                    '-t', str(duration + 0.2), # padding
                    '-i', file_path,
                    out_path
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"     Saved {out_name}")
                
            # Delete the original file to keep it clean
            os.remove(file_path)
        else:
            print(f"  -> Only 1 sound detected. Keeping original.")

if __name__ == "__main__":
    main()
