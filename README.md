# AccaLab

AccaLab is being built in scoped phases. Phase 0 provides an API-Football coverage probe. Phase 1a adds the core data schema, the daily fixture/odds ingestion command, the market registry, and settlement rules. Phase 1b adds sign-in, role-based page access, and the server-rendered app shell.
The daily ingestion captures current-season injuries, fixture lineups/statistics, standings and team-season statistics when the provider marks coverage available. Optional enrichment is bounded by `ACCALAB_DAILY_ENRICHMENT_BUDGET` (default 1,000 requests) and leaves a 5%/100-request reserve; skipped request groups are reported.

## Setup

Use Python 3.11 or later. Install the project and development tools with:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Set `API_FOOTBALL_KEY` and a random `SECRET_KEY` in `.env`. Leave `API_FOOTBALL_LEAGUE_IDS` blank to use all current leagues, or set comma-separated IDs to restrict ingestion. Create the schema and start the daily pull with:

```powershell
alembic upgrade head
accalab ingest-daily
```

Create the first administrator and run the local app with:

```powershell
accalab create-admin --email admin@example.com
accalab serve
```

The administrator password is requested without echo and is never accepted as a command-line argument. Open `http://127.0.0.1:8000/login`. Sign-in uses Argon2id, signed HttpOnly/SameSite session cookies, CSRF form tokens, and a persistent failed-login throttle. Email verification and password reset use expiring, single-use tokens stored only as hashes; email delivery will connect to the messaging adapter in its later phase.

The ingestion command stores fixtures from today through five days ahead, returned odds as append-only point-in-time snapshots, and the `/odds/bets` market catalogue. Where current-season API coverage is available, it also captures injuries, fixture lineups/statistics, standings, and team-season statistics. Optional enrichment requests are bounded by `ACCALAB_DAILY_ENRICHMENT_BUDGET` (default 1,000 requests) and preserve a 5%/100-request quota reserve; skipped request groups are reported. API calls use the existing rate limiter, retries, cache and quota guard. Set `DATABASE_URL` to use a different SQLAlchemy-supported database; SQLite defaults to `data/accalab.sqlite3` with WAL and foreign keys enabled.

Run the API odds probe separately with:

```powershell
accalab probe-odds
```

Probe responses are cached in `data/api_cache.sqlite3`; `/status` synchronizes the local request guard with the account's actual daily quota before the probe continues. Request counts and provider quota headers are reported in the generated report. API and ingestion settings are in `config/settings.toml`.
