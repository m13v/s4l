#!/usr/bin/env python3
"""Manual step-by-step simulation of the stats-linkedin scrape against
the linkedin-harness Chrome on port 9556. Reports findings at each
phase. NOT for production — diagnostic only.
"""
from __future__ import annotations

import json
import os
import sys
import time

CDP_URL = "http://127.0.0.1:9556"
COMMENTS_URL = "https://www.linkedin.com/in/me/recent-activity/comments/"


def log(phase: str, msg: str) -> None:
    print(f"[probe phase={phase}] {msg}", flush=True)


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # --- Phase 1: CDP attach ---
        log("1_attach", f"connect_over_cdp({CDP_URL})")
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL, timeout=5000)
        except Exception as e:
            log("1_attach", f"FAIL: {e!r}")
            return 1
        contexts = browser.contexts
        log("1_attach",
            f"OK browser_version={browser.version} "
            f"contexts={len(contexts)}")
        if not contexts:
            log("1_attach", "FAIL no contexts")
            return 1
        ctx = contexts[0]

        # --- Phase 2: cookie sanity ---
        log("2_cookies", "reading cookies on attached context")
        cookies = ctx.cookies()
        names = {c["name"] for c in cookies}
        li_at = any(c["name"] == "li_at" for c in cookies)
        jsess = any(c["name"] == "JSESSIONID" for c in cookies)
        log("2_cookies",
            f"total={len(cookies)} li_at={li_at} jsessionid={jsess} "
            f"has_bcookie={'bcookie' in names} has_lang={'lang' in names}")
        if not li_at:
            log("2_cookies", "WARN no li_at — session would fail")

        # --- Phase 3: page open + listeners ---
        log("3_page", "ctx.new_page()")
        page = ctx.new_page()

        seen_nav = []
        seen_429 = []
        seen_3xx = []

        def on_nav(frame):
            try:
                if frame == page.main_frame:
                    seen_nav.append(frame.url)
            except Exception:
                pass

        def on_resp(resp):
            try:
                if "linkedin.com" not in resp.url:
                    return
                if resp.status == 429:
                    seen_429.append((resp.url[:120], resp.status))
                elif 300 <= resp.status < 400:
                    seen_3xx.append((resp.url[:80], resp.status))
            except Exception:
                pass

        page.on("framenavigated", on_nav)
        page.on("response", on_resp)
        log("3_page", "listeners attached")

        # --- Phase 4: goto ---
        t0 = time.time()
        log("4_goto", f"page.goto({COMMENTS_URL}) wait=domcontentloaded "
                     f"timeout=30000")
        try:
            page.goto(COMMENTS_URL, wait_until="domcontentloaded",
                      timeout=30000)
        except Exception as e:
            log("4_goto", f"FAIL: {e!r}")
            page.close()
            return 1
        log("4_goto", f"OK elapsed={time.time()-t0:.2f}s url={page.url}")

        # --- Phase 5: settle ---
        log("5_settle", "wait_for_selector(article, main) timeout=10000")
        try:
            page.wait_for_selector("article, main", timeout=10000)
            log("5_settle", "OK selector found")
        except Exception as e:
            log("5_settle", f"WARN: {e!r}")
        log("5_settle", "wait_for_timeout(2500)")
        page.wait_for_timeout(2500)

        # --- Phase 6: post-goto state dump ---
        cur_url = page.url
        try:
            title = page.title()
        except Exception as e:
            title = f"<title_err: {e!r}>"
        log("6_state", f"url={cur_url}")
        log("6_state", f"title={title!r}")
        log("6_state", f"nav_events_main={seen_nav}")
        log("6_state", f"saw_429_count={len(seen_429)} "
                       f"3xx_count={len(seen_3xx)}")
        if seen_3xx:
            for u, s in seen_3xx[:5]:
                log("6_state", f"  3xx[{s}] {u}")

        # --- Phase 7: login/checkpoint heuristic ---
        url_l = cur_url.lower()
        is_authwall = any(m in url_l for m in (
            "/authwall", "/checkpoint", "/uas/login"))
        log("7_authwall", f"url_authwall={is_authwall}")

        # --- Phase 8: comments-tab signature ---
        log("8_sig", "evaluating comments-tab signature")
        try:
            sig = page.evaluate(
                """() => {
                  const urns = document.querySelectorAll(
                    '[data-urn^=\"urn:li:comment:\"], '
                    + '[data-id^=\"urn:li:comment:\"]'
                  ).length;
                  const imps = (
                    document.body && document.body.innerText || ''
                  ).match(/\\d+\\s+impressions?/g);
                  const articles = document.querySelectorAll(
                    'article').length;
                  return {urns, impressions_leaves: imps ? imps.length : 0,
                          articles};
                }"""
            )
            log("8_sig", f"OK {sig}")
        except Exception as e:
            log("8_sig", f"FAIL: {e!r}")
            sig = {}

        # --- Phase 9: NEW detectChallengeInDom mid-loop gate logic ---
        log("9_challenge", "evaluating detectChallengeInDom() probe")
        try:
            ch = page.evaluate(
                """() => {
                  const u = (location.href || '').toLowerCase();
                  if (u.indexOf('/authwall') !== -1
                      || u.indexOf('/checkpoint') !== -1
                      || u.indexOf('/uas/login') !== -1) {
                    return 'url:' + u.slice(0, 200);
                  }
                  const title = (document.title || '').toLowerCase();
                  if (title.indexOf('security verification') !== -1
                      || title.indexOf('checkpoint') !== -1
                      || title.indexOf(\"let's do a quick\") !== -1) {
                    return 'title:' + title.slice(0, 200);
                  }
                  const body = ((document.body && document.body.innerText)
                                 || '').slice(0, 400).toLowerCase();
                  const markers = [
                    \"let's do a quick security check\",
                    \"let us do a quick security check\",
                    \"verify you're a human\", \"press and hold\",
                    \"we couldn't verify\", \"we want to make sure\",
                    \"captcha\"];
                  for (let i = 0; i < markers.length; i++) {
                    if (body.indexOf(markers[i]) !== -1)
                      return 'body:' + markers[i];
                  }
                  return null;
                }"""
            )
            log("9_challenge", f"result={ch!r} (None = clean)")
        except Exception as e:
            log("9_challenge", f"FAIL: {e!r}")

        # --- Phase 10: ONE short harvest scroll (5 ticks, no full 40) ---
        log("10_harvest", "running short 5-tick harvest as smoke")
        harvest_js = r"""
        (opts) => new Promise(resolve => {
          const acc = new Map();
          const ticksLog = [];
          function harvest() {
            let added_this_tick = 0;
            document.querySelectorAll('article').forEach(art => {
              const urnEl = art.querySelector(
                '[data-urn^="urn:li:comment:"], '
                + '[data-id^="urn:li:comment:"]'
              );
              if (!urnEl) return;
              const urn = urnEl.getAttribute('data-urn')
                        || urnEl.getAttribute('data-id') || '';
              const m = urn.match(
                /^urn:li:comment:\((?:urn:li:)?(\w+):(\d+),(\d+)\)$/);
              if (!m) return;
              const cid = m[3];
              if (!acc.has(cid)) added_this_tick++;
              acc.set(cid, {comment_id: cid,
                            parent_kind: m[1], parent_id: m[2]});
            });
            return added_this_tick;
          }
          let ticks = 0;
          let stagnant = 0;
          let lastH = document.documentElement.scrollHeight;
          const tick = () => {
            const added = harvest();
            const sh = document.documentElement.scrollHeight;
            ticksLog.push({tick: ticks, added, total: acc.size,
                           scroll_height: sh});
            if (added === 0 && sh === lastH) stagnant++; else stagnant = 0;
            lastH = sh;
            const dy = opts.dy_min
                     + Math.random() * (opts.dy_max - opts.dy_min);
            window.scrollBy(0, dy);
            ticks++;
            const wait = opts.pause_min_ms
                       + Math.random()
                         * (opts.pause_max_ms - opts.pause_min_ms);
            if (ticks < opts.max_scrolls && stagnant < 4) {
              setTimeout(tick, wait);
            } else {
              setTimeout(() => {
                harvest();
                resolve({records: [...acc.values()], ticks, stagnant,
                         sh: document.documentElement.scrollHeight,
                         log: ticksLog});
              }, 1500);
            }
          };
          tick();
        });
        """
        t1 = time.time()
        try:
            result = page.evaluate(harvest_js, {
                "max_scrolls": 5,
                "pause_min_ms": 1800, "pause_max_ms": 3500,
                "dy_min": 600, "dy_max": 1100,
            })
            elapsed = time.time() - t1
            recs = result.get("records", [])
            log("10_harvest",
                f"OK elapsed={elapsed:.1f}s records={len(recs)} "
                f"ticks={result.get('ticks')} "
                f"stagnant={result.get('stagnant')} "
                f"sh_final={result.get('sh')}")
            for entry in result.get("log", []):
                log("10_harvest",
                    f"  tick={entry['tick']} added={entry['added']} "
                    f"total={entry['total']} sh={entry['scroll_height']}")
        except Exception as e:
            log("10_harvest", f"FAIL: {e!r}")

        # --- Phase 11: side-effect summary ---
        log("11_summary", f"final_url={page.url}")
        log("11_summary", f"main_nav_events={seen_nav}")
        log("11_summary", f"saw_429={seen_429}")
        log("11_summary", f"saw_3xx_count={len(seen_3xx)}")

        # --- Phase 12: cleanup ---
        log("12_cleanup", "closing OUR page (not the browser)")
        try:
            page.close()
        except Exception as e:
            log("12_cleanup", f"page.close FAIL: {e!r}")
        log("12_cleanup", "done")

    return 0


if __name__ == "__main__":
    sys.exit(main())
