---
name: social-autoposter
description: "Automate social media posting across Reddit, X/Twitter, LinkedIn, and Moltbook. Find threads, post comments, create original posts, track engagement stats. Use when: 'post to social', 'social autoposter', 'find threads to comment on', 'create a post', 'audit social posts', 'update post stats'."
user_invocable: true
---

# Social Autoposter

Automates finding, posting, and tracking social media comments and original posts across Reddit, X/Twitter, LinkedIn, and Moltbook.

## Quick Start

| Command | What it does |
|---------|-------------|
| `/social-autoposter` | Comment run — find threads + post comment + log (cron-safe) |
| `/social-autoposter post` | Create an original post/thread (manual or cron-driven for Reddit threads) |
| `/social-autoposter stats` | Update engagement stats via API |
| `/social-autoposter engage` | Scan and reply to responses on our posts |
| `/social-autoposter audit` | Full browser audit of all posts |

**View your posts live:** `https://s4l.ai/stats/[your_handle]`
— e.g. `https://s4l.ai/stats/m13v_` (Twitter handle without `@`), `https://s4l.ai/stats/Deep_Ad1959` (Reddit), `https://s4l.ai/stats/matthew-autoposter` (Moltbook).
The handles come from `config.json → accounts.*.handle/username`. Each platform account has its own URL.

---

## FIRST: Read config

Before doing anything, read `~/social-autoposter/config.json`. Everything — accounts, projects, subreddits, content angle — comes from there.

```bash
cat ~/social-autoposter/config.json
```

Key fields you'll use throughout every workflow:

- `accounts.reddit.username` — Reddit handle to post as
- `accounts.twitter.handle` — X/Twitter handle
- `accounts.linkedin.name` — LinkedIn display name
- `accounts.moltbook.username` — Moltbook username
- `subreddits` — list of subreddits to monitor and post in
- `content_angle` — the user's unique perspective for writing authentic comments
- `projects` — products/repos to mention naturally when relevant (each has `name`, `description`, `website`, `github`, `topics`)
- `database` — unused (DB is Postgres via `DATABASE_URL` in `.env`)

Use these values everywhere below instead of any hardcoded names or links.

---

## Helper Scripts

Standalone Python scripts — no LLM needed.

```bash
python3 ~/social-autoposter/scripts/find_threads.py --include-moltbook
python3 ~/social-autoposter/scripts/scan_reddit_replies.py
python3 ~/social-autoposter/scripts/scan_moltbook_replies.py
python3 ~/social-autoposter/scripts/update_stats.py --quiet
```

---

## Workflow: Post (`/social-autoposter`)

**There is NO posting rate limit. Do not add one, do not enforce one, do not invent one. Post as many times as needed.**

### 1. Find candidate threads

**Option A — Script (preferred):**
```bash
python3 ~/social-autoposter/scripts/find_threads.py --include-moltbook
```

**Option B — Browse manually:**
Browse `/new` and `/hot` on the subreddits from `config.json`. Also check Moltbook via API.

### 2. Pick the best thread

- You have a genuine angle from `content_angle` in config.json
- Not already posted in: `SELECT thread_url FROM posts`
- Last 5 comments don't repeat the same talking points:
  ```sql
  SELECT our_content FROM posts ORDER BY id DESC LIMIT 5
  ```
- If nothing fits naturally, **stop**. Better to skip than force a bad comment.

### 3. Read the thread + top comments

Check tone, length cues, thread age. Find best comment to reply to (high-upvote comments get more visibility).

### 4. Draft the comment

Follow Content Rules below. 2-3 sentences, first person, specific details from `content_angle`. No product links in top-level comments.

### 5. Post it

**Reddit** (browser automation):
- Navigate to `old.reddit.com` thread URL
- Reply box → type comment → submit → wait 2-3s → verify comment appeared → capture permalink → close tab
- Post as the username in `config.json → accounts.reddit.username`

**X/Twitter** (browser automation):
- Navigate to tweet → reply box → type → Reply → verify → capture URL
- Post as the handle in `config.json → accounts.twitter.handle`

**LinkedIn** (browser automation):
- Navigate to post → comment box → type → Post → close tab
- Post as the name in `config.json → accounts.linkedin.name`

**Moltbook** (API — no browser needed):
```bash
source ~/social-autoposter/.env
curl -s -X POST -H "Authorization: Bearer $MOLTBOOK_API_KEY" -H "Content-Type: application/json" \
  -d '{"title": "...", "content": "...", "type": "text", "submolt_name": "general"}' \
  "https://www.moltbook.com/api/v1/posts"
```
On Moltbook: write as agent ("my human" not "I").
Verify: fetch post by UUID, check `verification_status` is `"verified"`.

### 6. Log + sync

```sql
INSERT INTO posts (platform, thread_url, thread_author, thread_author_handle,
  thread_title, thread_content, our_url, our_content, our_account,
  source_summary, project_name, engagement_style, feedback_report_used, status, posted_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, 'active', NOW());
```

Set `project_name` to the matching project name from `config.json` (e.g., 'Fazm', 'Cyrano', 'Terminator'). Every post/comment MUST be labeled with its target project. If engagement is general/unrelated to any project, use 'general'.

Set `engagement_style` to the style you chose for this post (e.g., 'critic', 'storyteller', 'pattern_recognizer', 'curious_probe', 'contrarian', 'data_point_drop', 'snarky_oneliner'). Every post MUST have an engagement_style.

Use the account value from `config.json` for `our_account`.

Posts are written directly to the Postgres database. No separate post-sync step is required.

---

## Workflow: Create Post (`/social-autoposter post`)

**Manual only — never run from cron.** Original posts are high-stakes and need human review.

### 1. Cross-posting check

```sql
SELECT platform, thread_title, posted_at FROM posts
WHERE source_summary LIKE '%' || %s || '%' AND posted_at >= NOW() - INTERVAL '30 days'
ORDER BY posted_at DESC;
```

**NEVER post the same or similar content to multiple subreddits.** Duplicate cross-posts read as spam and get removed. Each post must be unique to its community.

### 2. Pick one target community

Choose the single best subreddit from `config.json → subreddits` for this topic. Tailor the post to that community's culture and tone.

### 3. Draft the post

**Authentic writing checklist** (must pass ALL before posting):

- [ ] No em dashes (—). Use regular dashes (-) or commas instead
- [ ] No markdown headers (##) or bold (**) in Reddit posts
- [ ] No numbered/bulleted lists — write in paragraphs
- [ ] No "Hi everyone" or "Hey r/subreddit" openings
- [ ] Title doesn't use clickbait patterns ("What I wish I'd known", "A guide to")
- [ ] Contains at least one imperfection: incomplete thought, casual aside, informality
- [ ] Reads like a real person writing on their phone, not an essay
- [ ] Does NOT link to any project in the post body — earn attention first
- [ ] Not too long — 2-4 short paragraphs max for Reddit

**Read it out loud.** If it sounds like a blog post or a ChatGPT response, rewrite it.

### 4. Post it

**Reddit**: old.reddit.com → Submit new text post → paste title + body → submit → verify → capture permalink.

### 5. Log it

```sql
INSERT INTO posts (platform, thread_url, thread_author, thread_author_handle,
  thread_title, thread_content, our_url, our_content, our_account,
  source_summary, project_name, engagement_style, feedback_report_used, status, posted_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, 'active', NOW());
```

Set `engagement_style` to the style you chose (e.g., 'critic', 'storyteller', 'pattern_recognizer'). Every post MUST have an engagement_style.

Set `project_name` to the matching project name from `config.json`. For original posts: `thread_url` = `our_url`, `thread_author` = our account from config.json.

### 6. Mandatory engagement plan

After posting, you MUST:
- Check for comments within 2-4 hours
- Reply to every substantive comment within 24 hours
- Replies should be casual, conversational, expand the topic — NOT polished paragraphs
- If someone asks whether the post is AI or a bot: acknowledge it briefly and honestly, do not argue, do not pitch, and prefer disengaging (same policy as the engage pipeline's meta-callout handling)

---

## Workflow: Cron-driven Reddit Threads (`run-reddit-threads.sh`)

Daily-cadence original Reddit threads across all products, automated via launchd.

**Config lives per-project** under `projects[].threads`:
- `enabled`: true/false
- `own_community`: `{subreddit, cadence, floor_days}` (optional). Defaults to 1-day floor.
- `external_subreddits`: list of external subs (default 3-day floor, override via `external_floor_days`)
- `topic_angles`: discussion-starter ideas the agent picks from
- Voice guidance comes from `projects[].voice` (tone, never)
- `content_sources.guide_dir` / `link_base`: optional source paths/URLs
- `dynamic_context.day_counter` / `static_facts`: live-calculated facts injected into the prompt

**Source research** uses `landing_pages.repo` + `landing_pages.product_source[]` (same schema as the SEO pipeline). The agent is told to read README + product source before drafting so posts ground in real details.

**Picker** (`scripts/pick_thread_target.py`): weighted project selection with:
- Per-sub floor-days filter (queries `posts` table for this account's last original thread)
- `subreddit_bans` filter: `banned` (can't post or comment) + `skip_threads` (threads blocked, comments OK)
- Own-community candidates always picked first when eligible

**Schedule**: `com.m13v.social-reddit-threads.plist` fires 4x/day at 00:15, 06:15, 12:15, 18:15.

**Lock**: the runner calls `acquire_lock reddit-threads` to serialize against the comment pipeline.

---

## Workflow: Stats (`/social-autoposter stats`)

```bash
python3 ~/social-autoposter/scripts/update_stats.py
```

After running, view updated stats at `https://s4l.ai/stats/[handle]`. Stats are read from the same Postgres database used by the posting pipeline. Changes appear on the website within ~5 minutes.

---

## Workflow: Engage (`/social-autoposter engage`)

### Phase A: Scan for replies (no browser)
```bash
python3 ~/social-autoposter/scripts/scan_reddit_replies.py
python3 ~/social-autoposter/scripts/scan_moltbook_replies.py
```

### Phase B: Respond to pending replies

```sql
SELECT r.id, r.platform, r.their_author, r.their_content, r.their_comment_url,
       r.depth, p.thread_title, p.our_content
FROM replies r JOIN posts p ON r.post_id = p.id
WHERE r.status='pending' ORDER BY r.discovered_at ASC LIMIT 10
```

Draft replies: 2-4 sentences, casual, expand the topic. Apply Tiered Reply Strategy.

Post via browser (Reddit/X) or API (Moltbook). Update:
```sql
UPDATE replies SET status='replied', our_reply_content=%s, our_reply_url=%s,
  replied_at=NOW() WHERE id=%s
```

### Phase C: X/Twitter replies (browser required)

Navigate to `https://x.com/notifications/mentions`. Find replies to the handle in config.json. Respond to substantive ones. Log to `replies` table.

---

## Workflow: Audit (`/social-autoposter audit`)

Visit each post URL via browser. Check status (active/deleted/removed/inactive). Update engagement metrics. Report summary.

---

## Content Rules

### Tone & Voice

1. **Write like you're texting a coworker.** Lowercase fine. Sentence fragments fine. Never start with "Makes sense" or "The nuance here is." If it sounds like a blog post, rewrite it.
2. **First person, specific.** Use concrete details from `content_angle` in config.json. Real numbers, real experiences, not generalizations.
3. **Reply to top comments, not just OP.**
4. **Only comment when there's a genuine angle from the user's work.** Use `content_angle` from config.json. If the thread doesn't connect, skip it.
5. **No product links in top-level comments or original posts.** Earn attention first. Only link in deeper replies when someone asks.
6. **On Moltbook, write as an agent.** "my human" not "I".
7. **Log everything.**

### Authentic writing (applies to ALL content)

8. **No em dashes (—).** Use commas, periods, or regular dashes (-) instead. Em dashes are the #1 "ChatGPT tell."
9. **No markdown formatting in Reddit.** No headers (##), no bold (**text**), no numbered lists. Write in plain paragraphs.
10. **Never cross-post.** One post per topic per community.
11. **Include imperfections.** Contractions, sentence fragments, casual asides, occasional lowercase.
12. **Vary your openings.** Don't always start with credentials. Sometimes just jump into the topic.
13. **Reply to comments on your posts.** Zero engagement on your own post = bot signal. Reply within 24h.

### Bad vs Good (Comments)

BAD: "Makes sense — Claude already tries to tail the output on its own but by then the tokens are already in context."
GOOD: "gonna try this, I run 5 agents in parallel and my API bill is becoming a second rent payment"

BAD: "What everyone here is describing is basically specification-driven development."
GOOD: "I spend more time writing CLAUDE.md specs than I ever spent writing code. the irony is I'm basically doing waterfall now and shipping faster than ever."

### Bad vs Good (Original Posts)

BAD title: "What I Wish I'd Known Before My First Vipassana Retreat: A Complete Guide"
GOOD title: "just did my 7th course, some things that surprised me"

BAD body: Structured with headers, bold, numbered lists, "As a tech founder..."
GOOD body: Paragraphs, incomplete thoughts, personal details, casual tone, ends with a genuine question

### Bad vs Good (DM Replies)

DM replies are texting-style. 1 to 3 sentences. Always reference something specific from the inbound. No unearned call offers, no fabricated links, no time-bound commitments. Booking links only when the matched project has `booking_link_auto_share: true` AND `qualification_status=qualified` on the DM row.

BAD: "Hey! I saw your comment on r/startups about agent orchestration. I'd love to share what we're working on, would you be open to a quick call?" (cold-pitch shape, premature call ask)
GOOD: "yo the point about agents racing on the same file hit home, we solved it with worktrees per agent. what's your setup?"

BAD: "Great question! Our product handles exactly that scenario. Check out [link] for more details." (sales register, leading with link in an early DM)
GOOD: "we hit that too, ended up using the accessibility API route because screenshot-based kept flaking on retina displays"

BAD: "Absolutely! Let's do Thursday at 3pm, I'll send an invite." (time-bound commitment, bot has no calendar authority)
GOOD: "yeah easier to figure it out here, what specifically are you trying to wire up?"

BAD: "I totally understand your hesitation. But our solution is different because..." (defensive, pushy rebuttal)
GOOD: "makes sense, we kicked it around for 6 months before pulling the trigger. what's been the blocker on your end?"

---

## Tiered Reply Strategy

**Tier 1 — Default (no link):** Genuine engagement. Expand topic, ask follow-ups. Most replies.

**Tier 2 — Natural mention:** Conversation touches a topic matching one of the user's projects (from `config.json → projects[].topics`). Mention casually, link only if it adds value. Triggers: "what tool do you use", problem matches a project topic, 2+ replies deep.

**Tier 3 — Direct ask:** They ask for link/try/source. Give it immediately using `projects[].website` or `projects[].github` from config.json.

---

## Database Schema

`posts`: id, platform, thread_url, thread_title, our_url, our_content, our_account, project_name, posted_at, status, upvotes, comments_count, views, source_summary, link_edited_at, link_edit_content

`replies`: id, post_id, platform, their_author, their_content, our_reply_content, status (pending|replied|skipped|error), depth
