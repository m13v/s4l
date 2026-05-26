---
name: posthog-funnel
description: "Full product funnel stats: social posts, pageviews, CTA clicks, bookings. Use when: 'funnel stats', 'product stats', 'how are cyrano/pieline doing', 'posthog funnel', 'project stats', 'boost hop', 'boost hop funnel'."
user_invocable: true
---

# Product Funnel Stats

Full marketing funnel per project: social posts -> pageviews -> CTA clicks -> Cal.com bookings.

## Usage

```
/posthog-funnel              # all products
/posthog-funnel cyrano       # single product
/posthog-funnel --days 7     # custom lookback
```

## How it works

Run the unified stats script:

```bash
python3 ~/social-autoposter/scripts/project_stats.py [--project NAME] [--days 30] [--quiet]
```

This queries three data sources in one shot:

| Source | What | Connection |
|--------|------|-----------|
| Posts DB (`DATABASE_URL`) | Social post counts, engagement, platform breakdown | `~/social-autoposter/.env` |
| PostHog API (`POSTHOG_PERSONAL_API_KEY`) | Pageviews, CTA clicks by domain | Project 330744 |
| Bookings DB (`BOOKINGS_DATABASE_URL`) | Cal.com bookings by client | Separate Postgres database (same Cloud SQL instance) |

## Product-to-Domain Mapping

| Product | Main Domain | SEO Landing Pages | Cal slug |
|---------|------------|-------------------|----------|
| Cyrano | `cyrano.systems` | `apartment-security-cameras.com` | `cyrano` |
| PieLine | `getpieline.com` | `aiphoneordering.com` | `pieline` |
| Fazm | `fazm.ai` | — | `fazm` |
| S4L | `s4l.ai` | — | `s4l` |

Domains are read from `config.json -> projects[].website` and `projects[].landing_pages.base_url`.

## PostHog Keys

Two keys, same project (330744):

- `NEXT_PUBLIC_POSTHOG_KEY` (`phc_...`) — project ingestion key, writes events from browsers and webhooks
- `POSTHOG_PERSONAL_API_KEY` (`phx_...`) — personal API key, reads/queries data

## Output

The script prints per-project:
- Social post counts (total, recent, active, removed) with platform breakdown
- Engagement totals (upvotes, comments, views)
- SEO landing page count (total pages created by the social-autoposter pipeline)
- PostHog pageviews with top pages breakdown
- CTA click details (button text, section, timestamp)
- Cal.com booking stats (total, booked, cancelled, real vs test)
- Funnel conversion rates (pageviews -> CTAs -> bookings)

Full funnel: social posts -> SEO pages created -> pageviews -> CTA clicks -> bookings

## Reporting format

When reporting results to the user, the **Project Highlights table MUST include all funnel columns**:

| Project | Posts | Engagement | SEO Pages | Pageviews | CTA Clicks | CTR | Bookings |
|---------|-------|-----------|-----------|-----------|------------|-----|----------|

Never omit SEO Pages or CTA Clicks from the summary table — these are core funnel metrics.
