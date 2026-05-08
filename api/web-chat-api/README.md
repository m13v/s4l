# web-chat-api

Public ingress for the `<FounderChatPanel>` widget. Sits in front of Neon
Postgres so the website never holds DB creds.

## What it does

- `POST /api/web-chat/send` — accept a visitor message, validate `project`
  against `config.json`, write to Neon, fire Resend notify on first message.
- `GET /api/web-chat/thread/:threadId` — return message history (widget poll).
- `GET /healthz` — liveness probe + count of projects loaded.

## How it fits into the larger pipeline

```
[website widget] → [this API] → [Neon] ← [check-web-chats.sh launchd] → [Claude]
                                  ↑                                        ↓
                                  └──── send_web_chat_reply.py ←───────────┘
                                  ↑
                                  └──── ingest_web_chat_replies.py (Gmail override rail)
```

This API only handles the visitor-facing edge. All AI replies, escalation,
and override are local cron jobs in social-autoposter.

## Local dev

```
cp .env.example .env  # add DATABASE_URL, RESEND_API_KEY, SOCIAL_AUTOPOSTER_CONFIG_JSON
pip install -r requirements.txt
SOCIAL_AUTOPOSTER_CONFIG_JSON=$(cat ~/social-autoposter/config.json) \
  RESEND_API_KEY=$(cat ~/analytics/.env.production.local | grep RESEND_API_KEY | cut -d= -f2-) \
  DATABASE_URL=$(grep DATABASE_URL ~/social-autoposter/.env | cut -d= -f2-) \
  uvicorn main:app --reload --port 8080
```

## Deploy

See `cloudbuild.yaml`. After first deploy, set env vars (use `echo -n` to
avoid the `\n` corruption that bit Stripe webhooks):

```bash
DATABASE_URL=$(grep DATABASE_URL ~/social-autoposter/.env | cut -d= -f2- | tr -d '"')
RESEND_API_KEY=$(grep RESEND_API_KEY ~/analytics/.env.production.local | cut -d= -f2- | tr -d '"')
CONFIG_JSON=$(cat ~/social-autoposter/config.json)

# Use Secret Manager for the JSON; env-var works for first dogfood:
gcloud run services update web-chat-api \
  --project=mk0r-prod --region=us-central1 \
  --update-env-vars="DATABASE_URL=$DATABASE_URL,RESEND_API_KEY=$RESEND_API_KEY"

# config.json is large; mount via Secret Manager in production.
```

Recommended: front the Cloud Run URL with a custom domain, e.g.
`chat.m13v.com`, and use that as the `apiOrigin` default in
`@m13v/seo-components` so consumer sites don't need extra config.
