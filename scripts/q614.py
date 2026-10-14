import os,psycopg2
url=[l.strip().split('=',1)[1] for l in open(os.path.expanduser('~/social-autoposter/.env')) if l.startswith('DATABASE_URL=')][0].strip('"').strip("'")
cur=psycopg2.connect(url).cursor()
cur.execute("select distinct metadata->>'theme_angle' from media_posts")
used={r[0] for r in cur.fetchall()}
for c in ["ai-killed-the-piano-tuner","ai-killed-the-wine-sommelier","ai-killed-the-avalanche-forecaster","ai-killed-the-hearing-aid-fitter","ai-killed-the-locksmith","ai-killed-the-farrier","ai-killed-the-elevator-inspector","ai-killed-the-grain-inspector","ai-killed-the-bridge-inspector","ai-killed-the-court-reporter","ai-killed-the-handwriting-examiner","ai-killed-the-diamond-grader","ai-killed-the-surf-forecaster","ai-killed-the-orchard-thinner"]:
    print(c, c in used)
cur.execute("select distinct metadata->>'engagement_style' from media_posts where metadata->>'engagement_style' like 'ig_%'")
print(sorted(r[0] for r in cur.fetchall()))
