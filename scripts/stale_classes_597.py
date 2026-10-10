import os,glob,hashlib,json,psycopg2,subprocess
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")
pub=os.path.expanduser('~/social-autoposter/mixer/remotion/public/mixer')
cls={}
for f in sorted(glob.glob(pub+'/tlh-*.mp4')):
    h=hashlib.md5(open(f,'rb').read()).hexdigest()[:8]
    cls.setdefault(h,[]).append(os.path.basename(f))
conn=psycopg2.connect(url,connect_timeout=20);cur=conn.cursor()
cur.execute("SELECT post_number,source_clips FROM media_posts WHERE source_clips IS NOT NULL")
last={}
f2h={b:h for h,bs in cls.items() for b in bs}
for pn,sc in cur.fetchall():
    for c in (sc or []):
        b=os.path.basename(c.get('src',''))
        h=f2h.get(b)
        if h: last[h]=max(last.get(h,0),pn or 0)
cur.execute("SELECT max(post_number) FROM media_posts"); print("max pn",cur.fetchone())
rows=[]
for h,bs in cls.items():
    d=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',os.path.join(pub,bs[0])]).strip())
    sz=os.path.getsize(os.path.join(pub,bs[0]))
    rows.append((last.get(h,0),h,round(d,3),sz,bs[:4]))
for r in sorted(rows)[:20]: print(r)
