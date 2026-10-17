import os,psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")
cur=psycopg2.connect(url,connect_timeout=20).cursor()
cur.execute("select distinct metadata->>'theme_angle' from media_posts where metadata->>'theme_angle' like 'ai-killed%'");print(sorted(r[0] for r in cur.fetchall()))
cur.execute("select distinct metadata->>'engagement_style' from media_posts");print(sorted(str(r[0]) for r in cur.fetchall()))
