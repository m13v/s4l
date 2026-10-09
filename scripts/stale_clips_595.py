import os,json,hashlib,subprocess,psycopg2,glob
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")
pub=os.path.expanduser('~/social-autoposter/mixer/remotion/public/mixer')
md5={};dur={}
for f in glob.glob(pub+'/tlh-*.mp4'):
    b=os.path.basename(f); md5[b]=hashlib.md5(open(f,'rb').read()).hexdigest()[:8]
    dur[b]=float(subprocess.check_output(['/opt/homebrew/bin/ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',f]).strip())
conn=psycopg2.connect(url,connect_timeout=20);cur=conn.cursor()
cur.execute("select post_number,source_clips from media_posts where source_clips is not null and post_type='organic'")
last={}
for pn,sc in cur.fetchall():
    for c in sc or []:
        b=os.path.basename(c.get('src',''))
        if b in md5: last[md5[b]]=max(last.get(md5[b],0),pn)
cls={}
for b,h in md5.items(): cls.setdefault(h,[]).append(b)
rows=sorted(cls.items(),key=lambda kv:last.get(kv[0],0))
for h,bs in rows[:14]: print(h,last.get(h,0),[(b,round(dur[b],3)) for b in bs][:3])
cur.execute("select max(post_number) from media_posts");print('max',cur.fetchone())
cur.execute("select post_number,variant_id,metadata->>'engagement_style',metadata->>'theme_angle' from media_posts order by post_number desc limit 8");[print(r) for r in cur.fetchall()]
