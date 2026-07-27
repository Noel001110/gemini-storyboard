import time
import json
import sqlite3

db = sqlite3.connect('store/pipeline.db')
db.row_factory = sqlite3.Row
cursor = db.cursor()

# Get existing youtube shorts
cursor.execute("SELECT * FROM upload_queue WHERE platform='youtube' AND render_target LIKE 'short_%' AND status='uploaded'")
rows = cursor.fetchall()

current_time = time.time()
interval = 4 * 3600

count = 0
for r in rows:
    cursor.execute("SELECT id FROM upload_queue WHERE platform='tiktok' AND vid=? AND render_target=? AND short_id=?", (r['vid'], r['render_target'], r['short_id']))
    if cursor.fetchone():
        continue
        
    sched = current_time + (count * interval)
    
    cursor.execute("""
        INSERT INTO upload_queue 
        (cid, vid, render_target, short_id, file_path, title, description, tags, category_id, privacy_status, scheduled_at, status, platform, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'tiktok', ?, ?)
    """, (r['cid'], r['vid'], r['render_target'], r['short_id'], r['file_path'], r['title'], r['description'], r['tags'], r['category_id'], r['privacy_status'], sched, current_time, current_time))
    
    count += 1

db.commit()
print(f"Scheduled {count} shorts for TikTok.")
db.close()
