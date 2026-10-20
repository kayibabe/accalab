# Phase 9a: Telegram messaging

## Delivered

- Added explicit Telegram consent, one-time hashed deep-link tokens, event preferences, and STOP handling.
- Added a database-backed outbox with event idempotency, retries, bounded attempts, cancellation, and dead-letter status.
- Added Telegram delivery through the Bot API; credentials and webhook secret come from the runtime environment.
- Connected official ticket publication to the outbox, nightly result updates to results ingestion, and Pro expiry reminders to the notification worker.
- Added the Account, Notifications controls and a five-minute outbox worker in APScheduler.
- Every outgoing message includes the 18+ responsible-gambling line. Free accounts are filtered when messages are queued and immediately before sending.

## Runtime setup

Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, and `TELEGRAM_WEBHOOK_SECRET` in the service environment. Configure Telegram's webhook URL as `/messaging/telegram/webhook` and set its `secret_token` to the same webhook secret. Apply the Alembic migration before starting the app or scheduler. Users connect the bot from Account, Notifications; the bot's `/start <token>` claims a 30-minute, single-use link. `/stop` and the Account stop control disable further delivery and cancel queued messages.

Run `accalab send-notifications` to queue due expiry reminders and process the outbox. The scheduler runs this command every five minutes. Ticket and nightly result messages are queued by their existing publication and settlement flows.

## Verification boundary

Automated tests exercise consent, link expiry and one-time use, role filtering, STOP, idempotent queueing, delivery retry, dead-lettering, migration, and scheduler registration. The Telegram bot and webhook have not been configured against a live Telegram account; live delivery remains to be exercised after credentials and webhook deployment are supplied.
