# Codeskys billing (Razorpay, test mode)

Code: `kova/billing.py`, migration 6 in `kova/db.py`, UI in `static/js/app.js` (Usage & Billing),
tests: `tests/test_billing.py` (24, against a fake Razorpay API).

## Status (2026-11-06)
- Implemented and tested against a fake Razorpay API: catalog pricing, order creation, checkout
  signature check, provider confirmation, signed webhooks, idempotency and replay protection,
  pending/failed/paid states, renewal, billing history, receipts, admin refunds, plan expiry.
- **Not yet run against Razorpay itself**: no test keys exist on the PC or the VPS.
- **Live payments are off.** `rzp_live_` keys are refused unless `RAZORPAY_ALLOW_LIVE=1`.
  Turning that on is an owner decision (Razorpay KYC/activation, final prices, tax/GST, refund policy).

## Checkout switch (owner decision 2026-11-06)
Proposed price: **Student ₹500 / 6 months**. Checkout stays **closed** until the owner confirms
final prices: orders are refused (503, Razorpay never contacted) unless `KOVA_BILLING_CHECKOUT=1`.
Keys and webhooks can be configured and tested while it is closed. Premium ₹2,499 is **not sellable**
(not in the catalog; its card says "period to be confirmed" and has no buy button).

## Billing model (from the existing public offer, not invented)
Landing page: "STUDENT PLAN ₹500 / 6 months". Default catalog:
`student-6m` = ₹500.00 (50000 paise), 180 days, grants the existing `pro` plan limits.
A one-time payment for a fixed period, no auto-debit. Paying again ("Renew") extends from the current end date.
The in-app cards previously said "Pro ₹510"; they now show the proposed Student ₹500 / 6 months.
Premium ₹2,499 is shown as not available until its billing period is confirmed. Set the real
catalog with `KOVA_BILLING_PLANS_JSON` (amounts in paise):
```json
{"student-6m": {"name": "Student", "grants_plan": "pro", "amount": 50000, "currency": "INR",
                "period_days": 180, "description": "Codeskys Student plan, 6 months"}}
```

## Rules
| Rule | How |
|---|---|
| Price | taken only from the server catalog; the client's amount is ignored |
| Access granted | only when Razorpay reports the payment **captured** for this order, this amount and this currency (verify fetches `/payments/{id}`; webhook `payment.captured`/`order.paid`) |
| Signature alone | never grants access. Provider unreachable at verify time → `pending`, then the webhook settles it |
| Once only | `entitlement_granted` conditional UPDATE, so verify + webhook + retries extend the plan exactly once |
| Webhook auth | HMAC-SHA256 of the raw body with `RAZORPAY_WEBHOOK_SECRET` |
| Replay | dedupe on `X-Razorpay-Event-Id` **and** sha256(body) (the header is not signed); bodies with `created_at` older than 3 days are ignored |
| States | created → pending → paid; created/pending → failed; failed → paid (a later attempt on the same order); paid → partially_refunded / refunded. Never paid → failed |
| Refunds | admin only (`POST /api/admin/billing/payments/<ref>/refund`, optional `amount` in paise). Access is removed only when a **full** refund is `processed` (period taken back). Partial refunds keep access |
| Admin-set plans | `plan_expires_at` NULL. Billing never changes them; checkout is refused (409) |
| Expiry | when `plan_expires_at` passes, the user drops to `hobby` on the next request. Existing projects stay; new ones obey hobby limits |
| Secrets | key secret and webhook secret are server-only (`*_FILE` supported) and never in responses/logs |
| CSP | Razorpay origins are added only on the signed-in app shell (`/`), only when billing is enabled |
| Receipts | `/billing/receipts/<ref>`, owner or admin only, paid/refunded payments only. A payment receipt, **not a GST tax invoice** |

## Configuration (website.env)
```
RAZORPAY_KEY_ID=rzp_test_...            # Razorpay Dashboard → Test Mode → API Keys
RAZORPAY_KEY_SECRET_FILE=/run/secrets/razorpay_key_secret   # or RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET_FILE=/run/secrets/razorpay_webhook_secret
# RAZORPAY_ALLOW_LIVE=1                 # NEVER without owner approval
# KOVA_BILLING_PLANS_JSON={...}
```
Razorpay Dashboard (Test Mode) → Webhooks → URL `https://codeskys.com/api/hooks/razorpay`
(after Phase E HTTPS), secret = the webhook secret, events: `payment.authorized`,
`payment.captured`, `payment.failed`, `order.paid`, `refund.processed`, `refund.failed`.
Before HTTPS exists the checkout + verify path still works (verify asks Razorpay
directly); only the pending→paid, failed and refund updates need the webhook.

## Sandbox test plan (once test keys exist)
1. Student signs in → Usage & Billing → "Pay ₹500" → Razorpay test card / test UPI success
   → plan Pro, end date +180 days, history "Paid", receipt opens.
2. Test failure method → history "Failed" with the reason; plan unchanged.
3. Pay again → end date +360 days.
4. Admin full refund → after `refund.processed` webhook: status Refunded, plan back to Hobby.
5. Re-send a webhook from the dashboard → `duplicate: true`, nothing changes.
6. `curl` the webhook with a wrong signature → 400.
