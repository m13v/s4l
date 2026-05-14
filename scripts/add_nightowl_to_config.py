#!/usr/bin/env python3
"""
One-shot script: insert the NightOwl project entry into ~/social-autoposter/config.json.

Idempotent: if a project with name='nightowl' (case-insensitive) already exists, the script
replaces it. Writes pretty JSON with 2-space indent. Does NOT touch any other section.
"""
import json
import shutil
import sys
from pathlib import Path
from datetime import datetime

CFG_PATH = Path.home() / "social-autoposter" / "config.json"

NIGHTOWL = {
    "name": "nightowl",
    "display_name": "NightOwl",
    "weight": 5,
    "description": (
        "Self-hosted monitoring dashboard for Laravel apps, built on top of the "
        "official laravel/nightwatch package. Routes Nightwatch telemetry "
        "(requests, queries, jobs, exceptions, scheduled tasks, cache, mail, "
        "notifications, outgoing HTTP, logs, users) into a PostgreSQL database "
        "the customer owns (BYOD). The agent itself is MIT-licensed open source; "
        "the hosted dashboard is a paid commercial product layered on top of it "
        "with issue lifecycle, alert routing, teams, and an MCP server for "
        "Claude Code / Cursor / Codex."
    ),
    "website": "https://usenightowl.com",
    "llms_txt": "https://usenightowl.com/llms.txt",
    "github": "https://github.com/lemed99/nightowl-agent",
    "platform": "Server-side PHP package + hosted web dashboard (Hetzner, Ashburn VA). Linux/macOS. PHP 8.2+, Laravel 11 or 12, PostgreSQL 14+ (16/17 recommended).",
    "setup": "composer require nightowl/agent && php artisan nightowl:install && php artisan nightowl:agent. Service provider auto-redirects laravel/nightwatch ingest to 127.0.0.1:2407. Set NIGHTOWL_DB_* env vars to point at the customer's Postgres. First payload lands in dashboard within 30 seconds.",
    "founders": {
        "leonce": (
            "Léonce Medewanou (GitHub: lemed99, Reddit: u/No_Beautiful9648, profile title "
            "'lemed99'). Solo founder and sole contributor (37 commits as of 2026-05-13). "
            "Background includes running an ERP company; describes himself as having a "
            "'deep UI/UX background'. Built NightOwl after deciding Laravel Nightwatch "
            "Cloud's per-event pricing and vendor-hosted telemetry were unacceptable for "
            "his use case. Active on r/laravel and r/PHP launching the product; got publicly "
            "called out on r/laravel for letting AI write a draft post that read as low-effort "
            "AI content, apologized and pulled the post — sensitive to authentic, hand-written "
            "engagement."
        )
    },
    "founder_accounts": {
        "real_name": "Léonce Medewanou",
        "github": "https://github.com/lemed99",
        "github_handle": "lemed99",
        "reddit": "https://old.reddit.com/user/No_Beautiful9648",
        "reddit_handle": "No_Beautiful9648",
        "reddit_profile_title": "lemed99",
        "reddit_account_created": "2024-10-15",
        "reddit_account_age_days_at_capture": 576,
        "reddit_total_karma_at_capture": 41,
        "reddit_capture_date": "2026-05-14",
        "support_email": "support@usenightowl.com",
        "legal_email": "legal@usenightowl.com"
    },
    "links": {},
    "features": [
        "Built on laravel/nightwatch ^1.26 - the official Laravel observability SDK does the instrumentation, NightOwl agent is the transport+storage+UI layer",
        "BYOD (Bring Your Own Database) - telemetry writes directly to the customer's PostgreSQL, never to NightOwl servers",
        "Agent is MIT open source (github.com/lemed99/nightowl-agent), dashboard is commercial",
        "ReactPHP non-blocking TCP ingest on port 2407, SQLite WAL buffer, drains to Postgres via the COPY protocol with synchronous_commit=off",
        "Benchmarked at 13,400 payloads/sec on a single agent instance",
        "12 Nightwatch record types captured: requests, queries, jobs, exceptions, commands, cache events, mail, notifications, outgoing HTTP, scheduled tasks, logs, users",
        "Exception fingerprinting + auto-grouping into nightowl_issues (group_hash, type, environment)",
        "Threshold-based performance issues for slow requests, queries, jobs, commands, scheduled tasks, outgoing requests, mail, notifications, cache",
        "Multi-channel alerts: Slack, Discord, Email (BYO SMTP), Webhook (HMAC-SHA256 signed) - unlimited channels per app, one-click test send",
        "Issue lifecycle UI: status (open/resolved/ignored), priority, assignees, comments, activity timeline, bulk actions, auto-resolve stale issues, auto-reopen on recurrence with cooldown",
        "Multi-environment per app: production/staging/local stamped from APP_ENV, deduped per environment so staging noise doesn't mute production alerts",
        "Multi-instance agent for ingest throughput and redundancy with per-instance CPU/memory/buffer/ingest tracking",
        "Granular data clearing: delete by table, date range, route, status code, or log level with row-count preview and 30-day protection window",
        "MCP server (Team+ plans): 81 typed tools exposing the full dashboard to Claude Code, Codex, Cursor with scoped bearer tokens and mcp-actor-type logging on every AI mutation",
        "App ownership transfer (Agency plan): hand off an entire app + agent token + monitoring history + alert channels to a client account at end of engagement",
        "Self-hosted dashboard tier ($5k one-time + optional $500/yr repo subscription): Docker Compose bundle, source access under NDA, runs entirely inside customer infrastructure for regulated/SOC2/HIPAA stacks"
    ],
    "differentiator": (
        "It is not a Nightwatch competitor — it sits on TOP of laravel/nightwatch, the "
        "official package, and just routes the telemetry to your own PostgreSQL instead "
        "of Laravel Cloud. Sentry/Datadog/AppSignal are generic polyglot APMs that don't "
        "go deep on Laravel-native concepts (queues, scheduler, cache, mail, notifications, "
        "Eloquent N+1). Laritor/Inspector/Flare are Laravel-aware but cloud-only with "
        "per-event pricing. Self-hosting Sentry is operationally heavy (Kafka, ClickHouse, "
        "Redis, Symbolicator). NightOwl is the only product that gives you (1) full Nightwatch "
        "telemetry depth, (2) data residency inside your own Postgres, (3) flat $5-$69/mo "
        "pricing that doesn't scale with traffic or seats, all in a single composer install."
    ),
    "icp": (
        "Primary: solo Laravel developers and freelancers who already use or want to use "
        "the official laravel/nightwatch package but won't or can't pay Laravel Cloud's "
        "per-event pricing, and who prefer their telemetry to live inside their own "
        "infrastructure. Secondary: small Laravel dev shops (2-10 person teams) with "
        "multiple client apps under management who need the Agency tier's app-transfer "
        "and unlimited-apps story. Tertiary: regulated teams (compliance/SOC2/HIPAA) who "
        "need fully self-hosted observability and will pay for the $5k perpetual license."
    ),
    "target_icp": "Laravel devs, PHP devs, freelancers running Laravel apps, small Laravel agencies managing multiple client apps, regulated teams (SOC2/HIPAA/internal-no-SaaS policy).",
    "job_titles": [
        "Laravel developer",
        "PHP developer",
        "full-stack developer",
        "backend engineer",
        "tech lead",
        "CTO at Laravel-first startup",
        "Laravel freelancer / consultant",
        "agency owner",
        "DevOps engineer"
    ],
    "geo_focus": "Global. Site/billing/hosting in US (Hetzner Ashburn VA + Polar). Founder writes in English, posts in English-language subreddits. PostHog analytics has explicit EU GDPR consent banner so they actively serve EU.",
    "pricing": {
        "hobby": {
            "price": "$5/month",
            "scope": "1 connected app, 1 user, 1 team, unlimited environments, 14-day dashboard lookback (data in your DB is unlimited)",
            "includes": "All Laravel events, issue management, alert channels (Slack/Discord/Email/Webhook), unlimited performance thresholds, data retention settings, community support",
            "excludes": "MCP server, granular data clearing, agent health monitoring, email support"
        },
        "team": {
            "price": "$15/month",
            "scope": "Up to 3 connected apps, up to 3 teams, up to 5 members per team, unlimited dashboard lookback",
            "includes": "Everything in Hobby + MCP server (Claude Code/Codex/Cursor) + granular data clearing + agent instance health monitoring (up to 3) + email support",
            "label": "MOST POPULAR"
        },
        "agency": {
            "price": "$69/month",
            "scope": "Unlimited connected apps, unlimited teams, unlimited members, unlimited agent instance health monitoring",
            "includes": "Everything in Team + app ownership transfer to client accounts"
        },
        "self_hosted": {
            "price": "$5,000 one-time perpetual license + optional $500/year for continued repo access",
            "scope": "Run nightowl-api + nightowl-frontend Docker Compose bundle inside a single company. Read access to private repos under NDA. Includes MSA, NDA, signed DPA, security packet (architecture, threat model, SBOM). 12 months of repo access included from purchase. No production support included.",
            "excludes": "Production support troubleshooting, custom feature work"
        },
        "flat": True,
        "scales_with_traffic": False,
        "per_seat": False,
        "per_event": False,
        "trial": "14-day free trial, no credit card required (Team tier features)",
        "billing_processor": "Polar",
        "annual_billing": "Not yet — on the roadmap. All plans monthly with no long-term commitment.",
        "cancellation": "Cancel anytime, takes effect at end of current billing period. Data lives in your Postgres, stays where it is on cancel."
    },
    "trial_offer": "14-day free trial of Team tier features, no credit card. At end of trial: pick a plan or account pauses. Monitoring data stays in customer's DB either way.",
    "launch_offer": {
        "launch_date": "2026-05-26",
        "offer": "Team & Agency plans: month 1 free + $10 off months 2-3",
        "team_savings": "$35 total (1*$15 + 2*$10)",
        "agency_savings": "$89 total (1*$69 + 2*$10)",
        "hobby_excluded": True,
        "join_url": "https://usenightowl.com/waitlist/",
        "mechanism": "Drop email, code sent on launch day (May 26)"
    },
    "competitor_domains": [
        "nightwatch.laravel.com",
        "sentry.io",
        "scoutapm.com",
        "bugsnag.com",
        "flareapp.io",
        "inspector.dev",
        "laritor.com",
        "queuewatch.com",
        "newrelic.com",
        "datadog.com",
        "datadoghq.com",
        "appsignal.com",
        "honeybadger.io",
        "rollbar.com",
        "betterstack.com",
        "axiom.co",
        "papertrail.com",
        "papertrailapp.com",
        "telescope.laravel.com"
    ],
    "competitive_positioning": {
        "vs_nightwatch_cloud": "Same package, different destination. Both use laravel/nightwatch for instrumentation. NightOwl writes to your Postgres flat $5-$69/mo; Nightwatch Cloud writes to Laravel Cloud servers with per-event pricing above the 300K free tier. NightOwl adds richer issue management (bulk actions, comments, priority, auto-resolve, auto-reopen) and granular data clearing.",
        "vs_sentry": "Laravel-native vs polyglot APM. Sentry covers JS/mobile/many backends but Laravel depth lives behind framework-native tools; NightOwl goes deep on queues, scheduled tasks, cache, mail, notifications. Sentry charges per event + per performance span + per seat; NightOwl flat. Sentry self-host is complex (Kafka/ClickHouse/Redis); NightOwl BYOD is just Postgres.",
        "vs_scout_apm": "$15/mo flat vs $99/host. Scout charges per host which compounds with horizontal scaling.",
        "vs_bugsnag": "Full APM vs errors-only. Bugsnag is just exception tracking; NightOwl covers the full Laravel surface.",
        "vs_flare": "Full APM vs exception-only. Same gap as Bugsnag.",
        "vs_laravel_telescope": "Production monitor vs local debugger. Telescope is meant for local dev; NightOwl is production-grade with retention, alerting, and team workflows.",
        "vs_inspector_dev": "BYOD flat pricing vs cloud tiered.",
        "vs_laritor": "BYOD flat vs cloud per-event. Founder notes Laritor's UI/UX is less polished than Nightwatch's.",
        "vs_queuewatch": "Full APM vs queue-only alerts.",
        "vs_new_relic": "Laravel-focused vs enterprise platform with much steeper price/complexity.",
        "vs_datadog": "$15 flat vs per-host + per-product.",
        "vs_appsignal": "Laravel-native vs polyglot APM.",
        "vs_honeybadger": "Full APM vs errors + uptime + cron only.",
        "vs_rollbar": "Full APM vs error tracking only.",
        "vs_better_stack": "Laravel APM vs logs + uptime.",
        "vs_axiom": "Opinionated APM vs event platform.",
        "vs_papertrail": "Structured APM vs log aggregator."
    },
    "messaging": (
        "Stop paying per event. Start at $5/month. Route your Nightwatch telemetry to "
        "your own PostgreSQL. Your data, your retention, your backups. Set up in 5 "
        "minutes with composer + one artisan command. 14-day free trial, no credit card."
    ),
    "content_angle": (
        "BYOD data residency + flat pricing + Laravel-native depth. Position against "
        "Laravel Cloud's per-event pricing without trashing Laravel (founder is "
        "actively pro-Nightwatch the package). Always lead with 'built on top of the "
        "official Laravel package' to defuse the 'why not just use Nightwatch' question. "
        "For technical subs, lead with the architecture (ReactPHP + SQLite WAL + COPY "
        "protocol) and benchmark (13.4k payloads/sec). For freelance/agency framing, "
        "lead with flat pricing scenarios at scale."
    ),
    "objection_handling": {
        "why_not_just_use_nightwatch_cloud": "Because Nightwatch Cloud charges per event above the 300K free tier and your telemetry lives on Laravel servers. NightOwl uses the same Nightwatch package — you keep all the instrumentation work the Laravel team did — but routes the data to your own Postgres at a flat $5-$69/month regardless of volume.",
        "is_this_open_source": "The agent (data collection + buffering + drain) is MIT open source on github.com/lemed99/nightowl-agent. The hosted dashboard (issue lifecycle UI, alerts, teams, MCP server) is commercial. You can run the agent alone, point it at your Postgres, and build your own UI on top — Metabase, Grafana, vibe-coded Next.js, whatever.",
        "what_if_laravel_ships_a_self_hosted_nightwatch_dashboard": "NightOwl's moat is in the dashboard layer (issue lifecycle, alert routing, MCP integration, agency multi-tenancy) plus the data-residency-by-default story, not in re-implementing instrumentation. The agent is open source and worst-case becomes a free transport for whatever Laravel ships.",
        "performance_overhead": "Negligible. The agent runs as a separate process. Your Laravel app fires async TCP payloads and never blocks on the request path. Benchmarked at 13,400 payloads/sec on a single instance.",
        "what_about_scale": "Run multiple agent instances; drain workers parallelize against your DB; agent buffers to local SQLite WAL under back-pressure so spikes never drop telemetry.",
        "data_leaving_my_infra": "No. The agent writes directly to your Postgres. The hosted dashboard reads from it over an encrypted connection with credentials you provide (and can rotate or revoke). NightOwl never holds a copy of your telemetry. Documented in /privacy and /subprocessors.",
        "what_happens_on_cancel": "Your data lives in your Postgres. Cancel and it stays exactly where it is. You lose dashboard access and agent updates — nothing else. Schema is documented and standard SQL.",
        "soc2_hipaa": "Not certified today. For regulated stacks, use the $5k self-hosted tier which puts both the agent and the dashboard inside your already-audited network. NDA + signed DPA + security packet included."
    },
    "search_topics": [
        "Laravel monitoring",
        "Laravel APM",
        "Laravel application monitoring",
        "self-hosted Laravel monitoring",
        "Laravel Nightwatch alternative",
        "self-hosted Nightwatch",
        "Nightwatch Cloud alternative",
        "Laravel observability",
        "Laravel error tracking",
        "Laravel exception tracking",
        "Laravel performance monitoring",
        "Laravel slow query detection",
        "Laravel N+1 detection",
        "Laravel queue monitoring",
        "Laravel job monitoring",
        "Laravel failed job monitoring",
        "Laravel scheduled task monitoring",
        "Laravel cron monitoring",
        "Laravel Horizon alternative",
        "Laravel Telescope production",
        "Laravel log aggregation",
        "Laravel log aggregation self-hosted",
        "Laravel Forge monitoring",
        "Laravel Cloud monitoring",
        "Laravel Vapor monitoring",
        "Laravel Octane monitoring",
        "Laravel Livewire monitoring",
        "Laravel Filament monitoring",
        "Laravel deadlock detection",
        "Laravel debug timeout",
        "Laravel P95 latency",
        "Laravel API latency",
        "Laravel memory usage per request",
        "Laravel notification tracking",
        "Laravel performance debugging production",
        "Laravel multi-tenant monitoring",
        "Laravel API monitoring",
        "BYOD APM",
        "self-hosted APM",
        "open source Laravel APM",
        "cheapest Laravel APM",
        "Laravel APM flat pricing",
        "Sentry alternative Laravel",
        "Sentry self-host",
        "Bugsnag alternative Laravel",
        "Scout APM alternative",
        "Datadog alternative Laravel",
        "AppSignal alternative Laravel",
        "New Relic alternative Laravel",
        "Inspector.dev alternative",
        "Laritor alternative",
        "Flare alternative",
        "Honeybadger alternative",
        "Rollbar alternative",
        "Better Stack alternative",
        "Axiom alternative",
        "Papertrail alternative",
        "Queuewatch alternative",
        "best Laravel monitoring tools 2026",
        "Laravel monitoring tools comparison",
        "Laravel monitoring open source",
        "MCP server Laravel",
        "MCP server Claude Code Laravel",
        "ReactPHP TCP server",
        "ReactPHP telemetry agent",
        "SQLite WAL buffer Postgres",
        "Postgres COPY protocol PHP",
        "PHP observability agent"
    ],
    "subreddits": [
        {
            "name": "laravel",
            "fit": "PRIMARY",
            "notes": "Founder's own launch sub. His own launch post (75 score, 46 comments, May 5, 2026) is the canonical thread. r/laravel mods are strict about AI-written content - founder got publicly called out for letting AI write a draft post. Engagement-style comments doing well; product seeding requires high authenticity."
        },
        {
            "name": "PHP",
            "fit": "PRIMARY",
            "notes": "Founder seeded here first (11 score, 8 comments, May 4, 2026). Smaller and more strict than r/laravel."
        },
        {
            "name": "webdev",
            "fit": "SECONDARY",
            "notes": "Broader audience, less Laravel-specific. Use for high-level pieces (BYOD APM, self-hosted observability)."
        },
        {
            "name": "selfhosted",
            "fit": "SECONDARY",
            "notes": "BYOD/self-hosted angle resonates. Audience cares about data residency and Docker Compose deployments."
        },
        {
            "name": "devops",
            "fit": "TERTIARY",
            "notes": "Observability tooling crowd. Strong on architecture deep dives (ReactPHP, SQLite WAL, Postgres COPY)."
        },
        {
            "name": "experiencedDevs",
            "fit": "TERTIARY",
            "notes": "Senior eng audience interested in cost/build vs buy decisions."
        }
    ],
    "subreddits_blocked": [
        {"name": "phpbb", "reason": "Forum software, not relevant"},
        {"name": "lumen", "reason": "Lumen retired, dead sub"},
        {"name": "laravel_php", "reason": "Crossposting only, low engagement"}
    ],
    "content_guardrails": [
        "Never claim NightOwl replaces or competes with laravel/nightwatch — it sits on top of it. Always say 'built on the Nightwatch package' or 'routes Nightwatch telemetry'.",
        "Never trash Laravel or Taylor Otwell — founder is on good terms with the ecosystem and active in r/laravel.",
        "Never call NightOwl 'fully open source' — the agent is MIT but the dashboard is commercial. Use 'open-source agent + commercial hosted dashboard' or just 'BYOD'.",
        "Do not promise SOC 2 / ISO 27001 / HIPAA certification — not certified. For regulated stacks point to the $5k self-hosted tier.",
        "Pricing: use 'from $5/month flat' or 'from $5-$69/month'. Never quote $5 as the all-in number.",
        "Hobby plan is 14-day dashboard lookback, not 14-day retention. Data in customer DB is unlimited; only the dashboard window is capped.",
        "PostgreSQL only. MySQL is not supported (yet). Do not imply otherwise.",
        "Avoid 'launched' phrasing until 2026-05-26. Before launch: 'launching May 26', 'on the waitlist', or 'in free trial'.",
        "Founder is sensitive to AI-written content getting flagged. Comments going into r/laravel or r/PHP should read as hand-written and substantive; the 'written with ai' suffix is a known trip-wire there.",
        "Em dashes (—, –) cause UTF-8 corruption in some channels (per global user pref). Use commas, semicolons, or separate sentences."
    ],
    "platforms_disabled": [],
    "open_source": {
        "agent_repo": "https://github.com/lemed99/nightowl-agent",
        "agent_license": "MIT",
        "agent_packagist": "https://packagist.org/packages/nightowl/agent",
        "agent_php_version": "8.2+",
        "agent_laravel_version": "11 or 12",
        "agent_required_extensions": ["pdo_pgsql", "pdo_sqlite", "pcntl", "posix", "zlib (optional, for gzip)"],
        "agent_stars_at_capture": 58,
        "agent_forks_at_capture": 3,
        "agent_contributors_at_capture": 1,
        "agent_commits_at_capture": 37,
        "agent_created": "2026-03-30",
        "agent_last_push_at_capture": "2026-05-13",
        "agent_capture_date": "2026-05-14",
        "dashboard_repo": "PRIVATE (not on GitHub publicly)",
        "dashboard_license": "Commercial — perpetual self-hosted license available at $5k one-time + optional $500/yr for repo updates"
    },
    "tech_stack": {
        "agent": {
            "language": "PHP 8.2+",
            "framework": "Laravel 11 or 12",
            "runtime": "ReactPHP non-blocking event loop",
            "buffer": "SQLite WAL (crash-safe, near-memory speed via mmap)",
            "drain": "PostgreSQL COPY protocol with synchronous_commit=off",
            "ingest_port": 2407,
            "udp_port": 2408,
            "health_port": 2409,
            "benchmark": "13,400 payloads/sec on a single instance"
        },
        "dashboard": {
            "hosting": "Hetzner (Ashburn, VA, USA)",
            "billing": "Polar",
            "analytics": "PostHog (EU instance, region-based consent banner)",
            "auth": "Sanctum sessions + CSRF tokens",
            "mcp_server": "81 typed tools, scoped bearer tokens, mutation logging with actor_type=mcp"
        },
        "customer_db": "PostgreSQL 14+ (16/17 recommended). All tables prefixed nightowl_."
    },
    "site_structure": {
        "total_pages_crawled": 103,
        "page_breakdown": {
            "marketing_root": [
                "/",
                "/pricing",
                "/self-hosted",
                "/waitlist",
                "/privacy",
                "/terms",
                "/subprocessors"
            ],
            "compare_hub_and_pages": [
                "/compare",
                "/compare/appsignal",
                "/compare/axiom",
                "/compare/better-stack",
                "/compare/bugsnag",
                "/compare/datadog",
                "/compare/flare",
                "/compare/honeybadger",
                "/compare/inspector-dev",
                "/compare/laravel-telescope",
                "/compare/laritor",
                "/compare/new-relic",
                "/compare/papertrail",
                "/compare/queuewatch",
                "/compare/rollbar",
                "/compare/scout-apm",
                "/compare/sentry"
            ],
            "alternatives_pages": [
                "/alternatives/best-laravel-monitoring-2026",
                "/alternatives/best-nightwatch-alternative",
                "/alternatives/best-sentry-alternative-laravel",
                "/alternatives/cheapest-laravel-apm",
                "/alternatives/laravel-api-monitoring",
                "/alternatives/laravel-forge-apm",
                "/alternatives/laravel-monitoring",
                "/alternatives/laravel-vapor-apm",
                "/alternatives/multi-tenant-laravel-monitoring",
                "/alternatives/nightwatch-cloud",
                "/alternatives/open-source-laravel-apm",
                "/alternatives/self-hosted-laravel-apm"
            ],
            "guides_count": 41,
            "learn_count": 18,
            "migrate_count": 7,
            "docs_pages": 15
        },
        "key_guides": [
            "/guides/debug-laravel-timeout",
            "/guides/detect-n-plus-one-queries-laravel",
            "/guides/how-to-find-slow-endpoints-laravel",
            "/guides/how-to-monitor-laravel-in-production",
            "/guides/laravel-api-latency",
            "/guides/laravel-cloud-monitoring",
            "/guides/laravel-deadlock-detection",
            "/guides/laravel-exception-tracking",
            "/guides/laravel-failed-job-monitoring",
            "/guides/laravel-filament-monitoring",
            "/guides/laravel-forge-monitoring",
            "/guides/laravel-horizon-alternative",
            "/guides/laravel-livewire-monitoring",
            "/guides/laravel-log-aggregation-self-hosted",
            "/guides/laravel-memory-usage-per-request",
            "/guides/laravel-notification-tracking",
            "/guides/laravel-octane-monitoring",
            "/guides/laravel-p95-latency",
            "/guides/laravel-performance-debugging-production"
        ]
    },
    "promotion_history_observed": {
        "reddit_r_laravel_launch_2026_05_05": {
            "url": "https://www.reddit.com/r/laravel/comments/1t4f8cd/i_built_a_selfhosted_alternative_for/",
            "title": "I built a self-hosted alternative for `laravel/nightwatch` and it's open source",
            "score": 75,
            "comments": 46,
            "status_at_capture": "alive"
        },
        "reddit_r_php_launch_2026_05_04": {
            "url": "https://www.reddit.com/r/PHP/comments/1t3lq3h/i_built_an_opensource_reactphpbased_telemetry/",
            "title": "I built an open-source ReactPHP-based telemetry agent for Laravel. It drives data from Nightwatch package into your own Postgres database via the COPY protocol",
            "score": 11,
            "comments": 8,
            "status_at_capture": "alive"
        },
        "our_engagement_so_far": [
            {
                "comment_url": "https://old.reddit.com/r/laravel/comments/1t4f8cd/i_built_a_selfhosted_alternative_for/olbuqx5/",
                "our_account": "Deep_Ad1959",
                "posted_at": "2026-05-12T07:26:24Z",
                "status": "active",
                "upvotes": 1,
                "tagged_project": "S4L (pre-NightOwl-onboarding engagement; founder may have seen this)"
            }
        ]
    },
    "context_doc": "/tmp/nightowl-crawl.md (1.1MB markdown dump of all 103 pages + docs + GitHub README/composer.json + founder Reddit profile, generated 2026-05-14)",
    "contact": "support@usenightowl.com (general), legal@usenightowl.com (privacy/terms/security)",
    "community": "https://discord.gg/  - their Discord linked from site footer (URL not captured)",
    "paused": True,
    "paused_reason": "New customer onboarded 2026-05-14. Keep paused until kickoff + posting kickoff decided. Crawl + context only for now; no auto-posting yet."
}


def main() -> int:
    cfg_path = CFG_PATH
    if not cfg_path.exists():
        print(f"ERROR: {cfg_path} does not exist", file=sys.stderr)
        return 1

    backup = cfg_path.with_suffix(f".json.pre-nightowl.{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(cfg_path, backup)
    print(f"Backup written: {backup}")

    with cfg_path.open("r") as f:
        cfg = json.load(f)

    projects = cfg.setdefault("projects", [])
    existing_idx = None
    for i, p in enumerate(projects):
        if str(p.get("name", "")).strip().lower() == "nightowl":
            existing_idx = i
            break

    if existing_idx is not None:
        print(f"Existing 'nightowl' entry found at index {existing_idx}; replacing.")
        projects[existing_idx] = NIGHTOWL
    else:
        print(f"No existing 'nightowl' entry; appending. New project count: {len(projects)+1}")
        projects.append(NIGHTOWL)

    with cfg_path.open("w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Wrote {cfg_path}.")
    print(f"Project count after merge: {len(cfg['projects'])}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
