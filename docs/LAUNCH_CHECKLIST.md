# AccaLab launch checklist

This checklist records launch evidence and outstanding decisions for the Malawi launch. A checked item needs dated, reviewable evidence in the linked internal record. The Admin pre-launch report is an operational readiness screen; passing it cannot prove an edge or guarantee returns. The owner decides when to set `registration_open`; this task does not change that setting.

## Pre-launch tracking gate

The live values are assembled in Admin → Backtests and pre-launch tracking from official eligible single selections, latest settlement corrections, stored closing odds and scheduled-run evidence where available. The report shows settled-leg count, tracking weeks, decile calibration/ECE, paired model and de-vigged market Brier scores, claimed versus realised EV, and mean CLV with a bootstrap 95% confidence interval.

| Gate | Requirement | Current evidence |
|---|---|---|
| Tracking period | At least 6 weeks | Read the live Admin report |
| Settled eligible legs | At least 800 | Read the live Admin report |
| Calibration | ECE at or below 0.03 | Read the live Admin report |
| Model quality | Brier no worse than the de-vigged market on a paired sample | Read the live Admin report; inspect the comparable sample count |
| Closing-line value | Positive mean, with confidence interval displayed | Read the live Admin report |
| Claimed versus realised EV | Review the measured gap and document its explanation | Read the live Admin report and add review evidence below |
| Job reliability | Confirm scheduled jobs ran reliably | Unverified in the app: persistent job-run history is not currently stored |

The report uses official single selections for the leg-level sample. Closing market probabilities are proportional de-vig estimates based on the selected bookmaker's latest stored pre-kickoff multi-selection quote set; CLV compares the captured selection price with that bookmaker's latest pre-kickoff price. Missing comparable quotes are excluded from the market Brier and CLV samples and are shown by sample counts. The app does not store the written explanation of the EV gap or scheduled-job history, so these gate checks remain unverified until reviewed and documented. The gate remains not ready when required evidence is missing or any check fails.

### Review log

| Date | Reviewer | Evidence reviewed | Decision / follow-up |
|---|---|---|---|
|  |  |  |  |

## Commercial, regulatory and account readiness

Do not treat this checklist as legal advice. Confirm the current rules and written permissions with the relevant provider and a Malawi-qualified adviser before public registration or paid distribution.

- [ ] **API-Football commercial odds use:** Review the current API-Football terms, plan restrictions, attribution requirements and permissions for displaying odds in a paid product. Save the applicable terms/version, date checked, provider response if needed, and approved display scope. Do not redistribute data beyond the permission obtained.
- [ ] **Malawi licensing and registration:** Obtain written advice on whether selling football selections/tips, subscriptions, or related analytics requires a licence, registration, or other approval in Malawi for the markets AccaLab covers. Record the exact markets and jurisdictions included, the adviser/authority, date, conclusion and any operating conditions. Do not launch paid tips until this is resolved.
- [ ] **PayChangu account:** Complete merchant onboarding and confirm the live account is approved for the intended digital subscription product and Malawi channels (Airtel Money, TNM Mpamba and cards as enabled). Record account owner and provider confirmation without recording secrets here.
- [ ] **PayChangu fees and settlement:** Confirm current transaction, withdrawal, refund and chargeback fees, settlement timing, supported payment methods and any product restrictions. Compare the published MWK price with net receipts and document the approved fee schedule and date checked.
- [ ] **Telegram bot:** Create/configure the production bot, protect its token in the environment, verify opt-in, linking, delivery, STOP handling, quiet hours, responsible-gambling footer, failure handling and a real end-to-end test. Record the test date and outcome.
- [ ] **Meta / WhatsApp:** Before enabling WhatsApp, complete Meta business verification, review the current gambling/tips policy and country eligibility, obtain approval for the intended messaging use, and approve and configure required templates. Keep `whatsapp_enabled` off until each check is evidenced.

## Customer protection and public terms

- [ ] **Responsible-gambling content:** Review the 18+ notice, no-guaranteed-profit wording, past-performance disclaimer, age/jurisdiction boundaries, safer-gambling guidance and help contacts with a qualified local reviewer. Verify the content is visible on public pages and included in every message.
- [ ] **Terms:** Review subscription/payment terms, renewal and expiry behavior for prepaid plans, cancellations, refunds, data/odds limitations, service availability, complaints and the analysis-only nature of the product. Confirm they match actual operations and legal advice.
- [ ] **Privacy:** Review data collected, purpose, lawful basis/consent, retention/deletion, account security, processors and cross-border transfers, user rights, contact details and incident handling. Confirm the notice matches deployed behavior and applicable Malawi requirements.
- [ ] **Public-flow review:** Verify pricing, waitlist while registration is closed, account entitlements, free sample rules, locked content, public record labels, timezone, mobile usability and accessibility. Confirm free users cannot access Pro resources by direct URL.
- [ ] **Operational review:** Verify backups and restore, secret handling, payment reconciliation, messaging STOP/failure paths, support contacts, incident ownership and a rollback procedure. Keep real-money betting and bet placement outside the product.

## Launch decision record

- Registration setting remains controlled separately by the owner: `registration_open` is not changed by this checklist or the report.
- [ ] All launch-gate criteria have current evidence and have been reviewed.
- [ ] Commercial odds display and Malawi legal/regulatory checks have written resolution.
- [ ] Payment and messaging providers have approved the intended use and operational tests passed.
- [ ] Responsible-gambling content, Terms and Privacy have been reviewed and match the product.
- [ ] The owner has recorded the launch decision and chosen whether to change `registration_open`.

Decision date:  
Decision owner:  
Evidence location:  
Decision and conditions:  
