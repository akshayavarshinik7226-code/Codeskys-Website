"""Sending email (sign-up codes). Uses plain SMTP, so it works with Gmail, Brevo, SendGrid,
Zoho or any mail provider; the settings are the KOVA_SMTP_* variables in the .env file.

Behaviour by situation:

* SMTP is configured                  -> the mail is really sent.
* tests (TESTING)                      -> the mail is kept in ``app.config["MAIL_OUTBOX"]``.
* development, SMTP NOT configured     -> the mail text is printed in the server window, so the
                                          owner can test sign-up on a laptop without a mail account.
* production, SMTP NOT configured      -> MailNotConfigured: sign-up says email is not set up yet.
                                          A code is never shown on screen or returned by the API.
"""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr


class MailNotConfigured(RuntimeError):
    pass


class MailFailed(RuntimeError):
    pass


def smtp_configured(cfg) -> bool:
    return bool(cfg.get("SMTP_HOST") and (cfg.get("SMTP_FROM") or cfg.get("SMTP_USER")))


def _sender(cfg) -> str:
    raw = cfg.get("SMTP_FROM") or cfg.get("SMTP_USER")
    name, addr = parseaddr(raw)
    if not addr:
        raise MailNotConfigured("KOVA_SMTP_FROM is not a valid email address.")
    return formataddr((name or cfg.get("PLATFORM_NAME", "Codeskys"), addr))


def build_message(cfg, to: str, subject: str, text: str, html: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = _sender(cfg)
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def send_mail(app, to: str, subject: str, text: str, html: str | None = None) -> str:
    """Send one message. Returns how it was handled: "sent", "outbox" or "console"."""
    cfg = app.config
    if cfg.get("TESTING") and not cfg.get("SMTP_HOST"):
        cfg.setdefault("MAIL_OUTBOX", []).append({"to": to, "subject": subject, "text": text, "html": html})
        return "outbox"
    if not smtp_configured(cfg):
        if cfg.get("ENV_NAME") == "production":
            raise MailNotConfigured("Email sending is not set up on this server.")
        banner = "=" * 62
        print(f"\n{banner}\n[DEV ONLY - email is not configured, so it is shown here]\n"
              f"To: {to}\nSubject: {subject}\n\n{text}\n{banner}\n", flush=True)
        return "console"
    msg = build_message(cfg, to, subject, text, html)
    host, port = cfg["SMTP_HOST"], int(cfg.get("SMTP_PORT") or 587)
    mode = cfg.get("SMTP_SECURITY", "starttls")
    try:
        if mode == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        with server:
            server.ehlo()
            if mode == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if cfg.get("SMTP_USER"):
                server.login(cfg["SMTP_USER"], cfg.get("SMTP_PASSWORD", ""))
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        app.logger.error("Could not send email to %s: %s", to, exc)
        raise MailFailed("The email could not be sent.") from exc
    return "sent"


def otp_email(cfg, code: str, minutes: int):
    """Subject, plain text and HTML for the sign-up code email."""
    brand = cfg.get("PLATFORM_NAME", "Codeskys")
    subject = f"{code} is your {brand} verification code"
    text = (f"Your {brand} verification code is: {code}\n\n"
            f"It works for {minutes} minutes. If you did not try to create a {brand} account, "
            f"you can ignore this email.\n\nNever share this code with anyone.\n")
    html = f"""<!doctype html><html><body style="margin:0;background:#f4f6fb;font-family:Segoe UI,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="440" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:14px;border:1px solid #e3e7f1">
<tr><td style="padding:28px 32px 8px;font-size:18px;font-weight:700;color:#10162f">{brand}</td></tr>
<tr><td style="padding:0 32px;font-size:15px;color:#3b4263;line-height:1.55">Use this code to finish creating your account:</td></tr>
<tr><td align="center" style="padding:20px 32px"><div style="font-size:34px;letter-spacing:10px;font-weight:800;color:#10162f;background:#eef1ff;border-radius:10px;padding:14px 10px">{code}</div></td></tr>
<tr><td style="padding:0 32px 28px;font-size:13px;color:#6b7390;line-height:1.55">The code works for {minutes} minutes. If you did not try to create a {brand} account, you can ignore this email. Never share this code with anyone.</td></tr>
</table></td></tr></table></body></html>"""
    return subject, text, html
