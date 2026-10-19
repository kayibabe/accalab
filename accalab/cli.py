"""Command line interface."""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from accalab.config import load_settings, validate_probe_settings
from accalab.db import make_engine, make_session_factory
from accalab.auth.security import hash_password, normalize_email
from accalab.ingest.api_football import ApiFootballClient
from accalab.ingest.daily import run_daily_ingest
from accalab.probe import render_report, run_probe, write_report
from accalab.models import AuditLog, User
from sqlalchemy import select


def main() -> int:
    parser = argparse.ArgumentParser(prog="accalab")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe-odds", help="Probe upcoming and historical API-Football odds")
    commands.add_parser("ingest-daily", help="Ingest fixtures, odds and markets for D through D+5")
    results_parser = commands.add_parser(
        "ingest-results", help="Ingest completed fixtures for recent league seasons"
    )
    results_parser.add_argument("--seasons", type=int, default=2)
    admin_parser = commands.add_parser(
        "create-admin", help="Create the first administrator account"
    )
    admin_parser.add_argument("--email", required=True)
    commands.add_parser("serve", help="Run the AccaLab web application")
    backtest_parser = commands.add_parser(
        "backtest-model", help="Run the expanding-window model quality evaluation"
    )
    backtest_parser.add_argument("--output", default="docs/PHASE2_REPORT.md")
    backtest_parser.add_argument("--minimum-training-matches", type=int)
    retrain_parser = commands.add_parser(
        "retrain-model", help="Train, evaluate and register a calibrated LightGBM candidate"
    )
    retrain_parser.add_argument("--artifact-directory")
    commands.add_parser(
        "scheduler", help="Run the configured APScheduler ingestion and retraining jobs"
    )
    agent_eval_parser = commands.add_parser(
        "evaluate-agents", help="Compare settled predictions with and without agent adjustments"
    )
    agent_eval_parser.add_argument("--output", default="docs/PHASE5_REPORT.md")
    args = parser.parse_args()

    settings = load_settings()
    if args.command == "create-admin":
        email = normalize_email(args.email)
        password = getpass.getpass("New administrator password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("Passwords do not match.", file=sys.stderr)
            return 2
        try:
            password_hash = hash_password(password)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        engine = make_engine()
        sessions = make_session_factory(engine)
        try:
            with sessions.begin() as session:
                if session.scalar(select(User.id).where(User.role == "admin")) is not None:
                    print(
                        "An administrator already exists; refusing to create another first admin.",
                        file=sys.stderr,
                    )
                    return 2
                if session.scalar(select(User.id).where(User.email == email)) is not None:
                    print("That email address is already in use.", file=sys.stderr)
                    return 2
                user = User(
                    email=email,
                    password_hash=password_hash,
                    role="admin",
                    plan="admin",
                    status="active",
                    email_verified_at=datetime.now().astimezone(),
                )
                session.add(user)
                session.flush()
                session.add(
                    AuditLog(
                        actor_user_id=user.id,
                        action="first_admin_created",
                        target_type="user",
                        target_id=str(user.id),
                        details={"email": email},
                    )
                )
            print(f"Administrator created for {email}.")
            return 0
        finally:
            engine.dispose()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("accalab.web.app:app", host="127.0.0.1", port=8000)
        return 0
    if args.command == "scheduler":
        import logging

        from accalab.scheduler import run_scheduler

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        try:
            run_scheduler()
            return 0
        except Exception as exc:
            print(f"Scheduler stopped: {exc}", file=sys.stderr)
            return 1
    if args.command == "retrain-model":
        from accalab.config import ROOT
        from accalab.modeling.stronger import retrain_and_register

        artifact_directory = Path(args.artifact_directory) if args.artifact_directory else None
        if artifact_directory is not None and not artifact_directory.is_absolute():
            artifact_directory = ROOT / artifact_directory
        engine = make_engine()
        session_factory = make_session_factory(engine)
        try:
            with session_factory.begin() as session:
                result = retrain_and_register(session, artifact_directory=artifact_directory)
            if result.bundle is None:
                print(
                    f"No model candidate trained: {result.report.get('reason', 'insufficient data')}"
                )
                return 0
            outcome = "promoted" if result.promotable else "rejected by promotion rules"
            market_metrics = ", ".join(
                f"{market} {values['baseline_log_loss']:.4f} → {values['candidate_log_loss']:.4f}"
                for market, values in result.report["markets"].items()
            )
            print(f"Model {result.bundle.version} {outcome}; holdout log loss: {market_metrics}.")
            return 0
        except Exception as exc:
            print(f"Model retraining failed: {exc}", file=sys.stderr)
            return 1
        finally:
            engine.dispose()
    if args.command == "evaluate-agents":
        from accalab.config import ROOT
        from accalab.agents.evaluation import load_agent_evaluation, render_agent_evaluation

        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = ROOT / output_path
        engine = make_engine()
        session_factory = make_session_factory(engine)
        try:
            with session_factory() as session:
                report = load_agent_evaluation(session)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(render_agent_evaluation(report), encoding="utf-8")
            print(
                f"Wrote paired agent evaluation for {report['sample_size']} settled selections "
                f"to {output_path}."
            )
            return 0
        except Exception as exc:
            print(f"Agent evaluation failed: {exc}", file=sys.stderr)
            return 1
        finally:
            engine.dispose()
    if args.command == "backtest-model":
        from accalab.config import ROOT
        from accalab.modeling.backtest import run_walk_forward_backtest, write_backtest_report
        from accalab.modeling.config import load_model_config
        from accalab.modeling.dixon_coles import MatchResult
        from accalab.models import Fixture

        engine = make_engine()
        session_factory = make_session_factory(engine)
        try:
            with session_factory() as session:
                fixtures = session.execute(
                    select(
                        Fixture.id,
                        Fixture.league_id,
                        Fixture.kickoff_at,
                        Fixture.home_team_id,
                        Fixture.away_team_id,
                        Fixture.home_goals,
                        Fixture.away_goals,
                        Fixture.season,
                    )
                    .where(
                        Fixture.home_goals.is_not(None),
                        Fixture.away_goals.is_not(None),
                        Fixture.status.in_(("FT", "AET", "PEN")),
                    )
                    .order_by(Fixture.league_id, Fixture.kickoff_at, Fixture.id)
                ).all()
            matches = [
                MatchResult(
                    fixture_id=row.id,
                    league_id=row.league_id,
                    kickoff_at=row.kickoff_at,
                    home_team_id=row.home_team_id,
                    away_team_id=row.away_team_id,
                    home_goals=row.home_goals,
                    away_goals=row.away_goals,
                    season=row.season,
                )
                for row in fixtures
            ]
            config = load_model_config()
            report = run_walk_forward_backtest(
                matches,
                config=config,
                minimum_training_matches=args.minimum_training_matches,
            )
            output_path = Path(args.output)
            if not output_path.is_absolute():
                output_path = ROOT / output_path
            write_backtest_report(output_path, report, config=config)
            print(
                f"Wrote model-quality evaluation for {len(report.league_metrics)} leagues "
                f"and {len(report.forecasts)} forecasts to {output_path}."
            )
            return 0
        except Exception as exc:
            print(f"Model evaluation failed: {exc}", file=sys.stderr)
            return 1
        finally:
            engine.dispose()
    if args.command == "ingest-results":
        from accalab.ingest.results import run_results_ingest
        from accalab.tracking.settlement import settle_completed_tickets

        try:
            validate_probe_settings(settings)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        engine = make_engine()
        session_factory = make_session_factory(engine)
        client = ApiFootballClient(
            api_key=settings.api_key or "",
            base_url=settings.base_url,
            cache_path=settings.cache_path,
            cache_ttl_seconds=settings.cache_ttl_seconds,
            timeout_seconds=settings.timeout_seconds,
            retries=settings.retries,
            backoff_seconds=settings.backoff_seconds,
            requests_per_minute=settings.requests_per_minute,
            daily_quota_limit=settings.daily_quota_limit,
        )
        try:
            with session_factory() as session:
                summary = run_results_ingest(
                    client, session, settings, seasons_per_league=args.seasons
                )
                settlement = settle_completed_tickets(session)
                session.commit()
            print(
                f"Ingested {summary.fixtures_stored} completed results across "
                f"{summary.completed_pairs} league-seasons ({summary.skipped_pairs} already complete); "
                f"{summary.requests_used} requests, {summary.cache_hits} cache hits, "
                f"{summary.quota_remaining} provider requests remaining. "
                f"Settled {settlement['tickets_settled']} tickets with "
                f"{settlement['new_results']} new leg results and "
                f"{settlement['corrections']} corrections."
            )
            return 0
        except Exception as exc:
            print(f"Results ingestion stopped: {exc}", file=sys.stderr)
            return 1
        finally:
            client.close()
            engine.dispose()
    if args.command == "probe-odds":
        try:
            validate_probe_settings(settings)
        except ValueError as exc:
            missing = []
            if not settings.api_key:
                missing.append("API_FOOTBALL_KEY is missing")
            message = f"Live probe not run: {'; '.join(missing) if missing else exc}"
            today = datetime.now(ZoneInfo(settings.timezone)).date()
            probe_window = {
                "run_date": today.isoformat(),
                "future_window": [
                    today.isoformat(),
                    (today + timedelta(days=settings.future_days)).isoformat(),
                ],
                "requests_used_this_run": 0,
                "requests_used_end": 0,
                "configured_limit": settings.daily_quota_limit,
                "provider_remaining": None,
                "provider_limit": None,
            }
            write_report(settings, render_report(probe_window, note=message))
            print(f"{message}\nReport written to {settings.report_path}", file=sys.stderr)
            return 2
        client = ApiFootballClient(
            api_key=settings.api_key or "",
            base_url=settings.base_url,
            cache_path=settings.cache_path,
            cache_ttl_seconds=settings.cache_ttl_seconds,
            timeout_seconds=settings.timeout_seconds,
            retries=settings.retries,
            backoff_seconds=settings.backoff_seconds,
            requests_per_minute=settings.requests_per_minute,
            daily_quota_limit=settings.daily_quota_limit,
        )
        try:
            result = run_probe(client, settings)
            result["configured_limit"] = settings.daily_quota_limit
            report = render_report(result)
            write_report(settings, report)
            print(f"Report written to {settings.report_path}")
            if result["errors"]:
                print("Probe stopped early; see report for the error.", file=sys.stderr)
                return 1
            return 0
        finally:
            client.close()
    if args.command == "ingest-daily":
        try:
            validate_probe_settings(settings)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        engine = make_engine()
        session_factory = make_session_factory(engine)
        client = ApiFootballClient(
            api_key=settings.api_key or "",
            base_url=settings.base_url,
            cache_path=settings.cache_path,
            cache_ttl_seconds=settings.cache_ttl_seconds,
            timeout_seconds=settings.timeout_seconds,
            retries=settings.retries,
            backoff_seconds=settings.backoff_seconds,
            requests_per_minute=settings.requests_per_minute,
            daily_quota_limit=settings.daily_quota_limit,
        )
        try:
            with session_factory() as session:
                summary = run_daily_ingest(client, session, settings)
            print(
                f"Ingested {summary.fixture_count} fixtures, {summary.odds_count} odds snapshots, "
                f"{summary.market_count} markets, and {summary.context_snapshots} context snapshots "
                f"across {summary.league_count} leagues; "
                f"{summary.requests_used} API requests ({summary.cache_hits} cache hits)."
            )
            if summary.enrichment_skipped:
                print(
                    f"Quota budget deferred {summary.enrichment_skipped} optional request groups."
                )
            return 0
        except Exception as exc:
            print(f"Daily ingest failed: {exc}", file=sys.stderr)
            return 1
        finally:
            client.close()
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
