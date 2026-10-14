import os,json,hashlib,glob,subprocess,psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")
pub=os.path.expanduser('~/s4l/mixer/remotion/public/mixer')
cls={}
for f in glob.glob(pub+'/tlh-*.mp4'):
    h=hashlib.md5(open(f,'rb').read()).hexdigest()[:8]
    cls.setdefault(h,[]).append(os.path.basename(f))
conn=psycopg2.connect(url,connect_timeout=20);cur=conn.cursor()
cur.execute("select post_number,variant_id,source_clips,status,target_account from media_posts where post_number>=595 or source_clips is not null order by post_number")
rows=cur.fetchall()
last={}
name2h={n:h for h,ns in cls.items() for n in ns}
for pn,vid,sc,st,ta in rows:
    for c in (sc or []):
        b=os.path.basename(c.get('src',''))
        if b in name2h: last[name2h[b]]=max(last.get(name2h[b],0),pn)
for pn,vid,sc,st,ta in rows:
    if pn>=608: print(pn,vid,st,ta)
out=[]
for h,ns in cls.items():
    ns=sorted(ns); f=pub+'/'+ns[0]
    d=subprocess.run(['/opt/homebrew/bin/ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',f],capture_output=True,text=True).stdout.strip()
    out.append((last.get(h,0),h,ns[0],d,len(ns)))
for o in sorted(out)[:12]: print(o)
cur.execute("select max(post_number) from media_posts");print(cur.fetchone())
