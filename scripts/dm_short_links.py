#!/usr/bin/env python3
"""Per-DM short link minting + resolution for outbound link tracking.

All outbound URLs in the DM-replies pipeline get wrapped through this tool so
clicks attribute to the originating DM. Booking links, GitHub repos, our own
website pages, third-party references — every URL we send goes through /r/<code>.

Subcommands:

  mint --dm-id N --target-url URL
      Idempotent on (dm_id, target_url). Returns a wrapped URL like
      https://<target_project_website>/r/<code>. Refuses if URL points at a
      project not in dms.target_projects[]; the caller must call
      `dm_conversation.py set-target-project --append --project NAME` first.
      Auto-stamps dms.booking_link_sent_at for kind='booking'.

  resolve --code CODE
      Used by the public /api/short-links/<code> endpoint. Bumps clicks,
      stamps first/last click timestamps, inserts a synthetic [CLICK_SIGNAL]
      row in dm_messages so the engage pipeline picks the thread up. Returns
      target_url + dm_id + project + platform.

  wrap-text --dm-id N --text "..."
      Find every URL in the text, mint each via the same path, substring-replace
      the original URLs with the wrapped versions. Prints the wrapped text on
      stdout. Used by reddit_browser.py / twitter_browser.py (via direct import
      of `wrap_text()`) and by the LinkedIn shell flow (subprocess).

The classifier maps a URL to (kind, matched_project_name) using config.json:
  - booking : URL starts with project.booking_link
  - github  : URL starts with project.github or matches project.landing_pages.github_repo
  - website : URL host == project.website host
  - other   : no project match (no project guard, kind='other')

Wrapped hostname is always the DM's primary `target_project.website` (consistent
per thread regardless of which project a given link points at).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import uuid
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, 'scripts'))

import db as dbmod  # noqa: E402

CONFIG_PATH = os.path.join(REPO_DIR, 'config.json')
CODE_ALPHABET = 'abcdefghijkmnpqrstuvwxyz23456789'
CODE_LEN = 8

# Default wrapper host used when a project's own /r/<code> redirector is NOT
# live (config.json short_links_live=false) and the operator hasn't set an
# explicit short_links_host. s4l.ai's resolver lives at
# @m13v/seo-components -> app.s4l.ai/api/short-links/<code> and is the
# social-autoposter-owned fallback. Routing through it keeps first-party click
# logging in post_link_clicks instead of dropping to UTM-only.
DEFAULT_FALLBACK_HOST = 'https://s4l.ai'

# Match http(s) URLs AND bare-domain references with a path. The bare-domain
# branch requires at least one path character so we don't match prose like
# "i.e." or "S.F." or version numbers. Greedy on the path; trailing punctuation
# is stripped by the caller. Both branches are normalized through
# _ensure_scheme() before classification.
#
# Third branch (added 2026-05-10): bare project hostnames with NO path. Built
# dynamically from config.json project websites + booking_link + github hosts.
# A 7d audit found 47/2094 Reddit DMs and 7/319 X DMs mention a project URL,
# but ZERO short links got minted because the model casually drops domains
# like "fazm.ai is the link" or "main one is fazm, ai agent for macos,
# github.com/m13v/fazm" without https:// or trailing path. Branches 1 and 2
# both miss those, so we never wrap them. The new branch matches a known
# project host as a bare token, with a negative lookahead so it doesn't
# overlap with branch 2 ('fazm.ai/path' still goes through branch 2).
def _build_project_bare_host_pattern():
    """Build an alternation of known project hostnames, longest-first."""
    try:
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
        projs = cfg.get('projects') or []
    except Exception:
        return None
    hosts = set()
    for p in projs:
        for field in ('website', 'booking_link', 'github'):
            v = (p.get(field) or '').strip()
            if not v:
                continue
            try:
                netloc = urlsplit(v if '://' in v else 'https://' + v).netloc
            except Exception:
                continue
            host = (netloc or '').lower().split(':', 1)[0]
            # Strip a literal 'www.' prefix only (lstrip would chew chars).
            if host.startswith('www.'):
                host = host[4:]
            if host and '.' in host:
                hosts.add(host)
    if not hosts:
        return None
    parts = sorted({re.escape(h) for h in hosts}, key=len, reverse=True)
    # \b on left, narrow lookahead on right. Reject:
    #   - word chars/slashes (mid-token or path → branch 2 territory)
    #   - dot+letter (sub-domain extension: 'runner.now.example.com' must NOT
    #     match 'runner.now')
    # ALLOW dot+non-letter (sentence-ending: 'try fazm.ai.' must match) and
    # plain punctuation/whitespace. Pre-2026-05-14 this was `(?![\w./])` which
    # over-rejected sentence-ending periods, so 'try fazm.ai.' yielded ZERO
    # matches and the URL went out bare.
    return r'\b(?:' + '|'.join(parts) + r')\b(?![\w/]|\.[a-z])'

_PROJECT_BARE_HOST_PAT = _build_project_bare_host_pattern()
_URL_RE = re.compile(
    (
        r'https?://[^\s<>"\']+'
        r'|'
        r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}/[^\s<>"\']*'
        + (r'|' + _PROJECT_BARE_HOST_PAT if _PROJECT_BARE_HOST_PAT else '')
    ),
    re.IGNORECASE,
)
_TRAILING_PUNCT = '.,;:!?)]}>\'"'


def _ensure_scheme(url: str) -> str:
    """Prepend https:// to bare-domain URLs so urlsplit and downstream consumers
    have a fully qualified URL. https? matches first branch of _URL_RE; the
    bare-domain branch (everything after the alternation) lacks a scheme."""
    if url.startswith(('http://', 'https://')):
        return url
    return 'https://' + url


def _load_projects():
    with open(CONFIG_PATH, 'r') as f:
        return [p for p in json.load(f).get('projects', []) if p.get('name')]


def _gen_code(n=CODE_LEN):
    return ''.join(secrets.choice(CODE_ALPHABET) for _ in range(n))


def _norm_host(url: str) -> str:
    try:
        return (urlsplit(url).netloc or '').lower().lstrip('www.')
    except Exception:
        return ''


def _classify_url(url: str, projects: list) -> tuple[str, str | None]:
    """Return (kind, project_name|None). Longest-prefix-wins across projects.

    Priority: booking > github > website > other. Ties within a kind go to the
    longest matching prefix so e.g. cal.com/team/mediar/fazm beats a hypothetical
    cal.com/team/mediar/ root. Bare-domain inputs are normalized to https:// first.
    """
    u = _ensure_scheme(url.strip())
    best_booking = ('', None)
    best_github = ('', None)
    best_website = ('', None)

    for p in projects:
        name = p.get('name')
        if not name:
            continue

        booking = (p.get('booking_link') or '').strip()
        if booking and u.startswith(booking.rstrip('?').rstrip('/')):
            if len(booking) > len(best_booking[0]):
                best_booking = (booking, name)

        gh = (p.get('github') or '').strip()
        if gh and u.startswith(gh.rstrip('/')):
            if len(gh) > len(best_github[0]):
                best_github = (gh, name)

        gh_repo = (p.get('landing_pages', {}) or {}).get('github_repo')
        if gh_repo:
            gh_url = f'https://github.com/{gh_repo.strip("/")}'
            if u.startswith(gh_url):
                if len(gh_url) > len(best_github[0]):
                    best_github = (gh_url, name)

        website = (p.get('website') or '').strip()
        if website:
            site_host = _norm_host(website)
            url_host = _norm_host(u)
            if site_host and url_host and (url_host == site_host or url_host.endswith('.' + site_host)):
                if len(site_host) > len(best_website[0]):
                    best_website = (site_host, name)

    if best_booking[1]:
        return ('booking', best_booking[1])
    if best_github[1]:
        return ('github', best_github[1])
    if best_website[1]:
        return ('website', best_website[1])
    return ('other', None)


def _build_target_url(target_url: str, kind: str, *, dm_id: int, project: str | None, platform: str) -> str:
    """Add UTM params for kinds where we control the analytics consumer.

    Canonical UTM scheme (matches _build_target_url_for_post + the pool
    minters): utm_source='s4l' identifies the agency for every customer's
    analytics ('this traffic came from S4L'). utm_term carries the platform
    (reddit | twitter | linkedin | github_issues) since utm_source is no
    longer platform-specific. utm_medium stays 'dm' to keep the DM rail
    distinct from posts. utm_content keeps the strict 'dm_<id>' shape
    consumed by bin/server.js (regex /^dm_(\\d+)$/) and project_stats_json
    (LIKE 'dm_%').

    Booking: Cal.com metadata[utm_*] survives to the booking webhook (the flat
    utm_* gets stripped by Cal's UI), Calendly accepts both — keep both.
    Website: our own domains run PostHog; flat utm_* is enough.
    Github / other: leave the URL untouched (no downstream UTM consumer).
    """
    if kind not in ('booking', 'website'):
        return target_url

    parts = urlsplit(target_url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))

    utm = {
        'utm_source': 's4l',
        'utm_medium': 'dm',
        'utm_campaign': (project or 'unknown').lower(),
        'utm_term': (platform or 'unknown').lower(),
        'utm_content': f'dm_{dm_id}',
    }
    for k, v in utm.items():
        existing.setdefault(k, v)
        if kind == 'booking':
            existing[f'metadata[{k}]'] = v

    new_query = urlencode(existing, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _build_target_url_for_post(target_url: str, kind: str, *, minted_session: str,
                                project: str | None, platform: str) -> str:
    """UTM stamping for PUBLIC post wrappers (utm_medium='post').

    See _build_target_url for the canonical UTM scheme rationale. utm_content
    keeps the 'post_<session>' shape so backfill_real_clicks.py can
    PostHog-join on it.
    """
    if kind not in ('booking', 'website'):
        return target_url

    parts = urlsplit(target_url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))

    utm = {
        'utm_source': 's4l',
        'utm_medium': 'post',
        'utm_campaign': (project or 'unknown').lower(),
        'utm_term': (platform or 'unknown').lower(),
        'utm_content': f'post_{minted_session}',
    }
    for k, v in utm.items():
        existing.setdefault(k, v)
        if kind == 'booking':
            existing[f'metadata[{k}]'] = v

    new_query = urlencode(existing, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _project_website(projects: list, name: str) -> str | None:
    for p in projects:
        if p.get('name') == name:
            site = (p.get('website') or '').strip().rstrip('/')
            return site or None
    return None


def _project_short_links_live(projects: list, name: str) -> bool:
    """True iff the project's OWN domain serves /r/<code>.

    Default true (preserves behavior for fazm, mediar, assrt, cyrano-systems
    and every other existing project where the customer's domain hosts the
    @m13v/seo-components /r/[code] handler).

    Set false in config.json for projects where the customer owns the domain
    but hasn't shipped the resolver (or the static CSV) yet. In that case the
    wrapper auto-routes through DEFAULT_FALLBACK_HOST (s4l.ai), so mints still
    produce a live /r/<code> with first-party click logging; we no longer drop
    to UTM-only. See _project_short_links_host for the host-resolution order.

    An explicit `short_links_host` in config.json (regardless of this flag)
    always wins and is used verbatim.
    """
    for p in projects:
        if p.get('name') == name:
            v = p.get('short_links_live')
            return True if v is None else bool(v)
    return True


def _project_short_links_host(projects: list, name: str) -> str | None:
    """Resolve the wrapper host where /r/<code> is served for this project.

    Resolution order (first match wins):
      1. Explicit `short_links_host` in config.json (e.g. "https://s4l.ai").
         Used to pin a project to a specific resolver-bearing host we operate.
      2. DEFAULT_FALLBACK_HOST (= https://s4l.ai) when `short_links_live` is
         explicitly false. Auto-applied so any project flagged as "customer
         hasn't deployed the resolver yet" still gets a live /r/<code> through
         the social-autoposter-owned resolver, instead of dropping to UTM-only.
      3. None → caller falls back to project.website (the legacy/default path,
         used when short_links_live is unset/true, meaning the customer's own
         domain has the @m13v/seo-components /r/[code] handler shipped).

    Callers should always do: `_project_short_links_host(p, name) or website`.

    The underlying target_url (where the resolver 302s) is unchanged in either
    case — it still points at the customer's site with full UTMs baked in at
    mint time. Only the wrapper host changes.
    """
    for p in projects:
        if p.get('name') == name:
            host = (p.get('short_links_host') or '').strip().rstrip('/')
            if host:
                return host
            if p.get('short_links_live') is False:
                return DEFAULT_FALLBACK_HOST
            return None
    return None


def utm_only_text(*, text: str, platform: str, project_name: str) -> str:
    """Walk every URL in text, replace with its UTM-tagged version (no minting,
    no DB). Safety-net helper for caller exception branches so a bare URL
    never escapes when wrap_text_for_post itself raises.
    """
    if not text:
        return text
    platform = (platform or '').lower()
    if platform == 'x':
        platform = 'twitter'
    minted_session = str(uuid.uuid4())
    projects = _load_projects()
    seen: dict[str, str] = {}
    for m in list(_URL_RE.finditer(text)):
        raw = m.group(0)
        stripped = raw.rstrip(_TRAILING_PUNCT)
        if stripped in seen:
            continue
        if re.search(r'/r/[a-z0-9]{4,32}(?:[/?#]|$)', stripped, re.IGNORECASE):
            seen[stripped] = stripped
            continue
        target = _ensure_scheme(stripped)
        kind, matched_project = _classify_url(target, projects)
        utm_url = _build_target_url_for_post(
            target, kind, minted_session=minted_session,
            project=matched_project or project_name, platform=platform,
        )
        seen[stripped] = utm_url

    def _sub(m):
        raw = m.group(0)
        stripped = raw.rstrip(_TRAILING_PUNCT)
        trailing = raw[len(stripped):]
        return seen.get(stripped, stripped) + trailing

    return _URL_RE.sub(_sub, text)


def _dm_row(dm_id: int):
    """Fetch the DM header over HTTP (GET /api/v1/dms/<id>).

    HTTP-only: there is no direct-Postgres path. Raises SystemExit on a miss,
    matching the prior DB behaviour.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from http_api import api_get
    resp = api_get(f"/api/v1/dms/{dm_id}", ok_on_404=True)
    if not resp or not resp.get('ok'):
        raise SystemExit(f"DM #{dm_id} not found")
    dm = (resp.get('data') or {}).get('dm') or {}
    if not dm:
        raise SystemExit(f"DM #{dm_id} not found")
    return dm


def _mint_one(*, dm_id: int, target_url: str, projects: list, projects_by_name: dict,
              dm: dict) -> dict:
    """Core mint logic, shared by `mint` CLI and `wrap_text` library call.

    HTTP-only: URL classification + UTM/booking target building happen here,
    then the insert-or-reuse runs server-side via POST /api/v1/dm-links/mint.
    There is no direct-Postgres path.

    Returns one of:
      {ok: True, code, short_url, target_url, kind, project, reused: bool}
      {ok: False, error: "target_project_required", needed_project, url}
      {ok: False, error: "no_primary_website", dm_id}
    """
    target_url = _ensure_scheme((target_url or '').strip())
    if not target_url or target_url == 'https://':
        return {'ok': False, 'error': 'empty_url'}

    platform = (dm.get('platform') or 'reddit').lower()
    if platform == 'x':
        platform = 'twitter'

    kind, matched_project = _classify_url(target_url, projects)

    # Target-project guard: if the URL maps to one of our projects, that project
    # must already be in the DM's target_projects[]. The caller is expected to
    # call set-target-project --append before retry. kind='other' bypasses.
    target_projects = dm.get('target_projects') or []
    if matched_project and matched_project not in target_projects:
        return {
            'ok': False,
            'error': 'target_project_required',
            'needed_project': matched_project,
            'url': target_url,
            'kind': kind,
        }

    # Wrapped hostname: use the DM's primary target_project website. Falls back
    # to the matched_project's website if target_project is unset (rare, only on
    # very fresh rows where set-project hasn't fired yet).
    # If the project has `short_links_host` set in config.json, that overrides
    # the wrapper hostname (used to route through a host WE operate, e.g.
    # s4l.ai, when the customer's domain has no /r/<code> resolver).
    primary = dm.get('target_project') or (matched_project if matched_project else None)
    website = _project_website(projects, primary) if primary else None
    if not website:
        return {
            'ok': False,
            'error': 'no_primary_website',
            'dm_id': dm_id,
            'detail': f"no website for project={primary!r}; set target_project first",
        }
    wrapper_host = (_project_short_links_host(projects, primary) if primary else None) or website

    final_target = _build_target_url(
        target_url,
        kind,
        dm_id=dm_id,
        project=matched_project,
        platform=platform,
    )

    # Insert-or-reuse server-side. The endpoint matches first on the FINAL
    # target_url (post-UTM, what the unique index (dm_id, target_url) is on),
    # then on the bare URL (covers rows minted before a given kind started
    # UTM-stamping). It also stamps dms.booking_link_sent_at for kind='booking'.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from http_api import api_post
    stamp_booking = bool(kind == 'booking' and not dm.get('booking_link_sent_at'))
    for _ in range(8):
        code = _gen_code()
        try:
            resp = api_post(
                "/api/v1/dm-links/mint",
                {
                    "dm_id": dm_id,
                    "code": code,
                    "target_url": final_target,
                    "bare_url": target_url if final_target != target_url else None,
                    "kind": kind,
                    "project_at_mint": matched_project,
                    "stamp_booking": stamp_booking,
                },
                ok_on_conflict=True,
            )
        except Exception as e:
            return {'ok': False, 'error': 'mint_api_unreachable', 'detail': str(e)}
        if resp and resp.get('ok'):
            data = resp.get('data') or {}
            ret_code = data.get('code') or code
            return {
                'ok': True,
                'code': ret_code,
                'short_url': f"{wrapper_host}/r/{ret_code}",
                'target_url': final_target,
                'kind': data.get('kind') or kind,
                'project': matched_project,
                'reused': bool(data.get('reused')),
            }
        e = (resp or {}).get('error') or {}
        e_code = e.get('code') if isinstance(e, dict) else None
        if e_code == 'code_collision':
            continue  # try another random code
        return {'ok': False, 'error': e_code or 'mint_api_error'}
    return {'ok': False, 'error': 'code_collision_after_8_tries'}


# ---- Library entry point used by reddit_browser.py / twitter_browser.py ----

def wrap_text(*, dm_id: int, text: str) -> dict:
    """Find every URL in `text`, mint each, substring-replace.

    Returns:
      {ok: True, text: "<wrapped>", minted_codes: [...], skipped: [...]}
      {ok: False, error: "...", url: "...", needed_project: "..." }

    On a target_project_required error, the caller should set-target-project
    --append the needed_project and retry. We DO NOT silently fall through —
    refusing here is the whole point of the multi-project guard.
    """
    if not text:
        return {'ok': True, 'text': text, 'minted_codes': [], 'skipped': []}

    projects = _load_projects()
    projects_by_name = {p['name']: p for p in projects}
    dm = _dm_row(dm_id)
    seen = {}  # original_url -> wrapped_url (dedup so identical URLs map once)
    minted_codes = []
    skipped = []

    # Iterate matches in order, replace each. Trailing punctuation common in
    # prose ("...github.com/foo.") is stripped from the URL before classify.
    for m in list(_URL_RE.finditer(text)):
        raw = m.group(0)
        stripped = raw.rstrip(_TRAILING_PUNCT)
        trailing = raw[len(stripped):]
        if stripped in seen:
            continue

        # If the URL is already a wrapped /r/<code> on one of our domains,
        # leave it alone. Recognized by path shape /r/<8 chars from alphabet>.
        if re.search(r'/r/[a-z0-9]{4,32}(?:[/?#]|$)', stripped, re.IGNORECASE):
            seen[stripped] = stripped
            skipped.append({'url': stripped, 'reason': 'already_wrapped'})
            continue

        res = _mint_one(
            dm_id=dm_id,
            target_url=stripped,
            projects=projects,
            projects_by_name=projects_by_name,
            dm=dm,
        )
        if not res.get('ok'):
            return {**res, 'ok': False}
        seen[stripped] = res['short_url']
        if not res.get('reused'):
            minted_codes.append(res['code'])
        elif res.get('code'):
            # Reused codes still surfaced so callers can backfill message_id.
            minted_codes.append(res['code'])

    if not seen:
        return {'ok': True, 'text': text, 'minted_codes': [], 'skipped': skipped}

    # Re-walk the text and substitute. Use the regex again to preserve
    # trailing punctuation outside the URL (we stripped it before classify).
    def _sub(m):
        raw = m.group(0)
        stripped = raw.rstrip(_TRAILING_PUNCT)
        trailing = raw[len(stripped):]
        wrapped = seen.get(stripped, stripped)
        return wrapped + trailing

    new_text = _URL_RE.sub(_sub, text)
    return {
        'ok': True,
        'text': new_text,
        'minted_codes': minted_codes,
        'skipped': skipped,
    }


# ---- Post-link library (parallel rail to DM, table=post_links) ----------

def _mint_one_post(*, target_url: str, projects: list, platform: str,
                    project_name: str, minted_session: str) -> dict:
    """Core mint logic for public posts. Mirrors _mint_one but writes to
    post_links instead of dm_links, with post_id and reply_id BOTH NULL at
    mint time (the caller backfills exactly one of them after log_post or
    reply_db returns the row id).

    Returns:
      {ok: True, code, short_url, target_url, kind}
      {ok: False, error: 'no_primary_website' | 'empty_url' | 'code_collision_after_8_tries'}

    External-short-links path: if the project's config.json entry has
    external_short_links=true, we don't mint a fresh code, we CLAIM one from
    the pre-minted pool (post_links rows where minted_session starts with
    'pool:' and post_id IS NULL and reply_id IS NULL). The pool exists so we
    can hand the client a STATIC CSV they host on their own domain redirector;
    if we minted fresh codes for these projects the CSV would go stale every
    cycle. The pool's target_url is fixed at pool-mint time (homepage with
    platform UTMs + code in utm_content), so the LLM's URL in the comment text
    is ignored for routing -- visitors always land on the destination we baked
    in. Pool depth managed by scripts/mint_external_pool.py.

    HTTP-only: all DB ops run server-side via /api/v1/post-links/* (mint +
    claim-pool). There is no direct-Postgres path and no fallback.
    """
    target_url = _ensure_scheme((target_url or '').strip())
    if not target_url or target_url == 'https://':
        return {'ok': False, 'error': 'empty_url'}

    kind, matched_project = _classify_url(target_url, projects)

    # Wrapper hostname comes from the project we're posting AS, not from any
    # URL classification. Posts always know which project they are for.
    # If the project has `short_links_host` set in config.json (e.g. for clients
    # whose own domain doesn't have a /r/<code> resolver), that overrides the
    # wrapper hostname and routes through a host we operate (s4l.ai). The
    # underlying target_url is unchanged; only the wrapper changes.
    website = _project_website(projects, project_name)
    if not website:
        return {
            'ok': False,
            'error': 'no_primary_website',
            'project': project_name,
            'detail': f"no website for project={project_name!r} in config.json",
        }
    host_override = _project_short_links_host(projects, project_name)
    wrapper_host = host_override or website

    platform_norm = (platform or '').lower()
    if platform_norm == 'x':
        platform_norm = 'twitter'

    project_cfg = next((p for p in projects if p.get('name') == project_name), None)

    # UTM URL is the universal fallback — used when short_links_live=false on
    # the project, OR when pool/mint can't produce a /r/<code> for any reason.
    # No DB row is created in fallback mode; PostHog still attributes via
    # utm_source/utm_campaign/utm_content=post_<minted_session>. The trade-off
    # is losing the post_links → posts join until the operator flips
    # short_links_live=true and the customer's redirector is live.
    fallback_target = _build_target_url_for_post(
        target_url,
        kind,
        minted_session=minted_session,
        project=matched_project or project_name,
        platform=platform,
    )

    def _utm_fallback(reason: str) -> dict:
        return {
            'ok': True,
            'code': None,
            'short_url': fallback_target,
            'target_url': fallback_target,
            'kind': kind,
            'utm_only': True,
            'fallback_reason': reason,
        }

    # Historically there was a UTM-fallback gate here for short_links_live=false
    # projects, but _project_short_links_host now auto-returns DEFAULT_FALLBACK_HOST
    # (s4l.ai) in that case, so we always have a live wrapper host and can mint.
    # The remaining _utm_fallback paths below are runtime failures of the mint
    # API / pool itself, where UTM is the genuine last resort.

    # Opt-in policy override: a project may set `force_utm_only: true` in
    # config.json to deliberately post UTM-tagged bare URLs instead of minting
    # a /r/<code> short link. This re-opens (per-project, explicitly) the path
    # that was globally closed on 2026-05-22. Trade-off: no /r/<code> means no
    # post_links row and no first-party post_link_clicks join; attribution still
    # works via the baked-in UTM scheme (utm_source/campaign/term/content) that
    # _build_target_url_for_post already applied to `fallback_target`.
    if project_cfg and project_cfg.get('force_utm_only'):
        return _utm_fallback('policy')

    if project_cfg and project_cfg.get('external_short_links'):
        # Pool path. Atomically claim the oldest unclaimed pool row server-side.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_post
        try:
            resp = api_post(
                "/api/v1/post-links/claim-pool",
                {
                    "project_name": project_name,
                    "platform": platform_norm,
                    "minted_session": minted_session,
                },
                ok_on_conflict=True,
            )
        except Exception:
            return _utm_fallback('api_unreachable')
        if not resp or not resp.get('ok'):
            err = (resp or {}).get('error') or {}
            err_code = err.get('code') if isinstance(err, dict) else None
            return _utm_fallback(err_code or 'pool_exhausted')
        data = resp.get('data') or {}
        pool_code = data.get('code')
        pool_target = data.get('target_url')
        return {
            'ok': True,
            'code': pool_code,
            'short_url': f"{wrapper_host}/r/{pool_code}",
            'target_url': pool_target,
            'kind': 'website',
            'from_pool': True,
        }

    # Fresh mint: try up to 8 random codes before giving up on collision.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from http_api import api_post
    for _ in range(8):
        code = _gen_code()
        try:
            resp = api_post(
                "/api/v1/post-links/mint",
                {
                    "code": code,
                    "platform": platform,
                    "project_name": project_name,
                    "target_url": fallback_target,
                    "kind": kind,
                    "project_at_mint": matched_project,
                    "minted_session": minted_session,
                },
                ok_on_conflict=True,
            )
        except Exception:
            return _utm_fallback('api_unreachable')
        if resp and resp.get('ok'):
            return {
                'ok': True,
                'code': code,
                'short_url': f"{wrapper_host}/r/{code}",
                'target_url': fallback_target,
                'kind': kind,
            }
        err = (resp or {}).get('error') or {}
        err_code = err.get('code') if isinstance(err, dict) else None
        if err_code == 'code_collision':
            continue  # try another random code
        return _utm_fallback(err_code or 'mint_api_error')
    return _utm_fallback('code_collision_after_8_tries')


def wrap_text_for_post(*, text: str, platform: str, project_name: str) -> dict:
    """Find every URL in `text`, mint into post_links, substring-replace.

    Returns:
      {ok: True, text: <wrapped>, minted_session, codes: [...], skipped: [...]}
      {ok: False, error: ..., url: ...}

    minted_session is a UUID the caller MUST pass to backfill_post_id /
    backfill_reply_id once the platform call returns the row id from
    log_post.py or reply_db.py. If the platform call fails, the codes are
    orphaned (post_id and reply_id stay NULL); they still resolve correctly
    via target_url frozen at mint time, just without attribution.

    Normalize platform: 'x' is collapsed to 'twitter' so analytics joins
    against posts.platform line up.
    """
    if not text:
        return {'ok': True, 'text': text, 'minted_session': None,
                'codes': [], 'skipped': []}

    platform = (platform or '').lower()
    if platform == 'x':
        platform = 'twitter'

    minted_session = str(uuid.uuid4())
    projects = _load_projects()
    seen = {}
    codes = []
    skipped = []

    for m in list(_URL_RE.finditer(text)):
        raw = m.group(0)
        stripped = raw.rstrip(_TRAILING_PUNCT)
        if stripped in seen:
            continue

        # Already-wrapped /r/<code> on one of our domains: leave alone.
        if re.search(r'/r/[a-z0-9]{4,32}(?:[/?#]|$)', stripped, re.IGNORECASE):
            seen[stripped] = stripped
            skipped.append({'url': stripped, 'reason': 'already_wrapped'})
            continue

        res = _mint_one_post(
            target_url=stripped,
            projects=projects,
            platform=platform,
            project_name=project_name,
            minted_session=minted_session,
        )
        if not res.get('ok'):
            return {**res, 'ok': False}
        seen[stripped] = res['short_url']
        if res.get('code') is not None:
            codes.append(res['code'])
        else:
            # UTM-only fallback (no /r/<code>): track in skipped[] so the
            # caller's logging doesn't see [None] in codes[] but still has
            # visibility into how the URL was handled.
            skipped.append({'url': stripped, 'reason': 'utm_fallback',
                            'detail': res.get('fallback_reason')})

    if not seen:
        return {'ok': True, 'text': text, 'minted_session': None,
                'codes': [], 'skipped': skipped}

    def _sub(m):
        raw = m.group(0)
        stripped = raw.rstrip(_TRAILING_PUNCT)
        trailing = raw[len(stripped):]
        wrapped = seen.get(stripped, stripped)
        return wrapped + trailing

    new_text = _URL_RE.sub(_sub, text)
    return {
        'ok': True,
        'text': new_text,
        'minted_session': minted_session,
        'codes': codes,
        'skipped': skipped,
    }


def _backfill_via_api(*, minted_session: str, post_id: int | None = None,
                      reply_id: int | None = None) -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from http_api import api_post
    body: dict = {"minted_session": minted_session}
    if post_id is not None:
        body["post_id"] = int(post_id)
    if reply_id is not None:
        body["reply_id"] = int(reply_id)
    try:
        resp = api_post("/api/v1/post-links/backfill", body)
    except Exception:
        return 0
    if not resp or not resp.get('ok'):
        return 0
    return int((resp.get('data') or {}).get('updated') or 0)


def backfill_post_id(*, minted_session: str, post_id: int) -> int:
    """Stamp post_links.post_id for every code minted under minted_session.

    Returns the rowcount affected. Safe to call multiple times (idempotent).
    Caller should NOT raise on rowcount==0 because some posts have no URLs
    and minted_session was None — the caller should skip the backfill in
    that case.

    HTTP-only: routes through /api/v1/post-links/backfill. There is no
    direct-Postgres path and no fallback.
    """
    if not minted_session or post_id is None:
        return 0
    return _backfill_via_api(minted_session=minted_session, post_id=post_id)


def backfill_reply_id(*, minted_session: str, reply_id: int) -> int:
    """Same as backfill_post_id but stamps post_links.reply_id (engage_reddit
    writes to the `replies` table, not `posts`). HTTP-only."""
    if not minted_session or reply_id is None:
        return 0
    return _backfill_via_api(minted_session=minted_session, reply_id=reply_id)


# ---- CLI subcommands ----

def cmd_mint(args):
    projects = _load_projects()
    projects_by_name = {p['name']: p for p in projects}
    dm = _dm_row(args.dm_id)
    res = _mint_one(
        dm_id=args.dm_id,
        target_url=args.target_url,
        projects=projects,
        projects_by_name=projects_by_name,
        dm=dm,
    )
    if not res.get('ok'):
        sys.stderr.write(json.dumps(res) + '\n')
        sys.exit(2)
    if args.json:
        print(json.dumps(res))
    else:
        print(res['short_url'])


# Bot User-Agent regex. Matches Twitter card prefetch, LinkedIn unfurl,
# Slack/Discord/Telegram/WhatsApp link previews, generic Google/Bing crawlers,
# and Pinterest/Embedly/Snapchat. We discovered 97 percent of /r/<code> hits
# fired within 30 seconds of mint, average 17s, which is the link-preview
# fingerprint. Real human ratio cross-referenced against PostHog pageviews
# was 5-8 percent. When a UA matches:
#   1. Skip the legacy `clicks` counter increment (so post-2026-05-07 the
#      legacy column is humans-only).
#   2. Skip the [CLICK_SIGNAL] insert into dm_messages so the engage pipeline
#      isn't woken up by a Slackbot.
#   3. Still log a row in dm_link_clicks with is_bot=true so historical
#      splits stay accurate.
#   4. Still return target_url so previews render.
import hashlib
import re
BOT_UA_RE = re.compile(
    r'bot|crawler|spider|Twitterbot|LinkedInBot|Slackbot|facebookexternalhit'
    r'|Discordbot|TelegramBot|WhatsApp|Applebot|Googlebot|Bingbot|YandexBot'
    r'|DuckDuckBot|redditbot|Pinterest|Embedly|Snapchat',
    re.IGNORECASE,
)


def cmd_resolve(args):
    # HTTP-only: bot detection + IP hashing happen here; the click logging and
    # join read run server-side via POST /api/v1/dm-links/resolve. There is no
    # direct-Postgres path.
    ua = (getattr(args, 'user_agent', '') or '').strip()
    referrer = (getattr(args, 'referrer', '') or '').strip() or None
    is_bot = bool(ua and BOT_UA_RE.search(ua))
    ip_raw = (getattr(args, 'ip', '') or '').strip()
    ip_hash = (
        hashlib.sha256(ip_raw.encode('utf-8')).hexdigest()[:16]
        if ip_raw else None
    )

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from http_api import api_post
    resp = api_post(
        "/api/v1/dm-links/resolve",
        {
            "code": args.code,
            "no_count": bool(args.no_count),
            "is_bot": is_bot,
            "ip_hash": ip_hash,
            "user_agent": ua or None,
            "referrer": referrer,
        },
        ok_on_404=True,
    )
    if not resp or not resp.get('ok'):
        print(json.dumps({'error': 'not_found', 'code': args.code}))
        return
    data = resp.get('data') or {}
    print(json.dumps({
        'dm_id': data.get('dm_id'),
        'platform': data.get('platform'),
        'project': data.get('project'),
        'kind': data.get('kind'),
        'target_url': data.get('target_url'),
        'is_bot': data.get('is_bot', is_bot),
    }))


def cmd_wrap_text(args):
    res = wrap_text(dm_id=args.dm_id, text=args.text)
    if not res.get('ok'):
        sys.stderr.write(json.dumps(res) + '\n')
        sys.exit(2)
    if args.json:
        print(json.dumps(res))
    else:
        # Stdout is the wrapped text only — ready to pipe into a `send` command
        # or a shell variable. Diagnostics go to stderr.
        if res.get('minted_codes') or res.get('skipped'):
            sys.stderr.write(json.dumps({
                'minted_codes': res['minted_codes'],
                'skipped': res['skipped'],
            }) + '\n')
        sys.stdout.write(res['text'])


def cmd_wrap_post_text(args):
    res = wrap_text_for_post(text=args.text, platform=args.platform,
                              project_name=args.project)
    if not res.get('ok'):
        sys.stderr.write(json.dumps(res) + '\n')
        sys.exit(2)
    # JSON envelope is the default for the post path because callers always
    # need minted_session for the backfill UPDATE. The shell scripts that
    # consume this WILL parse JSON.
    print(json.dumps(res))


def cmd_utm_text(args):
    """UTM-only wrap (no DB, no minting). Prints the wrapped text on stdout.
    Used by the Twitter engagement prompt where Claude types the reply through
    the browser MCP (twitter-harness bh_run type_text) and there is no Python
    posting layer to invoke wrap_text_for_post. The typed URL itself carries all attribution
    via utm_source=s4l + utm_term=<platform>; PostHog captures it on landing.
    """
    out = utm_only_text(text=args.text, platform=args.platform,
                        project_name=args.project)
    sys.stdout.write(out)


def cmd_backfill_post(args):
    n = backfill_post_id(minted_session=args.minted_session, post_id=args.post_id)
    print(json.dumps({'backfilled': n, 'post_id': args.post_id,
                      'minted_session': args.minted_session}))


def cmd_backfill_reply(args):
    n = backfill_reply_id(minted_session=args.minted_session, reply_id=args.reply_id)
    print(json.dumps({'backfilled': n, 'reply_id': args.reply_id,
                      'minted_session': args.minted_session}))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_mint = sub.add_parser('mint', help='Mint (or reuse) a wrapped /r/<code> short link for one URL')
    p_mint.add_argument('--dm-id', type=int, required=True)
    p_mint.add_argument('--target-url', required=True)
    p_mint.add_argument('--json', action='store_true', help='Print full JSON envelope')

    p_res = sub.add_parser('resolve', help='Look up code, increment clicks, return target URL')
    p_res.add_argument('--code', required=True)
    p_res.add_argument('--no-count', action='store_true', help='Skip click counter update (debugging)')
    # Bot detection inputs. When --user-agent matches the bot regex (Twitterbot,
    # LinkedInBot, Slackbot, facebookexternalhit, etc.), the legacy clicks
    # counter is NOT bumped, [CLICK_SIGNAL] is NOT inserted, but a row IS
    # appended to dm_link_clicks with is_bot=true so historical splits work.
    p_res.add_argument('--user-agent', default='', help='Caller User-Agent for bot detection')
    p_res.add_argument('--referrer', default='', help='Caller Referer header for analytics')
    p_res.add_argument('--ip', default='', help='Caller IP (sha256 hashed before storage)')

    p_wrap = sub.add_parser('wrap-text', help='Wrap every URL in TEXT through the mint pipeline')
    p_wrap.add_argument('--dm-id', type=int, required=True)
    p_wrap.add_argument('--text', required=True)
    p_wrap.add_argument('--json', action='store_true', help='Print full JSON envelope to stdout')

    p_wrap_post = sub.add_parser('wrap-post-text',
                                  help='Wrap URLs in a public post/comment text. '
                                       'Mints into post_links with NULL post_id; '
                                       'backfill via backfill-post or backfill-reply.')
    p_wrap_post.add_argument('--text', required=True)
    p_wrap_post.add_argument('--platform', required=True,
                             choices=['reddit', 'twitter', 'x', 'linkedin', 'github_issues', 'github', 'moltbook'])
    p_wrap_post.add_argument('--project', required=True,
                             help='project_name from config.json (drives wrapper hostname)')

    p_utm = sub.add_parser('utm-text',
                            help='UTM-only wrap (no DB write). Replaces every URL '
                                 'in --text with its UTM-tagged version and prints '
                                 'the result on stdout. Use when no Python posting '
                                 'layer is available (Claude-driven MCP typing).')
    p_utm.add_argument('--text', required=True)
    p_utm.add_argument('--platform', required=True,
                       choices=['reddit', 'twitter', 'x', 'linkedin', 'github_issues', 'github', 'moltbook'])
    p_utm.add_argument('--project', required=True,
                       help='project_name from config.json (drives utm_campaign + wrapper hostname classification)')

    p_bp = sub.add_parser('backfill-post',
                           help='Stamp post_links.post_id for every code minted '
                                'under --minted-session. Idempotent.')
    p_bp.add_argument('--minted-session', required=True)
    p_bp.add_argument('--post-id', type=int, required=True)

    p_br = sub.add_parser('backfill-reply',
                           help='Stamp post_links.reply_id for every code minted '
                                'under --minted-session. Idempotent.')
    p_br.add_argument('--minted-session', required=True)
    p_br.add_argument('--reply-id', type=int, required=True)

    args = ap.parse_args()
    if args.cmd == 'mint':
        cmd_mint(args)
    elif args.cmd == 'resolve':
        cmd_resolve(args)
    elif args.cmd == 'wrap-text':
        cmd_wrap_text(args)
    elif args.cmd == 'wrap-post-text':
        cmd_wrap_post_text(args)
    elif args.cmd == 'utm-text':
        cmd_utm_text(args)
    elif args.cmd == 'backfill-post':
        cmd_backfill_post(args)
    elif args.cmd == 'backfill-reply':
        cmd_backfill_reply(args)


if __name__ == '__main__':
    main()
