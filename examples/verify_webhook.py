"""Verify a Nullwake webhook in your receiver (any framework)."""
import hashlib
import hmac
import time


def verify(secret: str, headers: dict, body: bytes, tolerance_seconds: int = 300) -> bool:
    ts = headers.get("X-Nullwake-Timestamp", "")
    sig = headers.get("X-Nullwake-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > tolerance_seconds:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
