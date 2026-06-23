#!/usr/bin/env python3
"""Insert/replace the Capstacker project entry in config.json. Idempotent."""
import json, os, sys

CFG = os.path.join(os.path.dirname(__file__), "..", "config.json")
CFG = os.path.abspath(CFG)

entry = {
    "name": "Capstacker",
    "display_name": "Capstacker",
    "weight": 15,
    "description": (
        "Infrastructure / dealroom platform for outcome-based and equity compensation "
        "between early-stage startups and fractional operators, agencies, and specialists. "
        "Lets cash-constrained founders hire fractional execs (CMO/CFO/etc.), agencies, and "
        "advisors and pay via milestones, revenue share, equity, deferred cash, or success "
        "fees instead of cash upfront. Provides benchmarked terms, standardized two-document "
        "contracts (Operator Agreement + Project Assignment/Exhibit A), milestone tracking, "
        "and integrated payouts (Stripe Connect + Trustap escrow). Capstacker is a platform, "
        "NOT a party to any deal; it does not employ, guarantee payment, or resolve disputes."
    ),
    "website": "https://capstacker.io",
    "short_links_live": False,
    "short_links_host": "https://s4l.ai",
    "voice_relationship": "third_party",
    "get_started_link": "https://capstacker.io/accounts/signup/",
    "booking_link": "https://capstacker.io/",
    "billing": {
        "model": "$100/mo soft budget + $2/1k impressions + $50/1k clicks",
        "subscription": "$100/month (charge_automatically, active 2026-06-23)",
        "stripe_customer": "cus_Ukgcq8GuzQOFY9",
        "stripe_subscription": "sub_1TlBFjRzrfmaooMLDzPbDqsp",
        "first_invoice": "GAHFIKJC-0001 (paid)"
    },
    "founders": {
        "mustafa": (
            "Mustafa Abbasoglu, founder of Capstacker.io. Contact mustafa@capstacker.io. "
            "Came in via the S4L web-chat agent (WEB-CHAT #66, 2026-06-16). Wants pay-per-click "
            "optionality over impressions; thinks in cap-table impact. Onboarded 2026-06-23."
        )
    },
    "founder_accounts": {
        "real_name": "Mustafa Abbasoglu",
        "support_email": "mustafa@capstacker.io"
    },
    "icp": (
        "US-based early-stage founders (pre-Series A) who are cash-constrained but high-growth, "
        "negotiating with service providers (agencies, fractional CxOs, lawyers, advisors, "
        "recruiters) and want to structure deals with deferred fees, equity components, or "
        "milestone-based payments instead of paying cash upfront. Comfortable with non-standard "
        "deal structures; think in terms of cap-table impact, not just invoices. Secondary "
        "(supply side): fractional leaders, studios, and agencies who want to offer upside/"
        "performance deals without the legal, trust, and admin overhead. NOT hourly freelancers."
    ),
    "target_icp": (
        "Pre-seed to Series A startup founders, fractional CMOs/CFOs/COOs, growth and dev "
        "agencies, startup advisors, fundraising/finance/legal specialists."
    ),
    "job_titles": [
        "founder", "co-founder", "startup CEO", "fractional CMO", "fractional CFO",
        "fractional COO", "fractional executive", "growth lead", "agency owner",
        "agency founder", "marketing agency owner", "startup advisor", "fundraising advisor",
        "startup consultant", "head of growth"
    ],
    "geo_focus": (
        "Primary US (founder-stated ICP is US-based). UK relevant on the supply side "
        "(site cites 110K UK fractional roles, up from 2K in 2 years). Confirm with founder."
    ),
    "pricing_capstacker": {
        "operators": "Free to join the Operator Network. 5% platform fee, charged only when a deal pays out. No subscription, no lock-in.",
        "startups": "Subscription for platform access (structuring, tracking, paying deals in one place). Optional 'Operator Introductions' add-on, charged only if a deal is initiated.",
        "payments": "Funds secured upfront via escrow-like infrastructure (Trustap); released as milestones are approved. Payouts via Stripe Connect.",
        "speed": "Deals close in ~1-2 weeks (vs 4-8 weeks traditional senior hire / weeks of vendor shopping + $3K-$5K legal)."
    },
    "competitor_domains": [
        "upwork.com", "toptal.com", "fractionaljobs.io", "deel.com", "ruul.io",
        "lightercapital.com", "clear.co", "clearco.com", "carta.com", "rippling.com",
        "contra.com", "gun.io", "a.team", "pangea.app", "continuum.work", "braintrust.com"
    ],
    "competitive_positioning": {
        "vs_upwork_toptal": "Upwork and Toptal are cash-only and transactional, priced per hour/project. Capstacker is the deal-structuring layer for outcome/equity/deferred compensation, built for fractional execs and agencies paid for results, not freelancers billing time.",
        "vs_fractionaljobs": "fractionaljobs.io matches supply and demand but offers no deal-structuring, contracts, milestone tracking, or payout layer. Capstacker is the infrastructure that makes the deal actually close and pay out.",
        "vs_deel_ruul_rippling": "Deel, Ruul, and Rippling handle compliance and payouts but assume cash terms are already agreed. They are payroll/EOR, a different category. Capstacker structures the non-cash part (equity, deferred, milestone, success fees) and then pays out via Stripe Connect. Position as adjacent, not head-to-head.",
        "vs_carta": "Carta manages the cap table after equity is granted. Capstacker structures the operator/service-provider equity deal in the first place. Complementary, not competing.",
        "vs_lighter_capital_clearco": "Lighter Capital and Clearco are debt a founder takes on to pay providers in cash. Capstacker makes that unnecessary by letting the founder pay in equity / on outcomes instead of borrowing."
    },
    "search_topics": [
        "fractional CMO equity", "fractional CFO startup", "hire fractional executive startup",
        "pay contractor in equity", "pay agency in equity", "equity for advisors",
        "advisor equity vesting", "how much equity for fractional CMO", "deferred compensation startup",
        "milestone based contractor payment", "performance based marketing agency",
        "revenue share agency deal", "agency equity deal", "outcome based pricing agency",
        "can't afford senior hire startup", "extend startup runway", "preserve startup runway",
        "startup legal help deferred payment", "non-cash compensation startup",
        "cap table service providers", "equity compensation contractor",
        "Toptal alternative", "Upwork alternative startups", "fractionaljobs alternative",
        "Deel alternative equity", "Clearco alternative", "fractional hiring marketplace",
        "fractional executive marketplace", "hire growth agency equity", "startup advisor compensation",
        "vesting schedule for advisor", "SAFE for service providers", "milestone payments contractor escrow"
    ],
    "subreddits": [
        {"name": "startups", "fit": "PRIMARY", "notes": "Founders constantly discuss hiring, runway, equity splits, fractional help, agency burn. Core demand-side audience."},
        {"name": "Entrepreneur", "fit": "PRIMARY", "notes": "Broad founder audience; hiring/compensation/runway threads. Strict on self-promo; keep comments substantive."},
        {"name": "SaaS", "fit": "PRIMARY", "notes": "Early SaaS founders making fractional CMO / growth-agency decisions."},
        {"name": "ycombinator", "fit": "SECONDARY", "notes": "Pre-seed/seed founders, equity-literate, cap-table aware."},
        {"name": "EntrepreneurRideAlong", "fit": "SECONDARY", "notes": "Hands-on early founders bootstrapping growth, can't afford full-time senior hires."},
        {"name": "smallbusiness", "fit": "SECONDARY", "notes": "Owners weighing agency vs in-house; less equity-native, more outcome-based angle."},
        {"name": "agency", "fit": "SECONDARY", "notes": "Supply side: agency owners considering performance/equity deals with startup clients."},
        {"name": "marketing", "fit": "TERTIARY", "notes": "Growth/agency operators; lead with performance-based engagement angle."},
        {"name": "venturecapital", "fit": "TERTIARY", "notes": "Equity/runway literate; use for higher-level cap-table-impact pieces."}
    ],
    "subreddits_blocked": [
        {"name": "forhire", "reason": "Cash gig/hourly marketplace spam, wrong audience (we are not a freelance job board)"},
        {"name": "freelance", "reason": "Hourly-billing freelancers; founder explicitly is NOT targeting freelancers billing time"},
        {"name": "slavelabour", "reason": "Cheap-gig sub, off-brand"}
    ],
    "messaging": (
        "Hire the fractional execs, agencies, and specialists you can't pay full cash for, "
        "and pay on outcomes: milestones, revenue share, equity, or deferred fees. Benchmarked "
        "terms, standardized contracts, milestone tracking, and secure payouts in one place. "
        "Close in days, not weeks. Extend your runway instead of burning it."
    ),
    "content_angle": (
        "Lead with the founder pain: senior operators cost $180K+, agencies want $20K upfront, "
        "legal bills $600/hr, and you need all of them on pre-Series-A runway. Capstacker is the "
        "rails that make equity/outcome/deferred deals actually close and pay out fairly. For "
        "supply-side threads (agency/fractional operators), lead with 'offer upside deals without "
        "the legal/trust/admin nightmare.' Always frame as the deal-structuring + payout layer, "
        "never as a freelance job board or a payroll tool."
    ),
    "content_guardrails": [
        "Capstacker is a PLATFORM, not a party to the deal. Never claim it employs anyone, guarantees payment, or resolves disputes. The agreement is always directly between client and operator.",
        "Never give legal, tax, or securities/investment advice. Equity compensation has real securities and tax implications; point people to the platform and their own counsel, do not advise on specifics or quote 'safe' equity percentages.",
        "Never promise specific equity percentages, returns, or outcomes. Benchmarks are aggregated ranges, not guarantees.",
        "Operators are fractional execs, agencies, studios, and specialists paid for outcomes, NOT hourly freelancers billing time. Never frame Capstacker as an Upwork/Fiverr-style gig marketplace.",
        "Deel, Ruul, Rippling, and Carta are adjacent/complementary (payroll/EOR/cap-table), not head-to-head competitors. Do not trash them; position Capstacker as the layer they assume already exists (the structured non-cash deal). Stripe Connect + Trustap actually power Capstacker payouts, so never disparage Stripe.",
        "Em dashes and en dashes cause UTF-8 corruption in some channels. Use commas, semicolons, or separate sentences.",
        "Reddit comments must read as hand-written and substantive; startup/entrepreneur subs are strict on thinly-veiled promotion."
    ],
    "platforms_disabled": [],
    "contact": "mustafa@capstacker.io",
    "site_structure": {
        "marketing_root": ["/", "/about/", "/how-agreements-work/", "/pricing/", "/for-operators/", "/for-investors/", "/faq/", "/blog/"],
        "compare_pages": ["/vs/upwork/", "/vs/deel/", "/vs/safe-and-fast/"],
        "auth": ["/accounts/login/", "/accounts/signup/"]
    },
    "notes_pending": [
        "PostHog project not yet provisioned for Capstacker; add posthog.project_id + api_key_env once created (analytics wiring check will flag until then).",
        "Confirm with founder: demand-side (founders) vs supply-side (operators) priority; geo (US-only vs US+UK); his X/Twitter handle and whether to @-mention; converting use-case to lead with; any off-limits competitors/subs; compliance phrasing limits.",
        "Founder requested pay-per-click optionality over impressions; pricing currently $100/mo + $2/1k impr + $50/1k clicks. Revisit split after first cycle data."
    ]
}

with open(CFG) as f:
    cfg = json.load(f)

projects = cfg.setdefault("projects", [])
existing_idx = next((i for i, p in enumerate(projects) if (p.get("name") or "").lower() == "capstacker"), None)
if existing_idx is not None:
    projects[existing_idx] = entry
    action = "REPLACED"
else:
    projects.append(entry)
    action = "APPENDED"

with open(CFG, "w") as f:
    json.dump(cfg, f, indent=1, ensure_ascii=False)
    f.write("\n")

print(f"{action} Capstacker entry. Total projects now: {len(projects)}")
print("names:", [p.get("name") for p in projects])
