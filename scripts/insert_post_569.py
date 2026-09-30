import os, json, psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")

POST_NUMBER=569
VARIANT_ID="lesson-571"
TARGET_ACCOUNT="matthewheartful"
VIDEO_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-569.mp4")
CAPTION_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-569.caption.txt")
AUDIO="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-007_iphone-2EAC148F.m4a")
caption=open(CAPTION_PATH,encoding="utf-8").read()
assert len(caption)<=2150

clips=[("mixer/tlh-3-5.mp4",1.6,1.6),("mixer/tlh-3-4.mp4",1.6,1.6),("mixer/tlh-14-1.mp4",2.0,1.8),
       ("mixer/tlh-2-3.mp4",1.333,1.3),("mixer/tlh-2-1.mp4",1.333,1.3)]
source_clips=[]
t=0.0
for i,(src,disk,target) in enumerate(clips):
    source_clips.append({"src":src,"order":i,"start_sec":round(t,3),"end_sec":round(t+target,3),
                         "src_dur_sec":disk,"target_dur_sec":target,"speedup":round(disk/target,3)})
    t+=target

ov_texts=["i was a baseball scout for 22 years.","i said a machine can't see a kid's hands.",
          "then they thanked me for a player it found.","i said thank you. that's the confession."]
overlays=[]
for i,txt in enumerate(ov_texts):
    overlays.append({"text":txt,"order":i,"start_sec":round(i*1.9,1),"end_sec":round(i*1.9+1.9,1),"dur_sec":1.9})

metadata={
 "theme":"ai","format":"tlh","clip_count":5,"overlay_count":4,
 "source_repo":"social-autoposter/mixer",
 "theme_angle":"ai-killed-the-baseball-scout",
 "theme_label":"i said thank you for a find that wasn't mine",
 "caption_style":"here-is-a-story-8beat",
 "description_style":"first_person_confession",
 "composition_id":"TLH-lesson-571",
 "engagement_style":"ig_misattributed_praise_confession_arc",
 "applied_campaign_ids":[],
 "new_style":{
   "description":"the narrator is publicly thanked for a win the model actually produced, accepts the credit in the moment, and the confession of that accepted credit is the hinge that exposes what the human part of the job really is.",
   "example":"the owner thanked me by name for finding the kid nobody else saw. i said thank you. that's the confession.",
   "note":"use when the machine's win can plausibly land under the narrator's name and someone praises them for it out loud; skip when the narrator was openly replaced or nobody would credit them.",
   "why_existing_didnt_fit":"no reference styles were supplied; recent house arcs turn on apologies, lost bets, kept artifacts, live read-backs, and being rehired as reviewer. none use accepting praise that belonged to the model as the structural hinge.",
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
  7.6, 1080, 1920, 'draft', 'organic', %s, %s::jsonb, %s::jsonb, %s::jsonb, NOW(), NOW())
RETURNING id, post_number, variant_id, status, post_type, target_account, project_name;
""",(POST_NUMBER, VARIANT_ID, VIDEO_PATH, AUDIO, caption,
     TARGET_ACCOUNT, json.dumps(metadata), json.dumps(overlays), json.dumps(source_clips)))
print("INSERTED", cur.fetchone())
conn.commit(); conn.close()
print("OK")
