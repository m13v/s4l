import os, json, psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")

POST_NUMBER=586
VARIANT_ID="lesson-588"
TARGET_ACCOUNT="matthewheartful"
VIDEO_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-586.mp4")
CAPTION_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-586.caption.txt")
AUDIO="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-004_reel-A.m4a")
caption=open(CAPTION_PATH,encoding="utf-8").read()
assert len(caption)<=2150
assert os.path.exists(VIDEO_PATH)

clips=[("mixer/tlh-5.mp4",1.5,1.5),("mixer/tlh-21-3.mp4",2.0,2.0),("mixer/tlh-3-2.mp4",1.6,1.5),
       ("mixer/tlh-2-1.mp4",1.333,1.3),("mixer/tlh-78-1.mp4",2.0,1.5)]
source_clips=[]
t=0.0
for i,(src,disk,target) in enumerate(clips):
    source_clips.append({"src":src,"order":i+1,"start_sec":round(t,3),"end_sec":round(t+target,3),
                         "src_dur_sec":disk,"target_dur_sec":target,"speedup":round(disk/target,3)})
    t+=target

ov=[("i assessed trees for 19 years.",0.0,2.0),("i said a model can't hear a tree.",2.0,2.0),
    ("i overruled its flag. the oak fell in february.",4.0,2.0),("now i drill where the model points.",6.0,1.8)]
overlays=[{"text":txt,"order":i+1,"start_sec":s,"end_sec":round(s+d,1),"dur_sec":d} for i,(txt,s,d) in enumerate(ov)]

metadata={
 "theme":"ai","format":"tlh","clip_count":5,"overlay_count":4,
 "source_repo":"social-autoposter/mixer",
 "theme_angle":"ai-killed-the-arborist",
 "theme_label":"i said a model can't hear a tree, overruled its flag in my own handwriting, and the oak fell where the model said the wound was",
 "caption_style":"here-is-a-story-8beat",
 "description_style":"first_person_confession",
 "composition_id":"TLH-lesson-588",
 "engagement_style":"ig_vetoed_warning_vindicated_arc",
 "applied_campaign_ids":[],
 "new_style":{
   "description":"the machine raises a flag first, the narrator overrules it in writing with a stated reason, and the physical world later sides with the machine; the hinge is the narrator rereading their own veto note next to the incident report.",
   "example":"\"model overreads the lean.\" in my handwriting. the wound was in the scan the whole time.",
   "note":"use when the craft has a sign-off step where the human can veto a machine warning on the record (inspectors, assessors, reviewers, approvers); skip when the human never had the chance to overrule, or when the machine was wrong.",
   "why_existing_didnt_fit":"no reference styles were supplied; recent house arcs turn on tiebreaks between two machines, old signatures being audited, kept artifacts, apologies, and live read-backs. none stage the human actively vetoing a correct machine flag and then reading the veto back after reality settles it.",
   "target_chars":90
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
