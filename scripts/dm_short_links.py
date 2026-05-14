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
    # \b on left, lookahead on right rejects '.tld/' (branch 2 would catch
    # that) and any continuation char (digit/letter/dot/slash) so we don't
    # misfire mid-token. The trailing punctuation stripper at call time
    # handles the period-after-prose case ('check fazm.ai.').
    return r'\b(?:' + '|'.join(parts) + r')\b(?![\w./])'

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
        'utm_source': platform,           # reddit | twitter | linkedin
        'utm_medium': 'dm',
        'utm_campaign': (project or 'unknown').lower(),
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

    Same kinds as the DM path (booking + website get stamped, github + other
    pass through). utm_content uses the minted_session UUID at mint time;
    after log_post returns post_id we have it stored alongside in
    post_links, so analytics can join post_links → posts on session.
    """
    if kind not in ('booking', 'website'):
        return target_url

    parts = urlsplit(target_url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))

    utm = {
        'utm_source': platform,           # reddit | twitter | linkedin | github_issues
        'utm_medium': 'post',
        'utm_campaign': (project or 'unknown').lower(),
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


def _dm_row(conn, dm_id: int):
    cur = conn.execute(
        "SELECT id, platform, target_project, target_projects, project_name, "
        "       booking_link_sent_at "
        "FROM dms WHERE id = %s",
        (dm_id,),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"DM #{dm_id} not found")
    return dict(row)


def _existing_link(conn, dm_id: int, target_url: str):
    cur = conn.execute(
        "SELECT code, target_url, kind FROM dm_links "
        "WHERE dm_id = %s AND target_url = %s",
        (dm_id, target_url),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _mint_one(conn, *, dm_id: int, target_url: str, projects: list, projects_by_name: dict,
              dm: dict) -> dict:
    """Core mint logic, shared by `mint` CLI and `wrap_text` library call.

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
    primary = dm.get('target_project') or (matched_project if matched_project else None)
    website = _project_website(projects, primary) if primary else None
    if not website:
        return {
            'ok': False,
            'error': 'no_primary_website',
            'dm_id': dm_id,
            'detail': f"no website for project={primary!r}; set target_project first",
        }

    final_target = _build_target_url(
        target_url,
        kind,
        dm_id=dm_id,
        project=matched_project,
        platform=platform,
    )

    # Idempotent: lookup against the FINAL target_url (post-UTM) since that's
    # what the unique index (dm_id, target_url) is on. Looking up the bare URL
    # would miss when a prior mint stored the UTM-stamped form.
    existing = _existing_link(conn, dm_id, final_target)
    if not existing and final_target != target_url:
        # Also check the bare URL form, so a re-wrap that was minted before
        # we started UTM-stamping a given kind still resolves to the same row.
        existing = _existing_link(conn, dm_id, target_url)

    if existing:
        code = existing['code']
        # Refresh target_url in case UTM/booking_link updated since first mint.
        conn.execute(
            "UPDATE dm_links SET target_url = %s WHERE code = %s",
            (final_target, code),
        )
        conn.commit()
        return {
            'ok': True,
            'code': code,
            'short_url': f"{website}/r/{code}",
            'target_url': final_target,
            'kind': existing.get('kind') or kind,
            'project': matched_project,
            'reused': True,
        }

    for _ in range(8):
        code = _gen_code()
        try:
            conn.execute(
                "INSERT INTO dm_links (code, dm_id, target_url, kind, project_at_mint) "
                "VALUES (%s, %s, %s, %s, %s)",
                (code, dm_id, final_target, kind, matched_project),
            )
            conn.commit()
            break
        except Exception as e:
            # Code collision (PK) → retry with a new code. Other errors → bail.
            if 'duplicate key' in str(e).lower() and 'dm_links_pkey' in str(e).lower():
                conn.execute("ROLLBACK")
                continue
            # Unique (dm_id, target_url) collision: another mint raced us. Re-read.
            if 'uq_dm_links_dm_target' in str(e).lower():
                conn.execute("ROLLBACK")
                existing2 = _existing_link(conn, dm_id, target_url)
                if existing2:
                    return {
                        'ok': True,
                        'code': existing2['code'],
                        'short_url': f"{website}/r/{existing2['code']}",
                        'target_url': existing2['target_url'],
                        'kind': existing2.get('kind') or kind,
                        'project': matched_project,
                        'reused': True,
                    }
            raise
    else:
        return {'ok': False, 'error': 'code_collision_after_8_tries'}

    # Auto-stamp booking_link_sent_at on first booking-kind wrap. The legacy
    # mark-booking-sent CLI is still supported but becomes a no-op when this
    # path already stamped the timestamp.
    if kind == 'booking' and not dm.get('booking_link_sent_at'):
        conn.execute(
            "UPDATE dms SET booking_link_sent_at = NOW() WHERE id = %s "
            "AND booking_link_sent_at IS NULL",
            (dm_id,),
        )
        conn.commit()

    return {
        'ok': True,
        'code': code,
        'short_url': f"{website}/r/{code}",
        'target_url': final_target,
        'kind': kind,
        'project': matched_project,
        'reused': False,
    }


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
    conn = dbmod.get_conn()
    try:
        dm = _dm_row(conn, dm_id)
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
                conn,
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
    finally:
        conn.close()


# ---- Post-link library (parallel rail to DM, table=post_links) ----------

def _mint_one_post(conn, *, target_url: str, projects: list, platform: str,
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
    """
    target_url = _ensure_scheme((target_url or '').strip())
    if not target_url or target_url == 'https://':
        return {'ok': False, 'error': 'empty_url'}

    kind, matched_project = _classify_url(target_url, projects)

    # Wrapper hostname comes from the project we're posting AS, not from any
    # URL classification. Posts always know which project they are for.
    website = _project_website(projects, project_name)
    if not website:
        return {
            'ok': False,
            'error': 'no_primary_website',
            'project': project_name,
            'detail': f"no website for project={project_name!r} in config.json",
        }

    project_cfg = next((p for p in projects if p.get('name') == project_name), None)
    if project_cfg and project_cfg.get('external_short_links'):
        # Pool path. Atomically claim the oldest unclaimed pool row matching
        # (project_name, platform). FOR UPDATE SKIP LOCKED makes concurrent
        # cycles take different rows instead of contending on the same one.
        platform_norm = (platform or '').lower()
        if platform_norm == 'x':
            platform_norm = 'twitter'
        cur = conn.execute(
            "SELECT code, target_url FROM post_links "
            "WHERE project_name = %s AND platform = %s "
            "  AND post_id IS NULL AND reply_id IS NULL "
            "  AND minted_session LIKE 'pool:%%' "
            "ORDER BY minted_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED",
            (project_name, platform_norm),
        )
        row = cur.fetchone()
        if not row:
            return {
                'ok': False,
                'error': 'pool_exhausted',
                'project': project_name,
                'platform': platform_norm,
                'detail': (f"external_short_links pool empty for {project_name}/{platform_norm}; "
                           f"re-mint via scripts/mint_external_pool.py and send updated CSV to client"),
            }
        row = dict(row)
        pool_code = row['code']
        pool_target = row['target_url']
        # Re-stamp minted_session with the caller's session so backfill_post_id /
        # backfill_reply_id finds this row when log_post returns. Without this,
        # the pool row's minted_session stays 'pool:<slug>-<platform>' and the
        # backfill UPDATE matches nothing.
        conn.execute(
            "UPDATE post_links SET minted_session = %s "
            "WHERE code = %s",
            (minted_session, pool_code),
        )
        conn.commit()
        return {
            'ok': True,
            'code': pool_code,
            'short_url': f"{website}/r/{pool_code}",
            'target_url': pool_target,
            'kind': 'website',
            'from_pool': True,
        }

    final_target = _build_target_url_for_post(
        target_url,
        kind,
        minted_session=minted_session,
        project=matched_project or project_name,
        platform=platform,
    )

    # Posts mint fresh codes every call — no idempotency on (post_id, target_url)
    # because post_id is NULL at mint time. The minted_session UUID groups the
    # codes so the caller can backfill them all in one UPDATE after log_post
    # returns. If a wrap is retried (rare), we get duplicate codes pointing at
    # the same target_url; orphans of failed posts are bounded and harmless.
    for _ in range(8):
        code = _gen_code()
        try:
            conn.execute(
                "INSERT INTO post_links (code, platform, project_name, "
                "       target_url, kind, project_at_mint, minted_session) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (code, platform, project_name, final_target, kind,
                 matched_project, minted_session),
            )
            conn.commit()
            return {
                'ok': True,
                'code': code,
                'short_url': f"{website}/r/{code}",
                'target_url': final_target,
                'kind': kind,
            }
        except Exception as e:
            if 'duplicate key' in str(e).lower() and 'post_links_pkey' in str(e).lower():
                conn.execute("ROLLBACK")
                continue
            raise
    return {'ok': False, 'error': 'code_collision_after_8_tries'}


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
    conn = dbmod.get_conn()
    try:
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
                conn,
                target_url=stripped,
                projects=projects,
                platform=platform,
                project_name=project_name,
                minted_session=minted_session,
            )
            if not res.get('ok'):
                return {**res, 'ok': False}
            seen[stripped] = res['short_url']
            codes.append(res['code'])

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
    finally:
        conn.close()


def backfill_post_id(*, minted_session: str, post_id: int) -> int:
    """Stamp post_links.post_id for every code minted under minted_session.

    Returns the rowcount affected. Safe to call multiple times (idempotent).
    Caller should NOT raise on rowcount==0 because some posts have no URLs
    and minted_session was None — the caller should skip the backfill in
    that case.
    """
    if not minted_session or post_id is None:
        return 0
    conn = dbmod.get_conn()
    try:
        cur = conn.execute(
            "UPDATE post_links SET post_id = %s "
            "WHERE minted_session = %s AND post_id IS NULL",
            (post_id, minted_session),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def backfill_reply_id(*, minted_session: str, reply_id: int) -> int:
    """Same as backfill_post_id but stamps post_links.reply_id (engage_reddit
    writes to the `replies` table, not `posts`)."""
    if not minted_session or reply_id is None:
        return 0
    conn = dbmod.get_conn()
    try:
        cur = conn.execute(
            "UPDATE post_links SET reply_id = %s "
            "WHERE minted_session = %s AND reply_id IS NULL",
            (reply_id, minted_session),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


# ---- CLI subcommands ----

def cmd_mint(args):
    projects = _load_projects()
    projects_by_name = {p['name']: p for p in projects}
    conn = dbmod.get_conn()
    try:
        dm = _dm_row(conn, args.dm_id)
        res = _mint_one(
            conn,
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
    finally:
        conn.close()


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
    conn = dbmod.get_conn()
    try:
        cur = conn.execute(
            "SELECT l.code, l.dm_id, l.target_url, l.kind, "
            "       d.platform, d.target_project, d.project_name "
            "FROM dm_links l JOIN dms d ON d.id = l.dm_id "
            "WHERE l.code = %s",
            (args.code,),
        )
        row = cur.fetchone()
        if not row:
            print(json.dumps({'error': 'not_found', 'code': args.code}))
            return
        row = dict(row)
        platform = (row.get('platform') or 'reddit').lower()
        if platform == 'x':
            platform = 'twitter'

        ua = (getattr(args, 'user_agent', '') or '').strip()
        referrer = (getattr(args, 'referrer', '') or '').strip() or None
        is_bot = bool(ua and BOT_UA_RE.search(ua))
        ip_raw = (getattr(args, 'ip', '') or '').strip()
        ip_hash = (
            hashlib.sha256(ip_raw.encode('utf-8')).hexdigest()[:16]
            if ip_raw else None
        )

        if not args.no_count:
            # Per-click log row, captures every hit (human or bot).
            try:
                conn.execute(
                    "INSERT INTO dm_link_clicks (code, ip_hash, user_agent, is_bot, referrer) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (args.code, ip_hash, ua[:500] if ua else None, is_bot, referrer[:500] if referrer else None),
                )
            except Exception as e:
                sys.stderr.write(f"[dm_short_links] dm_link_clicks insert failed (non-fatal): {e}\n")

            if not is_bot:
                conn.execute(
                    "UPDATE dm_links SET "
                    "  clicks = clicks + 1, "
                    "  first_click_at = COALESCE(first_click_at, NOW()), "
                    "  last_click_at = NOW() "
                    "WHERE code = %s",
                    (args.code,),
                )
                try:
                    conn.execute(
                        "INSERT INTO dm_messages (dm_id, direction, author, content, message_at, logged_at) "
                        "VALUES (%s, 'inbound', '__click_signal__', "
                        "        '[CLICK_SIGNAL] short link clicked', NOW(), NOW())",
                        (row['dm_id'],),
                    )
                except Exception as e:
                    sys.stderr.write(f"[dm_short_links] click_signal insert failed (non-fatal): {e}\n")
            conn.commit()

        print(json.dumps({
            'dm_id': row['dm_id'],
            'platform': platform,
            'project': row.get('target_project') or row.get('project_name'),
            'kind': row.get('kind'),
            'target_url': row['target_url'],
            'is_bot': is_bot,
        }))
    finally:
        conn.close()


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
    elif args.cmd == 'backfill-post':
        cmd_backfill_post(args)
    elif args.cmd == 'backfill-reply':
        cmd_backfill_reply(args)


if __name__ == '__main__':
    main()
