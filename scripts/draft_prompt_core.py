#!/usr/bin/env python3
"""Single source of truth for every DRAFTING prompt, all platforms.

Why this file exists (2026-07-16): the X draft prompt lived in a bash heredoc
inside skill/run-twitter-cycle.sh and the Reddit draft prompt lived in
scripts/post_reddit.py:build_draft_prompt. Every prompt improvement (arm-aware
directives, voice corpus, self-memory, learned preferences) had to be edited
twice and in practice was edited once, so the lanes drifted. From now on:

  RULE: NO PROMPT TEXT IN SHELL, and no per-platform copies of shared
  sections. A drafting experiment is implemented HERE (arm-aware rendering)
  and applies to every platform on the same commit. Platform adapters supply
  only what is genuinely platform-specific: the candidate block, the
  selection-gate examples, and the output schema wording.

Consumers:
  - skill/run-twitter-cycle.sh: `pick-arm` then `render-twitter --ingredients
    <json file>` (blocks the shell already computes for other reasons ride in
    as ingredients; everything else is computed here).
  - scripts/post_reddit.py: imports render_reddit_prompt() /
    pick_draft_prompt_arm() directly.

Experiment surfaces owned by this module:
  - draft_prompt arm (treatment_v4 / control_v4): pick_draft_prompt_arm()
    (env override -> server per-install pin -> coin flip), the arm-aware
    DRAFT DIRECTIVE texts, the Draft-B divergence note, and the
    treatment-side skip of the per-style top-performers report.
    engagement_styles.get_assigned_style_prompt reads the SAME
    S4L_DRAFT_PROMPT_VARIANT env var at render time, so callers must export
    it (pick_draft_prompt_arm does) BEFORE rendering style blocks.
  - lane (promotion / personal_brand): lane-aware directive selection,
    persona whitelist vs ops-denylist projects JSON.

The twitter template below is a byte-exact transcription of the
run-twitter-cycle.sh PREP_PROMPT heredoc as of 2026-07-16 (verified by
scripts/test_draft_prompt_core.sh, which evals the shell fragment and diffs).
Do not "clean up" its wording here without knowing you are changing the live
prompt for every platform.
"""

import json
import os
import subprocess
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


def _repo_dir():
    return os.path.expanduser(
        os.environ.get("S4L_REPO_DIR") or os.environ.get("REPO_DIR") or "~/social-autoposter"
    )


def _config_path():
    # Prompt-sandbox config override: a sandbox run testing another install's
    # real voice/persona points this at a fetched copy instead of the
    # operator's own config.json. Checked BEFORE the real path since the
    # repo-root config.json is a symlink to the state dir.
    _sandbox_dir = os.environ.get("S4L_SANDBOX_CONFIG_DIR")
    if _sandbox_dir:
        return os.path.join(_sandbox_dir, "config.json")
    return os.path.join(_repo_dir(), "config.json")


# --------------------------------------------------------------------------
# Draft-prompt A/B arm (v4, voice-first). One pick function for every lane
# and platform. Precedence: pre-set env (sandbox forcing) -> server-side
# per-install pin -> coin flip at TWITTER_DRAFT_PROMPT_AB_RATE (default 0.5,
# a real holdback everywhere). Fails open to the coin flip; fails closed to
# treatment_v4 only if even the flip errors (mirrors the shell's `|| echo`).
# --------------------------------------------------------------------------

ARM_TREATMENT = "treatment_v4"
ARM_CONTROL = "control_v4"

# Must match the duplicated "voice_first" literal in
# engagement_styles.get_assigned_style_prompt (not imported across modules,
# matching how ARM_TREATMENT's "treatment_v4" string is itself duplicated
# there). treatment_v4 assigns NO real engagement style as of 2026-07-17;
# every treatment_v4 Twitter post is stamped with this sentinel instead.
STYLE_SENTINEL_TREATMENT = "voice_first"


def pick_draft_prompt_arm(env=None, export=True):
    """Return (variant, source). source in {env, pin, coin, fallback}.

    When export=True (default) the chosen variant is written back to
    os.environ["S4L_DRAFT_PROMPT_VARIANT"] so downstream in-process renderers
    (engagement_styles.get_assigned_style_prompt) and active_experiments
    .collect() see the same arm without any extra plumbing.
    """
    env = os.environ if env is None else env
    variant = (env.get("S4L_DRAFT_PROMPT_VARIANT") or "").strip()
    source = "env"
    if not variant:
        try:
            from http_api import api_get
            r = api_get("/api/v1/installations/draft-prompt-variant")
            variant = ((r or {}).get("data", {}) or {}).get("variant") or ""
            variant = variant.strip()
            if variant:
                source = "pin"
        except BaseException:
            variant = ""
    if not variant:
        try:
            import random
            rate = float(env.get("TWITTER_DRAFT_PROMPT_AB_RATE") or 0.5)
            rate = min(1.0, max(0.0, rate))
            variant = ARM_TREATMENT if random.random() < rate else ARM_CONTROL
            source = "coin"
        except Exception:
            variant = ARM_TREATMENT
            source = "fallback"
    if export:
        os.environ["S4L_DRAFT_PROMPT_VARIANT"] = variant
    return variant, source


# --------------------------------------------------------------------------
# Arm/lane-aware DRAFT DIRECTIVE + Draft-B divergence note.
# Text transcribed verbatim from run-twitter-cycle.sh (2026-07-16).
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Shared treatment_v4 core (2026-07-17). Written ONCE, used by BOTH lanes.
#
# Why this exists: promotion and persona used to carry independent copies of
# nearly-identical priority-order/skeleton-ban/vary-entry-point text. They
# drifted -- persona's copy was missing the "vary the entry point across
# replies" sentence promotion had, which a real batch comparison traced to
# the "nobody ___s it" tell appearing 2x as often in treatment as control:
# a batch shares ONE assigned style, and without an explicit
# vary-across-the-batch instruction the model reused that style's defining
# vocabulary verbatim across every unrelated candidate. Do NOT fork this
# text again for a lane-specific tweak; parameterize _treatment_core()
# instead, or add a lane-specific sentence OUTSIDE it.
# --------------------------------------------------------------------------

def _treatment_core(voice_examples_ref):
    """Shared PRIORITY ORDER + skeleton ban + entry-point-variation text.

    voice_examples_ref: the lane-specific noun phrase for priority item (2)
    (promotion scopes to "the matched project's voice.examples"; persona
    scopes to "voice.examples above" since it's already narrowed to the one
    persona project). No mention of engagement style anywhere in here on
    purpose: treatment_v4 assigns none (see STYLE_SENTINEL_TREATMENT).
    """
    return (
        "PRIORITY ORDER for how you write this, highest first: (1) "
        "learned_preferences.draft_style_notes and edit_examples are the "
        "strongest signal available, real human corrections to this "
        "account's own past drafts, and are MANDATORY, not advisory. (2) "
        f"The ACCOUNT VOICE CORPUS block and {voice_examples_ref} are "
        "VERBATIM GROUND TRUTH for how this account actually writes: "
        "capitalization, punctuation, contractions, sentence length, "
        "terseness. Match those mechanics exactly. (3) voice.tone is a "
        "supporting description only, never ground truth: if it ever "
        "conflicts with what the corpus/examples actually show, follow the "
        "examples, not the tone description. Do NOT use the "
        "concede-then-reverse skeleton in ANY form. Banned openings "
        "include: 'X is the easy part/half/win, the hard part is Y'; 'X "
        "was never the [thing], it's Y'; 'X isn't the [problem], it's Y'; "
        "'the real/actual/harder part is Y'; 'what actually "
        "breaks/ships/matters is Y'; 'the part nobody says/shows is Y'; 'X "
        "is solved, Y is what breaks'. Lead with substance from ONE entry "
        "point and vary the entry point AND the rhetorical move across "
        "every reply you draft this batch, even when the same topic or "
        "style would naturally suggest reusing the same construction: a "
        "concrete first-hand specific or number; a direct answer to the "
        "exact question asked; one sharp opinion with no hedge; a genuine "
        "question that moves the thread forward; or a relevant pointer. No "
        "warm-up framing sentence before the substance."
    )


_TREATMENT_LENGTH_NOTE = (
    "Length: match how long the account's own real examples above actually "
    "run; when the corpus doesn't make it obvious, default to one or two "
    "sentences, well under Twitter's 250-character practical limit. NEVER "
    "em dashes."
)

_DIRECTIVE_TREATMENT = (
    "Otherwise: draft a direct, natural reply that stands on its own as a "
    "useful contribution to the thread. Mention the matched project only "
    "when it is genuinely the most relevant thing to say, and state it "
    "plainly in one clause; most replies will not need it. "
    + _treatment_core("the matched project's voice.examples") +
    " " + _TREATMENT_LENGTH_NOTE +
    " Never violate voice.never. Treat learned_preferences.audience_avoid "
    "/ thread_avoid matches as strong reasons to skip the candidate. Never "
    "violate content_guardrails.do_not."
)

_DIRECTIVE_CONTROL = "Otherwise: draft a reply using the best engagement style. Length is governed ENTIRELY by the per-style LENGTH LIMIT in the style block above; obey that target and ceiling, do not apply any other length rule here. NEVER em dashes. Apply the matched project's `voice` block from ALL_PROJECTS_JSON: follow voice.tone, never violate voice.never, mirror voice.examples / voice.examples_good when present. The global learned_preferences block under PROJECT ROUTING is distilled human review feedback and is MANDATORY, not advisory: follow every learned_preferences.draft_style_notes entry when writing (it overrides the engagement style's structural template on conflict), and treat learned_preferences.audience_avoid / thread_avoid matches as strong reasons to skip the candidate. Never violate content_guardrails.do_not."

_DIVERGENCE_NOTE_TREATMENT = " draft_a_text and draft_b_text must be genuinely distinct takes on this thread regardless of style -- think of them as variant 1a and variant 1b: different entry point, different specific detail seized on, different rhetorical move -- not two phrasings of the same underlying observation. If 1a and 1b would say essentially the same thing, pick a different angle entirely for 1b rather than just rewording 1a."

_PERSONA_DIRECTIVE_TEMPLATE = "Otherwise: draft a reply that stands on its own as a genuinely useful contribution to THIS thread. Ground it in the persona's real, first-hand experience from the ACCOUNT VOICE CORPUS block below (specific projects, real numbers, sharp opinions, actual failures) and in the persona's `voice` block from ALL_PROJECTS_JSON. Add exactly ONE of: a concrete specific from that lived experience, a sharp non-obvious opinion, a useful pointer, or a question that genuinely moves the thread forward. NEVER generic agreement ('makes sense', 'this is spot on', 'great point', 'the nuance here is').@TREATMENT_CORE@ This is a personal account, not a brand: sound like a real person in the thread. If web search is available and the thread hinges on a current fact, verify it before drafting rather than guessing.@LENGTH_NOTE@ Follow voice.tone, never violate voice.never, mirror voice.examples / voice.examples_good when present. The global learned_preferences block under PROJECT ROUTING is distilled human review feedback and is MANDATORY, not advisory: follow every learned_preferences.draft_style_notes entry when writing @PREFS_RELATION@, and treat learned_preferences.audience_avoid / thread_avoid matches as strong reasons to skip the candidate. Never violate content_guardrails.do_not."


def draft_directive(arm=None, lane=None):
    """The arm+lane-aware DRAFT DIRECTIVE sentence block."""
    arm = arm or os.environ.get("S4L_DRAFT_PROMPT_VARIANT") or ""
    lane = lane if lane is not None else os.environ.get("S4L_ACTIVE_LANE", "")
    if lane == "personal_brand":
        if arm == ARM_TREATMENT:
            core = " " + _treatment_core("voice.examples above")
            length_note = " " + _TREATMENT_LENGTH_NOTE
            rel = "(per the PRIORITY ORDER above: learned_preferences and the account's real voice are what drive this reply)"
        else:
            core = ""
            length_note = " Length is governed by the per-style LENGTH LIMIT in the style block above. NEVER em dashes."
            rel = "(it overrides the engagement style's structural template on conflict)"
        return (
            _PERSONA_DIRECTIVE_TEMPLATE
            .replace("@TREATMENT_CORE@", core)
            .replace("@LENGTH_NOTE@", length_note)
            .replace("@PREFS_RELATION@", rel)
        )
    if arm == ARM_TREATMENT:
        return _DIRECTIVE_TREATMENT
    return _DIRECTIVE_CONTROL


def draft_b_divergence_note(arm=None):
    arm = arm or os.environ.get("S4L_DRAFT_PROMPT_VARIANT") or ""
    return _DIVERGENCE_NOTE_TREATMENT if arm == ARM_TREATMENT else ""


def skip_top_report(arm=None):
    """treatment_v4 skips the per-style top_performers exemplar report
    entirely: cross-account 'what wins in this style' signal competes with
    the account's own real voice. control_v4 keeps it. One decision point
    for every platform."""
    arm = arm or os.environ.get("S4L_DRAFT_PROMPT_VARIANT") or ""
    return arm == ARM_TREATMENT


# --------------------------------------------------------------------------
# Shared context blocks (computed from disk/config; deterministic).
# --------------------------------------------------------------------------

_CORPUS_HEADER = """## ACCOUNT VOICE CORPUS (raw first-hand material — ground your reply in THIS)
This is the account holder's own public writing and work, verbatim. Quote and draw real specifics from it: actual projects, real numbers, sharp opinions, real failures. Do NOT invent anything not supported here or in the project's voice block. Use it to make the reply concrete and unmistakably human, and as ground truth for HOW they write (capitalization, punctuation, contractions, length) regardless of which project this reply is for."""


def corpus_block():
    """The ACCOUNT VOICE CORPUS block (both lanes, every platform), or ''
    when persona_corpus.txt does not exist. Byte-matches the shell's
    CORPUS_BLOCK assembly (header + file content, command-substitution
    semantics: trailing newlines of the file stripped, one trailing \n)."""
    _sandbox_dir = os.environ.get("S4L_SANDBOX_CONFIG_DIR")
    path = (
        os.path.join(_sandbox_dir, "persona_corpus.txt")
        if _sandbox_dir
        else os.path.join(_repo_dir(), "persona_corpus.txt")
    )
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return ""
    return _CORPUS_HEADER + "\n" + content.rstrip("\n") + "\n"


# Ops-only plumbing the drafter can never use. Denylist, not whitelist, on
# purpose: an unknown future MARKETING field should reach the drafter by
# default; add a key here only when it is provably non-drafting plumbing.
# landing_pages must stay (the plan schema's has_landing_pages is defined
# against it).
_OPS_KEYS = {
    "posthog", "contact", "seo_author", "seo_roundup", "web_chat",
    "short_links_host", "short_links_live", "external_short_links",
    "force_utm_only", "booking_link_auto_share", "onboarded_at",
    "client", "client_engagement", "geo_focus", "engagement_start",
    "weight", "enabled", "demo_video", "platforms_disabled",
    "brand_domain", "learned_preferences",
}

# Personal-brand lane is pure organic growth: the drafter must NOT see any
# product config at all. Whitelist, not denylist: any field added to the
# persona entry later stays out unless explicitly allowed here.
_PERSONA_ALLOWED = {
    "name", "description", "content_angle", "voice",
    "voice_relationship", "content_guardrails",
}


def _load_config():
    return json.load(open(_config_path()))


def projects_json(lane=None, only_project=None):
    """The ALL_PROJECTS_JSON block: per-project drafting config as a JSON
    string. lane='personal_brand' emits ONLY the persona project through the
    whitelist; otherwise every project through the ops denylist.
    only_project (reddit lane: one project per draft call) narrows the
    promotion output to that single project's entry. Returns '{}' on any
    failure, mirroring the shell's `|| echo "{}"`."""
    lane = lane if lane is not None else os.environ.get("S4L_ACTIVE_LANE", "")
    try:
        config = _load_config()
        projects = config.get("projects", [])
        if lane == "personal_brand":
            persona = next((p for p in projects if p.get("persona") is True), None)
            out = {}
            if persona:
                out[persona["name"]] = {k: v for k, v in persona.items() if k in _PERSONA_ALLOWED}
            return json.dumps(out, indent=2)
        out = {}
        for p in projects:
            if only_project and p.get("name") != only_project:
                continue
            out[p["name"]] = {k: v for k, v in p.items() if k not in _OPS_KEYS}
        return json.dumps(out, indent=2)
    except Exception:
        return "{}"


def global_learned_prefs_json():
    """The install-wide learned_preferences block as a JSON string, history
    stripped (audit changelog, no prompt reader). '{}' on failure."""
    try:
        import learned_preferences as lp
        config = _load_config()
        block = lp.get_global_block(config)
        block.pop("history", None)
        return json.dumps(block, indent=2)
    except Exception:
        return "{}"


def recent_self_block(platform, limit=20):
    """Cross-cycle self-memory (anti-repetition negative context). Empty
    string on any failure; never blocks a cycle."""
    try:
        out = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, "recent_self_posts.py"),
             "--platform", platform, "--limit", str(limit)],
            capture_output=True, text=True, timeout=60,
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def product_names():
    """Every configured project name (lowercased) plus the agency tag, for
    the reddit never-name-the-product rule. Falls back to a generic list on
    config failure so the rule never silently vanishes."""
    names = []
    try:
        config = _load_config()
        for p in config.get("projects", []):
            n = (p.get("name") or "").strip().lower()
            if n and n not in names:
                names.append(n)
    except Exception:
        pass
    if "s4l" not in names:
        names.append("s4l")
    return names


# --------------------------------------------------------------------------
# TWITTER renderer. Byte-exact transcription of the run-twitter-cycle.sh
# PREP_PROMPT heredoc (2026-07-16). @TOKENS@ are unique and replaced below;
# everything else, including em dashes and '--' runs, is preserved verbatim.
# --------------------------------------------------------------------------

_TW_TEMPLATE = """@PREFIX@You are the Social Autoposter prep step.

Your ONLY job in THIS session:
  1. Read each candidate's thread context from the PRE-SCORED CANDIDATES block below (each entry's 'Text:' field is the parent tweet). You have WebSearch and WebFetch available: use them ONLY when a thread hinges on a current fact, a name, a release, or a claim you are not sure about, so your reply is specific and correct instead of vague. You do NOT have the Twitter/X browser this session — never fetch, navigate, or open a tweet/x.com URL, and never try to load the thread itself; the thread text you need is already inlined below. Most replies need no search at all; reach for it only when it materially improves the reply.
  2. Draft TWO independent replies for each fresh candidate, one under Draft A's assigned style and one under Draft B's assigned style below. Do not judge or rank them, the reviewer reads both and picks.
  3. Persist the recommended fresh draft via log_draft.py.
  4. Emit a structured plan describing the chosen candidates, both draft texts, and (when applicable) the SEO link keyword + slug.

You will NOT post anything. You will NOT generate landing pages. You will NOT call log_post.py. The shell handles all of that AFTER your session ends, with the browser lock released for the long landing-page build.

Read @SKILL_FILE@ for content rules and voice context.
Read @REPO_DIR@/config.json for project metadata.

## PRE-SCORED CANDIDATES (sorted by Virality DESC, highest first)
Virality is a composite predictor of how big this thread will get AFTER you reply: it combines engagement velocity (eng/hour), author reach (follower tier), age decay (6h half-life), retweet ratio, reply count, and discussion quality (reply:like ratio). On historical posted data the highest-Virality cohort (score >= 10000) received ~36x the median reply views of the lowest cohort (score < 10), so prioritize on-brand candidates with HIGH Virality. Rule of thumb: Virality >= 100 = strong thread on a real growth curve, your reply is likely to land 10-100x more eyeballs than a low-Virality thread. Delta (5min) is the raw T1-T0 engagement count and is shown for context only; do not re-rank on Delta.
@CANDIDATE_BLOCK@
@MEDIA_BLOCK@
@CORPUS_BLOCK@

## PROJECT ROUTING (per-candidate)
Each candidate has a 'Project match' field. Use that project unless the thread content clearly better fits another project.
All project configs: @ALL_PROJECTS_JSON@
Global learned preferences (ONE install-wide block; it applies to EVERY project. Any directive in this prompt that references a project's learned_preferences block means THIS block): @GLOBAL_LEARNED_PREFS_JSON@

## PROJECT TOP PERFORMERS (query on demand, do NOT skip routing first)
The feedback reports below carry a per-style exemplar only; project winners are no longer bulk-injected. AFTER you have decided which project a candidate's draft is for, you MAY pull that project's own recent winners (last 30 days, ranked by real click rate) when you are unsure how this product converts in replies:
   python3 @REPO_DIR@/scripts/top_performers.py --platform twitter --project 'PROJECT_NAME' --top 3 --brief --invoked-by '@BATCH_ID@'
(PROJECT_NAME exactly as it appears in the candidate's 'Project match' / config.json.) Treat the results as evidence of which CLAIMS and ANGLES landed for that product, never as structural templates: do not copy their sentence shape, opener, or pivot wording. One call per project at most; skip the call entirely for projects you already queried this session.

@RECENT_SELF_BLOCK@

## DRAFT A: assigned style + feedback from past performance
@TOP_REPORT@

@STYLES_BLOCK@

## DRAFT B: assigned style + feedback from past performance
@TOP_REPORT_B@

@STYLES_BLOCK_B@

## WORKFLOW
There is NO cap on how many candidates you may pick this cycle. Pick EVERY candidate whose thread is genuinely on-brand and worth a substantive reply. Skip a candidate ONLY when its thread is off-topic for the matched project, toxic / hateful, low-quality / spam, an audience mismatch, or a near-duplicate of something already replied to. Do NOT cap, quota, or balance picks by project: if the strongest candidates this cycle all belong to one project, pick all of them. Project routing matters; project diversification does not. Never force a weak entry just to add volume, and never drop a strong on-brand entry just to limit volume.

For each chosen candidate:
1. Read the candidate's parent tweet from its 'Text:' field in the PRE-SCORED CANDIDATES block above.
2. Understand the context from that inlined text (the thread text is already in this prompt; you do NOT have the Twitter browser, but you MAY use WebSearch/WebFetch for external facts when a thread needs them to be answered well).
3. DRAFT HANDLING (existing vs fresh):
   - If the candidate block shows an EXISTING DRAFT line AND draft age < 30 minutes, REUSE the draft text verbatim as draft_a_text/draft_a_style (set is_reused_draft=true, draft_b_text=null, draft_b_style=null). Do NOT call log_draft.py; do NOT redraft; do NOT write a second variant, prior cycle already paid the LLM cost for the one draft you have.
   - Otherwise (fresh candidate, is_reused_draft=false): write TWO independent drafts. Do NOT judge, rank, or pick a favorite between them, both are shown to the reviewer, who decides.
     - draft_a_text: follow the DRAFT A style block above (its own description/example/note/length limit).
     - draft_b_text: follow the DRAFT B style block above, written INDEPENDENTLY from scratch as if draft_a_text did not exist. Do NOT lightly reword draft_a_text into draft_b_text, they must diverge in length and rhetorical move because they follow different style templates, not just differ in phrasing. If you notice draft_b_text ending up as a paraphrase of draft_a_text, stop and rewrite it from Style B's own example instead.@DRAFT_B_DIVERGENCE_NOTE@
   - @DRAFT_DIRECTIVE@ (applies to both drafts on fresh candidates; each still obeys its OWN style's length limit, not a shared one).
3a. PERSIST DRAFT A (skip entirely for reused drafts):
     python3 @REPO_DIR@/scripts/log_draft.py --candidate-id CANDIDATE_ID --text 'DRAFT_A_TEXT' --style DRAFT_A_STYLE --assigned-style '@PICKED_STYLE@' --assigned-mode '@PICKED_MODE@'
   Always persist draft_a_text/draft_a_style here (Draft A is the single-draft representative used if a near-immediate next cycle reuses this candidate's draft verbatim per step 3 above); never draft_b.
   The --assigned-style / --assigned-mode flags carry the orchestrator's picker output (this cycle: mode=@PICKED_MODE@ style='@PICKED_STYLE_OR_INVENT@') into the candidate row so the post pipeline can coerce drift and register invented styles. Pass them VERBATIM as shown.
   If Draft A used an invented style (i.e. mode is invent and its STYLE is a new snake_case name not in the Draft A style block), ALSO pass:
     --new-style '{"description":"...","example":"...","why_existing_didnt_fit":"..."}'
   with the same description/example/why_existing_didnt_fit you put in draft_a_new_style in your output JSON for this candidate.
   Failure here is non-fatal, log a warning and continue.
4. EMIT one entry in the structured 'candidates' array with these fields:
   - candidate_id (int): from the candidate block
   - candidate_url (string): the parent tweet URL
   - thread_author (string): the @handle (no leading @)
   - thread_text (string): the parent tweet's text, condensed to <=500 chars if needed
   - matched_project (string): the project name to attribute this post to
   - is_reused_draft (bool, REQUIRED): true iff you reused an existing draft verbatim per step 3 above, false for a freshly-drafted candidate.
   - draft_a_text (string, REQUIRED): the FINAL Draft A reply text WITHOUT any URL appended (the shell appends the URL later). 250 chars is the hard ceiling (leaves room for a 23-char t.co link inside the 280-char cap) — stay well under it, not up to it. On a reused candidate this IS the reused text.
   - draft_a_style (string, REQUIRED): style name applied to draft_a_text (or the reused candidate's existing style). In USE mode (@PICKED_MODE@=use) this MUST be the Draft A assigned style name '@PICKED_STYLE@' verbatim; the orchestrator silently coerces drift back. In INVENT mode (@PICKED_MODE@=invent) this MUST be a NEW snake_case style name not in the Draft A style block.
   - draft_a_new_style (object, REQUIRED iff Draft A's INVENT mode produced a new name; OMIT or set null otherwise): {description (string), example (string), why_existing_didnt_fit (string), note (string, optional), target_chars (integer, REQUIRED)}. target_chars is the comment length THIS new style wins at, in characters; the example you write must be EXACTLY that length, write the example first, count its characters, then set target_chars to that count. Bias SHORT: one-liner style ~45, story-arc style up to ~180, never above 220.
   - draft_a_text_en (string, REQUIRED when language != 'en'; null when language == 'en'): a faithful English translation of draft_a_text. Display-only: the review card shows it so the operator can understand a non-English draft; it is NEVER posted. Translate meaning, not word-by-word; no added commentary.
   - draft_b_text (string, REQUIRED when is_reused_draft=false; null when is_reused_draft=true): the FINAL Draft B reply text, same rules as draft_a_text, written under the DRAFT B style block instead.
   - draft_b_style (string, REQUIRED when is_reused_draft=false; null when is_reused_draft=true): style name applied to draft_b_text, same rules as draft_a_style but against the Draft B assignment (USE mode style '@PICKED_STYLE_B@', mode @PICKED_MODE_B@).
   - draft_b_new_style (object, REQUIRED iff Draft B's INVENT mode produced a new name; OMIT or set null otherwise): same shape as draft_a_new_style.
   - draft_b_text_en (string, REQUIRED when is_reused_draft=false AND language != 'en'; null otherwise): faithful English translation of draft_b_text, same display-only rules as draft_a_text_en.
   - language (string): ISO 639-1 code (en, ja, zh, es, ...)
   - thread_text_en (string, REQUIRED when language != 'en'; OMIT when language == 'en'): a faithful English translation of thread_text (same <=500 char condensation). Display-only, never posted.
   - has_landing_pages (bool): true iff the matched project has BOTH landing_pages.repo AND landing_pages.base_url set in config.json. Otherwise false.
   - link_keyword (string, REQUIRED when has_landing_pages=true; OMIT otherwise): a SHORT 3-6 word phrase that captures the ESSENCE OF YOUR REPLY (not just the thread topic). Think: what would a reader search to find a useful page about what you just said?
   - link_slug (string, REQUIRED when has_landing_pages=true; OMIT otherwise): kebab-case, alphanumeric+hyphens only, max 50 chars.
   - search_topic (string, REQUIRED): normally the EXACT 'Search query' value from this candidate's block above, copied verbatim (do not paraphrase, normalise, or trim). EXCEPTION (cross-route): if the matched_project you chose for this candidate is DIFFERENT from the candidate's 'Project match' field (i.e. you re-routed the thread to a better-fitting project), set search_topic to an empty string "" instead. The origin query's topic belongs to the project that ISSUED that query, not the one you routed to; copying it onto the new project's post miscredits the new project's topic ranking and the issuing project's query bank. When matched_project equals the 'Project match' field, copy the topic verbatim as before. The shell stamps this onto posts.search_topic so the next cycle's Phase 1 can rank which topics convert (clicks per post) and evolve the universe accordingly.

5. CLASSIFY EVERY PRE-SCORED CANDIDATE into ONE of THREE outcomes. There is NO post cap and NO per-project quota: post EVERY thread you judge genuinely on-brand.
   (a) 'candidates' — an on-brand pick you are replying to this cycle (step 4 above). No cap.
   (b) 'rejected' — ONLY for a PERMANENT, thread-intrinsic reason this thread should NEVER be replied to for the matched project: off-topic for the project, toxic / hateful, low-quality / spam / promo / shill, audience or ICP mismatch, our own account, or stale. Reason must be <=200 chars, plain text, no quotes. CRITICAL: the shell marks every 'rejected' entry status='skipped', and a skipped (thread, project) is filtered out of ALL future scans for this account PERMANENTLY. Only reject things that will never be a good fit.
   (c) OMIT from BOTH arrays — for a TIMING-ONLY reason where the thread itself is fine but you are simply not posting to it right NOW. Omitting keeps it 'pending' so a later cycle can re-judge it. ALWAYS omit (NEVER reject) when your only reason is one of:
       - you preferred a stronger candidate this cycle (there is no cap, so ideally just post this one too; if you still defer, omit it),
       - it is a near-duplicate of another thread you are already picking THIS cycle,
       - you already engaged this author / a similar thread this cycle and want to avoid back-to-back over-engagement.
       These are DEFERRALS, not rejections. Putting any of them in 'rejected' would permanently blacklist a thread that is actually fine. Do NOT do that.
   It is fine for 'candidates' to be empty (nothing on-brand) and fine for 'rejected' to be empty (nothing permanently unsuitable).
   Do NOT update twitter_candidates yourself; the shell will mark every entry of 'rejected' as status='skipped' with the reason, and Phase 0 will salvage anything you omit or forget.

5a. SELF-IMPROVING PROJECT-WIDE EXCLUSION LIST (optional, on rejected entries only):
    When you put a candidate in 'rejected' BECAUSE of a stable, recurring CLASS of false-positive (not a one-off bad tweet), you MAY include a 'proposed_excludes' array of 1-3 specific keywords. If you do, the pipeline will (after a 2-distinct-batch activation gate) automatically append `-keyword` to ALL future Twitter searches for the matched_project, project-wide and persistent. This is the ONLY upstream block against the entire class of false-positive that a tighter Phase 1 query alone cannot prevent.

    USE THIS POWER NARROWLY. False-negatives (legit tweets being filtered out) are far worse than the cost of seeing one more cricket tweet. Apply ALL of these rules:

    - DO emit when: the false-positive is caused by a SPECIFIC ambiguous proper noun, brand, or domain term that has a wholly unrelated meaning collisional with the project. Example for Vipassana: an IPL/cricket thread surfaced because the search query included `Goenka` (the meditation teacher S.N. Goenka shares a surname with Sanjiv Goenka, owner of an IPL team). Right proposed_excludes: ['cricket','kohli','ipl','lsg','rcb']. WRONG proposed_excludes: ['goenka'] (would mute legit S.N. Goenka tweets).

    - DO NOT emit when: the candidate is just personally low-quality (spam, low engagement, generic), the language is wrong, the author is bot-like, or the thread is just slightly off-topic. Those are one-offs, NOT classes. Use the 'reason' field instead.

    - Each proposed term must be:
      * a SINGLE token, lowercase, ascii letters/digits/hyphen only, no spaces, length 3-32. (e.g. 'cricket', 'kohli', 'ipl', 'lsg', 'rcb-fan', 'crypto', 'memecoin').
      * SPECIFIC and unambiguous in the project's domain. Proper nouns, brand names, narrow jargon, sport/team/franchise terms preferred. Generic words like 'practice', 'retreat', 'meditation', 'work', 'tips', 'app', 'tool', 'help' are FORBIDDEN — they will produce false-negatives.
      * NOT a core search topic of the matched_project (the validator rejects any term in the project's search_topics, so don't waste tokens proposing one).

    - Cap: at most 3 terms per rejected entry. If you need more, you're probably proposing too generically — narrow the list.

    - Activation gate: each term needs >=2 SEPARATE batches to propose it before it goes live, so a single false-rejection cannot mute a search. You don't need to think about this — propose if you'd be confident a future cycle's Claude would also propose it; if not, leave proposed_excludes off.

    - When in doubt, omit the field entirely. The default behavior (no proposed_excludes) is safe; over-proposing is not.

CRITICAL:
- DO NOT post anything. The shell handles posting.
- DO NOT call twitter_browser.py.
- DO NOT call generate_page.py (the shell runs it AFTER your session, outside the lock).
- DO NOT call log_post.py or campaign_bump.py.
- You do NOT have the Twitter/X browser this session: never navigate, fetch, or open a tweet/x.com URL, and never try to reload the thread. WebSearch/WebFetch ARE available for external fact-checking only; use them sparingly and never to open the tweet itself.
- NEVER use em dashes. Use commas, periods, or regular dashes (-).
- Reply in the SAME LANGUAGE as the parent tweet."""


def render_twitter_prompt(ing):
    """Render the full X/Twitter prep prompt. `ing` is a dict of ingredient
    strings the cycle computes anyway (candidate/media blocks, arm-gated top
    reports, style blocks, self-memory); everything else is computed here.

    Keys: batch_id, skill_file, repo_dir, picked_style, picked_mode,
    picked_style_b, picked_mode_b, candidate_block, media_block, top_report,
    top_report_b, styles_block, styles_block_b, recent_self_block,
    prefix (optional, default ''), lane (optional), arm (optional).
    """
    arm = ing.get("arm") or os.environ.get("S4L_DRAFT_PROMPT_VARIANT") or ""
    lane = ing.get("lane") if ing.get("lane") is not None else os.environ.get("S4L_ACTIVE_LANE", "")
    picked_style = ing.get("picked_style") or ""
    return (
        _TW_TEMPLATE
        .replace("@PREFIX@", ing.get("prefix") or "")
        .replace("@SKILL_FILE@", ing["skill_file"])
        .replace("@REPO_DIR@", ing["repo_dir"])
        .replace("@CANDIDATE_BLOCK@", ing.get("candidate_block") or "")
        .replace("@MEDIA_BLOCK@", ing.get("media_block") or "")
        .replace("@CORPUS_BLOCK@", corpus_block())
        .replace("@ALL_PROJECTS_JSON@", projects_json(lane=lane))
        .replace("@GLOBAL_LEARNED_PREFS_JSON@", global_learned_prefs_json())
        .replace("@BATCH_ID@", ing.get("batch_id") or "")
        .replace("@RECENT_SELF_BLOCK@", ing.get("recent_self_block") or "")
        .replace("@TOP_REPORT@", "" if skip_top_report(arm) else (ing.get("top_report") or ""))
        .replace("@TOP_REPORT_B@", "" if skip_top_report(arm) else (ing.get("top_report_b") or ""))
        .replace("@STYLES_BLOCK@", ing.get("styles_block") or "")
        .replace("@STYLES_BLOCK_B@", ing.get("styles_block_b") or "")
        .replace("@DRAFT_B_DIVERGENCE_NOTE@", draft_b_divergence_note(arm))
        .replace("@DRAFT_DIRECTIVE@", draft_directive(arm, lane))
        .replace("@PICKED_STYLE_OR_INVENT@", picked_style or "(invent)")
        .replace("@PICKED_STYLE_B@", ing.get("picked_style_b") or "")
        .replace("@PICKED_MODE_B@", ing.get("picked_mode_b") or "use")
        .replace("@PICKED_STYLE@", picked_style)
        .replace("@PICKED_MODE@", ing.get("picked_mode") or "use")
    )


# --------------------------------------------------------------------------
# REDDIT renderer. Platform-specific text (selection gate, thread-content
# contract, output schema wording, subreddit excludes) transcribed from
# scripts/post_reddit.py:build_draft_prompt (2026-07-16); shared sections
# (corpus, project routing, learned prefs, self-memory, arm-aware directive,
# divergence note, on-demand top performers) are the SAME objects the
# twitter renderer uses, so a prompt experiment lands on both platforms in
# one edit here.
# --------------------------------------------------------------------------

_RD_TEMPLATE = """You will be handed up to @N_CANDIDATES@ Reddit thread(s) that survived the engagement-velocity (ripen) gate. Your job is to draft TWO independent comments (Draft A and Draft B, one under Draft A's assigned style and one under Draft B's assigned style below) for the ones where you can write something genuinely useful to that audience. Do not judge or rank them, the reviewer reads both and picks. Draft B must be written INDEPENDENTLY from scratch as if Draft A did not exist: do NOT lightly reword Draft A into Draft B, they must diverge in length and rhetorical move because they follow different style templates. If you notice Draft B ending up as a paraphrase of Draft A, stop and rewrite it from Style B's own example instead.@DRAFT_B_DIVERGENCE_NOTE@ Lean toward DRAFTING when the audience overlaps even partially with the project's user, and only OMIT on clear no-bridge cases.

Content angle: @CONTENT_ANGLE@

## Candidate threads (post-ripen):
@CANDIDATES_BLOCK@
@CORPUS_BLOCK@

## PROJECT ROUTING
Every draft this session is for ONE project: '@PROJECT_NAME@'.
Project config: @PROJECT_JSON@
Global learned preferences (ONE install-wide block; it applies to EVERY project. Any directive in this prompt that references a project's learned_preferences block means THIS block): @GLOBAL_LEARNED_PREFS_JSON@

## PROJECT TOP PERFORMERS (query on demand)
You MAY pull this project's own recent winners (last 30 days, ranked by real click rate) when you are unsure how this product lands in comments:
   python3 @REPO_DIR@/scripts/top_performers.py --platform reddit --project '@PROJECT_NAME@' --top 3 --brief --invoked-by '@BATCH_ID@'
Treat the results as evidence of which CLAIMS and ANGLES landed for that product, never as structural templates: do not copy their sentence shape, opener, or pivot wording. At most one call this session.

@RECENT_SELF_BLOCK@
@TOP_CTX@
## SELECTION GATE — soft fits are OK; reject only clear mismatches

The ripen step proves a thread is alive (people are voting/commenting). It does NOT prove the thread fits the project. Reddit search returns false positives based on raw token overlap (e.g. a search for "no-code app maker" surfaces r/gamemaker shader threads because of the word "maker"; a search for "E2E testing developer productivity QA" can surface a JonBenet murder thread because of how Reddit indexes acronyms). The gate exists to catch those token-overlap false positives, NOT to demand a perfect product fit on every thread.

For each thread, ask the **bridge test**:
"Could a thoughtful person from @PROJECT_NAME@'s audience plausibly read my comment and find it useful, regardless of whether they ever try the product?"

DRAFT it if YES. OMIT only if NO bridge exists at all (clear off-topic / hostile audience / token-overlap false positive). Soft / partial / adjacent fits are GOOD enough — a useful comment in an adjacent sub builds reputation even when no one converts. Don't optimize for purity. Don't artificially cap output. The post-phase will cap actual posting at a reasonable number, so feel free to draft for any thread that passes the soft bridge test.

DRAFT THESE (broad, inclusive — not just direct hits):
- Project: AI test automation (Assrt). Thread: "Playwright selectors keep breaking on every refactor" → direct fit. DRAFT.
- Project: AI test automation. Thread: r/QualityAssurance "How are people handling flaky CI tests?" → adjacent topic, same audience. DRAFT.
- Project: AI app builder (mk0r). Thread: "I want to prototype a tip calculator without learning React" → direct fit. DRAFT.
- Project: AI app builder. Thread: r/SaaS "Indie hackers shipping MVPs in a weekend" → adjacent: same builder mindset. DRAFT (helpful comment about iteration speed).
- Project: study tool (Studyly). Thread: r/medschool "best way to handle 200-slide lectures" → direct fit. DRAFT.
- Project: study tool. Thread: r/GetStudying "I'm burnt out, can't retain anything" → adjacent: study-habit audience. DRAFT (empathetic comment about active recall, even if no product mention).
- Project: home security camera (Cyrano). Thread: r/HomeImprovement "wired vs wireless cameras" → direct fit. DRAFT.

OMIT THESE (clear no-bridge cases only):
- Project: AI test automation. Thread: r/JonBenet "The Absurdity of the BDI Theory" → token-overlap false positive (BDI ≠ a testing acronym here). 1996 murder case audience. NO bridge. OMIT.
- Project: AI app builder. Thread: r/BostonSocialClub "Events worth leaving the house for this weekend" → matched on "tried"/"maker". Locals planning weekends. NO bridge. OMIT.
- Project: AI app builder. Thread: r/gamemaker "Using surfaces to create paper-like behavior" → GameMaker is a code IDE, not a no-code generator. Audience writes GML shaders. NO bridge. OMIT.
- Project: study tool. Thread: r/SubredditDrama "the alternative option is still running" → meta drama, no study angle. OMIT.
- Project: study tool. Thread: r/trichotillomania "the trich trance" → medical condition, not studying. OMIT.
- Project: study tool. Thread where you've ALREADY commented under any of our accounts (`already_posted=true` or our usernames in the comment list): obvious astroturfing. OMIT.
- Any thread where you'd be embarrassed to have your comment shown next to a @PROJECT_NAME@ link in the same Reddit thread.

## THREAD CONTENT (pre-fetched)
Each candidate above carries a THREAD CONTENT block (OP + top comments), already fetched for you. Never fetch, navigate, or open any reddit.com URL and never try to reload a thread; apply the SELECTION GATE using the inlined content only. WebSearch and WebFetch ARE available for EXTERNAL fact-checking: use them ONLY when a thread hinges on a current fact, a name, a release, or a claim you are not sure about, so your comment is specific and correct instead of vague. Most comments need no search at all. If a candidate is somehow missing its THREAD CONTENT block, OMIT it.

## CRITICAL CONTENT RULES (apply only to threads that pass the gate)
These platform rules are ABSOLUTE and override anything more permissive in the DRAFT DIRECTIVE below (including any allowance to mention the project).
- Go BIMODAL on length: 1 punchy sentence (<100 chars) OR 4-5 sentences of real substance. Avoid 2-3 sentence middle-ground.
- GROUNDING RULE — pick ONE lane per comment:
  LANE 1 - DISCLOSED STORY: open with a hedge ("hypothetically", "imagine someone running this", "scenario:") then you may invent specifics freely.
  LANE 2 - NO FABRICATION: every specific (numbers, durations, places, tools) must appear verbatim in the content_angle above. Otherwise drop the specific and pattern-frame ("the part that breaks down is...", "the typical failure mode is...").
- VOICE RELATIONSHIP: see the dedicated section below; it governs whether you speak AS the maker or as an outside observer.
- NEVER mention product names (@PRODUCT_NAMES@).
- NEVER include URLs or links in your comment text.
- Prefer replying to OP (top-level reply). ONE comment per thread.
- Statements beat questions. Be authoritative, not inquisitive.

## Content rules
@CONTENT_RULES@

## DRAFT A: assigned style
@STYLES_BLOCK_A@

## DRAFT B: assigned style
@STYLES_BLOCK_B@

## DRAFT DIRECTIVE
@DRAFT_DIRECTIVE@ (applies to both drafts; each still obeys its OWN style's length limit, not a shared one.)

@VOICE_RELATIONSHIP@

## PERSIST EACH DRAFT (keeps this session alive; do it per thread, not at the end)
Right after drafting BOTH texts for a thread, persist Draft A (never Draft B) with ONE quick Bash call, then move to the next thread:
     python3 @REPO_DIR@/scripts/log_draft.py --platform reddit --thread-url 'THREAD_URL' --text 'DRAFT_A_TEXT' --style DRAFT_A_STYLE
Work ONE thread at a time: gate it, draft both texts, persist, next. Failure here is non-fatal, log a warning and continue. Only after EVERY thread is handled do you assemble and return the single result JSON.

## OUTPUT FORMAT
Return ONE JSON object with two arrays (a JSON schema is enforced on this session). Both draft_a_text and draft_b_text are REQUIRED for every posts[] entry — write both, under their respective assigned styles above, applying the CRITICAL CONTENT RULES and Content rules to each independently:

{"posts": [...], "rejects": [...]}

Each posts[] entry is one thread that PASSES the SELECTION GATE:
{"thread_url": "SAME_URL_AS_GIVEN", "reply_to_url": null, "draft_a_text": "your Draft A comment here", "draft_a_style": "@STYLE_A_NAME@", "draft_a_new_style": null, "draft_b_text": "your Draft B comment here", "draft_b_style": "@STYLE_B_NAME@", "draft_b_new_style": null, "thread_author": "username", "thread_title": "thread title", "search_topic": "the seed concept"}

For threads that FAIL the gate, simply leave them out of posts. The shell handles unhandled candidates correctly (Phase 0 salvage on the next cycle re-checks them, and one-strike ripen failure has already pruned dead threads). When nothing passes, return {"posts": [], "rejects": []}.

## OPTIONAL: rejects[] (self-improving denylist)
When you OMIT a thread because of a recurring CLASS of false-positive (the SUB itself surfaces wrong-audience threads, not just this one thread), you MAY add a rejects[] entry for that thread:

{"thread_url": "SAME_URL_AS_GIVEN", "reason": "short reason", "proposed_excludes": ["subreddit:bestofredditorupdates"]}

Rules:
- proposed_excludes entries MUST use the typed form `subreddit:<slug>` (lowercase, no `r/` prefix). Future shape: `keyword:<word>` is accepted but unused today.
- DO emit when: the false-positive is structural — e.g. r/bestofredditorupdates is family drama matching on the word "alternative"; r/hfy is sci-fi narrative matching on the word "spaced"; r/superstonk is GME meme stock matching on "anki" via a random comment. The SUB is the false positive, not just this one post.
- DO NOT emit when: this specific thread is bad but the sub is fine in general (e.g. r/@PROJECT_NAME@'s natural audience like r/medicalschool, r/anki, r/getstudying — never propose excluding a top-performing sub).
- Activation gate: a term needs >=2 SEPARATE batches to propose it before it goes live on future Reddit searches. A single mistaken proposal cannot mute a sub. Propose if a thoughtful future cycle would likely agree; otherwise omit.
- 1-3 entries per reject is plenty. When in doubt, omit the field. Default (no reject line) is safe.

Examples of GOOD proposals:
- Reject r/bestofredditorupdates "Husband lied" → ["subreddit:bestofredditorupdates"]
- Reject r/hfy "The Trial of Humanity" → ["subreddit:hfy"]
- Reject r/battlefield6 "GAME UPDATE 1.3.1.0" → ["subreddit:battlefield6"]
- Reject r/superstonk "GMERICA acquisition" → ["subreddit:superstonk"]
- Reject r/nosleep "cursed doll" → ["subreddit:nosleep"]

Examples of WRONG proposals (do not emit):
- Reject a specific r/nursing thread because OP is venting → DO NOT exclude r/nursing (it's our target audience; just omit this thread)
- Reject one r/anki thread that's off-topic → DO NOT exclude r/anki (core ICP)

Do NOT narrate beyond the persist calls. Gate, draft-or-reject, persist, return the single JSON object."""


def render_reddit_prompt(ing):
    """Render the full Reddit draft prompt. Keys in `ing`:
    project_name, content_angle, candidates_block, n_candidates, batch_id,
    repo_dir, top_report (arm-gated here, pass unconditionally),
    styles_block_a, styles_block_b, style_a_name, style_b_name,
    recent_self_block, arm (optional), lane (optional, promotion assumed).
    """
    arm = ing.get("arm") or os.environ.get("S4L_DRAFT_PROMPT_VARIANT") or ""
    # Reddit drafting is promotion-lane work by definition today (one product
    # project per draft call); the persona directive would reference blocks
    # this prompt does not carry. Force the promotion directive regardless of
    # any stray S4L_ACTIVE_LANE in the environment.
    lane = ing.get("lane") or ""

    from engagement_styles import get_content_rules, get_voice_relationship_rule

    top_ctx = ""
    if not skip_top_report(arm):
        top_report = ing.get("top_report") or ""
        if top_report:
            lines = top_report.split("\n")[:20]
            top_ctx = "\n## Past performance feedback:\n" + "\n".join(lines) + "\n"

    project_name = ing.get("project_name") or "this project"
    return (
        _RD_TEMPLATE
        .replace("@N_CANDIDATES@", str(ing.get("n_candidates") or 0))
        .replace("@DRAFT_B_DIVERGENCE_NOTE@", draft_b_divergence_note(arm))
        .replace("@CONTENT_ANGLE@", ing.get("content_angle") or "")
        .replace("@CANDIDATES_BLOCK@", ing.get("candidates_block") or "")
        .replace("@CORPUS_BLOCK@", corpus_block())
        .replace("@PROJECT_JSON@", projects_json(lane=lane, only_project=ing.get("project_name")))
        .replace("@GLOBAL_LEARNED_PREFS_JSON@", global_learned_prefs_json())
        .replace("@REPO_DIR@", ing.get("repo_dir") or _repo_dir())
        .replace("@BATCH_ID@", ing.get("batch_id") or "")
        .replace("@RECENT_SELF_BLOCK@", ing.get("recent_self_block") or "")
        .replace("@TOP_CTX@", top_ctx)
        .replace("@PRODUCT_NAMES@", ", ".join(product_names()))
        .replace("@CONTENT_RULES@", get_content_rules("reddit"))
        .replace("@STYLES_BLOCK_A@", ing.get("styles_block_a") or "")
        .replace("@STYLES_BLOCK_B@", ing.get("styles_block_b") or "")
        .replace("@DRAFT_DIRECTIVE@", draft_directive(arm, lane))
        .replace("@VOICE_RELATIONSHIP@", get_voice_relationship_rule())
        .replace("@STYLE_A_NAME@", ing.get("style_a_name") or "style_name")
        .replace("@STYLE_B_NAME@", ing.get("style_b_name") or "style_name")
        .replace("@PROJECT_NAME@", project_name)
    )


# --------------------------------------------------------------------------
# Prompt snapshots: one durable full-prompt record per batch, every platform,
# same directory the X cycle already uses. Newest 50 kept per prefix.
# --------------------------------------------------------------------------

def snapshot_prompt(prompt, batch_id, platform):
    """Write the rendered prompt to the shared prep-prompts dir. Returns the
    path or '' (never raises; never blocks a cycle)."""
    try:
        state_dir = os.environ.get("S4L_STATE_DIR") or os.path.expanduser("~/.social-autoposter-mcp")
        d = os.path.join(state_dir, "prep-prompts")
        os.makedirs(d, exist_ok=True)
        prefix = "prep-prompt-" if platform == "twitter" else f"{platform}-draft-prompt-"
        path = os.path.join(d, f"{prefix}{batch_id}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(prompt)
        siblings = sorted(
            (p for p in os.listdir(d) if p.startswith(prefix)),
            key=lambda p: os.path.getmtime(os.path.join(d, p)),
            reverse=True,
        )
        for old in siblings[50:]:
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass
        return path
    except Exception:
        return ""


# --------------------------------------------------------------------------
# CLI (used by the shell cycle).
# --------------------------------------------------------------------------

def _cli():
    args = sys.argv[1:]
    if not args:
        print("usage: draft_prompt_core.py pick-arm | render-twitter --ingredients FILE", file=sys.stderr)
        return 2
    cmd = args[0]
    if cmd == "pick-arm":
        variant, source = pick_draft_prompt_arm()
        print(variant)
        print(f"[draft_prompt_core] arm={variant} source={source}", file=sys.stderr)
        return 0
    if cmd == "render-twitter":
        if len(args) < 3 or args[1] not in ("--ingredients", "--ingredients-dir"):
            print("render-twitter requires --ingredients FILE | --ingredients-dir DIR", file=sys.stderr)
            return 2
        if args[1] == "--ingredients":
            with open(args[2]) as f:
                ing = json.load(f)
        else:
            # Shell-cycle mode: multiline blocks ride as files in DIR (written
            # with printf, immune to ARG_MAX/env-size limits); scalars ride as
            # S4L_PREP_* env vars; arm/lane/prefix come from the cycle env
            # (S4L_DRAFT_PROMPT_VARIANT / S4L_ACTIVE_LANE / TW_ENGINE_PREFIX).
            d = args[2]

            def _blk(name):
                try:
                    with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                        return f.read()
                except OSError:
                    return ""

            ing = {
                "batch_id": os.environ.get("S4L_PREP_BATCH_ID", ""),
                "skill_file": os.environ.get("S4L_PREP_SKILL_FILE", ""),
                "repo_dir": _repo_dir(),
                "picked_style": os.environ.get("S4L_PREP_PICKED_STYLE", ""),
                "picked_mode": os.environ.get("S4L_PREP_PICKED_MODE", "use"),
                "picked_style_b": os.environ.get("S4L_PREP_PICKED_STYLE_B", ""),
                "picked_mode_b": os.environ.get("S4L_PREP_PICKED_MODE_B", "use"),
                "prefix": os.environ.get("TW_ENGINE_PREFIX", ""),
                "candidate_block": _blk("candidate_block"),
                "media_block": _blk("media_block"),
                "top_report": _blk("top_report"),
                "top_report_b": _blk("top_report_b"),
                "styles_block": _blk("styles_block"),
                "styles_block_b": _blk("styles_block_b"),
                "recent_self_block": _blk("recent_self_block"),
            }
        sys.stdout.write(render_twitter_prompt(ing))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
