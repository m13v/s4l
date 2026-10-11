import os,hashlib,json,subprocess,psycopg2
d=os.path.expanduser('~/social-autoposter/mixer/remotion/public/mixer')
files=[f for f in os.listdir(d) if f.startswith('tlh')]
md={f:hashlib.md5(open(os.path.join(d,f),'rb').read()).hexdigest()[:8] for f in files}
c=psycopg2.connect(os.environ['DATABASE_URL']).cursor()
c.execute("select post_number,source_clips from media_posts where source_clips is not null")
last={}
for pn,sc in c.fetchall():
    for s in sc or []:
        b=os.path.basename(s.get('src',''))
        if b in md:
            h=md[b]; last[h]=max(last.get(h,0),pn)
cls={}
for f,h in md.items(): cls.setdefault(h,[]).append(f)
rows=sorted(cls.items(), key=lambda kv: last.get(kv[0],0))
for h,fs in rows[:12]:
    f=sorted(fs)[0]
    dur=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',os.path.join(d,f)],capture_output=True,text=True).stdout.strip()
    print(h,last.get(h,0),f,dur,len(fs))
