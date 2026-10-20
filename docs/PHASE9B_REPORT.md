# Phase 9b: WhatsApp messaging

## Delivered

- Added a WhatsApp Cloud API template adapter, gated by `whatsapp_enabled = false` and a separate `policy_approved = false` setting.
- Added per-event template names, category, approval status, and language in `config/messaging.toml`. Outbound payloads contain only short, bounded event text and the required 18+ line; sending requires an approved template and an explicitly configured country/category cost estimate.
- Added +265 format validation, one-time phone verification, explicit event consent, and an 18+ confirmation. Online gambling messaging is restricted to the policy-listed countries; the hard-coded safety gate excludes Malawi even if the config country list is edited.
- Added signed Meta webhook verification, delivery status tracking, delivered-template cost estimates, WhatsApp STOP handling, and fallback to a separately consented Telegram or email channel after a WhatsApp failure.
- Added opt-in email delivery with a signed unsubscribe link. Email and WhatsApp remain subject to the same Free-account exclusion and 18+ message footer.
- Added Admin Messaging counts, recent delivery statuses, template approval status, dead-letter count, and estimated cost.

## Policy gate

The current WhatsApp Business policy requires prior Meta permission and licensing for messages promoting online gambling, and limits this messaging to Australia, Japan, Colombia, Mexico, and Peru. Malawi is not on that list. AccaLab's WhatsApp adapter therefore remains disabled and rejects Malawi numbers even if the configuration is changed. Review the live [WhatsApp Business Messaging Policy](https://whatsappbusiness.com/policy/) again before any future enablement.

## Runtime setup and limits

Apply the Alembic migration before deploying the app. `config/messaging.toml` is deliberately default-off; credentials are read from `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `META_GRAPH_API_VERSION`, and `META_APP_SECRET`. Configure the GET verification token with `WHATSAPP_WEBHOOK_VERIFY_TOKEN`. Set approved template names and categories only after Meta approval. Populate current category and country estimates before sending so delivered-message cost can be estimated without inventing a rate. Email fallback requires SMTP configuration and an HTTPS `PUBLIC_BASE_URL` for the direct unsubscribe link.

No WhatsApp template approval, Meta business permission, local gambling license, live Cloud API delivery, or SMTP delivery has been verified. Tests use deterministic fake responses and do not establish provider readiness.
