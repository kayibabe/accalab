# AccaLab: build specification

You are a senior engineer and product designer building AccaLab: a football analysis and accumulator-ticket platform on API-Football (api-sports.io, v3), sold as a subscription with Admin, Pro and Free users. It is analysis and tracking only. The system never places bets.

Start in plan mode. See "Workflow" at the end.

## Non-negotiable principles

1. Probabilities come from calibrated statistical/ML models. LLM agents may adjust or veto within bounded limits but never emit final probabilities alone.
2. Accumulators multiply bookmaker margin as well as odds. A leg is eligible only if its conservative EV is above threshold (section C). Never pad tickets with weak legs to reach an odds band.
3. Point-in-time correctness: every prediction, odds snapshot and ticket is stored with an as_of timestamp. No look-ahead anywhere, including backtests.
4. If a band cannot be filled from eligible legs, output no ticket for that band and record why. Never force one.
5. Honesty in every number shown: show sample sizes and confidence intervals; never present in-sample or backtest results as live results; never claim or imply guaranteed profit anywhere in the product.
6. Access control is enforced on the server (API and service layer), never only by hiding things in the UI.
7. The word "research" (and "experimental", "prototype", "paper trading", "beta") must not appear in any user-facing text outside the Admin area.

## Stack

Python 3.11+, FastAPI, SQLAlchemy + SQLite (WAL mode; keep the schema Postgres-compatible), Alembic, APScheduler, httpx, pydantic v2, pandas, scikit-learn, LightGBM, OR-Tools (CP-SAT), pytest, ruff, Anthropic SDK for agents. Server-rendered UI: Jinja2 templates, HTMX for partial updates, minimal vanilla JS, one hand-written CSS file driven by design tokens (CSS variables). No heavy frontend framework. Configuration through .env and a settings module; secrets only from the environment.

## A. Ingestion

- API client with rate limiter, retries with backoff, request and quota tracking (read the rate-limit headers), response caching, pagination support, raw JSON stored for audit.
- Daily job: fetch fixtures for today and the next 5 days (configurable window; all current leagues by default, optionally restricted by config), then odds for each fixture, plus injuries, lineups when available, standings, team statistics and head-to-head. Use API coverage flags and prefer date-based bulk endpoints to protect quota.
- Use /odds/bets to discover all markets and build a canonical market registry (API bet id to canonical market name, selection keys, line/handicap parsing).
- Odds snapshots are append-only: (fixture, bookmaker, market, selection, line, odds, captured_at). Re-snapshot at a configurable T-minus (default 3 hours before kickoff) and again at the last possible moment before kickoff (closing-line proxy).
- Odds and lineups 5 days out are often missing or stale. Handle missing coverage gracefully.

## B. Markets and settlement

- Analyse every market the API exposes, but mark a market "supported" only if it can be settled from fixture data. Include: 1X2, Double Chance, DNB, O/U goals (full and half time), BTTS, Asian and European handicap, Correct Score, HT/FT, team totals, odd/even, corners and cards where statistics are available.
- Settlement engine with unit tests per market, including Asian-handicap quarter lines (half win, half loss), pushes and voids.
- Two flags per market: `supported` (settleable) and `ticket_eligible` (allowed as an acca leg). Default ticket_eligible allowlist: 1X2, Double Chance, DNB, Asian handicap, O/U goals, BTTS. Wide-margin markets (Correct Score, HT/FT, exotic props) are analysed and stored but not ticket-eligible by default. Configurable.

## C. Probability, value and leg eligibility

- Baseline: Dixon-Coles (or bivariate Poisson with time decay) per league, producing a full scoreline matrix. Derive all goal-based market probabilities from the same matrix so markets stay mutually consistent.
- Second model: LightGBM on form, shot-based xG proxies, rest days, standings, H2H and market-implied features, for 1X2, O/U and BTTS. Calibrate (isotonic or Platt) on a time-ordered holdout. Blend with the baseline; weights chosen by walk-forward log loss.
- De-vig bookmaker odds (Shin or proportional) to get market-implied probability, and record each market's overround. Use a consensus across configured bookmakers and keep the best available price per selection.
- Quantify model uncertainty per selection (ensemble disagreement, bootstrap or calibration-bin error) to get p_low, a lower confidence bound on p_model.
- Store per selection: p_model, p_low, p_market, odds, overround, EV = p_model * odds - 1, EV_conservative = p_low * odds - 1, model_version, features_hash, as_of, and a short plain-language reason ("why this pick", max 140 characters, from model drivers and agent notes).
- A selection is an eligible leg only if all of these hold (thresholds in config):
  - the market is ticket_eligible and supported
  - EV_conservative > 0 and EV > 0.03
  - leg odds within [1.30, 2.20] (favourite-longshot bias, long-shot model uncertainty, and very short prices needing unrealistic precision)
  - the required EV threshold scales up with the market's overround
  - model confidence is above a minimum and the DataQualityAgent has not flagged it

## D. AI agents

Anthropic API, structured JSON validated with pydantic, every call logged with inputs, outputs and cost.

- DataQualityAgent: flags stale or odd odds, missing lineups, suspicious data.
- NewsContextAgent: turns injuries, lineups, suspensions, rotation, congestion and motivation into a bounded adjustment (cap +/-0.05 absolute probability) with a stated reason, or a veto.
- MarketAgent: flags odds movement and bookmaker disagreement against the model.
- CriticAgent: reviews final tickets for correlated or fragile legs and may veto legs with reasons.
- Agent value must be measurable: run with and without agents and compare Brier, log loss and CLV. Agents are switchable in config. Track agent cost per day.

## E. Ticket builder

Runs: an early provisional run (labelled "Preview" in the UI, excluded from performance) and the official run after the T-minus snapshot (default 3 hours before the kickoff window; configurable). Only official tickets enter performance tracking.

- Input: the pool of eligible legs.
- Bands (config): T1 [3.00, 5.00), T2 [5.00, 10.00), T3 [10.00, 20.00), T4 [20.00, 50.00), T5 [50.00, inf). Tiers: T1 and T2 "core", T3 "conditional", T4 and T5 "high odds".
- No leg-count limit by default (max_legs = None, configurable per band).
- Uniqueness: a (fixture, market, selection, line) appears in at most one ticket per day. At most one leg per fixture within a ticket.
- Fill in priority order T1, T2, T3, T4, T5. For each band, solve with CP-SAT on scaled integer log-odds: product of odds within the band, maximising ticket hit probability (product of p_low, with a penalty on low-confidence legs), preferring the fewest legs. Remove used legs before the next band. Do not maximise total EV: it piles legs into the longest ticket, where model error is amplified most.
- Day-type configs (weekday and weekend): which bands are enabled and the minimum pool size per band. The number of tickets per day is an output of the pool. Log per day: fixtures seen, eligible legs, per-band status (built, skipped, infeasible, with reason).
- Store tickets, legs, model and agent versions, the odds used, combined odds and model hit probability.

## F. Tracking and reporting

- Stake: flat 1 unit per ticket (units, not currency). Ticket result: lost if any leg loses; void legs drop to odds 1.0; won otherwise. Settle nightly, idempotently.
- Metrics by day, month and year, for each of T1 to T5, each tier and the system aggregate: tickets, staked, returned, hit rate, net P&L, ROI/yield (net P&L divided by staked), average odds, average legs.
- ROI confidence: bootstrap 95% CI on every ROI figure plus a plain-language sample-size note (about 1600 times the average odds in tickets are needed to detect a 5% ROI at 2 sigma).
- Leg-level metrics (the primary health check): hit rate, claimed versus realised EV, Brier score, calibration (decile bins), CLV against the closing snapshot, by market, league and leg-odds bucket.
- Every eligible leg is also tracked as a flat 1-unit single in a separate "singles" view.
- Also: max drawdown, longest losing streak, sample-size warnings.
- Implement metrics as SQL views or idempotent recomputation, never hand-maintained counters.

## G. Backtesting, learning and pre-launch tracking

- Phase 0 spike (do first and report before building on assumptions): what API-Football returns for historical odds on past dates for my plan, how long odds history is retained, which leagues and seasons have odds, the request budget to backfill two seasons for the league whitelist, and the API-Football terms on displaying or redistributing odds data in a paid product.
- Walk-forward, purged, point-in-time backtest over the previous two seasons. Out-of-sample only.
- If API historical odds are unavailable, support importing football-data.co.uk-format odds CSVs. Keep model-quality backtests (no odds) separate from betting backtests (with odds). Start storing our own odds snapshots from day one.
- Backtest the full pipeline (eligibility, builder, settlement) with the same code path as production.
- Weekly retraining with model versioning; promote a new model only if it beats the incumbent out-of-sample on log loss and calibration.
- Pre-launch tracking: run the whole system for 6 to 8 weeks before opening public registration (feature flag `registration_open`, default false). Results from this period are shown only in the Admin area.
- Launch gate (proposed defaults, shown in Admin and revisited at Phase 10): at least 6 weeks and 800 settled eligible legs; calibration error (ECE) at or below 0.03; Brier score not worse than the de-vigged market baseline; mean CLV positive with its confidence interval displayed; claimed-versus-realised EV gap explained; jobs running reliably. State plainly in the Admin report that this gate screens out a broken system and cannot prove an edge, which needs thousands of legs.

## H. Ops

- APScheduler jobs: daily pull, T-minus re-snapshots, provisional and official builder runs, nightly settlement, metrics refresh, weekly retrain, payment reconciliation, notification outbox. Idempotent and logged. A CLI command for every job.
- Config-driven: league scope (all current leagues by default, optional allowlist), bookmakers, thresholds, leg-odds window, market allowlist, bands and tiers, day-type settings, agent toggles, entitlements, feature flags.

## I. Users and access

- Roles: admin, pro, free. Registration creates a free user. Email and password (argon2), email verification, password reset, session cookies (HttpOnly, SameSite), CSRF protection, login rate limiting. User plan fields: plan, status, plan_start, plan_end.
- Entitlements live in one config table, are enforced server-side, and are covered by tests (a free user requesting a pro-only resource by URL gets 403, not an empty page). Defaults, all configurable:
  - Free (a deliberately limited "proof" tier): today's fixtures list with no per-fixture analysis; settled tickets from 1 to 3 days ago for all bands, including legs and results (this is the proof of record); headline performance for the last 30 days, aggregate only, with sample size; Learn pages. Today's tickets appear as locked cards showing band, number of legs, combined-odds range and status only. No Picks content, probabilities, EV, "why this pick", alerts or export.
  - Free weekly sample (`free_weekly_sample`, ON by default): each week, the official T1 ticket built on the configured weekday (default Saturday, configurable in Admin) is revealed in full (legs, odds, result) to Free users when published. If no T1 is built that day, use the next day that has one within the same week. Probabilities, EV and "why this pick" stay Pro-only. The ticket is chosen by this rule only, logged, never by hand or after results are known, and it counts in the performance record like any other ticket. It is labelled "Free sample" on the Today, Tickets and Results pages.
  - Pro: all five bands including previews, all leg details, probabilities, EV, "why this pick", full history, full performance with confidence intervals, singles view, CSV export, Telegram, WhatsApp and email alerts.
  - Admin: everything Pro has, plus the Administration area.
- Audit log for admin actions (plan changes, config changes, role changes, manual payments).

## J. UI and UX standards

Apply to every page. Build shared templates and components, not per-page styling.

Typography and sizing
- Font stack: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, Roboto, sans-serif. Do not bundle Segoe (licensed). Tabular figures in tables (font-variant-numeric: tabular-nums).
- Windows 11 type ramp as design tokens: Caption 12px, Body 14px, Body Large 18px, Subtitle 20px, Title 28px. Floors for reading with the naked eye: body and table text never below 14px on desktop and 16px on mobile; Caption only for timestamps and footnotes; line-height about 1.45; contrast at least 4.5:1.
- Use rem units and respect browser and OS zoom. Add a "Text size" control (100%, 115%, 130%) in Account, stored per user.
- Weight: normal (400) everywhere by default. Build hierarchy with size, colour, spacing and position, not bold. Semibold (600) is allowed only for the page title and the single main figure in a summary card. Never bold in table headers, table bodies, sidebar items, buttons or body text.

Look and feel
- Fluent / Windows 11 feel: 4px control radius, 8px card radius, 1px subtle borders instead of heavy shadows, one accent colour, calm neutral surfaces, 40px table row height, 32px desktop controls and 44px mobile touch targets. Light and dark themes following the OS (prefers-color-scheme) with a manual toggle. Visible keyboard focus. WCAG 2.2 AA.
- Mobile-first and lightweight (low-bandwidth users), no heavy JS, decimal odds only, kickoff times in the user's timezone (default Africa/Blantyre, selectable). Every data page shows "Odds as of <time>".

Tables
- One shared sortable-table component used by every table. Every column header is sortable from the start: click to sort ascending or descending, aria-sort set, keyboard operable, a visible sort indicator, numeric, date and odds-aware comparison, stable sort, a sensible default sort per table, and server-side sorting above a row-count threshold. No table may be built without it. Add a test that fails if any table header is not sortable.

Information density
- Each page answers one question. Show at most 6 to 7 columns by default, with a "Details" expander or side drawer for the rest and a column chooser for Pro and Admin. At most 4 summary cards at the top of a page. Plain-language labels, with a one-line glossary tooltip for EV, CLV, ROI and Brier. Enough information to understand the system, not everything the system knows.

Sidebar (follows the information flow: data, selection, packaging, outcome, measurement)
- Fixed left sidebar, collapsible to icons on small screens; a bottom bar or drawer on mobile. Main headers in this order for Free and Pro: Today, Fixtures, Picks, Tickets, Results, Performance, Learn, Account. Locked Pro content is shown as locked, not hidden. Account has tabs: Profile, Plan and billing, Notifications, Display (text size, theme, timezone).
- Admin sees the same plus an "Administration" group: Users and plans, Data and ingestion, Markets registry, Models and calibration, Agents, Backtests and pre-launch tracking, Builder and thresholds, Messaging, Payments, Jobs and logs, Audit log, Site content.

Naming and labelling
- Add an automated test that scans templates and public strings for the forbidden words (principle 7) outside admin templates, and a test that no template uses bold outside the permitted places.
- Backtest figures in user-facing pages are labelled "Backtest (simulated)" and kept visually separate from the "Live record".

## K. Public pages and landing page

- Public routes: / (landing), /pricing, /how-it-works, /record (public live record), /register, /login, /responsible-gambling, /terms, /privacy. Registration obeys `registration_open`; otherwise show a waitlist email form.
- Landing page goal: convince visitors to register, then upgrade to Pro. Sections in order: clear headline and one-line promise; today's real example (the Free sample ticket once published, otherwise a settled ticket from yesterday); how it works in 3 steps (we scan the day's matches, we keep only legs with value, we build tickets by odds band); a transparent Live record snapshot (real data with start date, number of tickets and confidence note; if the sample is small, say so); Free versus Pro comparison table generated from the entitlement config so it can never drift (pitch: Free shows what Pro users got yesterday, so visitors can judge the record themselves; Pro gives today's tickets); pricing; FAQ; responsible-gambling notice; a primary "Create free account" button and a secondary "See Pro" button.
- Copy rules: no guaranteed-profit or "sure win" language, no invented numbers, no fake testimonials, an 18+ notice, a note that odds change and past performance does not predict future results. Copy is editable from Admin "Site content".

## L. Competitive benchmark (Phase 0 deliverable)

Review 5 to 6 top applications in this domain and write docs/UX_BENCHMARK.md: layout, navigation, free-versus-paid split, how they show track records, how they present value, EV and tickets, pricing-page patterns, what feels cluttered. Start from Forebet, FootyStats, WinDrawWin, SofaScore and FotMob (layout and match presentation), and OddsJam and Action Network (value and pick presentation). Verify these are current and add better ones if found. Extract patterns; do not copy branding, assets, copy or code. Finish with a one-page adopt / avoid / differentiate summary and a page map per role, and use it to confirm page designs before building them.

## M. Messaging channels (Pro feature)

- Channels: Telegram (bot, build first) and WhatsApp (Cloud API directly or via a BSP), behind feature flag `whatsapp_enabled` (default false until Meta business verification, template approval and a check of Meta's policy on gambling-related content are done). Define a `MessageChannel` interface with adapters: telegram, whatsapp, email.
- Linking in Account, Notifications. Telegram: one-time deep link (t.me/<bot>?start=<token>). WhatsApp: the user enters their number and verifies with a one-time code. Store an explicit consent record per channel (timestamp, wording version, channel, user). STOP or unsubscribe works on both channels at any time.
- Events: official tickets published (per-band toggles), nightly results summary, plan-expiry reminders (5 days and 1 day before expiry, and on expiry). Quiet hours by user timezone.
- Message rules: no guaranteed-profit language; include an "18+, play responsibly" line and a link to the responsible-gambling page; link to the site for full details. Telegram messages may include legs; WhatsApp messages stay short (ticket id, band, number of legs, combined odds, link) to fit approved templates.
- Delivery: outbox table with idempotent sends, retries with backoff, dead-letter state, delivery-status webhooks, rate limiting. If WhatsApp fails, fall back to Telegram or email. Verify webhook authenticity (Telegram secret token, WhatsApp X-Hub-Signature-256).
- WhatsApp templates are stored in config with category and approval status. WhatsApp bills per delivered template message, so track message counts and estimated cost. Admin, Messaging shows channel health, template status, opt-in counts, delivery failures and message counts with estimated cost.
- Tests: consent required before any send, STOP honoured, outbox idempotency, fallback, free users never receive alerts.

## N. Payments (Malawi mobile money)

- Stripe is not available for Malawi-registered businesses, so do not build on it. Define a `PaymentProvider` interface (create_payment, verify_payment, handle_webhook) with adapters: `paychangu` (first; hosted Standard Checkout for Airtel Money, TNM Mpamba and cards, and optionally its Mobile Money Direct Charge), `manual` (Admin marks a payment as received with a reference), and a stub for future providers (direct Airtel Money or TNM merchant APIs, Flutterwave). Use the provider's sandbox for all development.
- Plans and prices are set in MWK in Admin, Site content (for example monthly, quarterly, annual). Prepaid periods with no automatic recurring debit: a verified payment extends `plan_end`. Renewal reminders go out through section M. Configurable grace period, then automatic downgrade to Free.
- Flow: user picks a plan, a payment record is created (pending, unique reference), provider checkout or mobile money prompt, then webhook and a server-side verify call to the provider (never trust a redirect or webhook payload alone), mark paid idempotently, extend the subscription, issue a receipt (in-app and email). A reconciliation job re-checks pending payments after N minutes. Handle failed, cancelled and expired states. Refunds are manual by Admin with an audit-log entry.
- Security: verify webhook signatures and guard against replays; store only provider references, never card numbers or mobile money PINs; keys only from the environment; validate Malawi numbers (+265) and let the user choose the operator.
- Admin, Payments: sortable transactions table, pending and failed queue, manual grant, revenue by month. User side: Plan and billing tab with current plan, expiry date, payment history and a renew button.
- Tests: webhook idempotency and signature, a double callback does not double-extend, expiry downgrade, reconciliation, entitlement changes right after payment.

## Engineering rules

- Layout: app/{ingest,markets,settlement,models,agents,builder,tracking,backtest,auth,billing,messaging,api,web,jobs}, tests/, migrations (Alembic).
- Tests: settlement edge cases; builder invariants (every ticket in band, no duplicate legs across tickets, one leg per fixture per ticket, legs eligible and within the leg-odds window, bands filled in priority order); leakage tests (nothing with timestamp after as_of is used); API client tests with recorded fixtures; entitlement tests per role and per route; free-sample selection test (chosen by rule only, counted in the record, no other of today's tickets visible to Free); the sortable-table test; the forbidden-words test; the bold-usage test; payments and messaging tests listed above.
- Type hints, ruff, a README with setup and runbook, docs/DECISIONS.md with key choices and known limitations, docs/LAUNCH_CHECKLIST.md (see Phase 10).
- Never hardcode secrets. Respect API quota: cache aggressively.

## Config defaults (all configurable)

| Setting | Default |
|---|---|
| Fixture window | today + 5 days |
| Odds snapshot T-minus | 3 hours before kickoff, plus last moment before kickoff |
| EV threshold / conservative EV | EV > 0.03 and EV_conservative > 0 |
| Leg odds window | 1.30 to 2.20 |
| Bands | 3 / 5 / 10 / 20 / 50 |
| Max legs | none |
| Stake | 1 unit per ticket |
| Agent probability adjustment cap | +/-0.05 |
| Free weekly sample | on, Saturday |
| Registration | closed (`registration_open` = false) |
| WhatsApp | off (`whatsapp_enabled` = false) |
| Default timezone | Africa/Blantyre |

## Workflow

1. Start in plan mode. Output a short plan, the DB schema, the page map per role and open questions. Wait for my approval.
2. Build phase by phase. At the end of each phase run the tests, summarise what works and what doesn't, and stop for my review.
   - Phase 0: API spike and data-availability report, UX benchmark and page map
   - Phase 1: schema, ingestion, market registry, settlement, auth and roles, app shell (design tokens, sidebar, sortable-table component)
   - Phase 2: baseline model, overround and de-vig, EV, leg eligibility, walk-forward backtest harness
   - Phase 3: ticket builder, tracking, metrics with CIs, singles view, the user pages (Today, Fixtures, Picks, Tickets, Results, Performance) with entitlements and the free sample
   - Phase 4: LightGBM, calibration, uncertainty (p_low), scheduler
   - Phase 5: AI agents and with/without evaluation
   - Phase 6: Admin area
   - Phase 7: landing page, public record, pricing page
   - Phase 8: payments (PayChangu adapter, manual adapter, subscription lifecycle, Admin Payments)
   - Phase 9: messaging (Telegram first, then WhatsApp behind its flag, Admin Messaging)
   - Phase 10: pre-launch tracking (Admin only) and docs/LAUNCH_CHECKLIST.md covering: the launch gate results, API-Football terms for commercial odds display, any licence or registration needed to sell betting tips in Malawi and the markets targeted, PayChangu account and fees, Telegram bot, Meta verification and gambling-policy check, responsible-gambling content, terms and privacy pages. I decide when to set `registration_open`.
3. Do not skip ahead, and do not add features I haven't listed without asking first.
