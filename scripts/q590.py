import os,psycopg2
env={}
for l in open(os.path.expanduser("~/social-autoposter/.env")):
    if "=" in l and not l.startswith("#"):
        k,v=l.split("=",1); env[k.strip()]=v.strip().strip('"').strip("'")
c=psycopg2.connect(env["DATABASE_URL"]).cursor()
c.execute("select post_number,variant_id,status,target_account from media_posts where post_number>=588 or variant_id in ('lesson-592') order by post_number")
print(c.fetchall())
c.execute("select distinct metadata->>'theme_angle' from media_posts where metadata->>'theme_angle' like 'ai-killed-the-%'")
print(sorted(r[0] for r in c.fetchall()))
c.execute("select metadata from media_posts where post_number=588")
print(c.fetchone())
