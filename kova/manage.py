"""Operator CLI.  Usage:
    python manage.py init-db
    python manage.py create-admin --email you@example.com --name "Your Name"
"""
import argparse
import getpass
import sys

from kova import create_app
from kova.db import connect, migrate
from kova.users import ValidationError, check_password, create_user


def main():
    parser = argparse.ArgumentParser(prog="manage.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    w = sub.add_parser("worker", help="run the deployment queue worker in the foreground")
    w.add_argument("--poll", type=float, default=1.0, help="seconds between polls when idle")
    w.add_argument("--once", action="store_true", help="drain ready jobs and exit")
    d = sub.add_parser("demo")
    d.add_argument("--email", required=True, help="owner of the demo projects")
    e = sub.add_parser("test-email", help="send a test email with the current KOVA_SMTP_* settings (to check email is set up)")
    e.add_argument("--to", required=True, help="the address to send the test to")
    x = sub.add_parser("set-expiry", help="TESTING: move a free project's expiry (e.g. to see the 404 page quickly)")
    x.add_argument("--slug", required=True, help="the project's link name")
    x.add_argument("--minutes", type=float, required=True, help="expire this many minutes from now (0 = right now)")
    p = sub.add_parser("create-admin")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default="Administrator")
    args = parser.parse_args()

    app = create_app()
    path = app.config["DATABASE"]
    if args.cmd == "init-db":
        print(f"Database ready at {path} (schema version {migrate(path)})")
        return 0
    if args.cmd == "worker":
        from kova.jobs import Worker, drain
        if args.once:
            n = drain(path, app.config)
            print(f"Processed {n} job(s).")
            return 0
        worker = Worker(path, app.config, poll=args.poll)
        print(f"Codeskys worker {worker.id} running against {path}. Ctrl-C to stop.")
        try:
            worker.run()
        except KeyboardInterrupt:
            print("\nWorker stopped.")
        return 0
    if args.cmd == "demo":
        from kova.seed import seed
        conn = connect(path)
        row = conn.execute("SELECT id FROM users WHERE email = ?", (args.email.strip().lower(),)).fetchone()
        conn.close()
        if row is None:
            print(f"No user with email {args.email}. Create one first with create-admin.")
            return 1
        n = seed(path, row["id"])
        print(f"Seeded {n} demo project(s) for {args.email}." if n else "Demo projects already present.")
        return 0
    if args.cmd == "test-email":
        from kova import mailer
        app = create_app()
        cfg = app.config
        print("Mail settings seen by Codeskys:")
        print(f"  host     : {cfg.get('SMTP_HOST') or '(empty)'}   port: {cfg.get('SMTP_PORT')}   security: {cfg.get('SMTP_SECURITY')}")
        print(f"  login    : {cfg.get('SMTP_USER') or '(empty)'}")
        print(f"  password : {'set' if cfg.get('SMTP_PASSWORD') else '(empty)'}")
        print(f"  from     : {cfg.get('SMTP_FROM') or '(empty)'}")
        if not mailer.smtp_configured(cfg):
            print("\nEmail is NOT set up: KOVA_SMTP_HOST (and KOVA_SMTP_FROM or KOVA_SMTP_USER) are empty.")
            return 1
        try:
            with app.app_context():
                how = mailer.send_mail(app, args.to, "Codeskys test email", "If you can read this, email sending works. Sign-up codes will now reach people.")
            print(f"\nSUCCESS ({how}): a test email was sent to {args.to}. Check the inbox (and spam).")
            return 0
        except Exception as exc:
            print(f"\nFAILED: {exc}")
            print("Common causes: wrong SMTP login or key, the From address is not verified at your mail provider, or the server blocks port 587.")
            return 1
    if args.cmd == "set-expiry":
        from datetime import datetime, timedelta, timezone
        conn = connect(path)
        row = conn.execute("SELECT id FROM projects WHERE slug = ?", (args.slug,)).fetchone()
        if row is None:
            print(f"No project with the link name '{args.slug}'.")
            return 1
        when = (datetime.now(timezone.utc) + timedelta(minutes=args.minutes)).isoformat(timespec="seconds")
        conn.execute("UPDATE projects SET free_expires_at = ? WHERE id = ?", (when, row["id"]))
        conn.execute("UPDATE deployments SET expires_at = ? WHERE project_id = ? AND status = 'ready' "
                     "AND expired_at IS NULL", (when, row["id"]))
        conn.commit()
        conn.close()
        print(f"'{args.slug}' will expire at {when} (UTC). The site goes to the 404 page within about a minute after that.")
        return 0
    password = getpass.getpass("Password (min 12 chars, with upper and lower case, a number and a symbol): ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.")
        return 1
    conn = connect(path)
    try:
        check_password(password, args.email)
        create_user(conn, args.email, args.name, password, "admin")
    except ValidationError as exc:
        print(f"Not created: {exc}")
        return 1
    finally:
        conn.close()
    print(f"Admin {args.email} created.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
