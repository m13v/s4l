import os,hashlib,json,subprocess,psycopg2,glob
conn=psycopg2.connect(os.environ['DATABASE_URL']);cur=conn.cursor()
cur.execute("select post_number, source_clips from media_posts where source_clips is not null")
last={}
for pn,sc in cur.fetchall():
    for c in (sc or []):
        b=os.path.basename(c.get('src',''))
        last[b]=max(last.get(b,0),pn)
d='public/mixer';cls={}
for f in glob.glob(d+'/tlh-*.mp4'):
    m=hashlib.md5(open(f,'rb').read()).hexdigest()[:10]
    cls.setdefault(m,[]).append(os.path.basename(f))
rows=[]
for m,fs in cls.items():
    lu=max(last.get(f,0) for f in fs)
    dur=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',d+'/'+fs[0]]).decode())
    sz=os.path.getsize(d+'/'+fs[0])
    rows.append((lu,m,sorted(fs),round(dur,3),sz))
for r in sorted(rows)[:14]: print(r)
