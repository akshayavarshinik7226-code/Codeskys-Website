import re
import sqlite3

from .db import utcnow
from .security import hash_password

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 12


class ValidationError(ValueError):
    pass


def create_user(db: sqlite3.Connection, email: str, name: str, password: str, role: str = "student") -> int:
    email = (email or "").strip().lower()
    name = (name or "").strip()
    if not EMAIL_RE.match(email):
        raise ValidationError("Enter a valid email address.")
    if not name:
        raise ValidationError("Enter a name.")
    if role not in ("admin", "student"):
        raise ValidationError("Role must be admin or student.")
    if len(password or "") < MIN_PASSWORD:
        raise ValidationError(f"Password must be at least {MIN_PASSWORD} characters.")
    try:
        cur = db.execute(
            "INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?,?,?,?,?)",
            (email, name, role, hash_password(password), utcnow()),
        )
        db.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValidationError("A user with this email already exists.")


def public_user(row) -> dict:
    keys = row.keys() if hasattr(row, "keys") else []
    return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"],
            "plan": row["plan"] if "plan" in keys else "hobby",
            "plan_expires_at": row["plan_expires_at"] if "plan_expires_at" in keys else None}
