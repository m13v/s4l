import os, json, psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")
POST_NUMBER=630
VARIANT_ID="lesson-633"
TARGET_ACCOUNT="matt_diak"
VIDEO_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-630.mp4")
CAPTION_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-630.caption.txt")
AUDIO="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-005_reel-B.m4a")
caption=open(CAPTION_PATH,encoding="utf-8").read()
assert len(caption)<=2150 and os.path.exists(VIDEO_PATH)
clips=[("mixer/tlh-4.mp4",1.5,1.3),("mixer/tlh-62-1.mp4",2.0,1.3),("mixer/tlh-4-2.mp4",2.0,1.3),
       ("mixer/tlh-3-2.mp4",1.6,1.3),("mixer/tlh-2.mp4",1.5,1.3),("mixer/tlh-6-4.mp4",2.0,1.3)]
source_clips=[];t=0.0
for i,(src,disk,target) in enumerate(clips):
    source_clips.append({"src":src,"order":i+1,"start_sec":round(t,3),"end_sec":round(t+target,3),
                         "src_dur_sec":disk,"target_dur_sec":target,"speedup":round(disk/target,3)})
    t+=target
ov=[("24 years judging gymnastics. i said a camera can't see what a bent knee costs.",0.0,2.0),
    ("the head judge showed us 30 vaults. eight were the same vault, renamed.",2.0,2.0),
    ("i gave the same vault two different scores. six times out of eight.",4.0,2.0),
    ("the camera never beat my eye. it just had the same eye every time.",6.0,1.8)]
overlays=[{"text":x,"order":i+1,"start_sec":s,"end_sec":round(s+d,1),"dur_sec":d} for i,(x,s,d) in enumerate(ov)]
metadata={
 "theme":"ai","format":"tlh","clip_count":6,"overlay_count":4,
 "source_repo":"social-autoposter/mixer",
 "theme_angle":"ai-killed-the-gymnastics-judge",
 "theme_label":"i told a room of forty judges a camera can't see what a bent knee costs, the head judge slipped eight renamed repeat vaults into a thirty-vault calibration, i scored the same vault two different numbers on six of the eight pairs while the camera scored every pair identically, and realised i never lost to the machine, i lost to my own inconsistency",
 "caption_style":"here-is-a-story-8beat",
 "description_style":"first_person_confession",
 "composition_id":"TLH-lesson-633",
 "engagement_style":"ig_self_disagreement_replay_arc",
 "new_style":{
   "description":"the machine never beats the narrator head-to-head; a hidden-repeat test shows the narrator scoring identical input differently, so the exposed flaw is self-inconsistency and the machine's only edge is agreeing with itself.",
   "example":"i gave the same vault two different scores. six times out of eight.",
   "note":"use when the craft is repeated scoring or grading of near-identical inputs where a blind duplicate can be slipped in; skip when each case is one-off and no repeat test is plausible.",
   "why_existing_didnt_fit":"no reference styles were supplied; recent house arcs pit the narrator against the machine via blind panels, wagers, answer keys and deferred harvests. in all of them the machine wins the comparison. none make the narrator lose to themself, with the machine merely consistent.",
   "target_chars":70
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
