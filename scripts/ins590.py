import os
p=os.path.expanduser("~/social-autoposter/mixer/remotion/src/mixer/data.ts")
s=open(p).read()
cap=open(os.path.expanduser("~/social-autoposter/scripts/cap590.txt")).read().rstrip("\n")
assert '"lesson-592"' not in s
anchor='  "lesson-591": {'
i=s.index(anchor); j=s.index("\n  },\n",i)+len("\n  },\n")
block='''  "lesson-592": {
    id: "lesson-592",
    // post-590 (matt_diak). Stalest content md5 classes (tlh-3-1/tlh-52-1 post 571, tlh-46-1 post 573,
    // tlh-5-1 post 575, tlh-66-1 post 577). onDiskDur 1.6/1.6/1.733/2.0/2.667 vs durSec 1.5/1.5/1.5/1.5/1.6 -> pure speedup.
    // Per-clip 45+45+45+45+48 = 228. Overlays 60+60+60+48 = 228.
    clipsV2: [
      { src: "mixer/tlh-3-1.mp4", durSec: 1.5 },
      { src: "mixer/tlh-52-1.mp4", durSec: 1.5 },
      { src: "mixer/tlh-46-1.mp4", durSec: 1.5 },
      { src: "mixer/tlh-66-1.mp4", durSec: 1.5 },
      { src: "mixer/tlh-5-1.mp4", durSec: 1.6 },
    ],
    overlays: [
      { text: "i broke down match film for 14 years.", startSec: 0.0, durSec: 2.0 },
      { text: "i said software can't see a pattern.",  startSec: 2.0, durSec: 2.0 },
      { text: "it found one i'd watched six times.",   startSec: 4.0, durSec: 2.0 },
      { text: "now it tags. i pick the three clips.",  startSec: 6.0, durSec: 1.6 },
    ],
    caption: `'''+cap.replace("`","\\`")+'''`,
  },
'''
s=s[:j]+block+s[j:]
open(p,"w").write(s)
print("ok")
