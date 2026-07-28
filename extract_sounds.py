import os
import subprocess
import re

TIMESTAMPS = """0:00 - Chinese Gong
0:06 - Taco Bell
0:08 - Awkward Moment
0:10 - Awkward Pause (1-2)
0:15 - Camera Flash
0:16 - Collect Gold
0:18 - Bone Crack
0:19 - Topac Engle
0:25 - Skibidi Boom
0:29 - Blinking
0:30 - Magic Spells
0:32 - RUDE - Eternal Youth
0:38 - Vine Boom (Slowed)
0:42 - Lie Detector
0:43 - Man falling down the Stairs
0:45 - Please Stand By
0:50 - Ultra Instinct
0:55 - The Good Ending
1:02 - Cash Register (1-5)
1:14 - Cinematic Boom
1:18 - Breast Twitch
1:20 - Thank You!
1:21 - Discord Call (Join)
1:22 - Discord Call (Leave)
1:23 - Discord Notification
1:24 - Discord Ringtone
1:30 - Undertakers Bell
1:33 - iPhone (Send - Recieve)
1:35 - Build Up
1:38 - 999 Credit Score Siren
1:42 - Daft Punk - Robot Rock
1:46 - Hell's Kitchen Suspense
1:49 - DA - Bells (1-3)
1:53 - TiK-TiK
1:54 - Bua Wa Wa Wa Wa
1:57 - Munch Bite
1:58 - French Meme
2:02 - God Damn!
2:03 - Travis Scott Meme
2:07 - Goku Power
2:11 - Anime Punch
2:13 - Lego Breaking
2:15 - Sakhteman Pezeshkan
2:19 - Gay Echo Voice
2:24 - Metal Pipe Falling
2:27 - TikTok Mentality
2:28 - Twitch Alert
2:30 - TF2 Om Nom Nom
2:33 - Goofy Ahh Car Honk
2:36 - Police Radio Beep
2:37 - Glass breaking
2:38 - Flashbang (Loud)"""

def parse_time(time_str):
    parts = list(map(int, time_str.split(':')))
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    elif len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0

def download_audio(url, output_path):
    print(f"Downloading {url} to {output_path}")
    subprocess.run([
        'yt-dlp',
        '--extract-audio',
        '--audio-format', 'mp3',
        '--extractor-args', 'youtube:player_client=android',
        '-o', output_path,
        url
    ], check=True)

def split_and_clean_audio(input_file, out_dir):
    lines = TIMESTAMPS.strip().split('\n')
    entries = []
    for line in lines:
        match = re.match(r'(\d+:\d+)\s+-\s+(.*)', line)
        if match:
            time_str = match.group(1)
            name = match.group(2).strip().replace('/', '-').replace('\\', '-')
            seconds = parse_time(time_str)
            entries.append({"time": seconds, "name": name})

    for i in range(len(entries)):
        start_time = entries[i]["time"]
        end_time = entries[i+1]["time"] if i + 1 < len(entries) else start_time + 10 # Assume max 10s for the last one
        
        duration = end_time - start_time
        name = entries[i]["name"]
        
        # Sanitize filename
        safe_name = "".join([c for c in name if c.isalnum() or c in ' -_']).rstrip()
        out_file = os.path.join(out_dir, f"{safe_name}.mp3")
        
        print(f"Extracting: {name} ({start_time}s to {start_time+duration}s)")
        
        # ffmpeg command to cut and remove silence
        filter_str = (
            "silenceremove=start_periods=1:start_duration=0:start_threshold=-45dB,"
            "areverse,"
            "silenceremove=start_periods=1:start_duration=0:start_threshold=-45dB,"
            "areverse"
        )
        
        cmd = [
            'ffmpeg', '-y',
            '-ss', str(start_time),
            '-t', str(duration),
            '-i', input_file,
            '-af', filter_str,
            out_file
        ]
        
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

if __name__ == "__main__":
    out_dir = "/Users/noel/gemini-storyboard/Sounds"
    os.makedirs(out_dir, exist_ok=True)
    
    compilation_mp3 = os.path.join(out_dir, "compilation_full.mp3")
    money_mp3 = os.path.join(out_dir, "Money_Cash_Sound.mp3")
    
    if not os.path.exists(compilation_mp3):
        download_audio("https://www.youtube.com/watch?v=kxKCRaAEwAY", compilation_mp3)
    
    # Download the short
    if not os.path.exists(money_mp3):
        tmp_money = os.path.join(out_dir, "tmp_money.mp3")
        download_audio("https://www.youtube.com/shorts/SmJKvWJNnRc", tmp_money)
        print("Cleaning up Money Cash Sound...")
        filter_str = (
            "silenceremove=start_periods=1:start_duration=0:start_threshold=-45dB,"
            "areverse,"
            "silenceremove=start_periods=1:start_duration=0:start_threshold=-45dB,"
            "areverse"
        )
        subprocess.run([
            'ffmpeg', '-y', '-i', tmp_money, '-af', filter_str, money_mp3
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(tmp_money):
            os.remove(tmp_money)

    print("Splitting compilation...")
    split_and_clean_audio(compilation_mp3, out_dir)
    print("Done!")
