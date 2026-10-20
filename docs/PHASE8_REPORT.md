# Phase 8: Payments

## Implemented

- Added payment and prepaid subscription records with a migration after the public waitlist migration.
- Added a `PaymentProvider` interface with a local manual adapter (the default) and a PayChangu Standard Checkout adapter.
- Added a Pro billing view, transaction history, Admin payment summaries, and an audited Admin action for confirming manual payments.
- PayChangu webhook signatures are checked against the unmodified request body. Callback and webhook events re-query PayChangu's verification endpoint before activating access.
- Repeated successful callbacks return the existing subscription instead of extending the plan again. Successful transactions must match the transaction reference, MWK currency and configured amount.
- Added daily payment reconciliation at 04:00 Africa/Blantyre. Pending payments are checked after 30 minutes and expire after 24 hours. Prepaid access has a configurable 30-day period and 5-day grace period by default; expired Pro access downgrades to Free while Admin access is preserved.
- Payment storage keeps transaction references and statuses. AccaLab does not store card numbers or mobile money PINs.

## Configuration before accepting payments

Set the Pro price in Admin, Site content. A blank price disables checkout. Manual payments are enabled by default. To enable PayChangu, configure `PAYMENT_PROVIDER=paychangu`, `PAYCHANGU_SECRET_KEY`, `PAYCHANGU_WEBHOOK_SECRET` and an HTTPS `PUBLIC_BASE_URL`; configure the PayChangu dashboard webhook to post to `/payments/paychangu/webhook`. Apply database migrations before deployment.

The integration follows PayChangu's [Standard Checkout](https://developer.paychangu.com/docs/standard-checkout), [transaction verification](https://developer.paychangu.com/docs/transaction-verification) and [webhook signature](https://developer.paychangu.com/docs/webhooks) guidance. No sandbox transaction was initiated in this implementation pass. The checked-in JSON fixtures are example response fixtures for deterministic tests, not captured merchant transactions. Confirm the merchant sandbox account's current payload and callback configuration before enabling PayChangu for users.

## Verification

- `python -m pytest -q`: 82 passed.
- `python -m ruff check .`: passed.
- CLI help lists `reconcile-payments`; Alembic reports `d20f5a9c8e31` as the head revision.

## Not included

Registration and email verification are not enabled by this phase. No live payment has been processed, no merchant fees or settlement terms have been verified, and this implementation does not provide refunds or automatic renewals.
