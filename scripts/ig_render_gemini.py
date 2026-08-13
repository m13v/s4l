#!/opt/homebrew/bin/python3.11
"""ig_render_gemini.py — deterministic Instagram reel render, Gemini-drafted.

The Gemini-provider sibling of the agentic render lane (run-instagram-render.sh
+ claude -p + mixer/SKILL.md). Same DB, same output contract, but the creative
step is ONE schema-validated Gemini generateContent call and everything else is
plain code. This is the render worker the hosted/mobile lane will run; locally
it serves as the end-to-end test harness.

Two hardcoded scenarios (per product decision 2026-08-13):
  tlh    "hook + video": 4 clips x 2.0s from the encoded public/mixer/tlh-*
         pool, 4 x 2.0s hook overlays, 8-beat story caption. organic,
         project_name=NULL, composition TLH-runtime (inputProps).
  mixer  product reel: an EXISTING registered Mixer-<variant> (clips + timing
         untouched), fresh title/step/finale overlay text + caption via
         --props overlayTextOverride. product, project_name=<variant.project>.

Output contract (matches mixer/SKILL.md):
  - out/post-NNN.mp4 (dubbed) + out/post-NNN.caption.txt
  - one media_posts draft row via POST /api/v1/media-posts (HTTP-only, pinned
    post_number from picker-context's advisory next_post_number; safe because
    both render lanes serialize on the instagram-render lock)

Usage:
  ig_render_gemini.py --account matt_diak                    # tlh organic
  ig_render_gemini.py --scenario mixer --variant spa         # product reel
  ig_render_gemini.py --dry-run                              # no DB write, no out/ write
  ig_render_gemini.py --force                                # ignore draft-buffer guard
  ig_render_gemini.py --upload-gcs                           # publish mp4 to GCS,
                                                             # store the URL as video_path
  ig_render_gemini.py --upload-gcs --post trial              # full headless run:
                                                             # render + row + IG publish

Cloud mode (Cloud Run render worker): --upload-gcs + --post make the run
fully host-independent — the mp4 goes to GCS (bucket $S4L_GCS_BUCKET, token
from the local ADC file when present, else the GCE/Cloud Run metadata
server) and the IG publish happens right here via post_to_ig's pure
functions, with creds from $IG_USER_ID/$IG_LONG_TOKEN/$IG_APP_SECRET (else
~/instagram-graph-api/.env).

Env: S4L_GEMINI_MODEL (default gemini-pro-latest, 404-falls-back to
gemini-flash-latest), GEMINI_API_KEY (else keychain gemini-api-key),
S4L_GCS_BUCKET (default mk0r-media-temp), AUTOPOSTER_API_BASE.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import draft_provider  # noqa: E402  (gemini_api_key)
from http_api import api_get, api_post, load_env  # noqa: E402

REPO_DIR = SCRIPTS_DIR.parent
REMOTION_DIR = REPO_DIR / "mixer" / "remotion"
OUT_DIR = REMOTION_DIR / "out"
AUDIO_DIR = REPO_DIR / "mixer" / "audio"
CLIP_MANIFEST = REMOTION_DIR / "src" / "mixer" / "clip-durations.json"
LOCK_DIR = Path("/tmp/social-autoposter-instagram-render.lock")

NODE_BIN = Path.home() / ".nvm" / "versions" / "node" / "v23.10.0" / "bin"
FFMPEG_CANDIDATES = [
    "/opt/homebrew/Cellar/ffmpeg/8.1.1/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "ffmpeg",
]

CAPTION_MAX = 2150  # IG cap 2200 minus 50-char suffix/encoding buffer
DRAFT_BUFFER_MAX = 3
TLH_CLIP_COUNT = 4
TLH_SLOT_SEC = 2.0
TLH_TOTAL_SEC = TLH_CLIP_COUNT * TLH_SLOT_SEC

# Income-claim framing is permanently banned on IG renders (Meta
# fraud-and-deceptive-practices restriction 2026-06-02, see mixer/SKILL.md).
BANNED_PATTERNS = [
    re.compile(r"\$\s?\d[\d,.]*\s*(?:/|a |per |each )?\s*(?:mo\b|month|week|day|yr|year)", re.I),
    re.compile(r"\b(?:they|client|customer)s?\s+paid\s+me\b", re.I),
    re.compile(r"\brecurring revenue\b", re.I),
    re.compile(r"\bsigned\s+\d+\s+clients\b", re.I),
]

# Registered Mixer variants (clips/timing live in data.ts; only overlay text is
# generated per render). Kept as a hardcoded map on purpose: the two-scenario
# v1 is a fixed-template product.
MIXER_VARIANTS = {
    "spa": {"project": "mk0r", "steps": 3},
    "autoshop": {"project": "mk0r", "steps": 3},
    "hotel": {"project": "mk0r", "steps": 3},
    "mk0r-retail": {"project": "mk0r", "steps": 3},
}
for _i in (1, 2):
    for _r in (1, 2, 3, 4):
        MIXER_VARIANTS[f"studyly-i{_i}-r{_r}"] = {"project": "studyly", "steps": 1}


def log(msg: str) -> None:
    print(f"[ig_render_gemini] {msg}", file=sys.stderr, flush=True)


def ffmpeg_bin() -> str:
    for c in FFMPEG_CANDIDATES:
        if c == "ffmpeg":
            found = shutil.which("ffmpeg")
            if found:
                return found
        elif Path(c).exists():
            return c
    raise SystemExit("ffmpeg not found")


# ── lock (mirror of skill/lock.sh semantics: mkdir + pid file, bail if held) ──

def acquire_render_lock() -> bool:
    try:
        LOCK_DIR.mkdir()
    except FileExistsError:
        pid_file = LOCK_DIR / "pid"
        holder = pid_file.read_text().strip() if pid_file.exists() else "?"
        # Stale-holder sweep: if the recorded pid is dead, take over.
        if holder.isdigit():
            try:
                os.kill(int(holder), 0)
                return False  # alive holder
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
        elif holder != "?":
            return False
        shutil.rmtree(LOCK_DIR, ignore_errors=True)
        try:
            LOCK_DIR.mkdir()
        except FileExistsError:
            return False
    (LOCK_DIR / "pid").write_text(str(os.getpid()))
    return True


def release_render_lock() -> None:
    pid_file = LOCK_DIR / "pid"
    if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
        shutil.rmtree(LOCK_DIR, ignore_errors=True)


# ── gemini (same call shape as claude_job._run_gemini_api) ────────────────────

def gemini_json(prompt: str, timeout: int = 300) -> tuple[dict, str]:
    """One generateContent call forced to JSON. Returns (parsed, model_used)."""
    key = draft_provider.gemini_api_key()
    if not key:
        raise SystemExit("no GEMINI_API_KEY env / keychain gemini-api-key")
    model = os.environ.get("S4L_GEMINI_MODEL", "").strip() or "gemini-pro-latest"
    fallback = "gemini-flash-latest"
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 16384,
            "responseMimeType": "application/json",
        },
    }).encode()
    last_err = None
    for attempt_model in (model, fallback if fallback != model else None):
        if not attempt_model:
            break
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{attempt_model}:generateContent"
        )
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                obj = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            tail = ""
            try:
                tail = e.read().decode()[-300:]
            except Exception:
                pass
            if e.code == 404 and attempt_model == model:
                log(f"model {model} 404; falling back to {fallback}")
                continue
            raise SystemExit(f"gemini HTTP {e.code}: {tail}")
        except Exception as e:
            last_err = e
            raise SystemExit(f"gemini request failed: {e}")
        try:
            cand = obj["candidates"][0]
            text = "".join(
                p.get("text", "") for p in cand.get("content", {}).get("parts", [])
            ).strip()
        except (KeyError, IndexError, TypeError):
            block = (obj.get("promptFeedback") or {}).get("blockReason", "?")
            raise SystemExit(f"gemini returned no candidates (blockReason={block})")
        if not text:
            raise SystemExit("gemini returned empty text")
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        for candidate in (text, cleaned):
            try:
                return json.loads(candidate), attempt_model
            except Exception:
                continue
        raise SystemExit(f"gemini output is not JSON: {text[:200]}")
    raise SystemExit(f"gemini call failed: {last_err}")


# ── validation ────────────────────────────────────────────────────────────────

def banned_hit(*texts: str) -> str | None:
    for t in texts:
        for pat in BANNED_PATTERNS:
            m = pat.search(t or "")
            if m:
                return m.group(0)
    return None


def tighten_caption(caption: str, opener: str) -> str:
    """Up to 2 focused Gemini calls to bring an over-limit caption under CAPTION_MAX."""
    for i in range(2):
        prompt = (
            "Tighten this Instagram caption to UNDER "
            f"{CAPTION_MAX} characters total. Keep the exact opener line "
            f"{opener!r}, keep every story beat, keep the lowercase blunt "
            "voice, cut adjectives and redundancy only. Reply with JSON: "
            '{"caption": "..."}\n\nCAPTION:\n' + caption
        )
        out, _ = gemini_json(prompt)
        cand = (out.get("caption") or "").strip()
        if cand and len(cand) <= CAPTION_MAX:
            return cand
        caption = cand or caption
        log(f"tighten attempt {i + 1} still {len(caption)} chars")
    raise SystemExit(f"caption tighten failed; still {len(caption)} chars")


# ── audio LRU (mirror of the picker heredoc in run-instagram-render.sh) ───────

def audio_lru(ctx: dict) -> list[str]:
    from datetime import datetime

    def parse_dt(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None

    usage = [(r[0] or "", parse_dt(r[1])) for r in (ctx.get("audio_usage") or [])]
    files = sorted(str(p) for p in AUDIO_DIR.glob("*.m4a"))
    ranked = []
    for f in files:
        base = os.path.basename(f)
        stem = os.path.splitext(base)[0]
        tok = stem.rsplit("_", 1)[-1] if "_" in stem else stem
        last = None
        for src, used_at in usage:
            if used_at is None:
                continue
            if base in src or (tok and tok in src):
                if last is None or used_at > last:
                    last = used_at
        ranked.append((f, last))
    ranked.sort(key=lambda x: (1, x[1].isoformat()) if x[1] else (0, ""))
    return [f for f, _ in ranked]


# ── scenario: tlh ─────────────────────────────────────────────────────────────

def tlh_prompt(pool: list[str], used_angles: list[str], story_brief: str | None,
               opener: str) -> str:
    default_brief = (
        "Theme: AI, first person, one of these angles: making money with AI "
        "(capability, never income claims), AI replacing jobs/skills (denial -> "
        "reckoning -> flip), contrarian-but-true hot takes on AI, AI agents "
        "(where they 5x you, where they break), AI as autopilot vs bottleneck. "
        "Persona: someone with 8-15 years in a concrete profession who was "
        "publicly wrong about AI and got flipped by one specific event. "
        "FORBIDDEN: generic life advice with no AI angle, product pitches or "
        "product names, vague 'AI changed my life'."
    )
    brief = story_brief or default_brief
    return f"""You are drafting ONE Instagram time-lapse-hook reel: 8 seconds of b-roll with 4 sequential text overlay cards, plus the post caption.

STORY BRIEF:
{brief}

CAPTION RULES (hard):
- Opens with the exact line: {opener}
- 8-beat arc: opener / specific age+setup / being wrong in public on the record / the specific event that broke the position / the felt sense (concrete sensory detail) / the workflow change / one sharp contrarian lesson line / closing instruction.
- Voice: blunt, lowercase, personal. No emoji, no hashtags, no markdown.
- UNDER {CAPTION_MAX} characters TOTAL. This is a hard limit.
- Never mention income amounts, "$X/month", clients paying, or revenue.

OVERLAY RULES (hard):
- Exactly 4 overlay texts, shown 2 seconds each in order.
- Each under 55 characters, lowercase, no emoji.
- Arc across the 4 cards: setup credential / the moment of being wrong (specific) / the felt sense / the flipped lesson. They must work WITHOUT the caption.

THEME ANGLE:
- Provide theme_angle: a short snake_case slug for this reel's angle (e.g. translator_agent_weekend, sre_pager_flip). It MUST NOT be any of these recently used angles: {json.dumps(sorted(used_angles))}
- Provide theme_label: a 5-10 word human-readable version.

B-ROLL CLIPS:
- Pick exactly {TLH_CLIP_COUNT} DISTINCT filenames from this pool (generic sped-up b-roll; exact visuals are interchangeable, pick any 4 different ones): {json.dumps(pool)}

Reply with ONLY this JSON shape:
{{"theme_angle": "...", "theme_label": "...", "clip_files": ["...", "...", "...", "..."], "overlays": ["...", "...", "...", "..."], "caption": "..."}}"""


def run_tlh(args, ctx: dict, manifest: dict) -> dict:
    cfg = json.loads((REPO_DIR / "config.json").read_text())
    ig_cfg = cfg.get("instagram", {}) or {}
    account_record = next(
        (a for a in (ig_cfg.get("accounts") or [])
         if (a.get("username") or "").lower() == args.account.lower()),
        {},
    )
    tlh_cfg = account_record.get("tlh") or {}
    opener = tlh_cfg.get("caption_opener") or "here is a story."
    story_brief = tlh_cfg.get("story_brief")

    import random
    pool = sorted(k for k in manifest if k.startswith("tlh-"))
    if len(pool) < TLH_CLIP_COUNT:
        raise SystemExit(f"tlh clip pool too small: {len(pool)}")
    # Shuffle before embedding in the prompt: models over-pick the first
    # entries of a sorted list, which clusters clips from one lesson family.
    random.shuffle(pool)
    used_angles = [a for a in (ctx.get("used_theme_angles_14d") or []) if a]

    prompt = tlh_prompt(pool, used_angles, story_brief, opener)
    draft, model_used = gemini_json(prompt)
    log(f"gemini draft via {model_used}: angle={draft.get('theme_angle')!r}")

    # -- validate, with ONE corrective retry carrying the error back ----------
    for attempt in range(2):
        errs = []
        clips = draft.get("clip_files") or []
        overlays = draft.get("overlays") or []
        caption = (draft.get("caption") or "").strip()
        angle = (draft.get("theme_angle") or "").strip()
        if len(clips) != TLH_CLIP_COUNT or len(set(clips)) != TLH_CLIP_COUNT:
            errs.append(f"clip_files must be {TLH_CLIP_COUNT} distinct entries")
        if any(c not in manifest for c in clips):
            errs.append("every clip_files entry must come from the given pool")
        if len(overlays) != 4 or any(not (o or "").strip() for o in overlays):
            errs.append("overlays must be 4 non-empty strings")
        if any(len(o) > 80 for o in overlays):
            errs.append("each overlay must be under 80 characters")
        if not caption.startswith(opener):
            errs.append(f"caption must start with {opener!r}")
        if not angle or angle in used_angles:
            errs.append("theme_angle missing or recently used")
        hit = banned_hit(caption, *overlays)
        if hit:
            errs.append(f"banned income-claim phrasing: {hit!r}")
        if not errs:
            break
        if attempt == 1:
            raise SystemExit(f"gemini draft failed validation twice: {errs}")
        log(f"draft invalid ({errs}); retrying once with feedback")
        draft, model_used = gemini_json(
            prompt + "\n\nYour previous answer failed validation: "
            + "; ".join(errs) + "\nFix ALL of these and reply again."
        )

    caption = (draft.get("caption") or "").strip()
    if len(caption) > CAPTION_MAX:
        log(f"caption {len(caption)} chars > {CAPTION_MAX}; tightening")
        caption = tighten_caption(caption, opener)

    clips = draft["clip_files"]
    overlays = [o.strip() for o in draft["overlays"]]
    variant_id = f"lesson-{args.post_number}"
    runtime_variant = {
        "id": variant_id,
        "clipsV2": [
            {"src": f"mixer/{c}", "durSec": TLH_SLOT_SEC} for c in clips
        ],
        "overlays": [
            {"text": o, "startSec": i * TLH_SLOT_SEC, "durSec": TLH_SLOT_SEC}
            for i, o in enumerate(overlays)
        ],
        "caption": caption,
    }
    return {
        "composition": "TLH-runtime",
        "props": {"variantId": variant_id, "runtimeVariant": runtime_variant},
        "variant_id": variant_id,
        "project_name": None,
        "post_type": "organic",
        "caption": caption,
        "duration_sec": TLH_TOTAL_SEC,
        "overlays_db": [
            {"order": i + 1, "text": o, "start_sec": i * TLH_SLOT_SEC,
             "end_sec": (i + 1) * TLH_SLOT_SEC, "dur_sec": TLH_SLOT_SEC}
            for i, o in enumerate(overlays)
        ],
        "source_clips_db": [
            {"order": i + 1, "src": f"mixer/{c}",
             "src_dur_sec": manifest.get(c), "target_dur_sec": TLH_SLOT_SEC,
             "speedup": round((manifest.get(c) or TLH_SLOT_SEC) / TLH_SLOT_SEC, 3)}
            for i, c in enumerate(clips)
        ],
        "metadata": {
            "composition_id": "TLH-runtime",
            "format": "tlh",
            "theme": "ai" if not story_brief else "account_brief",
            "theme_angle": draft["theme_angle"],
            "theme_label": draft.get("theme_label"),
            "clip_count": TLH_CLIP_COUNT,
            "overlay_count": 4,
            "caption_style": "story_arc_8beat",
            "engagement_style": "ig_defeat_flip_arc",
            "source_repo": "social-autoposter/mixer",
            "render_provider": "gemini-api",
            "render_model": model_used,
            "pipeline": "ig_render_gemini_v1",
        },
    }


# ── scenario: mixer ───────────────────────────────────────────────────────────

def mixer_prompt(variant: str, meta: dict, used_angles: list[str]) -> str:
    project = meta["project"]
    steps = meta["steps"]
    project_rules = {
        "mk0r": (
            "mk0r builds a complete real website for a local business from one "
            "prompt. Headlines describe the CAPABILITY (build a real site in "
            "one prompt, live in minutes). NEVER income/earnings framing: no "
            "'$X/month', no 'they paid me', no revenue, no signed clients."
        ),
        "studyly": (
            "studyly turns any course material into spaced-repetition study "
            "sessions. Angle: the failing/overwhelmed student who flips their "
            "grades. No income framing, no medical claims."
        ),
    }.get(project, f"Promote the {project} product truthfully, no income claims.")
    return f"""You are writing fresh overlay text + caption for ONE Instagram product reel built from the registered template variant {variant!r} (project {project}).

PRODUCT RULES: {project_rules}

Reply with ONLY this JSON shape:
{{
  "theme_angle": "short_snake_case_slug NOT in {json.dumps(sorted(used_angles))}",
  "theme_label": "5-10 words",
  "title": {{"headline": "2-3 short lines separated by \\n, one line is the literal placeholder __ACCENT__", "accentText": "the 2-4 word phrase that replaces __ACCENT__", "tagline": "one short sub-line"}},
  "step_overlays": [exactly {steps} short strings, each under 60 chars, imperative voice, walking through using the product],
  "finale_overlay": "one short closing instruction under 60 chars",
  "caption": "product story caption under {CAPTION_MAX} chars, lowercase, personal voice, no emoji, no hashtags, no income claims"
}}"""


def run_mixer(args, ctx: dict, manifest: dict) -> dict:
    variant = args.variant
    if variant not in MIXER_VARIANTS:
        raise SystemExit(
            f"--variant must be one of {sorted(MIXER_VARIANTS)} (got {variant!r})"
        )
    meta = MIXER_VARIANTS[variant]
    used_angles = [a for a in (ctx.get("used_theme_angles_14d") or []) if a]
    prompt = mixer_prompt(variant, meta, used_angles)
    draft, model_used = gemini_json(prompt)

    for attempt in range(2):
        errs = []
        title = draft.get("title") or {}
        steps = draft.get("step_overlays") or []
        finale = (draft.get("finale_overlay") or "").strip()
        caption = (draft.get("caption") or "").strip()
        if not (title.get("headline") and title.get("accentText")):
            errs.append("title.headline and title.accentText required")
        if "__ACCENT__" not in (title.get("headline") or ""):
            errs.append("title.headline must contain the literal __ACCENT__ placeholder")
        if len(steps) != meta["steps"] or any(not (s or "").strip() for s in steps):
            errs.append(f"step_overlays must be {meta['steps']} non-empty strings")
        if not finale:
            errs.append("finale_overlay required")
        if not caption:
            errs.append("caption required")
        hit = banned_hit(caption, finale, title.get("headline", ""),
                         title.get("tagline", ""), *steps)
        if hit:
            errs.append(f"banned income-claim phrasing: {hit!r}")
        if not errs:
            break
        if attempt == 1:
            raise SystemExit(f"gemini mixer draft failed validation twice: {errs}")
        log(f"mixer draft invalid ({errs}); retrying once")
        draft, model_used = gemini_json(
            prompt + "\n\nYour previous answer failed validation: "
            + "; ".join(errs) + "\nFix ALL of these and reply again."
        )

    caption = (draft.get("caption") or "").strip()
    if len(caption) > CAPTION_MAX:
        caption = tighten_caption(caption, caption.split("\n", 1)[0][:20])

    title = draft["title"]
    steps = [s.strip() for s in draft["step_overlays"]]
    finale = draft["finale_overlay"].strip()
    override = {
        "titleConfig": {
            "headline": title["headline"],
            "accentText": title["accentText"],
            **({"tagline": title["tagline"]} if title.get("tagline") else {}),
        },
        "stepOverlays": steps,
        "finaleOverlay": finale,
    }
    return {
        "composition": f"Mixer-{variant}",
        "props": {"variantId": variant, "overlayTextOverride": override},
        "variant_id": variant,
        "project_name": meta["project"],
        "post_type": "product",
        "caption": caption,
        "duration_sec": None,  # probed from the rendered file
        "overlays_db": (
            [{"order": 0, "text": title["headline"], "role": "title"}]
            + [{"order": i + 1, "text": s, "role": "step"} for i, s in enumerate(steps)]
            + [{"order": len(steps) + 1, "text": finale, "role": "finale"}]
        ),
        "source_clips_db": [{"note": f"registered variant {variant} (data.ts)"}],
        "metadata": {
            "composition_id": f"Mixer-{variant}",
            "format": "mixer",
            "theme_angle": draft.get("theme_angle"),
            "theme_label": draft.get("theme_label"),
            "caption_style": "product_story",
            "engagement_style": (
                "ig_walkin_storefront_playbook" if meta["project"] == "mk0r"
                else "ig_studyly_failing_student_arc"
            ),
            "overlay_text_override": override,
            "source_repo": "social-autoposter/mixer",
            "render_provider": "gemini-api",
            "render_model": model_used,
            "pipeline": "ig_render_gemini_v1",
        },
    }


# ── GCS + IG (cloud mode) ─────────────────────────────────────────────────────

GCS_ADC = Path.home() / ".config" / "gcloud" / "legacy_credentials" / "matt@mediar.ai" / "adc.json"
IG_ENV_FILE = Path.home() / "instagram-graph-api" / ".env"


def gcs_access_token() -> str:
    """OAuth token for GCS: local ADC refresh-token file when present (operator
    Mac), else the metadata server (Cloud Run / GCE service account)."""
    if GCS_ADC.exists():
        creds = json.loads(GCS_ADC.read_text())
        data = urllib.parse.urlencode({
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        with urllib.request.urlopen(
            urllib.request.Request("https://oauth2.googleapis.com/token", data=data),
            timeout=30,
        ) as r:
            return json.loads(r.read())["access_token"]
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def gcs_upload(video_path: Path) -> str:
    bucket = os.environ.get("S4L_GCS_BUCKET", "").strip() or "mk0r-media-temp"
    token = gcs_access_token()
    name = urllib.parse.quote(video_path.name, safe="")
    url = (
        f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
        f"?uploadType=media&name={name}"
    )
    req = urllib.request.Request(
        url, data=video_path.read_bytes(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        json.loads(r.read())
    public = f"https://storage.googleapis.com/{bucket}/{name}"
    log(f"GCS: uploaded {video_path.name} -> {public}")
    return public


def resolve_ig_creds(account: str) -> dict:
    """IG creds from env vars first (cloud), else ~/instagram-graph-api/.env
    resolved through config.json's per-account env-var names (operator Mac)."""
    env = dict(os.environ)
    if not (env.get("IG_USER_ID") and env.get("IG_LONG_TOKEN") and env.get("IG_APP_SECRET")):
        if IG_ENV_FILE.exists():
            for line in IG_ENV_FILE.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip())
    cfg = json.loads((REPO_DIR / "config.json").read_text())
    rec = next(
        (a for a in ((cfg.get("instagram") or {}).get("accounts") or [])
         if (a.get("username") or "").lower() == account.lower()),
        {},
    )
    user_id = env.get(rec.get("ig_user_id_env") or "IG_USER_ID") or env.get("IG_USER_ID")
    token = env.get(rec.get("ig_long_token_env") or "IG_LONG_TOKEN") or env.get("IG_LONG_TOKEN")
    secret = env.get("IG_APP_SECRET")
    if not (user_id and token and secret):
        raise SystemExit(f"IG creds unresolved for account {account!r}")
    return {"ig_user_id": user_id, "ig_long_token": token, "ig_app_secret": secret}


def post_now(video_path: Path, video_url: str, caption: str, account: str,
             post_type: str, trial: bool) -> dict:
    """Publish the already-uploaded reel from this process (cloud mode).
    Reuses post_to_ig's pure functions so campaign suffixes, container flow,
    and the mark_posted write stay single-sourced."""
    sys.path.insert(0, str(REPO_DIR / "mixer"))
    import post_to_ig as pig
    from datetime import datetime, timezone

    creds = resolve_ig_creds(account)
    caption, campaign_ids = pig.apply_campaign_suffixes(caption)
    if len(caption) > 2200:
        raise SystemExit(f"caption {len(caption)} chars > IG hard limit after suffix")
    meta = pig.ig_post_reel(
        creds["ig_user_id"], creds["ig_long_token"], creds["ig_app_secret"],
        video_url, caption, trial=trial,
    )
    posted_at = datetime.now(timezone.utc)
    permalink = meta.get("permalink", "")
    pig.mark_posted(video_path, video_url, permalink, posted_at,
                    post_type=post_type, target_account=account,
                    caption_text=caption, campaign_ids=campaign_ids)
    log(f"published{' (trial)' if trial else ''}: {permalink}")
    return {"permalink": permalink, "media_id": meta.get("id"), "trial": trial}


# ── render + dub + persist ────────────────────────────────────────────────────

def remotion_render(composition: str, props: dict, out_path: Path) -> None:
    env = dict(os.environ)
    env["PATH"] = f"{NODE_BIN}:{env.get('PATH', '')}"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="ig_props_", delete=False
    ) as f:
        json.dump(props, f)
        props_path = f.name
    try:
        cmd = [
            "npx", "remotion", "render", "src/index.ts", composition,
            str(out_path), f"--props={props_path}", "--concurrency=2",
        ]
        log(f"rendering {composition} -> {out_path.name}")
        r = subprocess.run(
            cmd, cwd=REMOTION_DIR, env=env,
            capture_output=True, text=True, timeout=900,
        )
        if r.returncode != 0:
            raise SystemExit(
                f"remotion render failed rc={r.returncode}: {r.stderr[-800:]}"
            )
    finally:
        os.unlink(props_path)


def probe_duration(path: Path) -> float:
    ffprobe = ffmpeg_bin().replace("ffmpeg", "ffprobe")
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return float(r.stdout.strip())


def dub_audio(silent: Path, audio: Path, out: Path, duration: float) -> None:
    r = subprocess.run(
        [ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(silent),
         "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
         "-c:a", "aac", "-t", f"{duration}", "-movflags", "+faststart",
         str(out)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        raise SystemExit(f"ffmpeg dub failed: {r.stderr[-400:]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--account", default="matt_diak")
    ap.add_argument("--scenario", choices=("tlh", "mixer"), default="tlh")
    ap.add_argument("--variant", help="mixer scenario: registered variant id")
    ap.add_argument("--dry-run", action="store_true",
                    help="render to /tmp, print the row, skip DB + out/")
    ap.add_argument("--force", action="store_true",
                    help="bypass the draft-buffer guard")
    ap.add_argument("--upload-gcs", action="store_true",
                    help="upload the dubbed mp4 to GCS and store its public "
                         "URL as the row's video_path (cloud mode)")
    ap.add_argument("--post", choices=("trial", "normal"),
                    help="publish to IG right after the row insert (implies "
                         "--upload-gcs); 'trial' = non-followers only")
    args = ap.parse_args()
    if args.post:
        args.upload_gcs = True
    if args.scenario == "mixer" and not args.variant:
        raise SystemExit("--scenario mixer requires --variant")

    load_env()
    if not acquire_render_lock():
        log("instagram-render lock held by a live process; skipping cleanly")
        sys.exit(0)
    try:
        ctx = (api_get(
            "/api/v1/media-posts/picker-context",
            query={"target_account": args.account, "window_days": 7},
        ).get("data") or {})

        post_type = "organic" if args.scenario == "tlh" else "product"
        drafts = ctx.get("draft_counts") or []
        if args.scenario == "tlh":
            buffered = sum(int(r.get("count") or 0) for r in drafts
                           if r.get("post_type") == post_type)
        else:
            proj = MIXER_VARIANTS.get(args.variant or "", {}).get("project")
            buffered = sum(int(r.get("count") or 0) for r in drafts
                           if r.get("post_type") == post_type
                           and r.get("project_name") == proj)
        if buffered >= DRAFT_BUFFER_MAX and not args.force:
            log(f"draft buffer healthy ({buffered} >= {DRAFT_BUFFER_MAX}); skipping")
            sys.exit(0)

        args.post_number = int(ctx.get("next_post_number") or 1)
        nnn = f"{args.post_number:03d}"
        manifest = json.loads(CLIP_MANIFEST.read_text())

        plan = run_tlh(args, ctx, manifest) if args.scenario == "tlh" \
            else run_mixer(args, ctx, manifest)

        # audio: least-recently-used local track (never network-sourced)
        lru = audio_lru(ctx)
        if not lru:
            raise SystemExit(f"no audio tracks in {AUDIO_DIR}")
        audio_track = lru[0]

        silent = Path(tempfile.gettempdir()) / f"ig_gemini_{nnn}_silent.mp4"
        remotion_render(plan["composition"], plan["props"], silent)
        duration = plan["duration_sec"] or round(probe_duration(silent), 2)

        out_dir = Path(tempfile.gettempdir()) if args.dry_run else OUT_DIR
        video_path = out_dir / f"post-{nnn}.mp4"
        dub_audio(silent, Path(audio_track), video_path, duration)
        silent.unlink(missing_ok=True)
        caption_path = video_path.with_name(f"post-{nnn}.caption.txt")
        caption_path.write_text(plan["caption"])
        log(f"deliverables: {video_path} ({duration}s) + caption "
            f"({len(plan['caption'])} chars)")

        gcs_url = None
        if args.upload_gcs:
            gcs_url = gcs_upload(video_path)
            plan["metadata"]["render_host"] = os.uname().nodename

        row = {
            "post_number": args.post_number,
            "video_path": gcs_url or str(video_path),
            "caption_text": plan["caption"],
            "post_type": plan["post_type"],
            "target_account": args.account,
            "variant_id": plan["variant_id"],
            "project_name": plan["project_name"],
            "audio_source": f"local:{audio_track}",
            "duration_sec": duration,
            "width": 1080,
            "height": 1920,
            "overlays": plan["overlays_db"],
            "source_clips": plan["source_clips_db"],
            "metadata": plan["metadata"],
        }
        if args.dry_run:
            log("dry-run: skipping POST /api/v1/media-posts")
            print(json.dumps(row, indent=2))
            return
        resp = api_post("/api/v1/media-posts", row)
        allocated = ((resp.get("data") or {}).get("post_number"))
        log(f"draft row created: post_number={allocated}")

        post_result = None
        if args.post:
            post_result = post_now(
                video_path, gcs_url, plan["caption"], args.account,
                plan["post_type"], trial=(args.post == "trial"),
            )

        print(json.dumps({
            "post_number": allocated,
            "video_path": gcs_url or str(video_path),
            "caption_len": len(plan["caption"]),
            "variant_id": plan["variant_id"],
            "post_type": plan["post_type"],
            "theme_angle": plan["metadata"].get("theme_angle"),
            "posted": post_result,
        }, indent=2))
    finally:
        release_render_lock()


if __name__ == "__main__":
    main()
