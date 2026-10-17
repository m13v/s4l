import os,json,subprocess,psycopg2,glob,collections
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")
pub=os.path.expanduser('~/social-autoposter/mixer/remotion/public/mixer')
h={};cls=collections.defaultdict(list)
for f in sorted(glob.glob(pub+'/tlh-*.mp4')):
    d=subprocess.check_output(['/sbin/md5','-q',f]).decode().strip();b=os.path.basename(f);h[b]=d;cls[d].append(b)
conn=psycopg2.connect(url,connect_timeout=20);cur=conn.cursor()
cur.execute("select post_number,variant_id,target_account from media_posts where post_number>=620 or variant_id in ('lesson-627','lesson-628','lesson-629','lesson-630') order by post_number");print(cur.fetchall())
cur.execute("select post_number,source_clips from media_posts where source_clips is not null")
last={}
for pn,sc in cur.fetchall():
    for c in sc or []:
        b=os.path.basename(c.get('src',''));d=h.get(b)
        if d: last[d]=max(last.get(d,0),pn)
rows=[]
for d,m in cls.items():
    dur=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',pub+'/'+m[0]]).decode())
    rows.append((last.get(d,0),d[:8],m,round(dur,3)))
for r in sorted(rows)[:14]: print(r)
