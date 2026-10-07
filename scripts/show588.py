import os,psycopg2,json
env={}
for l in open(os.path.expanduser("~/social-autoposter/.env")):
    if "=" in l and not l.startswith("#"):
        k,v=l.split("=",1); env[k.strip()]=v.strip().strip('"').strip("'")
c=psycopg2.connect(env["DATABASE_URL"]).cursor()
c.execute("select * from media_posts where post_number=588")
r=c.fetchone(); cols=[d[0] for d in c.description]
for k,v in zip(cols,r):
    if k!='caption_text': print(k,"=",json.dumps(v,default=str)[:400])
