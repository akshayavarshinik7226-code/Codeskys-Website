"""Database-backed token-bucket rate limiting.

Stored in SQLite rather than memory so the limit holds across worker processes
and survives a restart — an in-memory counter would reset every time the app
reloads, which is exactly when a brute-force attempt would benefit.
"""
import time

from .db import utcnow


def rate_limit(conn, bucket: str, limit: int, per_seconds: int, cost: float = 1.0) -> bool:
    """Consume one token. Returns True when the caller is over the limit.

    ``limit`` tokens refill evenly over ``per_seconds``.
    """
    now = time.time()
    rate = float(limit) / float(per_seconds)
    row = conn.execute("SELECT tokens, updated_at FROM rate_limits WHERE bucket = ?", (bucket,)).fetchone()
    if row is None:
        tokens = float(limit)
        last = now
    else:
        tokens = row["tokens"]
        try:
            last = float(row["updated_at"])
        except (TypeError, ValueError):
            last = now
        tokens = min(float(limit), tokens + (now - last) * rate)
    if tokens < cost:
        conn.execute(
            "INSERT INTO rate_limits (bucket, tokens, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(bucket) DO UPDATE SET tokens = excluded.tokens, updated_at = excluded.updated_at",
            (bucket, tokens, str(now)))
        conn.commit()
        return True
    tokens -= cost
    conn.execute(
        "INSERT INTO rate_limits (bucket, tokens, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(bucket) DO UPDATE SET tokens = excluded.tokens, updated_at = excluded.updated_at",
        (bucket, tokens, str(now)))
    conn.commit()
    return False


def reset(conn, bucket: str):
    conn.execute("DELETE FROM rate_limits WHERE bucket = ?", (bucket,))
    conn.commit()


def prune(conn, older_than_seconds: int = 86400):
    cutoff = str(time.time() - older_than_seconds)
    conn.execute("DELETE FROM rate_limits WHERE updated_at < ?", (cutoff,))
    conn.commit()
