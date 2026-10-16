import os,psycopg2
from dotenv import load_dotenv
load_dotenv(os.path.expanduser('~/social-autoposter/.env'))
c=psycopg2.connect(os.environ['DATABASE_URL']).cursor()
c.execute("select distinct metadata->>'theme_angle' from media_posts")
print(' '.join(sorted(r[0] or '' for r in c.fetchall())))
c.execute("select distinct metadata->>'engagement_style' from media_posts")
print([r[0] for r in c.fetchall()])
