# Email setup (sign-up codes) and next steps

Sign-up now needs a 6-digit code sent by email. Until SMTP is set on the server, new sign-ups on the live site
show "email verification is not set up". Meanwhile an administrator can create test users on **Team**
(they skip the code), and `KOVA_EMAIL_OTP=0` switches the code off temporarily.

## 1. Brevo (free, 300 emails/day) - proper setup, about 30 minutes
1. brevo.com -> sign up with the Codeskys email -> confirm the email Brevo sends.
2. Brevo -> **Senders, Domains & Dedicated IPs** -> **Domains** -> **Add a domain** -> `codeskys.com`.
3. Brevo shows DNS records (a Brevo code TXT, two DKIM CNAMEs, a DMARC TXT). Add each at the place that manages the
   codeskys.com DNS (GoDaddy -> My Products -> codeskys.com -> DNS -> Add). Copy the *name* and *value* exactly.
4. Back in Brevo press **Authenticate this domain**. It can take from 5 minutes to a few hours.
5. Brevo -> your name (top right) -> **SMTP & API** -> **SMTP** tab -> note the **SMTP server** (`smtp-relay.brevo.com`),
   **Port** (`587`) and **Login**; press **Generate a new SMTP key**, name it `codeskys`, and copy the key at once.
6. On the server, edit `/etc/codeskys/website.env` and add (values are yours; no quotes, no spaces around `=`):
   ```
   KOVA_SMTP_HOST=smtp-relay.brevo.com
   KOVA_SMTP_PORT=587
   KOVA_SMTP_SECURITY=starttls
   KOVA_SMTP_USER=<the Login from Brevo>
   KOVA_SMTP_PASSWORD=<the SMTP key>
   KOVA_SMTP_FROM=Codeskys <no-reply@codeskys.com>
   KOVA_TRUSTED_PROXIES=1
   ```
7. Recreate the website container so it reads the file, then test:
   `docker exec codeskys-website-1 python manage.py test-email --to <your email>`
   It prints the settings it sees and either SUCCESS or the reason for FAILED.

Fastest for a same-day test (no DNS): a Gmail account with 2-step verification and an *App password*:
host `smtp.gmail.com`, port `587`, user = the Gmail address, password = the 16-character app password,
from = the same Gmail address.

## 2. After this update
- Sign-in limits now count each visitor by their own address (`KOVA_TRUSTED_PROXIES=1`, Caddy in front). Before, everybody
  shared one address, so a few attempts blocked every user ("too many attempts").
- Administrator accounts are never locked out (a very high hourly ceiling remains); everyone stays signed in for
  30 days (`KOVA_SESSION_DAYS`).
- One account per browser for ordinary users; an administrator's browser is unlimited. `KOVA_ONE_ACCOUNT_PER_BROWSER=0` turns it off.
- QR code on the payment page for Rs 510 and Rs 1,599.

## 3. Next, in this order
1. Email (above) and test a real sign-up.
2. GitHub sign-in: create the OAuth App as in `docs/GITHUB_OAUTH.md` and add `GITHUB_CLIENT_ID` and the secret file.
3. Free-plan expiry test on the live site with a student account:
   `docker exec codeskys-website-1 python manage.py set-expiry --slug <link-name> --minutes 0`
4. Remove or fix the two crash-looping student apps (missing `joblib` in their requirements.txt).
5. Private repositories: decide the Coolify GitHub App approach (still refused today).
