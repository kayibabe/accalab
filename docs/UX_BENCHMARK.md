# AccaLab UX benchmark and role page map

**Reviewed:** 2026-10-19  
**Scope:** Publicly available product pages and feature descriptions. This is a pattern review, not a feature-by-feature usability test. Competitor coverage and feature descriptions below are their own published claims; availability can differ by country, device, account, and subscription.

## Products reviewed

| Product | Current positioning observed | Useful pattern for AccaLab |
| --- | --- | --- |
| [Forebet](https://www.forebet.com/) | Free mathematical football predictions, probabilities, match analytics and statistics across a large set of leagues. | Put the forecast beside the fixture and make the predicted outcome legible at a glance. Keep the supporting trends available at match level. |
| [FootyStats](https://footystats.org/) | League, team, match and head-to-head statistics, search/filter tools, predictions and paid features. The site describes coverage of 1,500+ leagues and CSV export. | Let users move from a date or league to a specific match, and make the underlying statistics explorable. Separate broader analytics and export tools by plan. |
| [WinDrawWin](https://www.windrawwin.com/us/picks/) | Daily and date-based football predictions, league pages, match statistics and accumulator/parlay content. It describes coverage of 140+ leagues. | Make Today, Tomorrow, upcoming and past prediction views easy to distinguish; expose league entry points and show the record next to predictions. |
| [SofaScore](https://www.sofascore.com/) | Live scores, fixtures, results, league tables, match and player statistics, highlights and comparison tools across many competitions. | Use consistent match-centred navigation and progressive detail: score/status first, then teams, match statistics and deeper comparisons. |
| [FotMob](https://www.fotmob.com/download) | Football scores, fixtures, news, alerts, xG and shot maps, commentary and other match details. Its current download page describes coverage of 500+ leagues. | Make follow/favourite controls and timely notifications easy to understand; keep live status and match context close together. |
| [Action Network](https://www.actionnetwork.com/app) | Betting odds, expert picks, line alerts, PRO tools and personal bet tracking. Its app page describes a personalized analytics dashboard with projected win probability. | Treat tracking as a first-class workflow and show a user's record as a coherent history, with odds and analysis attached to each entry. |

These products are adjacent references, not equivalent services. Some emphasize live-score utility, some prediction content, and some betting-market tools. Their league counts, predictions, odds, and marketing claims are not evidence of AccaLab model quality or coverage.

## Adopt, avoid, differentiate

### Adopt

- **Start from the fixture and date.** Give users a compact path from a day or league to a fixture, then reveal the analysis in the fixture context. Keep kickoff time and timezone explicit.
- **Use progressive detail.** Put the key status and a small number of decision-relevant facts first. Put market-by-market detail, data quality, model version, and source timestamps in a details view.
- **Make filters and saved preferences practical.** Date, league, team, market, and status filters should reduce scanning rather than add a dashboard of controls. Preserve preferences where the account supports them.
- **Keep history attached to the published item.** A ticket or selection should lead to its original odds, as-of time, outcome, and later closing-price comparison. Show both a summary and a route to the underlying entries.
- **Use notifications as a user-controlled convenience.** Allow per-event and per-band choices, consent, quiet hours, and a clear way to stop messages.
- **Explain plan limits before a locked interaction.** Locked cards should state what is available on the current plan and what a paid plan adds, without exposing restricted selections or values.

### Avoid

- **Do not imitate competitor branding, layouts, assets, copy, or proprietary data presentation.** Borrow only general interaction patterns.
- **Do not make confidence or profit language stand in for evidence.** A prediction, trend, or positive historical ROI is not a guarantee. Avoid unqualified "sure win", "risk-free", or guaranteed-profit claims.
- **Do not present a probability without its meaning and context.** Distinguish model probability from bookmaker odds, show when both were captured, and avoid implying that a high probability alone makes a selection valuable.
- **Do not mix previews into the settled record.** Preview and official tickets need distinct labels and separate performance treatment. Never display backtest results as live results.
- **Do not bury the result in promotional modules, dense trend lists, or an unfiltered all-market table.** Default to the next useful decision; provide deeper material on demand.
- **Do not assume odds or betting links are available or appropriate in every jurisdiction.** Keep AccaLab analysis and tracking separate from bet placement, and respect data-display rights and local requirements.
- **Do not turn a wide league catalogue into an implied coverage promise.** Show actual data/model coverage and missing inputs by competition; league availability does not mean every market is supported or ticket-eligible.

### Differentiate AccaLab

- **Evidence attached to each decision.** For every eligible selection show the market and line, calibrated probability, conservative probability bound, decimal odds, expected value, overround context, odds as-of time, model version, and a short reason. Surface data-quality exclusions clearly.
- **One reproducible pipeline from single to ticket.** The same point-in-time inputs and eligibility rules feed the flat-single view and the accumulator builder. Users can inspect why a leg qualified and why a band did or did not produce a ticket.
- **A clean boundary between Preview, Official, Backtest, and Live record.** Label each state in the interface and exclude previews and simulations from live performance metrics.
- **Performance that shows its uncertainty.** Put sample size and a bootstrap 95% interval beside every ROI figure; include calibration, Brier score, claimed versus realised EV, and closing-line value in the detailed performance view.
- **No forced accumulator.** When the eligible pool cannot fill a band, say so and give the reason. Empty days are valid outcomes and should not be hidden by filler picks.
- **Global league access with honest per-league coverage.** Ingestion can include all current leagues by default, while the interface reports where odds, statistics, history, or model validation are sparse. Do not present "all leagues" as "equal evidence in all leagues."
- **Designed for analysis, not bet execution.** Provide no bet-placement action. Make responsible-gambling information visible and keep performance language measured.

## Page map by role

Public routes are available without signing in. Account-only navigation follows the product specification: **Today, Fixtures, Picks, Tickets, Results, Performance, Learn, Account**. The server enforces the same entitlements as the UI; hiding a link is not access control.

| Role | Page | Main question answered | Access and key contents |
| --- | --- | --- | --- |
| Public | `/` | What is AccaLab and what evidence is available? | Landing page in the specified order: promise, real example ticket, three-step explanation, live-record snapshot with start date and sample/confidence note, entitlement comparison, pricing, FAQ, responsible-gambling notice, and clear account actions. Use a settled ticket from yesterday if no Free sample is published. |
| Public | `/pricing` | What does each plan include and cost? | Generate plan rows and MWK prices from the entitlement and plan configuration; explain Free and Pro limits plainly. |
| Public | `/how-it-works` | How does a fixture become a tracked result? | Explain data capture, model probabilities, eligibility, ticket construction, and settlement without promising an edge. |
| Public | `/record` | What has actually settled? | Live record with the same sample sizes, confidence intervals, date range, and Preview/Official separation used inside the product. |
| Public | `/register`, `/login` | Can I create an account or sign in? | Registration follows `registration_open`; otherwise show the waitlist form. Include verification and reset flows when enabled. |
| Public | `/responsible-gambling`, `/terms`, `/privacy` | What are the boundaries and terms? | Responsible-gambling information, terms and privacy content. Include 18+ and past-performance disclosures where required by the specification. |
| Free | Today | What is available today? | Today's fixtures without analysis. Today's tickets appear only as locked cards showing band, leg count, combined-odds range and status; no legs, probabilities, EV, or pick explanations. Reveal only the rule-selected weekly T1 Free sample when available, without probabilities, EV, or why-this-pick. |
| Free | Fixtures | Which fixtures are on today's schedule? | Today's fixture list and basic fixture details; no analysis content. |
| Free | Picks | What is included in Picks? | Locked Pro page/state; no selection content or values leak through the UI or API. |
| Free | Tickets | What tickets have settled? | Settled tickets from one to three days ago across all bands, including legs and results, subject to the weekly sample rule and its label. Today's tickets remain locked cards except the sample. |
| Free | Results | What happened to recent tickets? | Settled ticket results from one to three days ago with legs and outcomes. |
| Free | Performance | What is the recent headline record? | Aggregate 30-day headline performance with sample size. Do not expose per-pick probabilities, EV, explanations, alerts, or export. |
| Free | Learn | What do the terms mean? | Learning pages and glossary content. |
| Free | Account | How do I manage my account? | Profile, Plan and billing, Notifications, and Display tabs, with Free entitlements enforced. |
| Pro | Today | What is published for today? | Official tickets and clearly separated Preview tickets; leg details and the as-of context are available. |
| Pro | Fixtures | What fixtures have data? | Fixture list and detailed match context, with data coverage and timestamps visible. |
| Pro | Picks | Which individual selections qualify? | Eligible picks and detailed probabilities, odds, EV, conservative EV, overround, data-quality status, and why-this-pick. Show exclusions with reasons where useful. |
| Pro | Tickets | Which accumulator tickets were built? | All bands, legs, odds and status; distinguish Preview from Official and show band/pool diagnostics without implying guaranteed outcomes. |
| Pro | Results | How did selections and tickets settle? | Full ticket and leg history, results, void/push treatment, and closing-price comparison. |
| Pro | Performance | How has the record behaved over time? | Full history; daily, monthly, yearly, band/tier and aggregate views; ROI with sample size and bootstrap CI; calibration, Brier, claimed vs realised EV, CLV, and separate flat-singles view. |
| Pro | Learn | How should I interpret the evidence? | Glossary, model/market explanations, and clear limits of historical and simulated results. |
| Pro | Account | How do I manage my subscription and alerts? | Profile, Plan and billing, Notifications, and Display; consent, channel settings, quiet hours, and account text-size control. |
| Admin | Administration: Users and plans | Who has access and on what plan? | Search and manage users, plans, status, and plan dates; record administrative actions in the audit log. |
| Admin | Administration: Data and ingestion | Is the input data usable and current? | Ingestion jobs, API quota, snapshots, source timestamps, fixture/odds/statistics coverage by league, and failure reasons. |
| Admin | Administration: Markets registry | Which markets can the system settle and publish? | Canonical market list, source bookmaker labels, supported and ticket-eligible flags, and registry provenance. |
| Admin | Administration: Models and calibration | Are model outputs calibrated? | Model versions, per-league/market validation, calibration, Brier and baseline comparisons, with sample sizes. |
| Admin | Administration: Agents | What did each analysis agent contribute? | Agent toggles, bounded changes/vetoes, structured output, call logs, reasons and cost. |
| Admin | Administration: Backtests and pre-launch tracking | What does simulated and pre-launch evidence show? | Walk-forward backtests clearly labelled simulated and isolated from live results; prospective tracking and launch-gate metrics with their sample sizes and intervals. |
| Admin | Administration: Builder and thresholds | Why was a band filled or left empty? | Day-type settings, thresholds, eligible pool diagnostics, per-band status and reasons. Changes are auditable. |
| Admin | Administration: Messaging | Are messages consented to and delivered? | Channel health, opt-ins, STOP state, templates, outbox failures, delivery status and estimated WhatsApp cost. |
| Admin | Administration: Payments | Are payments reconciled? | Provider and sandbox status, pending/stale payments, references, receipts and manual-refund audit entries; never display payment secrets. |
| Admin | Administration: Jobs and logs | Are scheduled tasks healthy? | Job status, run times, errors and relevant operational logs. |
| Admin | Administration: Audit log | What administrative changes occurred? | Searchable append-only view of actor, action, target, timestamp and outcome. |
| Admin | Administration: Site content | What public copy and pricing are configured? | Editable landing copy, FAQs, plan text and configured prices with change audit. |

## Shared interaction rules

- A displayed price or prediction carries an `as_of` timestamp. Show the user's timezone (default Africa/Blantyre) and make stale or missing data visible.
- Every table uses the shared sortable-table pattern, with keyboard operation, visible sort state, stable sorting and appropriate numeric/date/odds handling. Keep default views to six or seven columns and move secondary fields into a Details drawer.
- Keep body and table text readable at desktop and mobile sizes, provide visible focus, and support light/dark themes and the Account text-size control.
- Apply role checks in the service layer and test direct URL/API access for each role. Locked UI is informative but never substitutes for authorization.
- Keep the official T1 Free sample rule-driven, selected before results, labelled "Free sample", recorded in performance, and stripped of probabilities, EV and why-this-pick.
- Display result samples and uncertainty with the metric. Historical performance describes observed outcomes; it does not guarantee future outcomes.

## Sources

- [Forebet home and product description](https://www.forebet.com/)
- [FootyStats home, search, coverage and export description](https://footystats.org/) and [FootyStats Premium](https://footystats.org/premium)
- [WinDrawWin soccer picks and league coverage](https://www.windrawwin.com/us/picks/)
- [SofaScore football live scores and product description](https://www.sofascore.com/)
- [FotMob app and coverage description](https://www.fotmob.com/download)
- [Action Network app features](https://www.actionnetwork.com/app) and [Action Network odds and tracking FAQ](https://www.actionnetwork.com/general/faq)
