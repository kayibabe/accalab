# AccaLab: Codex task list

Give Codex one task at a time, in order. AGENTS.md must be in the repo root. Each task ends the same way: run `pytest` and `ruff check`, summarise what works and what does not, and stop. Review before sending the next task.

## Task 1 (Phase 0a): API probe

Create the project skeleton, config, and an API-Football client with rate limiting, retries, caching and quota tracking. Add a CLI command `accalab probe-odds` that, across all current leagues by default (with an optional configured filter), reports: (a) which markets and bookmakers the odds endpoint returns for fixtures up to 5 days ahead, (b) whether odds are returned for fixtures that finished 1, 7, 30 and 90 days ago, (c) requests used. Write the findings to docs/PHASE0_REPORT.md, including a note on API-Football's terms for displaying odds in a paid product. Build nothing else.

## Task 2 (Phase 0b): UX benchmark

Review 5 to 6 top apps in this domain per AGENTS.md and write docs/UX_BENCHMARK.md (adopt, avoid, differentiate) plus a page map per role (Free, Pro, Admin). No code.

## Task 3 (Phase 1a): Data foundation

SQLAlchemy schema (fixtures, teams, odds_snapshots, markets_registry with supported and ticket_eligible flags, predictions, tickets, ticket_legs, results, agent_logs, users, audit_log), the Alembic migration, the daily ingestion job, the canonical market registry from /odds/bets, and the settlement engine with unit tests for every supported market, including Asian handicap quarter lines.

## Task 4 (Phase 1b): Auth, roles and app shell

Auth and roles (admin, pro, free), the entitlement config with service-layer enforcement and per-role, per-route tests, and the app shell: design tokens, Windows 11 type ramp, sidebar by role, the shared sortable-table component, and the tests for sortable headers, forbidden words and bold usage. One placeholder page per sidebar header. A CLI command to create the first admin.

## Task 5 (Phase 2): Model, value and eligibility

Dixon-Coles with time decay, scoreline-matrix-to-markets derivation, de-vig with per-market overround, EV and EV_conservative, the leg-eligibility filter (market allowlist, leg-odds window, overround-scaled threshold), and the walk-forward backtest harness with a leakage test. Report log loss and calibration per league.

## Task 6 (Phase 3a): Ticket builder

Priority-order CP-SAT fill T1 to T5, one leg per fixture per ticket, global leg uniqueness, day-type band configs, provisional versus official runs, per-day pool diagnostics, and a "no ticket" result with a reason when a band is infeasible. Property-based tests: every ticket within its band, no duplicate legs across tickets, one leg per fixture per ticket, all legs eligible and within the leg-odds window, bands filled in priority order.

## Task 7 (Phase 3b): Tracking and user pages

Ticket settlement, metrics views (day, month and year by band, tier and aggregate, with bootstrap CIs and sample-size notes), leg-level metrics, the singles view, and the user pages (Today, Fixtures, Picks, Tickets, Results, Performance) with entitlement gating, locked cards, and the free weekly sample. Tests: sample chosen by rule only, counted in the record, no other of today's tickets visible to Free.

## Task 8 (Phase 4): Stronger model and scheduler

LightGBM models for 1X2, O/U and BTTS, isotonic or Platt calibration on a time-ordered holdout, blending with the baseline by walk-forward log loss, p_low uncertainty, model versioning and the weekly retrain job with promotion rules. Wire up the APScheduler jobs and a CLI command per job.

## Task 9 (Phase 5): AI agents

DataQuality, NewsContext, Market and Critic agents with the Anthropic SDK, pydantic-validated JSON, full call logging with cost, bounded adjustments (+/-0.05) and vetoes with reasons, per-agent toggles, and the with-versus-without evaluation (Brier, log loss, CLV). Generate the per-selection "why this pick" text.

## Task 10 (Phase 6): Admin area

The Administration group: Users and plans, Data and ingestion (quota, snapshots, coverage), Markets registry, Models and calibration, Agents, Backtests and pre-launch tracking, Builder and thresholds, Jobs and logs, Audit log, Site content (landing copy, plans and prices). All tables use the shared sortable component. Admin only.

## Task 11 (Phase 7): Landing and public pages

Landing page, /pricing, /how-it-works, /record, /responsible-gambling, /terms and /privacy per AGENTS.md. The Free versus Pro table and plan prices are generated from the entitlement and plan config. Waitlist form when `registration_open` is false. Tests for forbidden words and no guaranteed-profit language.

## Task 12 (Phase 8): Payments

PaymentProvider interface, the paychangu and manual adapters, payments and subscriptions tables, the Plan and billing page, Admin, Payments, the reconciliation job and the expiry and grace-period downgrade. Use the PayChangu sandbox and recorded fixtures. Tests: signature verification, webhook idempotency, a double callback does not double-extend, expiry downgrade, entitlement changes right after payment.

## Task 13 (Phase 9a): Telegram messaging

MessageChannel interface, the outbox, consent records, STOP handling, the Telegram adapter, the Account, Notifications tab, and the events (tickets published, nightly results, expiry reminders). Tests: consent required before sending, STOP honoured, outbox idempotency, free users never receive alerts.

## Task 14 (Phase 9b): WhatsApp messaging

The WhatsApp adapter behind `whatsapp_enabled` (leave it off), template config with category and approval status, short template payloads, delivery webhooks, fallback to Telegram or email, and Admin, Messaging with message counts and estimated cost.

## Task 15 (Phase 10): Pre-launch and launch checklist

Build the Admin pre-launch report (leg-level calibration, CLV with CI, claimed versus realised EV, Brier against the de-vigged market baseline, weeks run, settled legs) with the launch gate from AGENTS.md, and write docs/LAUNCH_CHECKLIST.md covering the items listed in the Phase 10 workflow of CLAUDE.md. Do not change `registration_open`.
