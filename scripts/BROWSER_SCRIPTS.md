# Browser Script Design Guide

Scripts that run inside platform browser agents (linkedin-agent, reddit-agent) via `browser_run_code`, or the twitter-harness MCP via `bh_run`. Each script bundles a multi-step browser interaction into a single call, replacing 5-10 Claude tool calls with 2.

## Calling Convention

```
// Call 1: Set params
browser_run_code code: async (page) => {
  await page.evaluate(() => {
    window.__params = { url: "...", text: "..." };
  });
}

// Call 2: Run script  
browser_run_code filename: scripts/browser/the_script.js
```

## Script Skeleton

```javascript
// action_platform.js — One-line description
async (page) => {
  const params = await page.evaluate(() => window.__params);
  if (!params || !params.requiredField) {
    return JSON.stringify({ ok: false, error: 'missing_params' });
  }
  try {
    // 1. Navigate
    // 2. Wait/scroll for content
    // 3. Interact (click, type)
    // 4. Verify result
    return JSON.stringify({ ok: true, data: result });
  } catch (e) {
    return JSON.stringify({ ok: false, error: e.message });
  }
}
```

## Design Rules

1. **Params via `window.__params`** — never hardcode URLs or text. Script is reusable.

2. **Return JSON always** — `{ok, data}` or `{ok: false, error}`. Error codes must be actionable: `comment_not_found`, `link_already_present`, `not_logged_in`, `save_failed`.

3. **Script owns the waits** — `waitForTimeout`, `waitFor`, scroll-and-retry loops. Caller just runs the script and reads the result.

4. **`keyboard.type()` not `fill()`** — React apps (LinkedIn) don't detect `fill()`. Always `keyboard.type()` for rich text editors.

5. **Scroll before find** — social platforms lazy-load comments. Scroll into view first.

6. **Locate by role/name** — `page.getByRole('button', {name: /pattern/i})` not CSS classes.

7. **One script = one complete action** — "edit a comment", "scan notifications", "post a reply". Not too granular, not too broad.

8. **Safety checks inside** — dedup, link-already-present, already-replied. Don't trust the caller.

## When to Script vs Prompt

| Script | Claude prompt |
|--------|--------------|
| Steps are deterministic | Needs judgment (content generation) |
| Same flow every time | Flow varies by context |
| No content creation | Writes replies, picks projects |
| Runs many times | One-off |

Claude decides WHAT (pick project, write text). Scripts execute HOW (navigate, click, type, save).

## Existing Scripts

| Script | Platform | Action |
|--------|----------|--------|
| `scripts/browser/scrape_reddit_views.js` | Reddit | Scroll profile page and extract post view counts |

For Reddit chat ingestion, use `scripts/reddit_chat_sync.py` (ingest-unread /
list-unread / backfill-chat-urls). It reads matrix-js-sdk's IndexedDB cache
directly, which is more reliable than DOM scraping of the sidebar.

## Platform Notes

### LinkedIn
- Rich text editor uses React — `fill()` won't trigger change detection
- Edit textbox is always the LAST `textbox[name="Text editor for creating comment"]`
- Our comment menu: `button[name=/View more options for Matthew/i]`
- Official API cannot read or edit comments (403/404)

### Reddit
- old.reddit.com is easier to automate than new reddit
- Edit button is visible on our own comments without a menu click

### Twitter/X
- Tweets cannot be edited via API or browser (X premium edit is limited)
