import os, json, psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")
POST_NUMBER=603
VARIANT_ID="lesson-605"
TARGET_ACCOUNT="matthewheartful"
VIDEO_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-603.mp4")
CAPTION_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-603.caption.txt")
AUDIO="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-010_iphone-0973CE8D.m4a")
caption=open(CAPTION_PATH,encoding="utf-8").read()
assert len(caption)<=2150 and os.path.exists(VIDEO_PATH)
clips=[("mixer/tlh-1.mp4",1.5,1.5),("mixer/tlh-68-1.mp4",2.0,1.6),("mixer/tlh-62-1.mp4",2.0,1.6),
       ("mixer/tlh-63-1.mp4",2.0,1.6),("mixer/tlh-21-3.mp4",2.0,1.6)]
source_clips=[];t=0.0
for i,(src,disk,target) in enumerate(clips):
    source_clips.append({"src":src,"order":i+1,"start_sec":round(t,3),"end_sec":round(t+target,3),
                         "src_dur_sec":disk,"target_dur_sec":target,"speedup":round(disk/target,3)})
    t+=target
ov=[("i aged cheese in a cave for 23 years.",0.0,2.0),("i said: a sensor can't hear a wheel.",2.0,2.0),
    ("it flagged row four. i wrote: sound.",4.0,2.0),("row four split. now i taste what it can't call.",6.0,1.9)]
overlays=[{"text":x,"order":i+1,"start_sec":s,"end_sec":round(s+d,1),"dur_sec":d} for i,(x,s,d) in enumerate(ov)]
metadata={
 "theme":"ai","format":"tlh","clip_count":5,"overlay_count":4,
 "source_repo":"social-autoposter/mixer",
 "theme_angle":"ai-killed-the-cheesemaker",
 "theme_label":"i told the cheesemakers' guild a sensor can't hear a wheel, kept my own logbook beside the model's, and row four split six weeks after it flagged them",
 "caption_style":"here-is-a-story-8beat",
 "description_style":"first_person_confession",
 "composition_id":"TLH-lesson-605",
 "engagement_style":"ig_side_by_side_log_entries_arc",
 "new_style":{
   "description":"the narrator's own dated log entries are quoted in pairs against the machine's entries for the same dates, so the reader watches the gap open before the event that proves the machine right, and the close is a new paired entry showing the new division of labor.",
   "example":"march 3. its log: pull and test. my log: all sound. leave them.",
   "note":"use for trades where a judgment gets written down on a date (inspections, tastings, forecasts, checklists); skip when the narrator's work leaves no written record to quote.",
   "why_existing_didnt_fit":"no reference styles were supplied; recent house arcs use struck-through rulebooks, signed verdicts recalled, kept-vs-gone tallies and vetoed warnings. none use dated paired log entries as the spine, where the timestamp gap itself is the reveal.",
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
cur.execute("select caption_text=%s from media_posts where post_number=%s",(caption,POST_NUMBER));print("caption match",cur.fetchone())
