import hashlib
import secrets


def generate_api_key() -> tuple[str, str]:
    """Generate an API key. Returns (full_key, prefix)."""
    random_part = secrets.token_hex(24)  # 48 hex chars
    full_key = f"sk-{random_part}"
    prefix = full_key[:8]  # "sk-" + first 5 hex chars
    return full_key, prefix


def hash_key(key: str) -> str:
    """SHA-256 hash of an API key."""
    return hashlib.sha256(key.encode()).hexdigest()
