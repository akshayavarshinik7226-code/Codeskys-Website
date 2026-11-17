# Update notes: Batch A + A2 + B + UPI payments (merged onto the live code)

This update was made on top of the code that is live now (the `project.zip` from the server) and
passes all of its tests: **528 passing** (the live code's 471, plus the new ones). Nothing the live
code already does was removed. The database updates itself when the app starts (migrations 11 and 12).

## What changed (for the person deploying)

**Free plan rules**
- One free project per account, **for life**; one 24-hour clock per project (`projects.free_expires_at`).
  Every redeploy shows the same expiry. After it ends: 404 page with an Upgrade button, and redeploy /
  re-upload / webhook deploys are refused (HTTP 402). Deleting the project does not give a new window
  (`users.free_started_at`); the delete dialog warns about it.
- Only an administrator can change a project's lifetime hours. **Administrator accounts are exempt** from
  the free-plan limits and expiry (for testing); use a student account to test the free rules.
- Redeploy now publishes the project's current branch and the newest commit (before it rebuilt the old one).
  Only the latest deployment can be redeployed or published; older ones are view-only history.

**Paid hosting (uses the existing `hosting_until` system, only the offers changed)**
- `hosting-6m`: Rs 510, 180 days. `hosting-lifetime`: Rs 1,599 (stored as a date 100 years away).
  These replace `student-6m` (Rs 500) and `hosting-30d` in the *default* catalog; a custom
  `KOVA_BILLING_PLANS_JSON` still works exactly as before. The 24 h grace, reminders and refund logic are unchanged.
- A paying person may have up to 100 projects (`plans.project_limit`).

**Sign-up and passwords**
- Two-step sign-up: details -> 6-digit code emailed (10 min, 5 tries, resend after 30 s). Fake, disposable
  and placeholder addresses are refused. New passwords need 12+ characters with upper/lower case, a number
  and a symbol (login never re-checks the rule, so older passwords still work).
- **The code is sent over SMTP. Set the `KOVA_SMTP_*` variables first** (below). In production with no SMTP
  settings, sign-up shows "email verification is not set up". `KOVA_EMAIL_OTP=0` switches the code off.

**Manual UPI payments** (`kova/upi.py`, page `#/pay/<ref>`, admin page `#/payments`)
- Billing page -> choose a plan -> receipt page with a `upi://pay?...` link to `codeskysindia@okhdfcbank`
  -> the person types the 12-digit transaction ID -> "verifying" screen -> an administrator approves
  on **Payments** -> `hosting_until` is set and live projects are extended.
- Every payment is also written to `instance/upi_payments.csv` (opens in Excel) with the name, email, plan,
  amount, time and transaction ID, and can be downloaded from the Payments page.
- One transaction ID can only be used once; nothing is granted before approval.

**Screens**: the old "Start a new project" screens, kept exactly; a folder can be dropped (packed to a zip in the
browser, `node_modules` and `.env` left out); "Connect your GitHub / GitLab account" buttons; "Log In"
wording; live links open in a new tab; deployment pages highlight Deployments; show/hide password,
strong-password suggester; project rename also renames the live link (with an availability tick).

## Round 2 corrections (16 Nov)
- Payment page: verification wording "up to 8 hours", Net Banking button (shows "available only for international
  customers"), only a "Submit" button, mobile-only note for UPI, share/copy payment link (WhatsApp, email, other apps),
  success screen "Your plan has been successfully upgraded" with Dashboard / Deployments buttons, no email shown
  to users (administrators still see payer emails on the Payments page).
- Administrators get an in-app alert **and an email** for every payment to verify (needs SMTP), a count badge on
  Payments in the menu, and the payer is emailed on approval/rejection.
- Long builds: the queue/waiting box became a "Building your project" page with a real stage-based progress bar,
  an elapsed timer and, after 12 minutes, a note asking people to keep the page open.
- Regular users see "Included in paid plans. Upgrade your plan to unlock this feature." wherever the platform showed
  a warning; administrators still see the original warnings (so they can be found and fixed one by one).

## Round 3 (17 Nov)
- **Sign-in limits per real visitor** (`KOVA_TRUSTED_PROXIES`, default 1 in production): behind Caddy the app used to see one
  address for everybody, so a few attempts blocked all sign-ins and sign-ups. Administrators are never locked out.
- Signed in for 30 days (`KOVA_SESSION_DAYS`) instead of until the browser closes.
- One account per browser (`browser_links`, migration 13); administrator browsers are unlimited.
- UPI QR codes (Rs 510 and Rs 1,599, Codeskys logo in the centre, verified to scan) on the payment page.
- `python manage.py test-email --to <address>` checks the email settings. See `docs/EMAIL_AND_NEXT_STEPS.md`.

## Round 4 (18 Nov)
- **Updates show immediately.** Every static file address now carries `?v=<hash of the files>` and is cached for good only
  under that version; un-versioned files always re-check. Before, browsers could keep an old `app.js` for hours, so a new
  Billing page or menu looked "not updated" on a live site that had already been updated.
- Administrators see the **build id** in the sidebar next to the engine name (`build abc123def4`). If it does not change
  after a release, the container is still running the old files.

## Steps to deploy (same way as the last update)

1. **Back up first**: `cp -a /srv/codeskys/website/instance /root/instance.backup-$(date +%F)` and copy
   `/etc/codeskys/website.env` to `website.env.pre-AB`.
2. Replace the files of the website project with the ones in this zip (the `kova` folder). Do **not** copy a
   `.env`, `.venv` or `instance` folder.
3. Add to `/etc/codeskys/website.env` (names only here, values are yours):
   ```
   KOVA_SMTP_HOST=            # e.g. smtp-relay.brevo.com
   KOVA_SMTP_PORT=587
   KOVA_SMTP_USER=
   KOVA_SMTP_PASSWORD=
   KOVA_SMTP_FROM=Codeskys <no-reply@codeskys.com>
   KOVA_SMTP_SECURITY=starttls
   KOVA_MAX_UPLOAD_MB=100
   ```
   The UPI settings have working defaults (`KOVA_UPI_ID`, `KOVA_UPI_NAME`, `KOVA_UPI_AID`). For deliverability add
   the SPF and DKIM records your mail provider shows for `codeskys.com`.
4. Rebuild the website image and recreate the container exactly as before
   (`codeskys/website:local`, then `docker compose up -d --no-deps website`).
5. Check: `docker logs codeskys-website-1 --tail 30` shows no traceback; open `/login`
   (tab says **Log In**); create a student account and confirm the email code arrives; upload a folder;
   open Billing -> Upgrade -> Payments page as admin.

## Rollback
Restore `/etc/codeskys/website.env.pre-AB` and the previous image/files, then recreate the website container.
The new migrations only add columns and tables (`free_expires_at`, `free_started_at`, `pending_signups`,
`upi_payments`), so the old code keeps working on the migrated database.

## Known limits (honest list)
- Private repositories are still refused on the Coolify path (`engine.coolify_refusal`), as in the live code.
  Supporting them needs a Coolify GitHub App or deploy key decision; it was not changed here.
- GitHub/GitLab sign-in still needs the OAuth App keys on the server (see `docs/GITHUB_OAUTH.md`).
- UPI cannot confirm itself: an administrator checks the bank app before approving.
- The sheet is a `.csv` file (Excel opens it); a live Google Sheet would need Google API keys.
