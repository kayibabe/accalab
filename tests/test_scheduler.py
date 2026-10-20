from accalab.scheduler import build_scheduler


def test_scheduler_registers_ingestion_and_weekly_retraining_jobs() -> None:
    scheduler = build_scheduler()

    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == {
        "daily-ingestion",
        "daily-results-and-settlement",
        "weekly-model-retraining",
        "daily-payment-reconciliation",
        "notification-outbox",
    }
    assert jobs["weekly-model-retraining"].args == ("retrain-model",)
    assert jobs["daily-payment-reconciliation"].args == ("reconcile-payments",)
    assert jobs["notification-outbox"].args == ("send-notifications",)
    assert jobs["notification-outbox"].trigger.interval.total_seconds() == 300
    assert str(scheduler.timezone) == "Africa/Blantyre"
