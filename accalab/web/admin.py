"""Read-only, bounded data contexts for the Administration pages."""

from __future__ import annotations

import sqlite3
import tomllib
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from accalab.config import ROOT, load_settings
from accalab.entitlements import ADMIN_SECTIONS, ROLE_ENTITLEMENTS
from accalab.models import (
    AgentLog,
    AuditLog,
    DataSnapshot,
    Fixture,
    MarketRegistry,
    MessageConsent,
    MessageOutbox,
    ModelVersion,
    OddsSnapshot,
    Payment,
    Prediction,
    User,
)
from accalab.tracking.metrics import build_prelaunch_report
from accalab.web.public import load_site_content


def _count(session: Session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _when(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _rows(session: Session, statement) -> list[dict]:
    return [dict(row) for row in session.execute(statement).mappings().all()]


def _read_toml(name: str) -> dict:
    path = ROOT / "config" / name
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _quota() -> tuple[int, int]:
    """Read only the local quota ledger; do not initialize a provider client."""
    settings = load_settings()
    if not settings.cache_path.exists():
        return 0, settings.daily_quota_limit
    try:
        with sqlite3.connect(settings.cache_path) as db:
            row = db.execute(
                "SELECT requests_used FROM quota_usage WHERE usage_date = ?",
                (datetime.now(UTC).date().isoformat(),),
            ).fetchone()
        return (int(row[0]) if row else 0), settings.daily_quota_limit
    except sqlite3.Error:
        return 0, settings.daily_quota_limit


def _admin_data(session: Session, key: str) -> dict:
    if key == "users-and-plans":
        rows = _rows(
            session,
            select(
                User.email.label("email"),
                User.role.label("role"),
                User.plan.label("plan"),
                User.status.label("status"),
                User.plan_end.label("plan_end_sort"),
                User.created_at.label("created_sort"),
            )
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(200),
        )
        for row in rows:
            row["plan_end"] = _when(row.pop("plan_end_sort"))
            row["plan_end_sort"] = row["plan_end"]
            row["created"] = _when(row.pop("created_sort"))
            row["created_sort"] = row["created"]
        return {
            "cards": [
                ("Accounts", _count(session, User)),
                (
                    "Active",
                    session.scalar(
                        select(func.count()).select_from(User).where(User.status == "active")
                    )
                    or 0,
                ),
                (
                    "Verified",
                    session.scalar(
                        select(func.count())
                        .select_from(User)
                        .where(User.email_verified_at.is_not(None))
                    )
                    or 0,
                ),
            ],
            "columns": [
                ("email", "Email", "text"),
                ("role", "Role", "text"),
                ("plan", "Plan", "text"),
                ("status", "Status", "text"),
                ("plan_end", "Plan ends", "date"),
                ("created", "Created", "date"),
            ],
            "rows": rows,
            "note": "Account and plan changes are not available in this view.",
        }

    if key == "data-and-ingestion":
        used, limit = _quota()
        snapshots = _rows(
            session,
            select(
                DataSnapshot.data_type.label("type"),
                DataSnapshot.scope_type.label("scope"),
                DataSnapshot.scope_key.label("key"),
                DataSnapshot.source.label("source"),
                DataSnapshot.as_of.label("as_of_sort"),
            )
            .order_by(DataSnapshot.as_of.desc(), DataSnapshot.id.desc())
            .limit(100),
        )
        for row in snapshots:
            row["as_of"] = _when(row.pop("as_of_sort"))
            row["as_of_sort"] = row["as_of"]
        return {
            "cards": [
                ("API requests today", f"{used} / {limit}"),
                ("Fixtures", _count(session, Fixture)),
                ("Odds snapshots", _count(session, OddsSnapshot)),
                ("Data snapshots", _count(session, DataSnapshot)),
            ],
            "columns": [
                ("type", "Data type", "text"),
                ("scope", "Scope", "text"),
                ("key", "Scope key", "text"),
                ("source", "Source", "text"),
                ("as_of", "As of", "date"),
            ],
            "rows": snapshots,
            "note": "Quota is read from the local API client ledger. Snapshot coverage is represented by the rows below.",
        }

    if key == "markets-registry":
        rows = _rows(
            session,
            select(
                MarketRegistry.canonical_name.label("market"),
                MarketRegistry.display_name.label("display"),
                MarketRegistry.supported.label("supported"),
                MarketRegistry.ticket_eligible.label("eligible"),
                MarketRegistry.updated_at.label("updated_sort"),
            )
            .order_by(MarketRegistry.canonical_name, MarketRegistry.display_name)
            .limit(300),
        )
        for row in rows:
            row["supported"] = "Yes" if row["supported"] else "No"
            row["eligible"] = "Yes" if row["eligible"] else "No"
            row["updated"] = _when(row.pop("updated_sort"))
            row["updated_sort"] = row["updated"]
        supported = (
            session.scalar(
                select(func.count())
                .select_from(MarketRegistry)
                .where(MarketRegistry.supported.is_(True))
            )
            or 0
        )
        eligible = (
            session.scalar(
                select(func.count())
                .select_from(MarketRegistry)
                .where(MarketRegistry.ticket_eligible.is_(True))
            )
            or 0
        )
        return {
            "cards": [
                ("Markets", _count(session, MarketRegistry)),
                ("Settleable", supported),
                ("Ticket eligible", eligible),
            ],
            "columns": [
                ("market", "Canonical market", "text"),
                ("display", "Provider label", "text"),
                ("supported", "Settleable", "text"),
                ("eligible", "Ticket eligible", "text"),
                ("updated", "Updated", "date"),
            ],
            "rows": rows,
            "note": "Registry changes are intentionally not exposed here.",
        }

    if key == "models-and-calibration":
        rows = _rows(
            session,
            select(
                ModelVersion.id.label("version"),
                ModelVersion.status.label("status"),
                ModelVersion.feature_version.label("features"),
                ModelVersion.trained_through.label("trained_sort"),
                ModelVersion.created_at.label("created_sort"),
            )
            .order_by(ModelVersion.created_at.desc())
            .limit(100),
        )
        for row in rows:
            row["trained"] = _when(row.pop("trained_sort"))
            row["trained_sort"] = row["trained"]
            row["created"] = _when(row.pop("created_sort"))
            row["created_sort"] = row["created"]
        promoted = (
            session.scalar(
                select(func.count())
                .select_from(ModelVersion)
                .where(ModelVersion.status == "promoted")
            )
            or 0
        )
        return {
            "cards": [
                ("Model versions", _count(session, ModelVersion)),
                ("Promoted", promoted),
                ("Predictions", _count(session, Prediction)),
            ],
            "columns": [
                ("version", "Version", "text"),
                ("status", "Status", "text"),
                ("features", "Features", "text"),
                ("trained", "Trained through", "date"),
                ("created", "Created", "date"),
            ],
            "rows": rows,
            "note": "Calibration evidence and promotion metrics are stored with each model version; detailed model evaluation remains in the model reports.",
        }

    if key == "agents":
        rows = _rows(
            session,
            select(
                AgentLog.agent_name.label("agent"),
                AgentLog.status.label("status"),
                AgentLog.model_id.label("model"),
                AgentLog.cost_usd.label("cost_sort"),
                AgentLog.as_of.label("as_of_sort"),
            )
            .order_by(AgentLog.as_of.desc(), AgentLog.id.desc())
            .limit(100),
        )
        for row in rows:
            row["cost"] = f"${float(row.pop('cost_sort') or 0):.4f}"
            row["cost_sort"] = row["cost"].replace("$", "")
            row["as_of"] = _when(row.pop("as_of_sort"))
            row["as_of_sort"] = row["as_of"]
        cfg = _read_toml("agents.toml").get("agents", {})
        enabled = bool(cfg.get("enabled", False))
        return {
            "cards": [
                ("Logged calls", _count(session, AgentLog)),
                ("Agents enabled", "Yes" if enabled else "No"),
                (
                    "Logged spend",
                    f"${float(session.scalar(select(func.coalesce(func.sum(AgentLog.cost_usd), 0))) or 0):.4f}",
                ),
            ],
            "columns": [
                ("agent", "Agent", "text"),
                ("status", "Status", "text"),
                ("model", "Model", "text"),
                ("cost", "Cost", "numeric"),
                ("as_of", "As of", "date"),
            ],
            "rows": rows,
            "note": "Call payloads and provider errors are withheld from this summary table.",
        }

    if key == "backtests-and-pre-launch-tracking":
        report = build_prelaunch_report(session)
        start = report["tracking_start"]
        report["tracking_start_display"] = _when(start)
        report["brier_display"] = f"{report['brier']:.4f}" if report["brier"] is not None else "—"
        report["market_brier_display"] = (
            f"{report['market_brier']:.4f}" if report["market_brier"] is not None else "—"
        )
        report["ece_display"] = f"{report['ece']:.3f}" if report["ece"] is not None else "—"
        report["claimed_ev_display"] = (
            f"{report['claimed_ev']:.2%}" if report["claimed_ev"] is not None else "—"
        )
        report["realised_ev_display"] = (
            f"{report['realised_ev']:.2%}" if report["realised_ev"] is not None else "—"
        )
        report["ev_gap_display"] = (
            f"{report['realised_ev'] - report['claimed_ev']:+.2%}"
            if report["claimed_ev"] is not None and report["realised_ev"] is not None
            else "—"
        )
        report["clv_display"] = (
            f"{report['mean_clv']:.2%} · 95% CI {report['clv_ci_low']:.2%} to {report['clv_ci_high']:.2%}"
            if report["mean_clv"] is not None and report["clv_ci_low"] is not None
            else "—"
        )
        return {
            "cards": [
                ("Tracking began", report["tracking_start_display"]),
                ("Weeks run", f"{report['weeks']:.1f}"),
                ("Settled eligible legs", report["settled_legs"]),
                ("Launch gate", report["gate_status"]),
            ],
            "columns": [
                ("criterion", "Gate criterion", "text"),
                ("measured", "Current evidence", "text"),
                ("requirement", "Requirement", "text"),
                ("status", "Status", "text"),
            ],
            "rows": report["gate_rows"],
            "prelaunch": report,
            "note": (
                "This proposed gate screens out a broken system; it cannot prove an edge, which needs thousands of legs. "
                "Eligible legs are official single selections with their latest settlement correction. The market baseline "
                "uses the selected bookmaker's latest pre-kickoff multi-selection quote set and proportional de-vigging. "
                "The claimed-versus-realised EV explanation is not recorded in the app and requires review. "
                "Job reliability is unverified because scheduled job-run history is not persisted."
            ),
        }

    if key == "builder-and-thresholds":
        cfg = _read_toml("builder.toml")
        rows = []
        for group, values in cfg.items():
            if isinstance(values, dict):
                for name, value in values.items():
                    rows.append({"group": group, "setting": name, "value": str(value)})
        return {
            "cards": [
                ("Configuration groups", len(cfg)),
                ("Weekday minimum pool", cfg.get("weekday", {}).get("minimum_pool", "—")),
                ("Weekend minimum pool", cfg.get("weekend", {}).get("minimum_pool", "—")),
            ],
            "columns": [
                ("group", "Group", "text"),
                ("setting", "Setting", "text"),
                ("value", "Configured value", "text"),
            ],
            "rows": rows,
            "note": "Values are loaded from config/builder.toml. Editing is not enabled in the Admin view.",
        }

    if key == "jobs-and-logs":
        cfg = _read_toml("strong_model.toml").get("scheduler", {})
        rows = [
            {
                "job": "Daily ingestion",
                "schedule": f"Daily {cfg.get('daily_ingest_hour', '—'):02}:00 {cfg.get('timezone', '')}",
                "state": "Configured",
            },
            {
                "job": "Results and settlement",
                "schedule": f"Daily {cfg.get('results_ingest_hour', '—'):02}:00 {cfg.get('timezone', '')}",
                "state": "Configured",
            },
            {
                "job": "Model retraining",
                "schedule": f"{cfg.get('weekly_retrain_day', '—')} {cfg.get('weekly_retrain_hour', '—'):02}:00 {cfg.get('timezone', '')}",
                "state": "Configured",
            },
        ]
        return {
            "cards": [
                ("Scheduled jobs", len(rows)),
                ("Ingestion snapshots", _count(session, DataSnapshot)),
                ("Model runs", _count(session, ModelVersion)),
            ],
            "columns": [
                ("job", "Job", "text"),
                ("schedule", "Schedule", "text"),
                ("state", "Configuration", "text"),
            ],
            "rows": rows,
            "note": "The scheduler currently logs to its process output; persistent job-run history is not available.",
        }

    if key == "audit-log":
        rows = _rows(
            session,
            select(
                AuditLog.action.label("action"),
                AuditLog.target_type.label("target"),
                AuditLog.target_id.label("target_id"),
                User.email.label("actor"),
                AuditLog.created_at.label("created_sort"),
            )
            .outerjoin(User, User.id == AuditLog.actor_user_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(200),
        )
        for row in rows:
            row["actor"] = row["actor"] or "System"
            row["target_id"] = row["target_id"] or "—"
            row["created"] = _when(row.pop("created_sort"))
            row["created_sort"] = row["created"]
        return {
            "cards": [("Audit events", _count(session, AuditLog)), ("Showing", len(rows))],
            "columns": [
                ("created", "When", "date"),
                ("actor", "Actor", "text"),
                ("action", "Action", "text"),
                ("target", "Target", "text"),
                ("target_id", "Target ID", "text"),
            ],
            "rows": rows,
            "note": "The latest 200 events are shown.",
        }

    if key == "site-content":
        plans = []
        for role, entitlements in ROLE_ENTITLEMENTS.items():
            plans.append({"role": role.title(), "access": ", ".join(sorted(entitlements))})
        content = load_site_content()
        pro_price = content["plans"]["pro"].get("price_mwk")
        return {
            "cards": [
                ("Registration", "Open" if content["registration_open"] else "Closed"),
                ("Pro price", f"MWK {int(pro_price):,}" if pro_price else "Not set"),
                ("Plan tiers", len(plans)),
            ],
            "columns": [("role", "Role", "text"), ("access", "Current entitlement keys", "text")],
            "rows": plans,
            "note": "Public copy and plan pricing are loaded from config/site_content.json. Registration remains controlled by the registration_open setting.",
            "site_content": content,
        }

    if key == "payments":
        rows = _rows(
            session,
            select(
                Payment.id.label("id"),
                Payment.provider.label("provider"),
                Payment.tx_ref.label("transaction"),
                Payment.manual_reference.label("manual_reference"),
                Payment.amount_mwk.label("amount_sort"),
                Payment.status.label("status"),
                Payment.created_at.label("created_sort"),
            ).order_by(Payment.created_at.desc(), Payment.id.desc()).limit(200),
        )
        for row in rows:
            row["amount"] = f"MWK {int(row.pop('amount_sort')):,}"
            row["created"] = _when(row.pop("created_sort"))
        pending_manual = session.scalars(
            select(Payment)
            .where(Payment.provider == "manual", Payment.status == "pending")
            .order_by(Payment.created_at)
            .limit(100)
        ).all()
        pending_count = int(
            session.scalar(select(func.count()).select_from(Payment).where(Payment.status == "pending")) or 0
        )
        paid_count = int(
            session.scalar(select(func.count()).select_from(Payment).where(Payment.status == "succeeded")) or 0
        )
        return {
            "cards": [
                ("Payments", _count(session, Payment)),
                ("Pending", pending_count),
                ("Paid", paid_count),
            ],
            "columns": [
                ("provider", "Provider", "text"),
                ("transaction", "Transaction", "text"),
                ("manual_reference", "Manual reference", "text"),
                ("amount", "Amount", "text"),
                ("status", "Status", "text"),
                ("created", "Created", "text"),
            ],
            "rows": rows,
            "pending_manual": [
                {"id": item.id, "tx_ref": item.tx_ref, "amount": f"MWK {item.amount_mwk:,}"}
                for item in pending_manual
            ],
            "note": "Payment references and status are retained for reconciliation. AccaLab does not store card numbers or mobile money PINs.",
        }

    if key == "messaging":
        cfg = _read_toml("messaging.toml").get("whatsapp", {})
        statuses = _rows(
            session,
            select(
                MessageOutbox.channel.label("channel"),
                MessageOutbox.event_type.label("event"),
                MessageOutbox.status.label("status"),
                MessageOutbox.whatsapp_status.label("delivery_status"),
                MessageOutbox.template_name.label("template"),
                MessageOutbox.estimated_cost_usd.label("cost_sort"),
                MessageOutbox.delivered_at.label("delivered_at"),
            )
            .order_by(MessageOutbox.created_at.desc(), MessageOutbox.id.desc())
            .limit(100),
        )
        for row in statuses:
            row["cost"] = (
                f"USD {float(row['cost_sort']):.4f}"
                if row["delivered_at"] is not None and row["cost_sort"] is not None
                else "—"
            )
            row["cost_sort"] = str(row["cost_sort"] or "")
            row["delivered_at"] = _when(row["delivered_at"])
        enabled_consents = dict(
            session.execute(
                select(MessageConsent.channel, func.count())
                .where(
                    MessageConsent.consented_at.is_not(None),
                    MessageConsent.stopped_at.is_(None),
                    MessageConsent.destination.is_not(None),
                )
                .group_by(MessageConsent.channel)
            ).all()
        )
        delivered = int(
            session.scalar(
                select(func.count()).select_from(MessageOutbox).where(
                    MessageOutbox.channel == "whatsapp", MessageOutbox.delivered_at.is_not(None)
                )
            )
            or 0
        )
        estimated = session.scalar(
            select(func.sum(MessageOutbox.estimated_cost_usd)).where(
                MessageOutbox.channel == "whatsapp", MessageOutbox.delivered_at.is_not(None)
            )
        )
        dead_letters = int(
            session.scalar(
                select(func.count()).select_from(MessageOutbox).where(
                    MessageOutbox.status == "dead_letter"
                )
            )
            or 0
        )
        templates = [
            {
                "event": event,
                "name": item.get("name", "—"),
                "category": item.get("category", "—"),
                "approval": item.get("approval_status", "unknown").title(),
            }
            for event, item in cfg.get("templates", {}).items()
        ]
        cost_label = f"USD {float(estimated):.4f}" if estimated is not None else "Rate not configured"
        return {
            "cards": [
                ("WhatsApp", "Enabled" if cfg.get("whatsapp_enabled") else "Disabled"),
                ("Delivered templates", delivered),
                ("Estimated WhatsApp cost", cost_label),
                ("Dead-letter messages", dead_letters),
            ],
            "columns": [
                ("channel", "Channel", "text"),
                ("event", "Event", "text"),
                ("status", "Queue status", "text"),
                ("delivery_status", "Provider status", "text"),
                ("template", "Template", "text"),
                ("cost", "Estimated cost", "text"),
                ("delivered_at", "Delivered", "date"),
            ],
            "rows": statuses,
            "message_templates": templates,
            "note": (
                f"Active opt-ins — Telegram: {enabled_consents.get('telegram', 0)}, "
                f"email: {enabled_consents.get('email', 0)}, "
                f"WhatsApp: {enabled_consents.get('whatsapp', 0)}. "
                "WhatsApp stays disabled unless Meta policy approval, country eligibility, "
                "approved templates, consent, and current template cost estimates are configured. "
                "Cost is estimated from delivered templates; provider billing may differ."
            ),
        }

    return {"cards": [], "columns": [], "rows": [], "note": "No data is available."}


def build_admin_page_context(session: Session, section: str) -> dict:
    item = next(
        (entry for entry in ADMIN_SECTIONS if entry.path.rsplit("/", 1)[1] == section), None
    )
    if item is None:
        raise ValueError(f"Unknown Admin section: {section}")
    data = _admin_data(session, section)
    return {
        "section": section,
        "cards": data["cards"],
        "columns": data["columns"],
        "rows": data["rows"],
        "note": data["note"],
        "has_table": bool(data["columns"]),
        "title": item.label,
        "site_content": data.get("site_content"),
        "pending_manual": data.get("pending_manual", []),
        "message_templates": data.get("message_templates", []),
        "prelaunch": data.get("prelaunch"),
    }
