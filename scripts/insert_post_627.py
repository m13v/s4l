import os, json, psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")
POST_NUMBER=627
VARIANT_ID="lesson-630"
TARGET_ACCOUNT="matt_diak"
VIDEO_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-627.mp4")
CAPTION_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-627.caption.txt")
AUDIO="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-012_iphone-49B43323.m4a")
caption=open(CAPTION_PATH,encoding="utf-8").read()
assert len(caption)<=2150 and os.path.exists(VIDEO_PATH)
clips=[("mixer/tlh-2-4.mp4",1.333,1.3),("mixer/tlh-3-1.mp4",1.6,1.5),("mixer/tlh-5-1.mp4",2.667,1.3),
       ("mixer/tlh-48-1.mp4",1.767,1.3),("mixer/tlh-45-1.mp4",1.733,1.3),("mixer/tlh-14-1.mp4",2.0,1.1)]
source_clips=[];t=0.0
for i,(src,disk,target) in enumerate(clips):
    source_clips.append({"src":src,"order":i+1,"start_sec":round(t,3),"end_sec":round(t+target,3),
                         "src_dur_sec":disk,"target_dur_sec":target,"speedup":round(disk/target,3)})
    t+=target
ov=[("20 years engraving sheet music. i said software can't place a page turn.",0.0,2.0),
    ("i wrote the publisher eleven reasons. kept the letter in my drawer for the first disaster.",2.0,2.0),
    ("the orchestra read-through. the conductor stopped twice. both times on my page turns.",4.0,2.0),
    ("i reread the letter. reason four was describing my own part.",6.0,1.8)]
overlays=[{"text":x,"order":i+1,"start_sec":s,"end_sec":round(s+d,1),"dur_sec":d} for i,(x,s,d) in enumerate(ov)]
metadata={
 "theme":"ai","format":"tlh","clip_count":6,"overlay_count":4,
 "source_repo":"social-autoposter/mixer",
 "theme_angle":"ai-killed-the-music-engraver",
 "theme_label":"i told the publisher software can't place a page turn, wrote an unsent eleven-reason letter saved for the first disaster, the orchestra read-through stopped twice on my own page turns while the software's parts sailed, and reason four of my letter described my own part",
 "caption_style":"here-is-a-story-8beat",
 "description_style":"first_person_confession",
 "composition_id":"TLH-lesson-630",
 "engagement_style":"ig_unsent_letter_reread_arc",
 "new_style":{
   "description":"the narrator writes a protest letter against the machine and saves it unsent for the first disaster; after the defeat they reread it and each objection turns out to describe their own work.",
   "example":"reason four said it would put turns mid-run. it was describing my part.",
   "note":"use when the expert would plausibly put objections in writing (memo, letter, list) before the test; skip when the defeat is instant and there is no time to draft anything.",
   "why_existing_didnt_fit":"no reference styles were supplied; recent house arcs use planted complaints, negotiated vetoes, apprentices switching sides and old quotes read back by others. none use a private, unsent document the narrator rereads alone, where the objections become a self-audit.",
   "target_chars":75
 }
}
conn=psycopg2.connect(url,connect_timeout=20);cur=conn.cursor()
cur.execute("SELECT post_number,variant_id FROM media_posts WHERE post_number=%s OR variant_id=%s;",(POST_NUMBER,VARIANT_ID))
ex=cur.fetchall()
if ex: print("ABORT existing rows:",ex); raise SystemExit(1)
cur.execute("""INSERT INTO media_posts
 (post_number, project_name, variant_id, video_path, audio_source, caption_text, caption_version,
  duration_sec, width, height, status, post_type, target_account, metadata, overlays, source_clips, created_at, updated_at)
VALUES (%s, NULL, %s, %s, %s, %s, 'v1', 8.0, 1080, 1920, 'draft', 'organic', %s, %s::jsonb, %s::jsonb, %s::jsonb, NOW(), NOW())
RETURNING id, post_number, variant_id, status, post_type, target_account, project_name;""",
(POST_NUMBER,VARIANT_ID,VIDEO_PATH,AUDIO,caption,TARGET_ACCOUNT,json.dumps(metadata),json.dumps(overlays),json.dumps(source_clips)))
print("INSERTED",cur.fetchone());conn.commit()
cur.execute("select caption_text=%s, metadata->>'engagement_style', metadata->>'theme_angle', jsonb_array_length(source_clips), jsonb_array_length(overlays) from media_posts where post_number=%s",(caption,POST_NUMBER));print("verify",cur.fetchone())
