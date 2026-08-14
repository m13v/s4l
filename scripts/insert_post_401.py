import os, json, psycopg2
from pathlib import Path

env = {}
for line in Path(os.path.expanduser("~/social-autoposter/.env")).read_text().splitlines():
    if line.startswith("DATABASE_URL="):
        env["DATABASE_URL"] = line.split("=",1)[1].strip().strip('"').strip("'")
DB = env.get("DATABASE_URL")
if not DB:
    import subprocess
    DB = subprocess.check_output(["security","find-generic-password","-s","s4l-database-url","-w"]).decode().strip()
caption = Path(os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-401.caption.txt")).read_text()
video_path = os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-401.mp4")

# 4 fresh remixed clips; durSec == on-disk length so speedup=1.0 at slot time
clips = [("mixer/tlh-14-1.mp4",2.0),("mixer/tlh-6-3.mp4",2.0),("mixer/tlh-3-3.mp4",1.6),("mixer/tlh-3-2.mp4",1.6)]
source_clips=[]; t=0.0
for i,(src,srcdur) in enumerate(clips):
    tgt=srcdur
    source_clips.append({"order":i,"src":src,"src_dur_sec":round(srcdur,3),"target_dur_sec":round(tgt,3),"speedup":round(srcdur/tgt,3),"start_sec":round(t,3),"end_sec":round(t+tgt,3)}); t+=tgt
overlays_txt=["i reported depositions for 19 years.","an agent transcribed the day in minutes.","cold coffee. kitchen. midnight.","the keystrokes were never the job."]
overlays=[{"order":i,"text":x,"start_sec":round(i*1.8,3),"end_sec":round(i*1.8+1.8,3),"dur_sec":1.8} for i,x in enumerate(overlays_txt)]
metadata={"composition_id":"TLH-lesson-401","format":"TLH","theme":"ai","theme_angle":"ai-killed-the-court-reporter","theme_label":"court reporter defeat-flip","clip_count":4,"overlay_count":4,"caption_style":"ig_defeat_flip_arc","description_style":"ig_defeat_flip_arc","source_repo":"social-autoposter/mixer","engagement_style":"ig_defeat_flip_arc"}
audio_source="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-007_iphone-2EAC148F.m4a")

conn=psycopg2.connect(DB); cur=conn.cursor()
cur.execute("SELECT id,post_number,status FROM media_posts WHERE variant_id=%s OR post_number=%s",("lesson-401",401))
existing=cur.fetchall()
if existing:
    print("PREEXISTING rows (aborting to avoid dup):",existing); cur.close(); conn.close(); raise SystemExit(0)
cur.execute("""
INSERT INTO media_posts
 (post_number, project_name, variant_id, video_path, audio_source, caption_text,
  caption_version, duration_sec, width, height, status, post_type, target_account,
  source_clips, overlays, metadata)
VALUES (%s, NULL, %s, %s, %s, %s, 'v1', 7.2, 1080, 1920, 'draft', 'organic', 'matt_diak', %s, %s, %s)
RETURNING id, post_number, variant_id, status, post_type, target_account, project_name;
""", (401,"lesson-401",video_path,audio_source,caption,json.dumps(source_clips),json.dumps(overlays),json.dumps(metadata)))
row=cur.fetchone(); conn.commit()
print("INSERTED:",row)
cur.close(); conn.close()
