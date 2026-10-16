import os,json,psycopg2,subprocess
from dotenv import load_dotenv
load_dotenv(os.path.expanduser('~/social-autoposter/.env'))
R=os.path.expanduser('~/social-autoposter/mixer/remotion/')
cap=open(R+'out/post-621.caption.txt').read()
clips=[("tlh-25-1.mp4",1.6),("tlh-1.mp4",1.5),("tlh-63-1.mp4",1.5),("tlh-68-1.mp4",1.6),("tlh-21-3.mp4",1.6)]
sc=[];t=0
for i,(b,d) in enumerate(clips,1):
    f=R+'public/mixer/'+b
    sd=float(subprocess.run(['/opt/homebrew/bin/ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',f],capture_output=True,text=True).stdout)
    sc.append(dict(order=i,src=f,src_dur_sec=sd,target_dur_sec=d,speedup=round(sd/d,4),start_sec=round(t,3),end_sec=round(t+d,3)));t+=d
ov_t=["19 years counting salmon at a dam window. one clicker per species.","i said: a camera can't tell a jack from a sockeye in murky water.","it matched my count. fish for fish. all season.","19 years counting. never once had time to ask why."]
ov=[dict(order=i+1,text=x,start_sec=i*2.0,end_sec=min(i*2.0+2.0,7.8),dur_sec=round(min(2.0,7.8-i*2.0),3)) for i,x in enumerate(ov_t)]
meta=dict(composition_id="TLH-lesson-623",format="tlh",theme="ai",theme_angle="ai-killed-the-fish-ladder-counter",theme_label="i told the public meeting a camera cannot tell a jack from a sockeye in brown water, the model matched my dam window count fish for fish all season, then a biologist asked why the sockeye ran late and i realized 19 years of clicking left no room to ask",clip_count=5,overlay_count=4,caption_style="here-is-a-story-8beat",description_style="first_person_confession",source_repo="social-autoposter/mixer",engagement_style="ig_counted_so_i_could_ask_arc",
  new_style=dict(description="the machine ties the narrator at the core task, and the real defeat is realizing the task consumed all their attention; the flip is the question they never had room to ask.",example="counted every one of them for 19 years and never once asked why they were late.",note="use when the job is high-volume vigilance (counting, sorting, watching); not for judgment or taste jobs where the machine wins on quality.",why_existing_didnt_fit="no reference styles were supplied; existing DB archetypes frame the loss as accuracy, coverage, or fatigue, none frame it as stolen attention that hid the real question.",target_chars=80),
  audio_track="track-007_iphone-2EAC148F.m4a",target_account="matt_diak")
conn=psycopg2.connect(os.environ['DATABASE_URL']);c=conn.cursor()
c.execute("select 1 from media_posts where post_number=621 or variant_id='lesson-623'")
assert not c.fetchall()
c.execute("""insert into media_posts (post_number,project_name,variant_id,video_path,audio_source,caption_text,caption_version,duration_sec,width,height,status,source_clips,overlays,metadata,post_type,target_account)
values (621,NULL,'lesson-623',%s,%s,%s,'v1',8.0,1080,1920,'draft',%s,%s,%s,'organic','matt_diak') returning id""",
 (R+'out/post-621.mp4','local:'+os.path.expanduser('~/social-autoposter/mixer/audio/track-007_iphone-2EAC148F.m4a'),cap,json.dumps(sc),json.dumps(ov),json.dumps(meta)))
print(c.fetchone());conn.commit()
c.execute("select caption_text=%s,status,target_account,project_name from media_posts where post_number=621",(cap,));print(c.fetchone())
