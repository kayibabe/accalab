# AGENTS.md: AccaLab

Football analysis and accumulator-ticket platform on API-Football v3, sold as a subscription (Admin, Pro, Free). Analysis and tracking only; never place bets. Work on one scoped task at a time (see CODEX_TASKS.md) and do not add features beyond the task.

## Rules

- Python 3.11, FastAPI, SQLAlchemy and Alembic on SQLite (WAL; Postgres-compatible schema), APScheduler, httpx, pydantic v2, pandas, scikit-learn, LightGBM, OR-Tools CP-SAT, pytest, ruff. UI: Jinja2, HTMX, minimal vanilla JS, one CSS file of design tokens. No heavy frontend framework.
- Point-in-time correctness: every odds snapshot, prediction and ticket carries as_of. No look-ahead.
- Probabilities come from calibrated models. LLM agents only apply bounded adjustments (+/-0.05) or vetoes, with logged reasons.
- Never force a ticket: if a band cannot be filled, emit none and log why.
- Show sample sizes and bootstrap 95% confidence intervals on every ROI figure. Never imply guaranteed profit. Never show backtest numbers as live results.
- Access control is enforced server-side. Secrets only from the environment.
- Tests are required for every module. Run `pytest` and `ruff check` before finishing any task, then summarise what works and what does not.
- The words "research", "experimental", "prototype", "paper trading" and "beta" must not appear in user-facing text outside the Admin area (automated test over templates).

## Domain spec

- Daily: fetch fixtures for D to D+5, odds, injuries, lineups, standings and team stats (bulk date endpoints; respect quota; include all current leagues by default and use API coverage flags; an explicit configured league filter may restrict scope). Discover all markets via /odds/bets into a canonical registry. Odds snapshots are append-only; re-snapshot about 3 hours before kickoff and again at the last moment before kickoff (closing proxy).
- Markets have `supported` (settleable) and `ticket_eligible` flags. Default ticket_eligible: 1X2, Double Chance, DNB, Asian handicap, O/U goals, BTTS. Correct Score, HT/FT and exotics are analysed but not ticket-eligible.
- Settlement engine per supported market, including Asian handicap quarter lines, pushes and voids.
- Model: Dixon-Coles per league, scoreline matrix, all goal-based markets from the same matrix; LightGBM with isotonic calibration; de-vig (Shin or proportional) with per-market overround; p_low lower confidence bound; per-selection "why this pick" (max 140 characters).
- Eligible leg: market ticket_eligible; EV = p*odds - 1 > 0.03; EV_conservative = p_low*odds - 1 > 0; leg odds in [1.30, 2.20]; required edge scaled by overround; not flagged by data-quality checks. All thresholds in config.
- Tickets (flat 1 unit; the official run happens after the roughly 3-hour snapshot; the earlier run is "Preview" and excluded from performance): T1 [3,5), T2 [5,10), T3 [10,20), T4 [20,50), T5 [50,inf). Tiers: T1 and T2 core, T3 conditional, T4 and T5 high odds. No leg cap by default. A (fixture, market, selection, line) appears in at most one ticket per day; at most one leg per fixture per ticket.
- Builder fills bands in priority order T1 to T5; per band CP-SAT on scaled integer log-odds, product of odds within the band, maximising the product of p_low (fewest legs), then removes used legs. Never maximise total EV. Day-type config (weekday and weekend) sets enabled bands and minimum pool. The ticket count is an output of the pool. Log per-day pool diagnostics and per-band status with reasons.
- Ticket result: any lost leg means lost; void legs drop to odds 1.0; otherwise won.
- Metrics by day, month and year, per band, per tier and aggregate: tickets, staked, returned, hit rate, net P&L, ROI with bootstrap CI and a sample-size note (about 1600 times the average odds in tickets for a 5% ROI at 2 sigma). Leg-level (primary health check): calibration, Brier, claimed versus realised EV, CLV against the closing snapshot. Every eligible leg is also tracked as a flat 1-unit single in a separate view. SQL views or idempotent recomputation only.
- Backtest: walk-forward, purged, out-of-sample, last two seasons, same code path as production. football-data.co.uk CSV odds import if API odds history is unavailable. Model-quality backtests (no odds) are separate from betting backtests. Store our own odds snapshots from day one.
- Agents (Anthropic SDK, pydantic-validated JSON, all calls logged): DataQuality, NewsContext, Market, Critic. Switchable; evaluated with versus without.
- Pre-launch tracking runs 6 to 8 weeks before public registration (flag `registration_open`, default false) and is visible in the Admin area only. The launch gate (at least 6 weeks and 800 settled legs, ECE at or below 0.03, Brier no worse than the de-vigged market baseline, mean CLV positive with its CI shown) screens out a broken system; it cannot prove an edge. Say so in the report.

## Users and access

- Roles admin, pro, free. Email and password (argon2), email verification, password reset, HttpOnly SameSite cookies, CSRF, login rate limiting. Plan fields: plan, status, plan_start, plan_end.
- One entitlement config, enforced in the service layer and tested per role and route (a free user requesting a pro resource by URL gets 403).
- Free is a limited proof tier: today's fixtures (no analysis); settled tickets from 1 to 3 days ago for all bands with legs and results; headline 30-day performance (aggregate, with sample size); Learn pages; today's tickets only as locked cards (band, number of legs, combined-odds range, status). No Picks content, probabilities, EV, why-this-pick, alerts or export.
- `free_weekly_sample` is ON by default: the official T1 ticket built on the configured weekday (default Saturday; if none, the next day with a T1 that week) is fully revealed to Free users (legs, odds, result), without probabilities, EV or why-this-pick. Selection is by rule only, logged, never manual or after results, and counted in the record. Labelled "Free sample" in the UI.
- Pro: all bands including previews, leg details, probabilities, EV, why-this-pick, full history and performance with CIs, singles view, CSV export, Telegram, WhatsApp and email alerts. Admin: all of Pro plus Administration.
- Audit log for admin actions.

## UI and UX standards (every page, via shared templates and components)

- Font stack: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, Roboto, sans-serif; do not bundle Segoe. Tabular numerals in tables.
- Windows 11 type ramp as tokens: Caption 12px, Body 14px, Body Large 18px, Subtitle 20px, Title 28px. Body and table text never below 14px on desktop and 16px on mobile; Caption only for timestamps and footnotes; line-height about 1.45; contrast at least 4.5:1. Use rem, respect zoom, add a Text size control (100, 115, 130 percent) in Account.
- Weight 400 by default. Hierarchy through size, colour and spacing. Semibold (600) only on the page title and the single main figure in a summary card. Never bold in table headers, bodies, sidebar, buttons or body text (automated test).
- Fluent / Windows 11 look: 4px control radius, 8px card radius, 1px borders, one accent colour, 40px table rows, 32px desktop and 44px mobile controls, light and dark themes following the OS plus a toggle, visible focus, WCAG 2.2 AA. Mobile-first and lightweight. Decimal odds. Times in the user's timezone (default Africa/Blantyre). "Odds as of <time>" on data pages.
- One shared sortable-table component; every header sortable from the start (click, aria-sort, keyboard, indicator, numeric/date/odds-aware, stable, default sort, server-side above a row threshold). A test fails if any table header is not sortable.
- Low information load: one question per page, at most 6 to 7 default columns, a Details drawer for the rest, at most 4 summary cards, plain-language labels with one-line glossary tooltips.
- Sidebar order for Free and Pro: Today, Fixtures, Picks, Tickets, Results, Performance, Learn, Account. Locked Pro content is shown as locked, not hidden. Account tabs: Profile, Plan and billing, Notifications, Display. Admin adds an Administration group: Users and plans, Data and ingestion, Markets registry, Models and calibration, Agents, Backtests and pre-launch tracking, Builder and thresholds, Messaging, Payments, Jobs and logs, Audit log, Site content.
- Backtest figures are labelled "Backtest (simulated)" and kept separate from the "Live record".

## Public pages

- Routes: /, /pricing, /how-it-works, /record, /register, /login, /responsible-gambling, /terms, /privacy. Registration obeys `registration_open` (otherwise a waitlist form).
- Landing order: headline and promise; a real example ticket (the Free sample once published, otherwise a settled ticket from yesterday); how it works in 3 steps; live record snapshot with start date, number of tickets and confidence note; Free versus Pro table generated from the entitlement config; pricing; FAQ; responsible-gambling notice; primary "Create free account" and secondary "See Pro" buttons. No guaranteed-profit language, no invented numbers or testimonials, an 18+ notice, a past-performance disclaimer. Copy is editable in Admin, Site content.
- The competitive benchmark of 5 to 6 top apps (start from Forebet, FootyStats, WinDrawWin, SofaScore, FotMob, OddsJam, Action Network; verify they are current) goes in docs/UX_BENCHMARK.md as adopt / avoid / differentiate. Patterns only; never copy branding, assets, copy or code.

## Messaging

- `MessageChannel` interface with telegram (build first), whatsapp (behind `whatsapp_enabled`, default false) and email adapters. Linking in Account, Notifications (Telegram deep-link token; WhatsApp number plus one-time code). Explicit consent record per channel; STOP works on every channel. Events: official tickets published (per-band toggles), nightly results, plan-expiry reminders. Quiet hours by timezone.
- No guaranteed-profit language; every message carries an 18+ responsible-gambling line. WhatsApp messages stay short and use approved templates stored in config with category and approval status; WhatsApp bills per delivered template, so track counts and estimated cost. Outbox with idempotent sends, retries, dead-letter, delivery webhooks, signature verification, rate limits, fallback to Telegram or email. Free users never receive alerts. Admin, Messaging shows health, template status, opt-ins, failures and cost.

## Payments

- Stripe is unavailable in Malawi; do not use it. `PaymentProvider` interface (create_payment, verify_payment, handle_webhook) with adapters `paychangu` (hosted Standard Checkout for Airtel Money, TNM Mpamba and cards; optional Mobile Money Direct Charge) and `manual` (Admin marks paid with a reference). Sandbox for development.
- Prices in MWK, set in Admin. Prepaid periods, no auto-debit: payment extends plan_end; reminders via messaging; grace period, then downgrade to Free.
- Flow: pending payment with unique reference, checkout, webhook and a server-side verify call, idempotent mark-paid, extend subscription, receipt. Reconciliation job for stale pending payments. Never store card numbers or mobile money PINs; keys from the environment; validate +265 numbers. Refunds are manual with an audit entry.
