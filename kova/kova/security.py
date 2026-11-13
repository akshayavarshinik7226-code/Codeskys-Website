import hashlib
import hmac
import os
import re

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2,
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# Text that may come from infrastructure (an upstream error body, an exception
# message) before a student sees it: no addresses, paths, tokens or engine names.
_REDACTIONS = [
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer [redacted]"),
    (re.compile(r"\b\d+\|[A-Za-z0-9]{16,}"), "[redacted]"),                          # API token "id|secret"
    (re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+"), "[address]"),                    # URLs
    (re.compile(r"(?i)host\s*=\s*'[^']*'(?:\s*,\s*port\s*=\s*\d+)?"), "[address]"),  # urllib3
    (re.compile(r"(?i)\b(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}\b"
                r"|(?<![\w:])(?:[0-9a-f]{1,4}:)*:(?::?[0-9a-f]{1,4})+\b"), "[address]"),   # IPv6
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "[address]"),                # IPv4
    (re.compile(r"(?i)(?<![\w.-])[a-z][a-z0-9.-]*:\d{2,5}\b"), "[address]"),  # host:port
    (re.compile(r"(?<![\w.])/(?:data|var|etc|root|home|srv|opt|run|tmp|usr)/\S*"), "[path]"),
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "[redacted]"),                            # long secrets
    (re.compile(r"(?i)\bcoolify[\w.-]*"), "deployment engine"),
]


def redact(text) -> str:
    text = str(text or "")
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


# Used to keep login timing similar when the email does not exist.
DUMMY_HASH = hash_password("not-a-real-password")
