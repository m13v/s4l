# web-chat deployment runbook

End-to-end deploy steps for the web founder-chat pipeline. Mirror of the Fazm
inbox setup; everything is centralized in social-autoposter.

Each step is **idempotent** and **gated** so you can pause between them. The
goal: get `<FounderChatPanel project="Mediar" />` live on mediar.ai with
end-to-end Claude-driven replies + Gmail override rail.

---

## Inventory of what was built

```
~/social-autoposter/
  scripts/
    web_chat_schema.sql           ← Neon migration
    check_unread_web_chats.py     ← step 1 (find unread threads)
    claim_web_chat.py             ← step 1.5 (cooldown lock)
    unclaim_web_chat.py           ← step 1.6 (retry on Claude failure)
    send_web_chat_reply.py        ← step 3 (insert agent msg + email visitor)
    poll_web_chat.py              ← step 4 (block on visitor follow-up)
    dump_web_chat_history.py      ← prompt context dump
    ingest_web_chat_replies.py    ← override rail (Gmail [WEB-CHAT #N] poll)
    web_chat_config_snippet.json  ← per-project config addition
  skill/
    check-web-chats.sh            ← orchestrator (mirror of check-founder-chat.sh)
    ingest-web-chat-replies.sh    ← ingest wrapper for launchd
    WEB-CHAT-SKILL.md             ← workflow prompt for Claude
    WEB-CHAT-VOICE.md             ← per-project tone rules
  launchd/
    com.m13v.web-chat.plist           ← 15s tick (process unread threads)
    com.m13v.web-chat-ingest.plist    ← 5min tick (ingest Gmail overrides)
  api/web-chat-api/
    main.py                       ← FastAPI Cloud Run service
    Dockerfile
    requirements.txt
    cloudbuild.yaml
    README.md

~/seo-components/
  src/components/FounderChatPanel.tsx   ← the widget
  src/index.ts                          ← export added
```

---

## Step 1: Run the Neon migration

```bash
cd ~/social-autoposter
DATABASE_URL=$(grep '^DATABASE_URL=' .env | sed 's/^DATABASE_URL=//' | tr -d '"')
psql "$DATABASE_URL" -f scripts/web_chat_schema.sql
```

Verify:
```bash
psql "$DATABASE_URL" -c "\d web_chat_threads"
psql "$DATABASE_URL" -c "\d web_chat_messages"
```

Expected: two tables with the columns from `web_chat_schema.sql`. Pure
additive change, no impact on existing tables.

---

## Step 2: Add per-project config

Open `~/social-autoposter/scripts/web_chat_config_snippet.json` for the
recommended per-project values. To enable on the dogfood three (Mediar, fazm,
Assrt) only:

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

# Override notify_email per project where it differs (e.g. Mediar):
jq '(.projects[] | select(.name=="Mediar") | .web_chat.notify_email) = "matt@mediar.ai"' \
  config.json > /tmp/c.json && mv /tmp/c.json config.json
```

Verify:
```bash
jq '.projects[] | select(.web_chat.enabled==true) | {name, web_chat}' config.json
```

---

## Step 3: Deploy the Cloud Run API

```bash
cd ~/social-autoposter/api/web-chat-api

# First-time only: create the Artifact Registry repo.
gcloud artifacts repositories create web-chat-api \
  --project=mk0r-prod --location=us-central1 --repository-format=docker

# Build + deploy.
gcloud builds submit --config cloudbuild.yaml \
  --project=mk0r-prod --substitutions=_REGION=us-central1
```

Set env vars (use `echo -n` to avoid `\n` corruption that took out Stripe
webhooks back in April):

```bash
DBURL=$(grep '^DATABASE_URL=' ~/social-autoposter/.env | sed 's/^DATABASE_URL=//' | tr -d '"' | tr -d '\n')
RKEY=$(grep '^RESEND_API_KEY=' ~/analytics/.env.production.local | sed 's/^RESEND_API_KEY=//' | tr -d '"' | tr -d '\n')
CONFIG=$(cat ~/social-autoposter/config.json | tr -d '\n')

gcloud run services update web-chat-api \
  --project=mk0r-prod --region=us-central1 \
  --update-env-vars "DATABASE_URL=$DBURL,RESEND_API_KEY=$RKEY,DEFAULT_NOTIFY_EMAIL=i@m13v.com"

# config.json is too large for an env var on Cloud Run (~80kB). Mount via Secret Manager:
gcloud secrets create social-autoposter-config --project=mk0r-prod --replication-policy=automatic
echo -n "$CONFIG" | gcloud secrets versions add social-autoposter-config --data-file=- --project=mk0r-prod
gcloud run services update web-chat-api \
  --project=mk0r-prod --region=us-central1 \
  --update-secrets="SOCIAL_AUTOPOSTER_CONFIG_JSON=social-autoposter-config:latest"
```

Verify env did not get a `\n`:
```bash
gcloud run services describe web-chat-api --project=mk0r-prod --region=us-central1 \
  --format="value(spec.template.spec.containers[0].env)" 2>&1 | tr ';' '\n' | grep -E "DATABASE_URL|RESEND"
```

Sanity test:
```bash
URL=$(gcloud run services describe web-chat-api --project=mk0r-prod --region=us-central1 --format='value(status.url)')
curl -s "$URL/healthz"  # → {"ok": true, "projects_loaded": <N>}
```

(Optional) custom domain:
```bash
gcloud run domain-mappings create --service=web-chat-api \
  --domain=chat.m13v.com --region=us-central1 --project=mk0r-prod
# Then add the CNAME/A records it tells you to.
```

---

## Step 4: Publish the seo-components widget

```bash
cd ~/seo-components
npm version patch         # bump to e.g. 0.37.0
npm publish               # → @m13v/seo-components new version on npm
npm run bump:consumers    # propagates to fazm-website, mediar-website, etc.
```

Drop the widget into the dogfood site (mediar-website example):

```tsx
// app/layout.tsx (or any high-level layout)
import { FounderChatPanel } from "@m13v/seo-components";

export default function RootLayout({ children }) {
  return (
    <html>
      <body>
        {children}
        <FounderChatPanel project="Mediar" apiOrigin="https://chat.m13v.com" />
      </body>
    </html>
  );
}
```

(If `chat.m13v.com` isn't mapped yet, pass the raw Cloud Run URL:
`apiOrigin="https://web-chat-api-XXXXX-uc.a.run.app"`.)

Deploy mediar-website (Vercel auto-deploys on push).

---

## Step 5: Smoke-test the visitor side without launchd

Before flipping the launchd jobs on, manually trigger one round-trip:

```bash
# 1. From mediar.ai (or curl), send a test message:
curl -X POST https://chat.m13v.com/api/web-chat/send \
  -H "content-type: application/json" \
  -d '{"project":"Mediar","visitorId":"web_smoketest1","text":"hey, smoke test","email":"i@m13v.com","pageUrl":"https://mediar.ai"}'
# → {"threadId":"wc_...","messageId":N}

# 2. Verify it landed:
psql "$DATABASE_URL" -c "SELECT id, thread_id, project_name, visitor_email, last_message_text FROM web_chat_threads ORDER BY id DESC LIMIT 3;"

# 3. Run the orchestrator manually (no launchd yet):
bash ~/social-autoposter/skill/check-web-chats.sh

# 4. Watch the session log:
ls -t ~/social-autoposter/skill/logs/web-chat-session-*.log | head -1 | xargs tail -f
```

You should see Claude pick up the message, send a reply, fire the
`[WEB-CHAT #N]` notification email to your inbox, and the visitor
(i@m13v.com here) gets the agent's reply via Resend.

---

## Step 6: Flip the launchd jobs on

```bash
# Symlink + load the 15s job (mirror of fazm-founder-chat).
ln -sf ~/social-autoposter/launchd/com.m13v.web-chat.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.m13v.web-chat.plist

# 5min ingest job for [WEB-CHAT #N] override replies.
ln -sf ~/social-autoposter/launchd/com.m13v.web-chat-ingest.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.m13v.web-chat-ingest.plist

# Verify:
launchctl list | grep -E "web-chat"
```

Watch:
```bash
tail -f ~/social-autoposter/skill/logs/web-chat.log \
        ~/social-autoposter/skill/logs/web-chat-ingest.log
```

---

## Step 7: End-to-end dogfood

1. Open mediar.ai in a private window (no widget interaction history).
2. Click the bubble. Drop your real email, send a test question.
3. Within ~15s, the launchd tick spawns Claude, you should:
   - get a `[WEB-CHAT #N] Mediar: <your-email>` summary in your inbox
   - see a reply appear in the widget on the next 30s poll
   - get the same reply forwarded to your email via Resend
4. Reply to the `[WEB-CHAT #N]` email in Gmail with your override text.
5. Within ~5min the ingest job picks it up, your reply lands as a
   `sender='founder'` row, gets emailed to the visitor and surfaces in the
   widget on next poll.

---

## Rollback / pause

Pause without deleting state:
```bash
launchctl unload ~/Library/LaunchAgents/com.m13v.web-chat.plist
launchctl unload ~/Library/LaunchAgents/com.m13v.web-chat-ingest.plist
```

Disable on a single site (no redeploy needed):
```bash
jq '(.projects[] | select(.name=="Mediar") | .web_chat.enabled) = false' \
  ~/social-autoposter/config.json > /tmp/c.json && mv /tmp/c.json ~/social-autoposter/config.json
# Cloud Run picks this up next time the secret is refreshed; or push a new
# version of the secret immediately.
```

Tear down the API:
```bash
gcloud run services delete web-chat-api --project=mk0r-prod --region=us-central1
```

The DB tables are separate (`web_chat_threads`, `web_chat_messages`); drop
them only if you really want to nuke history.

---

## Adding a new site to the rail

1. Add a `web_chat` block to that project entry in `config.json`.
2. Re-push the secret:
   ```bash
   echo -n "$(cat ~/social-autoposter/config.json)" | \
     gcloud secrets versions add social-autoposter-config --data-file=- --project=mk0r-prod
   gcloud run services update web-chat-api --project=mk0r-prod --region=us-central1 \
     --update-secrets="SOCIAL_AUTOPOSTER_CONFIG_JSON=social-autoposter-config:latest"
   ```
3. Drop `<FounderChatPanel project="<NewProjectName>" />` somewhere on the
   site, deploy.
4. That's it. The launchd jobs already process every project; routing is
   driven by the `project_name` column.
