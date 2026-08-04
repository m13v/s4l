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
caption = Path(os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-364.caption.txt")).read_text()
video_path = os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-364.mp4")

# 4 clips, each filling a 2.0s slot (pre-encoded proven clips, speedup=1.0 at slot time)
clips = [("mixer/tlh-61-1.mp4",2.0),("mixer/tlh-64-1.mp4",2.0),("mixer/tlh-69-1.mp4",2.0),("mixer/tlh-78-1.mp4",2.0)]
source_clips=[]; t=0.0
for i,(src,srcdur) in enumerate(clips):
    tgt=2.0
    source_clips.append({"order":i,"src":src,"src_dur_sec":round(srcdur,3),"target_dur_sec":tgt,"speedup":round(srcdur/tgt,3),"start_sec":round(t,3),"end_sec":round(t+tgt,3)}); t+=tgt
overlays_txt=["i approved loans for 14 years.","an agent scored the stack by lunch.","cold coffee. kitchen. midnight.","the scoring was never the job."]
overlays=[{"order":i,"text":x,"start_sec":i*2.0,"end_sec":i*2.0+2.0,"dur_sec":2.0} for i,x in enumerate(overlays_txt)]
metadata={"composition_id":"TLH-lesson-364","format":"TLH","theme":"ai","theme_angle":"ai-killed-the-loan-officer","theme_label":"loan officer defeat-flip","clip_count":4,"overlay_count":4,"caption_style":"ig_defeat_flip_arc","description_style":"ig_defeat_flip_arc","source_repo":"social-autoposter/mixer","engagement_style":"ig_defeat_flip_arc"}
audio_source="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-012_iphone-49B43323.m4a")

conn=psycopg2.connect(DB); cur=conn.cursor()
cur.execute("SELECT id,post_number,status FROM media_posts WHERE variant_id=%s OR post_number=%s",("lesson-364",364))
existing=cur.fetchall()
if existing:
    print("PREEXISTING rows (aborting to avoid dup):",existing); cur.close(); conn.close(); raise SystemExit(0)
cur.execute("""
INSERT INTO media_posts
 (post_number, project_name, variant_id, video_path, audio_source, caption_text,
  caption_version, duration_sec, width, height, status, post_type, target_account,
  source_clips, overlays, metadata)
VALUES (%s, NULL, %s, %s, %s, %s, 'v1', 8, 1080, 1920, 'draft', 'organic', 'matt_diak', %s, %s, %s)
RETURNING id, post_number, variant_id, status, post_type, target_account, project_name;
""", (364,"lesson-364",video_path,audio_source,caption,json.dumps(source_clips),json.dumps(overlays),json.dumps(metadata)))
row=cur.fetchone(); conn.commit()
print("INSERTED:",row)
cur.close(); conn.close()
