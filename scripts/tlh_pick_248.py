import re, os, subprocess, json, hashlib
from collections import Counter, defaultdict
from itertools import combinations

DATA = os.path.expanduser("~/social-autoposter/mixer/remotion/src/mixer/data.ts")
PUB  = os.path.expanduser("~/social-autoposter/mixer/remotion/public/mixer")
src = open(DATA).read()

# Extract every clipsV2 block and legacy clips arrays referencing tlh-*.mp4
# Find all "mixer/tlh-...mp4" occurrences grouped per variant via clipsV2:[ ... ] blocks
sets = []
for m in re.finditer(r'clipsV2:\s*\[(.*?)\]', src, re.S):
    clips = re.findall(r'mixer/(tlh-[0-9a-z\-]+\.mp4)', m.group(1))
    if clips:
        sets.append(clips)
# legacy v1: clips arrays inside TLH? lesson-1 uses clips[] with tlh-1..5
for m in re.finditer(r'clips:\s*\[(.*?)\]', src, re.S):
    clips = re.findall(r'"(tlh-[0-9a-z\-]+\.mp4)"', m.group(1))
    if clips:
        sets.append(clips)

def family(fn):
    # tlh-<firstint>-... -> first integer group
    mm = re.match(r'tlh-(\d+)', fn)
    return mm.group(1) if mm else fn

usage = Counter()
cooc = Counter()
for s in sets:
    for c in set(s):
        usage[c]+=1
    for a,b in combinations(sorted(set(s)),2):
        cooc[(a,b)]+=1

# available encoded tlh clips on disk
avail = sorted([f for f in os.listdir(PUB) if re.match(r'tlh-[0-9a-z\-]+\.mp4$', f)])

def probe(fn):
    p = os.path.join(PUB, fn)
    out = subprocess.run(["ffprobe","-v","error","-select_streams","v:0",
        "-show_entries","stream=width,height:format=duration","-of","json",p],
        capture_output=True,text=True).stdout
    j = json.loads(out)
    st = j["stream"][0] if "stream" in j else j["streams"][0]
    dur = float(j["format"]["duration"])
    return st["width"], st["height"], dur

def blackframes(fn):
    p = os.path.join(PUB, fn)
    r = subprocess.run(["ffmpeg","-v","error","-i",p,"-vf","blackdetect=d=0.1:pic_th=0.98",
        "-an","-f","null","-"],capture_output=True,text=True)
    return "black_start" in r.stderr

def sha(fn):
    return hashlib.sha256(open(os.path.join(PUB,fn),'rb').read()).hexdigest()

# Filter avail to valid 1080x1920, dur>=1.75 (holds a 2.0s slot acceptably or exact), non-black
os.environ["PATH"]="/opt/homebrew/Cellar/ffmpeg/8.1.1/bin:"+os.environ["PATH"]
valid=[]
info={}
for f in avail:
    try:
        w,h,d = probe(f)
    except Exception as e:
        continue
    if (w,h)!=(1080,1920): continue
    info[f]=(w,h,d)
    valid.append(f)

print("total prior sets:", len(sets))
print("valid 1080x1920 clips on disk:", len(valid))

# Build candidate 4-sets: distinct families, zero pairwise co-occurrence, low total usage.
# Rank by (max pairwise cooc, total usage). Verify durations >=1.75 and non-black + distinct sha lazily on the winner.
from itertools import combinations as comb
# group valid by family, prefer low-usage clips
valid_sorted = sorted(valid, key=lambda f:(usage[f], f))
best=None
# to keep it tractable, restrict candidate pool to the 40 lowest-usage valid clips across distinct families
pool = valid_sorted[:60]
results=[]
for quad in comb(pool,4):
    fams=[family(f) for f in quad]
    if len(set(fams))!=4: continue
    pairs=list(comb(sorted(quad),2))
    maxco=max(cooc[p] for p in pairs)
    tot=sum(usage[f] for f in quad)
    results.append((maxco,tot,quad))
results.sort(key=lambda x:(x[0],x[1]))
print("\ntop 15 candidate quads (maxcooc, totalusage, clips):")
for maxco,tot,quad in results[:15]:
    print(maxco,tot,list(quad),"durs",[round(info[f][2],3) for f in quad])
