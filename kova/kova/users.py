import re
import sqlite3

from .db import utcnow
from .security import hash_password

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 12
MAX_PASSWORD = 128


class ValidationError(ValueError):
    pass


# ---------------------------------------------------------------- passwords

# A short list of the passwords people actually pick. A rule check alone would let
# "Password@12345" through, so the most common ones are refused by name.
COMMON_PASSWORDS = {
    "password", "password1", "password123", "passw0rd", "qwerty", "qwerty123", "qwertyuiop", "letmein",
    "welcome", "welcome1", "welcome123", "admin", "admin123", "administrator", "iloveyou", "abc123",
    "123456", "12345678", "123456789", "1234567890", "111111", "000000", "monkey", "dragon", "football",
    "changeme", "codeskys", "codeskys123", "student", "student123", "india123", "test1234", "asdfghjkl",
}
# Passwords that are only one of these words with digits/symbols/capitals added ("Password@12345").
COMMON_WORDS = {
    "password", "passwrd", "passw", "qwerty", "qwertyuiop", "letmein", "welcome", "admin", "administrator",
    "iloveyou", "codeskys", "student", "changeme", "india", "dragon", "monkey", "football", "asdfghjkl",
    "abcdef", "abcdefgh", "login", "master", "secret", "default",
}


def password_problems(password: str, email: str = "") -> list[str]:
    """Everything wrong with ``password``, in plain words. An empty list means it is acceptable."""
    pw = password or ""
    problems = []
    if len(pw) < MIN_PASSWORD:
        problems.append(f"at least {MIN_PASSWORD} characters")
    if len(pw) > MAX_PASSWORD:
        return [f"no more than {MAX_PASSWORD} characters"]
    if not re.search(r"[a-z]", pw):
        problems.append("a lowercase letter")
    if not re.search(r"[A-Z]", pw):
        problems.append("an uppercase letter")
    if not re.search(r"\d", pw):
        problems.append("a number")
    if not re.search(r"[^A-Za-z0-9]", pw):
        problems.append("a symbol such as ! @ # $ %")
    return problems


def check_password(password: str, email: str = "") -> None:
    """Raise ValidationError with a friendly message if the password is not strong enough."""
    pw = password or ""
    missing = password_problems(pw, email)
    if missing:
        raise ValidationError("Choose a stronger password. It needs " + _join(missing) + ".")
    squashed = re.sub(r"[^a-z0-9]", "", pw.lower())
    letters_only = re.sub(r"[^a-z]", "", pw.lower())
    if squashed in COMMON_PASSWORDS or pw.lower() in COMMON_PASSWORDS or letters_only in COMMON_WORDS:
        raise ValidationError("That password is too common. Pick something harder to guess.")
    local = re.sub(r"[^a-z0-9]", "", (email or "").split("@")[0].lower())
    if len(local) >= 4 and local in squashed:
        raise ValidationError("Your password should not contain your email name.")
    if len(set(pw)) < 5:
        raise ValidationError("That password repeats the same few characters. Pick something harder to guess.")


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------- email addresses

# Throw-away inboxes: they receive the code but exist only to dodge accountability.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "guerrillamail.biz",
    "sharklasers.com", "grr.la", "10minutemail.com", "10minutemail.net", "20minutemail.com", "tempmail.com",
    "temp-mail.org", "temp-mail.io", "tempmail.net", "tempmailo.com", "tempr.email", "throwawaymail.com",
    "yopmail.com", "yopmail.net", "yopmail.fr", "trashmail.com", "trashmail.net", "trash-mail.com",
    "getnada.com", "nada.email", "maildrop.cc", "dispostable.com", "fakeinbox.com", "mailnesia.com",
    "mintemail.com", "spamgourmet.com", "mohmal.com", "emailondeck.com", "burnermail.io", "moakt.com",
    "mailcatch.com", "mytemp.email", "tmail.ws", "tmpmail.org", "tmpmail.net", "discard.email",
    "inboxkitten.com", "harakirimail.com", "mail-temporary.com", "dropmail.me", "33mail.com",
    "byom.de", "spambox.us", "mailforspam.com", "jetable.org", "armyspy.com", "cuvox.de", "dayrep.com",
    "einrot.com", "fleckens.hu", "gustr.com", "rhyta.com", "superrito.com", "teleworm.us",
}
# Addresses that are obviously placeholders, not real mailboxes.
PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net", "test.com", "domain.com", "email.com", "sample.com",
    "yourdomain.com", "mydomain.com", "company.com", "abc.com", "xyz.com", "asdf.com", "nomail.com",
    "noemail.com", "none.com", "fake.com", "fakemail.com", "invalid.com",
}
RESERVED_TLDS = {"test", "example", "invalid", "localhost", "local", "internal", "lan", "home", "corp"}
PLACEHOLDER_LOCALS = {
    "test", "testing", "tester", "fake", "asdf", "asdfgh", "qwerty", "abc", "abcd", "abc123", "xyz", "none",
    "noemail", "nomail", "noreply", "na", "dummy", "sample", "email", "name", "temp", "temporary", "user",
    "username", "yourname", "youremail", "myemail", "someone", "nobody", "null", "123", "1234", "12345",
}
_LOCAL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._%+\-]{0,62}[a-z0-9])?$")
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def normalise_email(email: str) -> str:
    """Return the cleaned, lower-case address, or raise ValidationError saying what is wrong."""
    email = (email or "").strip().lower()
    if not email or len(email) > 254 or email.count("@") != 1:
        raise ValidationError("Enter a valid email address, like name@gmail.com.")
    local, domain = email.split("@")
    if not _LOCAL_RE.match(local) or ".." in local:
        raise ValidationError("That email address doesn't look right. Check it and try again.")
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(x) for x in labels):
        raise ValidationError("That email address doesn't look right. Check it and try again.")
    tld = labels[-1]
    if len(tld) < 2 or not (tld.isalpha() or tld.startswith("xn--")):
        raise ValidationError("That email address doesn't look right. Check it and try again.")
    if tld in RESERVED_TLDS or domain in PLACEHOLDER_DOMAINS or any(
            domain == d or domain.endswith("." + d) for d in ("example.com", "example.org", "example.net")):
        raise ValidationError("Use your real email address. Placeholder addresses can't be used.")
    if domain in DISPOSABLE_DOMAINS or any(domain.endswith("." + d) for d in DISPOSABLE_DOMAINS):
        raise ValidationError("Temporary or disposable email addresses can't be used. Use your real email address.")
    if local in PLACEHOLDER_LOCALS or len(set(local)) == 1 and len(local) >= 3:
        raise ValidationError("That looks like a made-up email address. Use your real email address.")
    return email


def create_user(db: sqlite3.Connection, email: str, name: str, password: str, role: str = "student",
                *, password_hash: str | None = None) -> int:
    """Create an account. ``password_hash`` lets a verified sign-up reuse the hash it already made."""
    email = (email or "").strip().lower()
    name = (name or "").strip()
    if not EMAIL_RE.match(email):
        raise ValidationError("Enter a valid email address.")
    if not name:
        raise ValidationError("Enter a name.")
    if role not in ("admin", "student"):
        raise ValidationError("Role must be admin or student.")
    if password_hash is None:
        if len(password or "") < MIN_PASSWORD:
            raise ValidationError(f"Password must be at least {MIN_PASSWORD} characters.")
        password_hash = hash_password(password)
    try:
        cur = db.execute(
            "INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?,?,?,?,?)",
            (email, name, role, password_hash, utcnow()),
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
