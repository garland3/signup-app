from app.models.user import User
from app.models.api_key import APIKey


def test_user_model_fields():
    cols = {c.name for c in User.__table__.columns}
    assert cols == {"id", "email", "display_name", "created_at", "updated_at"}


def test_api_key_model_fields():
    cols = {c.name for c in APIKey.__table__.columns}
    expected = {
        "id", "user_id", "name", "prefix", "key_hash",
        "created_at", "last_used_at", "expires_at",
        "rate_limit", "scopes", "is_active", "revoked_at",
    }
    assert cols == expected
