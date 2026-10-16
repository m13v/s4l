import os,hashlib,glob,json,subprocess,psycopg2
from dotenv import load_dotenv
load_dotenv(os.path.expanduser('~/social-autoposter/.env'))
c=psycopg2.connect(os.environ['DATABASE_URL']).cursor()
c.execute("select post_number,variant_id,status,target_account from media_posts where post_number>=615 or variant_id in ('lesson-623','lesson-624') order by post_number")
for r in c.fetchall(): print(r)
c.execute("select post_number,source_clips from media_posts where source_clips is not null")
last={}
pub=os.path.expanduser('~/social-autoposter/mixer/remotion/public/')
md5={}
for f in glob.glob(pub+'mixer/tlh-*.mp4'):
    md5[os.path.basename(f)]=hashlib.md5(open(f,'rb').read()).hexdigest()[:10]
for pn,sc in c.fetchall():
    for s in sc or []:
        b=os.path.basename(s.get('src',''))
        if b in md5:
            m=md5[b]; last[m]=max(last.get(m,0),pn)
fam={}
for b,m in md5.items(): fam.setdefault(m,[]).append(b)
rows=[]
for m,bs in fam.items():
    f=pub+'mixer/'+sorted(bs)[0]
    d=subprocess.run(['/opt/homebrew/bin/ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',f],capture_output=True,text=True).stdout.strip()
    rows.append((last.get(m,0),m,sorted(bs)[0],d,os.path.getsize(f)))
for r in sorted(rows)[:12]: print(r)
