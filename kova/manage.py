"""Operator CLI.  Usage:
    python manage.py init-db
    python manage.py create-admin --email you@example.com --name "Your Name"
"""
import argparse
import getpass
import sys

from kova import create_app
from kova.db import connect, migrate
from kova.users import ValidationError, create_user


def main():
    parser = argparse.ArgumentParser(prog="manage.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    w = sub.add_parser("worker", help="run the deployment queue worker in the foreground")
    w.add_argument("--poll", type=float, default=1.0, help="seconds between polls when idle")
    w.add_argument("--once", action="store_true", help="drain ready jobs and exit")
    d = sub.add_parser("demo")
    d.add_argument("--email", required=True, help="owner of the demo projects")
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
    password = getpass.getpass("Password (min 12 chars): ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.")
        return 1
    conn = connect(path)
    try:
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
