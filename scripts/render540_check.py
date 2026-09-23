import os, psycopg2, json, re
url = None
for line in open(os.path.expanduser("~/social-autoposter/.env")):
    if line.startswith("DATABASE_URL="):
        url = line.split("=",1)[1].strip()
        break
conn = psycopg2.connect(url)
cur = conn.cursor()

cur.execute("SELECT MAX(post_number) FROM media_posts;")
print("MAX post_number:", cur.fetchone()[0])

for pn in (540,):
    cur.execute("SELECT count(*) FROM media_posts WHERE post_number=%s;", (pn,))
    print(f"post_number={pn} rows:", cur.fetchone()[0])

for vid in ("lesson-542","lesson-541","lesson-540"):
    cur.execute("SELECT count(*), MAX(post_number) FROM media_posts WHERE variant_id=%s;", (vid,))
    print(f"variant {vid}:", cur.fetchone())

# theme angle freshness candidates
cands = ["ai-killed-the-piano-tuner","ai-killed-the-perfumer","ai-killed-the-watchmaker",
         "ai-killed-the-stonemason","ai-killed-the-glassblower","ai-killed-the-bookbinder",
         "ai-killed-the-cobbler","ai-killed-the-florist","ai-killed-the-fishmonger",
         "ai-killed-the-tailor","ai-killed-the-butcher","ai-killed-the-neon-bender",
         "ai-killed-the-blacksmith","ai-killed-the-cartographer","ai-killed-the-cheesemonger"]
print("\n-- theme_angle DB usage --")
for a in cands:
    cur.execute("SELECT count(*) FROM media_posts WHERE metadata->>'theme_angle'=%s;", (a,))
    print(f"{a}: {cur.fetchone()[0]}")

# last 10 organic renders and their source clip basenames
print("\n-- last 12 renders (post_number, variant_id, project, clips) --")
cur.execute("""SELECT post_number, variant_id, project_name, source_clips, audio_source
               FROM media_posts ORDER BY post_number DESC LIMIT 12;""")
rows = cur.fetchall()
recent_clips = {}
for pn, vid, proj, clips, audio in rows:
    names = []
    if clips:
        for c in clips:
            src = c.get("src","")
            base = src.split("/")[-1]
            names.append(base)
    print(pn, vid, proj, names, "| audio:", (audio or "")[:60])
    if pn >= 531:  # window for freshness
        for n in names:
            recent_clips.setdefault(n, pn)
print("\n-- clip basenames used in posts >=531 (avoid these) --")
print(sorted(recent_clips.keys()))
cur.close(); conn.close()
