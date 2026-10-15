import os, json, psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")
POST_NUMBER=614
VARIANT_ID="lesson-616"
TARGET_ACCOUNT="matthewheartful"
VIDEO_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-614.mp4")
CAPTION_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-614.caption.txt")
AUDIO="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-023_DYUenfSRQe7.m4a")
caption=open(CAPTION_PATH,encoding="utf-8").read()
assert len(caption)<=2150 and os.path.exists(VIDEO_PATH)
clips=[("mixer/tlh-2-6.mp4",1.333,1.3),("mixer/tlh-6-4.mp4",2.0,2.0),("mixer/tlh-78-1.mp4",2.0,1.5),
       ("mixer/tlh-5.mp4",1.5,1.5),("mixer/tlh-4-1.mp4",2.0,1.5)]
source_clips=[];t=0.0
for i,(src,disk,target) in enumerate(clips):
    source_clips.append({"src":src,"order":i+1,"start_sec":round(t,3),"end_sec":round(t+target,3),
                         "src_dur_sec":disk,"target_dur_sec":target,"speedup":round(disk/target,3)})
    t+=target
ov=[("22 years inspecting elevators. my name hung in every car.",0.0,2.0),("i said: a sensor hears a motor. it can't feel a ride.",2.0,2.0),
    ("a week riding along with a red pen. nothing to correct.",4.0,2.0),("day five it flagged a car i passed. the brake was gone.",6.0,1.8)]
overlays=[{"text":x,"order":i+1,"start_sec":s,"end_sec":round(s+d,1),"dur_sec":d} for i,(x,s,d) in enumerate(ov)]
metadata={
 "theme":"ai","format":"tlh","clip_count":5,"overlay_count":4,
 "source_repo":"social-autoposter/mixer",
 "theme_angle":"ai-killed-the-elevator-inspector",
 "theme_label":"i told the code committee a sensor can't feel a ride, rode along for a week with a red pen and found nothing to correct, then it flagged a hospital elevator brake i had passed in march",
 "caption_style":"here-is-a-story-8beat",
 "description_style":"first_person_confession",
 "composition_id":"TLH-lesson-616",
 "engagement_style":"ig_ride_along_nothing_to_correct_arc",
 "new_style":{
   "description":"the narrator is sent to shadow the machine for a fixed stretch to log its errors; tension builds through a day-by-day blank error log, and the defeat lands when the machine's one flag is the narrator's own past miss.",
   "example":"monday, nothing. tuesday, nothing. by thursday i was checking its work twice.",
   "note":"use for inspection or review trades where someone could plausibly be assigned to watch the machine work; skip when the narrator has no reason to shadow it in person.",
   "why_existing_didnt_fit":"no reference styles were supplied; recent house arcs use sealed envelopes, rigged tests, appeals and checker-becomes-checked. none use an empty error log over a fixed observation window as the spine, where the absence of mistakes is itself the reveal.",
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
cur.execute("select caption_text=%s from media_posts where post_number=%s",(caption,POST_NUMBER));print("caption match",cur.fetchone())
