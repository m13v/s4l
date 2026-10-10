import os, json, psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")

POST_NUMBER=597
VARIANT_ID="lesson-599"
TARGET_ACCOUNT="matt_diak"
VIDEO_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-597.mp4")
CAPTION_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-597.caption.txt")
AUDIO="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-011_iphone-E1760FF1.m4a")
caption=open(CAPTION_PATH,encoding="utf-8").read()
assert len(caption)<=2150
assert os.path.exists(VIDEO_PATH)

clips=[("mixer/tlh-5-2.mp4",2.667,2.0),("mixer/tlh-61-1.mp4",2.0,1.5),("mixer/tlh-6-3.mp4",2.0,1.5),
       ("mixer/tlh-59-1.mp4",2.0,1.5),("mixer/tlh-3-3.mp4",1.6,1.5)]
source_clips=[]
t=0.0
for i,(src,disk,target) in enumerate(clips):
    source_clips.append({"src":src,"order":i+1,"start_sec":round(t,3),"end_sec":round(t+target,3),
                         "src_dur_sec":disk,"target_dur_sec":target,"speedup":round(disk/target,3)})
    t+=target

ov=[("i graded coins by eye for 22 years.",0.0,2.0),("i said: a camera can't see luster.",2.0,2.0),
    ("an app caught a cleaned coin i'd sold.",4.0,2.0),("now it scans first. i make the call.",6.0,2.0)]
overlays=[{"text":txt,"order":i+1,"start_sec":s,"end_sec":round(s+d,1),"dur_sec":d} for i,(txt,s,d) in enumerate(ov)]

metadata={
 "theme":"ai","format":"tlh","clip_count":5,"overlay_count":4,
 "source_repo":"social-autoposter/mixer",
 "theme_angle":"ai-killed-the-coin-grader",
 "theme_label":"i told the coin club a camera can't see luster, then a regular's app flagged a cleaned morgan dollar i had graded mint state and sold him",
 "caption_style":"here-is-a-story-8beat",
 "description_style":"first_person_confession",
 "composition_id":"TLH-lesson-599",
 "engagement_style":"ig_signed_verdict_recall_arc",
 "new_style":{
   "description":"an old verdict the narrator personally signed (their handwriting, their name on the record) walks back through the door, the machine overturns it, and the narrator ends by displaying the overturned artifact as a keepsake.",
   "example":"my handwriting was on the flip. the app said cleaned. it was right.",
   "note":"use for trades that leave a signed, durable judgment behind (graders, appraisers, inspectors, certifiers); skip when the narrator's work leaves no artifact with their name on it.",
   "why_existing_didnt_fit":"no reference styles were supplied; recent house arcs use blind tests, silent client churn, vetoed warnings and live side-by-sides. none hinge on the narrator's own past signed verdict returning years later and being reversed in front of the customer.",
   "target_chars":75
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
  8.0, 1080, 1920, 'draft', 'organic', %s, %s::jsonb, %s::jsonb, %s::jsonb, NOW(), NOW())
RETURNING id, post_number, variant_id, status, post_type, target_account, project_name;
""",(POST_NUMBER, VARIANT_ID, VIDEO_PATH, AUDIO, caption,
     TARGET_ACCOUNT, json.dumps(metadata), json.dumps(overlays), json.dumps(source_clips)))
print("INSERTED", cur.fetchone())
conn.commit(); conn.close()
print("OK")
