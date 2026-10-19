# AccaLab: how to start

## Files

- CLAUDE.md: the full specification for Claude Code. Copy it to the repo root.
- AGENTS.md: the rules file for Codex. Copy it to the repo root (both files can live in the same repo).
- CODEX_TASKS.md: the ordered tasks to give Codex one at a time.

## Environment variables you will need

API_FOOTBALL_KEY, ANTHROPIC_API_KEY, PAYCHANGU_PUBLIC_KEY, PAYCHANGU_SECRET_KEY (sandbox first), TELEGRAM_BOT_TOKEN, SECRET_KEY (sessions), and later the WhatsApp Cloud API credentials. Keep them in .env and never commit it.

## First message for Claude Code

Open the repo in Claude Code, switch to plan mode and send:

    Read CLAUDE.md in full. Do not write code yet. Give me (1) a short plan for Phase 0 and Phase 1, (2) the proposed database schema, (3) a page map per role (Free, Pro, Admin), and (4) any questions or conflicts you found in the specification. Wait for my approval before building.

## First message for Codex

With AGENTS.md in the repo root, send Task 1 from CODEX_TASKS.md. Review the result, then send the next task.

## Outside the code (start these early)

1. Open a PayChangu merchant account, complete their identity checks, and confirm their fees and settlement terms.
2. Create the Telegram bot with BotFather.
3. Start Meta business verification for WhatsApp, and read Meta's policy on gambling-related content before building on WhatsApp.
4. Check whether selling betting tips needs a licence or registration in Malawi and in any market you will advertise to, and check API-Football's terms on showing odds in a paid product.

## What the launch gate can and cannot tell you

The gate (6 weeks, 800 settled legs, calibration error at or below 0.03, Brier no worse than the de-vigged market baseline, positive CLV with its interval shown) screens out a system that is broken. It cannot prove an edge: confirming a 3 percent edge per leg takes thousands of legs. So the landing page shows the live record with its sample size and makes no promise about future results.
