import os, json, psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")
POST_NUMBER=625
VARIANT_ID="lesson-628"
TARGET_ACCOUNT="matthewheartful"
VIDEO_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-625.mp4")
CAPTION_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-625.caption.txt")
AUDIO="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-004_reel-A.m4a")
caption=open(CAPTION_PATH,encoding="utf-8").read()
assert len(caption)<=2150 and os.path.exists(VIDEO_PATH)
clips=[("mixer/tlh-3-4.mp4",1.6,1.5),("mixer/tlh-4-4.mp4",2.0,1.3),("mixer/tlh-61-1.mp4",2.0,1.3),
       ("mixer/tlh-64-1.mp4",2.0,1.3),("mixer/tlh-2-3.mp4",1.333,1.3),("mixer/tlh-5-3.mp4",2.667,1.1)]
source_clips=[];t=0.0
for i,(src,disk,target) in enumerate(clips):
    source_clips.append({"src":src,"order":i+1,"start_sec":round(t,3),"end_sec":round(t+target,3),
                         "src_dur_sec":disk,"target_dur_sec":target,"speedup":round(disk/target,3)})
    t+=target
ov=[("18 years matching car paint by eye. i told the shop a camera can't see the flop.",0.0,2.0),
    ("i negotiated a veto: pearls and tri-coats go through me. owner wrote it on the board.",2.0,2.0),
    ("three vetoes. three spray-out cards in the sun. mine had the halo every time.",4.0,2.0),
    ("month two. my name was still on the veto line. nobody had bothered to erase it.",6.0,1.8)]
overlays=[{"text":x,"order":i+1,"start_sec":s,"end_sec":round(s+d,1),"dur_sec":d} for i,(x,s,d) in enumerate(ov)]
metadata={
 "theme":"ai","format":"tlh","clip_count":6,"overlay_count":4,
 "source_repo":"social-autoposter/mixer",
 "theme_angle":"ai-killed-the-paint-matcher",
 "theme_label":"i told the body shop a camera can't see metallic flop, negotiated a personal veto on pearls and tri-coats, lost all three vetoes to the camera's spray-out card in the sun, and my name stayed on the whiteboard veto line nobody bothered to erase",
 "caption_style":"here-is-a-story-8beat",
 "description_style":"first_person_confession",
 "composition_id":"TLH-lesson-628",
 "engagement_style":"ig_negotiated_veto_backfires_arc",
 "new_style":{
   "description":"the narrator accepts the machine only on condition of a personal veto over the hardest cases; every veto they exercise loses, and the running veto count becomes the scoreboard of their own error.",
   "example":"three vetoes. three cards in the sun. all three losses on my card.",
   "note":"use for trades where the expert can plausibly bargain for final say on a hard category (color, pearls, edge cases); skip when the narrator has no leverage to negotiate a carve-out.",
   "why_existing_didnt_fit":"no reference styles were supplied; recent house arcs use planted complaints, ride-alongs with an empty error log, public wagers and appeals. none use a negotiated carve-out that the narrator keeps exercising and keeps losing, where the concession itself is the measurement.",
   "target_chars":80
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
