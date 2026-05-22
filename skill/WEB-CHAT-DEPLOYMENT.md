# web-chat deployment runbook

End-to-end deploy steps for the cross-site founder-chat pipeline.

**Architecture in one diagram:**

```
[ visitor on mediar.ai / fazm.ai / assrt.ai / etc. ]
  ↓ <FounderChatPanel> (from @m13v/seo-components)
  ↓ POST https://social-autoposter-website.vercel.app/api/web-chat/send
  ↓
[ Next.js route on social-autoposter-website (Vercel) ]
  ↓ INSERT
[ Postgres web_chat_threads / web_chat_messages ]   ← SAME Postgres as the rest of social-autoposter
  ↑ poll every 15s
[ launchd com.m13v.web-chat → check-web-chats.sh → claude -p ... → send_web_chat_reply.py ]
                                                      │
                                                      ↓ Resend → visitor email
                                                      ↓ send-email.js → "[WEB-CHAT #N]" → matt@mediar.ai (or i@m13v.com)
                                                      ↑ matt replies in Gmail
[ launchd com.m13v.web-chat-ingest (5min) → ingest_web_chat_replies.py ] ← matches "[WEB-CHAT #N]"
                                                      ↓ INSERTs as sender='founder'
                                                      ↓ Resend → visitor
```

**No Cloud Run. No new infra projects.** Just two new Vercel routes on the
sibling repo + the same Postgres DB you already use.

---

## What was built

```
~/social-autoposter-website/                       (Vercel)
  src/app/api/web-chat/send/route.ts               POST visitor message
  src/app/api/web-chat/thread/[threadId]/route.ts  GET thread for widget poll
  src/lib/web-chat/projects.ts                     project allowlist (auto-generated)
  src/lib/web-chat/cors.ts                         per-project CORS allowlist
  src/lib/web-chat/notify.ts                       Resend founder-notify
  src/lib/web-chat/rate-limit.ts                   5 msgs/min per IP

~/social-autoposter/                               (local)
  scripts/web_chat_schema.sql                      Postgres migration
  scripts/check_unread_web_chats.py                step 1 (find unread)
  scripts/claim_web_chat.py                        cooldown lock
  scripts/unclaim_web_chat.py                      retry on Claude failure
  scripts/send_web_chat_reply.py                   insert agent msg + Resend visitor
  scripts/poll_web_chat.py                         block on visitor follow-up
  scripts/dump_web_chat_history.py                 prompt context
  scripts/ingest_web_chat_replies.py               Gmail [WEB-CHAT #N] override rail
  scripts/sync_web_chat_config.py                  config.json -> website projects.ts
  skill/check-web-chats.sh                         orchestrator (15s)
  skill/ingest-web-chat-replies.sh                 ingest wrapper (5min)
  skill/WEB-CHAT-SKILL.md                          Claude workflow
  skill/WEB-CHAT-VOICE.md                          tone rules
  launchd/com.m13v.web-chat.plist
  launchd/com.m13v.web-chat-ingest.plist

~/seo-components/
  src/components/FounderChatPanel.tsx              the widget
  src/index.ts                                     export added
```

---

## Step 1: run the Postgres migration

The website routes and the local scripts both read/write the same two tables.

```bash
DATABASE_URL=$(grep '^DATABASE_URL=' ~/social-autoposter/.env | sed 's/^DATABASE_URL=//' | tr -d '"')
psql "$DATABASE_URL" -f ~/social-autoposter/scripts/web_chat_schema.sql
psql "$DATABASE_URL" -c "\d web_chat_threads"
```

Pure additive change. Won't touch anything else.

---

## Step 2: enable per-project config

Add `web_chat` blocks to `~/social-autoposter/config.json`. To enable on the
dogfood three (Mediar, fazm, Assrt):

```bash
cd ~/social-autoposter
for project in Mediar fazm Assrt; do
  jq --arg n "$project" '
    (.projects[] | select(.name==$n)) += {
      "web_chat": {
        "enabled": true,
        "notify_email": "i@m13v.com",
        "founder_name": "matt",
        "reply_eta": "usually replies within a couple hours"
      }
    }' config.json > /tmp/c.json && mv /tmp/c.json config.json
done

# Per-site notify_email overrides where needed:
jq '(.projects[] | select(.name=="Mediar") | .web_chat.notify_email) = "matt@mediar.ai"' \
  config.json > /tmp/c.json && mv /tmp/c.json config.json
```

---

## Step 3: sync config to the website allowlist

```bash
python3 ~/social-autoposter/scripts/sync_web_chat_config.py
# Output:
#   wrote ~/social-autoposter-website/src/lib/web-chat/projects.ts (3 enabled / 3 total)
#     [on] Assrt           -> i@m13v.com
#     [on] Mediar          -> matt@mediar.ai
#     [on] fazm            -> i@m13v.com
```

Commit + push:

```bash
cd ~/social-autoposter-website
git add src/lib/web-chat/ src/app/api/web-chat/
git commit -m "wire web-chat API + initial dogfood projects"
git push
# Vercel auto-deploys.
```

Verify the deploy:
```bash
curl -s https://social-autoposter-website.vercel.app/api/web-chat/thread/wc_does_not_exist
# → {"error":"not_found"}    (404 is correct; means the route exists)
```

---

## Step 4: env vars on Vercel

The routes need `DATABASE_URL` (already set) and `RESEND_API_KEY` (already
set, used by `/api/waitlist`). Verify they don't have a trailing `\n`:

```bash
cd ~/social-autoposter-website
vercel env pull /tmp/env-check --environment production
grep -E '^(DATABASE_URL|RESEND_API_KEY)=' /tmp/env-check
rm /tmp/env-check
```

Optional: `WEB_CHAT_NOTIFY_FROM` (default `Web Chat Agent <matt@mail.omi.me>`).
Set if you want to customise the founder-notify From: address:

```bash
echo -n "Web Chat Agent <matt@mail.omi.me>" | vercel env add WEB_CHAT_NOTIFY_FROM production
```

---

## Step 5: publish the widget

```bash
cd ~/seo-components
npm version patch
npm publish
npm run bump:consumers   # propagates to mediar-website, fazm-website, etc.
```

Drop the widget into the dogfood site (mediar-website example):

```tsx
// ~/mediar-website/src/app/layout.tsx (or any high-level layout)
import { FounderChatPanel } from "@m13v/seo-components";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        {children}
        <FounderChatPanel project="Mediar" />
      </body>
    </html>
  );
}
```

(`apiOrigin` defaults to `https://social-autoposter-website.vercel.app`.
Override if you ever map a custom domain like `chat.m13v.com`.)

Push mediar-website; Vercel auto-deploys.

---

## Step 6: smoke-test the visitor edge BEFORE flipping launchd on

```bash
# 1. Send a fake message from your terminal (skip the widget for this test).
curl -X POST https://social-autoposter-website.vercel.app/api/web-chat/send \
  -H "content-type: application/json" \
  -H "origin: https://mediar.ai" \
  -d '{"project":"Mediar","visitorId":"web_smoketest1","text":"hey, smoke test","email":"i@m13v.com","pageUrl":"https://mediar.ai"}'
# → {"threadId":"wc_...","messageId":N}

# 2. Verify the row landed in Postgres:
psql "$DATABASE_URL" -c "
  SELECT id, thread_id, project_name, visitor_email, last_message_text
    FROM web_chat_threads ORDER BY id DESC LIMIT 3;"

# 3. Verify the founder-notify email arrived in i@m13v.com.

# 4. Run the orchestrator manually one time (no launchd yet):
bash ~/social-autoposter/skill/check-web-chats.sh

# 5. Watch the Claude session log:
ls -t ~/social-autoposter/skill/logs/web-chat-session-*.log | head -1 | xargs tail -f
```

You should see Claude pick up the message, send a reply (visible in
`web_chat_messages` and forwarded to i@m13v.com via Resend), then send a
`[WEB-CHAT #N]` summary email.

---

## Step 7: turn on the launchd jobs

```bash
ln -sf ~/social-autoposter/launchd/com.m13v.web-chat.plist        ~/Library/LaunchAgents/
ln -sf ~/social-autoposter/launchd/com.m13v.web-chat-ingest.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.m13v.web-chat.plist
launchctl load ~/Library/LaunchAgents/com.m13v.web-chat-ingest.plist
launchctl list | grep web-chat
```

Watch:
```bash
tail -f ~/social-autoposter/skill/logs/web-chat.log \
        ~/social-autoposter/skill/logs/web-chat-ingest.log
```

---

## Step 8: end-to-end dogfood from the actual widget

1. Open mediar.ai in a private window.
2. Click the chat bubble. Drop your real email, send a question.
3. Within ~15s the launchd tick spawns Claude. You should:
   - get `[WEB-CHAT #N] Mediar: <your-email>` in your inbox
   - see the agent reply appear in the widget on the next 30s poll
   - get the same reply via Resend in your email
4. Reply to the `[WEB-CHAT #N]` email in Gmail with your override text.
5. Within ~5min the ingest job picks it up; your reply lands as
   `sender='founder'` in `web_chat_messages` and gets emailed to the visitor.

---

## Adding a new site

```bash
# 1. Add the web_chat block to config.json
jq '(.projects[] | select(.name=="Cyrano")) += {"web_chat":{"enabled":true,"notify_email":"i@m13v.com","founder_name":"matt","reply_eta":"usually replies within a couple hours"}}' \
  ~/social-autoposter/config.json > /tmp/c.json && mv /tmp/c.json ~/social-autoposter/config.json

# 2. Sync to the website
python3 ~/social-autoposter/scripts/sync_web_chat_config.py

# 3. Commit + push (Vercel auto-deploys)
cd ~/social-autoposter-website && git add -A && git commit -m "enable web-chat for Cyrano" && git push

# 4. Drop <FounderChatPanel project="Cyrano" /> into the consumer site, push.
```

---

## Pause / rollback

```bash
# Pause the local agent (visitor messages still land in Postgres, just not auto-replied).
launchctl unload ~/Library/LaunchAgents/com.m13v.web-chat.plist
launchctl unload ~/Library/LaunchAgents/com.m13v.web-chat-ingest.plist

# Disable on a single site (no redeploy needed for the LOCAL pipeline,
# but the website route also rejects the project, so re-sync + push).
jq '(.projects[] | select(.name=="Mediar") | .web_chat.enabled) = false' \
  ~/social-autoposter/config.json > /tmp/c.json && mv /tmp/c.json ~/social-autoposter/config.json
python3 ~/social-autoposter/scripts/sync_web_chat_config.py
cd ~/social-autoposter-website && git add -A && git commit -m "pause web-chat on Mediar" && git push
```

DB tables are isolated; drop only if you want to nuke history:
```sql
DROP TABLE web_chat_messages;
DROP TABLE web_chat_threads;
```
