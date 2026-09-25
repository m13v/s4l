import os, json, psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0]

POST_NUMBER=546
VARIANT_ID="lesson-548"
TARGET_ACCOUNT="matt_diak"
VIDEO_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-546.mp4")
CAPTION_PATH=os.path.expanduser("~/social-autoposter/mixer/remotion/out/post-546.caption.txt")
AUDIO="local:"+os.path.expanduser("~/social-autoposter/mixer/audio/track-001_DWBsamriWgC.m4a")
caption=open(CAPTION_PATH,encoding="utf-8").read()

clip_srcs=["mixer/tlh-2-1.mp4","mixer/tlh-6-4.mp4","mixer/tlh-3-5.mp4","mixer/tlh-5-3.mp4","mixer/tlh-4-1.mp4","mixer/tlh-2-4.mp4"]
dur=1.333
source_clips=[]
t=0.0
for i,src in enumerate(clip_srcs):
    source_clips.append({"src":src,"order":i,"start_sec":round(t,3),"end_sec":round(t+dur,3),
                         "src_dur_sec":dur,"target_dur_sec":dur,"speedup":1.0})
    t+=dur

ov_texts=["i priced risk for 20 years.","an agent built the model in 12 seconds.",
          "cold coffee. kitchen. 2am.","the tables were never the job."]
overlays=[]
for i,txt in enumerate(ov_texts):
    overlays.append({"text":txt,"order":i,"start_sec":i*2.0,"end_sec":i*2.0+2.0,"dur_sec":2.0})

metadata={
 "theme":"ai","format":"tlh","clip_count":6,"overlay_count":4,
 "source_repo":"social-autoposter/mixer",
 "theme_angle":"ai-killed-the-actuary",
 "theme_label":"the tables were never the job",
 "caption_style":"here-is-a-story-8beat",
 "description_style":"first_person_confession",
 "composition_id":"TLH-lesson-548",
 "engagement_style":"ig_one_night_timestamp_arc",
 "applied_campaign_ids":[],
 "new_style":{
   "name":"ig_one_night_timestamp_arc",
   "description":"an 8-beat defeat-flip told through one night's clock, timestamping how fast the agent moved against how long the craft took.",
   "example":"9:12pm i said a model cannot price risk. 2:03am i was reading its work by cold coffee.",
   "note":"use for organic ai-lesson reels when the collapse compresses into a single night; skip when the story has no natural single-night timeline.",
   "why_existing_didnt_fit":"the defeat_flip_arc carries the beats but has no time spine, so the years-vs-seconds speed gap is only stated; the clock makes it land structurally.",
   "target_chars":1900
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
  platforms, posted_urls, engagement, created_at, updated_at)
VALUES
 (%s, NULL, %s, %s, %s, %s, 'v1',
  8, 1080, 1920, 'draft', 'organic', %s, %s::jsonb, %s::jsonb, %s::jsonb,
  '{}'::text[], '{}'::jsonb, '{}'::jsonb, NOW(), NOW())
RETURNING id, post_number, variant_id, status, post_type, target_account, project_name;
""",(POST_NUMBER, VARIANT_ID, VIDEO_PATH, AUDIO, caption,
     TARGET_ACCOUNT, json.dumps(metadata), json.dumps(overlays), json.dumps(source_clips)))
print("INSERTED", cur.fetchone())
conn.commit(); conn.close()
print("OK")
