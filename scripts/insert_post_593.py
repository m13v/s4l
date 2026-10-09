import os, json, psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")

POST_NUMBER=593
VARIANT_ID="lesson-595"
TARGET_ACCOUNT="matt_diak"
VIDEO_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-593.mp4")
CAPTION_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-593.caption.txt")
AUDIO="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-014_iphone-DBAA6455.m4a")
caption=open(CAPTION_PATH,encoding="utf-8").read()
assert len(caption)<=2150
assert os.path.exists(VIDEO_PATH)

clips=[("mixer/tlh-6-2.mp4",2.0,1.8),("mixer/tlh-2-6.mp4",1.333,1.3),("mixer/tlh-1.mp4",1.5,1.5),
       ("mixer/tlh-13-1.mp4",2.0,1.6),("mixer/tlh-9-1.mp4",2.0,1.6)]
source_clips=[]
t=0.0
for i,(src,disk,target) in enumerate(clips):
    source_clips.append({"src":src,"order":i+1,"start_sec":round(t,3),"end_sec":round(t+target,3),
                         "src_dur_sec":disk,"target_dur_sec":target,"speedup":round(disk/target,3)})
    t+=target

ov=[("i tuned pianos by ear for 22 years.",0.0,2.0),("i wrote: a phone can't hear a piano.",2.0,2.0),
    ("my client's grandson tuned hers. it was better.",4.0,2.0),("now the app does pitch. i fix the rest.",6.0,1.8)]
overlays=[{"text":txt,"order":i+1,"start_sec":s,"end_sec":round(s+d,1),"dur_sec":d} for i,(txt,s,d) in enumerate(ov)]

metadata={
 "theme":"ai","format":"tlh","clip_count":5,"overlay_count":4,
 "source_repo":"social-autoposter/mixer",
 "theme_angle":"ai-killed-the-piano-tuner",
 "theme_label":"i wrote that a phone can't hear a piano, a loyal client went quiet, and when i knocked her grandson's phone tuning was better than mine",
 "caption_style":"here-is-a-story-8beat",
 "description_style":"first_person_confession",
 "composition_id":"TLH-lesson-595",
 "engagement_style":"ig_silent_client_knock_arc",
 "new_style":{
   "description":"a long-time customer quietly stops booking, the narrator goes to their door, and the customer casually shows that a machine has been doing the job better; the hinge is the narrator praising the machine's work before learning who did it.",
   "example":"i said whoever did this knows what they're doing. she said her grandson did it with his phone.",
   "note":"use for trades with repeat clients who can churn silently (tuners, groomers, bookkeepers, tailors); skip when there is no recurring client relationship or the narrator never meets the work firsthand.",
   "why_existing_didnt_fit":"no reference styles were supplied; recent house arcs use blind tests, vetoed warnings, archive re-audits, tiebreaks and running counters. none turn on silent churn discovered face to face, with the narrator unknowingly complimenting the machine's output.",
   "target_chars":85
 }
}

conn=psycopg2.connect(url, connect_timeout=20); cur=conn.cursor()
cur.execute("SELECT post_number,variant_id FROM media_posts WHERE post_number=%s OR variant_id=%s;",(POST_NUMBER,VARIANT_ID))
existing=cur.fetchall()
if existing:
    print("ABORT existing rows:",existing); conn.close(); raise SystemExit(1)

cur.execute("""
INSERT INTO media_posts
 (post_number, project_name, variant_id, video_path, audio_source, caption_text, caption_version,
  duration_sec, width, height, status, post_type, target_account, metadata, overlays, source_clips,
  created_at, updated_at)
VALUES
 (%s, NULL, %s, %s, %s, %s, 'v1',
  7.8, 1080, 1920, 'draft', 'organic', %s, %s::jsonb, %s::jsonb, %s::jsonb, NOW(), NOW())
RETURNING id, post_number, variant_id, status, post_type, target_account, project_name;
""",(POST_NUMBER, VARIANT_ID, VIDEO_PATH, AUDIO, caption,
     TARGET_ACCOUNT, json.dumps(metadata), json.dumps(overlays), json.dumps(source_clips)))
print("INSERTED", cur.fetchone())
conn.commit(); conn.close()
print("OK")
