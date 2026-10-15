import os,json,hashlib,glob,psycopg2
from dotenv import dotenv_values
url=dotenv_values('.env')['DATABASE_URL']
c=psycopg2.connect(url).cursor()
c.execute("select max(post_number) from media_posts"); print('max',c.fetchone())
c.execute("select post_number,variant_id,status,target_account from media_posts where post_number>=612 or variant_id in ('lesson-619','lesson-618') order by post_number"); 
for r in c.fetchall(): print(r)
c.execute("select post_number,source_clips from media_posts where source_clips is not null")
last={}
pub='mixer/remotion/public/mixer/'
def md5(p):
    return hashlib.md5(open(p,'rb').read()).hexdigest()[:10]
for pn,sc in c.fetchall():
    for s in sc or []:
        p=pub+os.path.basename(s.get('src',''))
        if os.path.exists(p):
            h=md5(p); last[h]=max(last.get(h,0),pn)
files={}
for f in glob.glob(pub+'tlh-*.mp4'):
    files.setdefault(md5(f),[]).append(os.path.basename(f))
rows=sorted(files.items(),key=lambda kv:last.get(kv[0],0))
import subprocess
for h,fs in rows[:12]:
    f=pub+fs[0]
    d=subprocess.run(['/opt/homebrew/Cellar/ffmpeg/8.1.1/bin/ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',f],capture_output=True,text=True).stdout.strip()
    print(h,last.get(h,0),d,os.path.getsize(f),fs[:3])
