import os, json, psycopg2
from pathlib import Path

env = {}
for line in Path(os.path.expanduser("~/social-autoposter/.env")).read_text().splitlines():
    if line.startswith("DATABASE_URL="):
        env["DATABASE_URL"] = line.split("=",1)[1].strip().strip('"').strip("'")
DB = env["DATABASE_URL"]
caption = Path(os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-266.caption.txt")).read_text()
video_path = os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-266.mp4")

clips = [("mixer/tlh-45-1.mp4",1.733333),("mixer/tlh-63-1.mp4",2.0),("mixer/tlh-51-1.mp4",2.0),("mixer/tlh-78-1.mp4",2.0),("mixer/tlh-25-1.mp4",1.933333)]
source_clips=[]; t=0.0
for i,(src,srcdur) in enumerate(clips):
    tgt=1.6
    source_clips.append({"order":i,"src":src,"src_dur_sec":round(srcdur,3),"target_dur_sec":tgt,"speedup":round(srcdur/tgt,3),"start_sec":round(t,3),"end_sec":round(t+tgt,3)}); t+=tgt
overlays_txt=["i transcribed charts for 14 years.","an agent cleared nine hours overnight.","cold coffee. kitchen counter. midnight.","the transcribing was never the job."]
overlays=[{"order":i,"text":x,"start_sec":i*2.0,"end_sec":i*2.0+2.0,"dur_sec":2.0} for i,x in enumerate(overlays_txt)]
metadata={"composition_id":"TLH-lesson-232","format":"TLH","theme":"ai","theme_angle":"ai-killed-the-medical-transcriptionist","theme_label":"medical transcriptionist defeat-flip","clip_count":5,"overlay_count":4,"caption_style":"ig_defeat_flip_arc","description_style":"ig_defeat_flip_arc","source_repo":"social-autoposter/mixer","engagement_style":"ig_defeat_flip_arc"}
audio_source="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-004_reel-A.m4a")

conn=psycopg2.connect(DB); cur=conn.cursor()
cur.execute("SELECT id,post_number,status FROM media_posts WHERE variant_id=%s OR post_number=%s",("lesson-232",266))
existing=cur.fetchall()
if existing:
    print("PREEXISTING rows (aborting to avoid dup):",existing); cur.close(); conn.close(); raise SystemExit(0)
cur.execute("""
INSERT INTO media_posts
 (post_number, project_name, variant_id, video_path, audio_source, caption_text,
  caption_version, duration_sec, width, height, status, post_type, target_account,
  source_clips, overlays, metadata)
VALUES (%s, NULL, %s, %s, %s, %s, 'v1', 8, 1080, 1920, 'draft', 'organic', 'matthewheartful', %s, %s, %s)
RETURNING id, post_number, variant_id, status, post_type, target_account, project_name;
""", (266,"lesson-232",video_path,audio_source,caption,json.dumps(source_clips),json.dumps(overlays),json.dumps(metadata)))
row=cur.fetchone(); conn.commit()
print("INSERTED:",row)
cur.close(); conn.close()
