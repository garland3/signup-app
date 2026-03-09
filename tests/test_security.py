from app.core.security import generate_api_key, hash_key


def test_generate_api_key_format():
    full_key, prefix = generate_api_key()
    assert full_key.startswith("sk-")
    assert len(full_key) == 51  # "sk-" + 48 hex chars
    assert prefix.startswith("sk-")
    assert len(prefix) == 8  # "sk-" + first 5 hex chars


def test_generate_api_key_unique():
    key1, _ = generate_api_key()
    key2, _ = generate_api_key()
    assert key1 != key2


def test_hash_key_deterministic():
    key = "sk-abc123"
    h1 = hash_key(key)
    h2 = hash_key(key)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex digest


def test_hash_key_different_inputs():
    h1 = hash_key("sk-aaa")
    h2 = hash_key("sk-bbb")
    assert h1 != h2
