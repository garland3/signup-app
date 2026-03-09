# API Key Manager Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a FastAPI app with PostgreSQL where users manage API keys (create, view, revoke) behind reverse proxy auth.

**Architecture:** FastAPI backend serves REST endpoints and static HTML/CSS/JS frontend. Auth via reverse proxy header injection (production) with debug bypass (development). PostgreSQL stores users and hashed API keys. Docker Compose orchestrates services.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0+ (async), PostgreSQL 16, plain HTML/CSS/JS, Docker Compose

---

### Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `app/__init__.py`

**Step 1: Create pyproject.toml**

```toml
[project]
name = "signup-app"
version = "0.1.0"
description = "API key management service"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.34.0",
    "sqlalchemy[asyncio]>=2.0.0",
    "asyncpg>=0.30.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "python-dotenv>=1.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
    "httpx>=0.27.0",
    "aiosqlite>=0.20.0",
]
```

**Step 2: Create .env.example**

```env
# Core
DEBUG_MODE=true
DATABASE_URL=postgresql+asyncpg://signup:signup@localhost:5432/signup_app

# Auth
AUTH_USER_HEADER=X-User-Email
TEST_USER=test@test.com
PROXY_SECRET_HEADER=X-Proxy-Secret
PROXY_SECRET=
FEATURE_PROXY_SECRET_ENABLED=false

# Feature Flags
FEATURE_KEY_LAST_USED=false
FEATURE_KEY_EXPIRATION=false
FEATURE_KEY_RATE_LIMIT=false
FEATURE_KEY_SCOPES=false
```

**Step 3: Create .gitignore**

```
__pycache__/
*.pyc
.venv/
.env
*.egg-info/
dist/
```

**Step 4: Create app/__init__.py**

Empty file.

**Step 5: Commit**

```bash
git init
git add pyproject.toml .env.example .gitignore app/__init__.py AGENTS.md docs/
git commit -m "feat: project scaffolding with dependencies and config"
```

---

### Task 2: Configuration (Pydantic Settings)

**Files:**
- Create: `app/core/__init__.py`
- Create: `app/core/config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`

**Step 1: Write the failing test**

```python
# tests/test_config.py
import os
import pytest


def test_default_settings():
    """Settings load with defaults when no env vars set."""
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("DEBUG_MODE", None)
    from app.core.config import Settings
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///test.db")
    assert s.DEBUG_MODE is False
    assert s.AUTH_USER_HEADER == "X-User-Email"
    assert s.TEST_USER == "test@test.com"
    assert s.FEATURE_KEY_LAST_USED is False
    assert s.FEATURE_KEY_EXPIRATION is False
    assert s.FEATURE_KEY_RATE_LIMIT is False
    assert s.FEATURE_KEY_SCOPES is False
    assert s.FEATURE_PROXY_SECRET_ENABLED is False


def test_debug_mode_enabled():
    from app.core.config import Settings
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///test.db", DEBUG_MODE=True)
    assert s.DEBUG_MODE is True
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.config'`

**Step 3: Write implementation**

```python
# app/core/__init__.py
```

```python
# app/core/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Core
    DEBUG_MODE: bool = False
    DATABASE_URL: str

    # Auth
    AUTH_USER_HEADER: str = "X-User-Email"
    TEST_USER: str = "test@test.com"
    PROXY_SECRET_HEADER: str = "X-Proxy-Secret"
    PROXY_SECRET: str = ""
    FEATURE_PROXY_SECRET_ENABLED: bool = False

    # Feature Flags
    FEATURE_KEY_LAST_USED: bool = False
    FEATURE_KEY_EXPIRATION: bool = False
    FEATURE_KEY_RATE_LIMIT: bool = False
    FEATURE_KEY_SCOPES: bool = False

    model_config = {"env_file": ".env", "extra": "ignore"}


settings: Settings | None = None


def get_settings() -> Settings:
    global settings
    if settings is None:
        settings = Settings()
    return settings
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/core/ tests/
git commit -m "feat: pydantic settings with feature flags"
```

---

### Task 3: Database Models

**Files:**
- Create: `app/database.py`
- Create: `app/models/__init__.py`
- Create: `app/models/user.py`
- Create: `app/models/api_key.py`
- Create: `tests/test_models.py`

**Step 1: Write the failing test**

```python
# tests/test_models.py
import pytest
import uuid
from datetime import datetime, timezone


def test_user_model_fields():
    from app.models.user import User
    cols = {c.name for c in User.__table__.columns}
    assert cols == {"id", "email", "display_name", "created_at", "updated_at"}


def test_api_key_model_fields():
    from app.models.api_key import APIKey
    cols = {c.name for c in APIKey.__table__.columns}
    expected = {
        "id", "user_id", "name", "prefix", "key_hash",
        "created_at", "last_used_at", "expires_at",
        "rate_limit", "scopes", "is_active", "revoked_at",
    }
    assert cols == expected
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write implementation**

```python
# app/database.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


engine = None
async_session_factory = None


def init_db(database_url: str):
    global engine, async_session_factory
    engine = create_async_engine(database_url, echo=False)
    async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session_factory() as session:
        yield session
```

```python
# app/models/__init__.py
from app.models.user import User
from app.models.api_key import APIKey

__all__ = ["User", "APIKey"]
```

```python
# app/models/user.py
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    api_keys = relationship("APIKey", back_populates="user")
```

```python
# app/models/api_key.py
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Integer, Boolean, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rate_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scopes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user = relationship("User", back_populates="api_keys")
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/database.py app/models/ tests/test_models.py
git commit -m "feat: SQLAlchemy models for users and api_keys"
```

---

### Task 4: Key Generation and Hashing

**Files:**
- Create: `app/core/security.py`
- Create: `tests/test_security.py`

**Step 1: Write the failing test**

```python
# tests/test_security.py
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
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_security.py -v`
Expected: FAIL — `ImportError`

**Step 3: Write implementation**

```python
# app/core/security.py
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
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_security.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/core/security.py tests/test_security.py
git commit -m "feat: API key generation and SHA-256 hashing"
```

---

### Task 5: Auth Middleware

**Files:**
- Create: `app/core/middleware.py`
- Create: `tests/test_middleware.py`

**Step 1: Write the failing test**

```python
# tests/test_middleware.py
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import AsyncClient, ASGITransport

from app.core.config import Settings
from app.core.middleware import AuthMiddleware


def _make_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware, settings=settings)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/me")
    async def me(request: Request):
        return {"email": request.state.user_email}

    return app


@pytest.mark.asyncio
async def test_health_no_auth_required():
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///test.db", DEBUG_MODE=False)
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_protected_route_no_header_returns_401():
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///test.db", DEBUG_MODE=False)
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_with_header():
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///test.db", DEBUG_MODE=False)
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me", headers={"X-User-Email": "alice@example.com"})
    assert r.status_code == 200
    assert r.json()["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_debug_mode_fallback():
    s = Settings(
        DATABASE_URL="sqlite+aiosqlite:///test.db",
        DEBUG_MODE=True,
        TEST_USER="debug@test.com",
    )
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["email"] == "debug@test.com"


@pytest.mark.asyncio
async def test_proxy_secret_required():
    s = Settings(
        DATABASE_URL="sqlite+aiosqlite:///test.db",
        DEBUG_MODE=False,
        FEATURE_PROXY_SECRET_ENABLED=True,
        PROXY_SECRET="mysecret",
    )
    app = _make_app(s)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Without secret
        r = await c.get("/api/me", headers={"X-User-Email": "a@b.com"})
        assert r.status_code == 401

        # With wrong secret
        r = await c.get(
            "/api/me",
            headers={"X-User-Email": "a@b.com", "X-Proxy-Secret": "wrong"},
        )
        assert r.status_code == 401

        # With correct secret
        r = await c.get(
            "/api/me",
            headers={"X-User-Email": "a@b.com", "X-Proxy-Secret": "mysecret"},
        )
        assert r.status_code == 200
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_middleware.py -v`
Expected: FAIL — `ImportError`

**Step 3: Write implementation**

```python
# app/core/middleware.py
import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import Settings

# Paths that do not require authentication
PUBLIC_PATHS = {"/api/health", "/api/keys/verify"}


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Allow public paths
        if path in PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)

        # Skip non-API paths (serve frontend)
        if not path.startswith("/api"):
            return await call_next(request)

        # Proxy secret check (production only)
        if (
            self.settings.FEATURE_PROXY_SECRET_ENABLED
            and not self.settings.DEBUG_MODE
        ):
            proxy_secret = request.headers.get(self.settings.PROXY_SECRET_HEADER, "")
            if not hmac.compare_digest(proxy_secret, self.settings.PROXY_SECRET):
                return JSONResponse(
                    {"detail": "Unauthorized"}, status_code=401
                )

        # Extract user email from header
        user_email = request.headers.get(self.settings.AUTH_USER_HEADER)

        # Debug mode fallback
        if not user_email and self.settings.DEBUG_MODE:
            user_email = self.settings.TEST_USER

        if not user_email:
            return JSONResponse(
                {"detail": "Unauthorized"}, status_code=401
            )

        request.state.user_email = user_email
        return await call_next(request)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_middleware.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/core/middleware.py tests/test_middleware.py
git commit -m "feat: auth middleware with reverse proxy and debug bypass"
```

---

### Task 6: Health and User Routes

**Files:**
- Create: `app/routes/__init__.py`
- Create: `app/routes/health.py`
- Create: `app/routes/users.py`
- Create: `tests/test_routes_health.py`
- Create: `tests/test_routes_users.py`

**Step 1: Write failing tests**

```python
# tests/test_routes_health.py
import pytest
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
async def test_health_endpoint():
    from tests.conftest import create_test_app
    app = create_test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

```python
# tests/test_routes_users.py
import pytest
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
async def test_me_returns_user_email():
    from tests.conftest import create_test_app
    app = create_test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/me", headers={"X-User-Email": "alice@example.com"})
    assert r.status_code == 200
    data = r.json()
    assert data["email"] == "alice@example.com"
    assert "id" in data
```

**Step 2: Create test conftest and verify tests fail**

```python
# tests/conftest.py
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base, get_session
from app.core.config import Settings


def create_test_app():
    """Create a FastAPI app configured for testing with in-memory SQLite."""
    from fastapi import FastAPI
    from app.core.middleware import AuthMiddleware
    from app.routes.health import router as health_router
    from app.routes.users import router as users_router

    settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DEBUG_MODE=False,
    )

    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    test_session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    app = FastAPI()
    app.state.settings = settings
    app.add_middleware(AuthMiddleware, settings=settings)
    app.include_router(health_router)
    app.include_router(users_router)

    @app.on_event("startup")
    async def setup_db():
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def override_get_session():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    return app
```

Run: `pytest tests/test_routes_health.py tests/test_routes_users.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Write implementation**

```python
# app/routes/__init__.py
```

```python
# app/routes/health.py
from fastapi import APIRouter

router = APIRouter(prefix="/api")


@router.get("/health")
async def health():
    return {"status": "ok"}
```

```python
# app/routes/users.py
from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_session
from app.models.user import User

router = APIRouter(prefix="/api")


async def get_or_create_user(email: str, session: AsyncSession) -> User:
    """Get existing user or create on first request."""
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=email)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


@router.get("/me")
async def me(request: Request, session: AsyncSession = Depends(get_session)):
    user = await get_or_create_user(request.state.user_email, session)
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "created_at": user.created_at.isoformat(),
    }
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routes_health.py tests/test_routes_users.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/routes/ tests/conftest.py tests/test_routes_health.py tests/test_routes_users.py
git commit -m "feat: health and user info endpoints with auto-create"
```

---

### Task 7: API Key CRUD Routes

**Files:**
- Create: `app/routes/keys.py`
- Create: `tests/test_routes_keys.py`

**Step 1: Write failing tests**

```python
# tests/test_routes_keys.py
import pytest
from httpx import AsyncClient, ASGITransport


@pytest.fixture
def app():
    from tests.conftest import create_test_app
    return create_test_app()


AUTH = {"X-User-Email": "alice@example.com"}


@pytest.mark.asyncio
async def test_create_key(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys", json={"name": "My Key"}, headers=AUTH)
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "My Key"
    assert data["key"].startswith("sk-")
    assert len(data["key"]) == 51
    assert "prefix" in data
    assert "id" in data


@pytest.mark.asyncio
async def test_list_keys_masked(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/keys", json={"name": "Key 1"}, headers=AUTH)
        await c.post("/api/keys", json={"name": "Key 2"}, headers=AUTH)
        r = await c.get("/api/keys", headers=AUTH)
    assert r.status_code == 200
    keys = r.json()
    assert len(keys) == 2
    for k in keys:
        assert "key" not in k  # Full key must never appear in list
        assert "prefix" in k
        assert "name" in k


@pytest.mark.asyncio
async def test_revoke_key(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        create_r = await c.post("/api/keys", json={"name": "Temp"}, headers=AUTH)
        key_id = create_r.json()["id"]
        r = await c.delete(f"/api/keys/{key_id}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["is_active"] is False


@pytest.mark.asyncio
async def test_update_key_name(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        create_r = await c.post("/api/keys", json={"name": "Old"}, headers=AUTH)
        key_id = create_r.json()["id"]
        r = await c.patch(f"/api/keys/{key_id}", json={"name": "New"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["name"] == "New"


@pytest.mark.asyncio
async def test_cannot_access_other_users_keys(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        create_r = await c.post(
            "/api/keys", json={"name": "Alice Key"}, headers=AUTH
        )
        key_id = create_r.json()["id"]
        r = await c.delete(
            f"/api/keys/{key_id}", headers={"X-User-Email": "bob@example.com"}
        )
    assert r.status_code == 404
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_routes_keys.py -v`
Expected: FAIL

**Step 3: Write implementation**

Update `tests/conftest.py` to include keys router:

Add to the `create_test_app` function after the users router import:
```python
    from app.routes.keys import router as keys_router
    # ...
    app.include_router(keys_router)
```

```python
# app/routes/keys.py
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_session
from app.models.api_key import APIKey
from app.models.user import User
from app.core.security import generate_api_key, hash_key
from app.routes.users import get_or_create_user

router = APIRouter(prefix="/api")


class CreateKeyRequest(BaseModel):
    name: str


class UpdateKeyRequest(BaseModel):
    name: str | None = None


def _key_response(key: APIKey, include_full_key: str | None = None) -> dict:
    data = {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "created_at": key.created_at.isoformat(),
        "is_active": key.is_active,
    }
    if include_full_key:
        data["key"] = include_full_key
    if key.last_used_at:
        data["last_used_at"] = key.last_used_at.isoformat()
    if key.revoked_at:
        data["revoked_at"] = key.revoked_at.isoformat()
    if key.expires_at:
        data["expires_at"] = key.expires_at.isoformat()
    return data


@router.post("/keys", status_code=201)
async def create_key(
    body: CreateKeyRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = await get_or_create_user(request.state.user_email, session)
    full_key, prefix = generate_api_key()
    api_key = APIKey(
        user_id=user.id,
        name=body.name,
        prefix=prefix,
        key_hash=hash_key(full_key),
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return _key_response(api_key, include_full_key=full_key)


@router.get("/keys")
async def list_keys(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = await get_or_create_user(request.state.user_email, session)
    result = await session.execute(
        select(APIKey)
        .where(APIKey.user_id == user.id)
        .order_by(APIKey.created_at.desc())
    )
    keys = result.scalars().all()
    return [_key_response(k) for k in keys]


@router.patch("/keys/{key_id}")
async def update_key(
    key_id: str,
    body: UpdateKeyRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = await get_or_create_user(request.state.user_email, session)
    result = await session.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user.id)
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="Key not found")
    if body.name is not None:
        api_key.name = body.name
    await session.commit()
    await session.refresh(api_key)
    return _key_response(api_key)


@router.delete("/keys/{key_id}")
async def revoke_key(
    key_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = await get_or_create_user(request.state.user_email, session)
    result = await session.execute(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user.id)
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="Key not found")
    api_key.is_active = False
    api_key.revoked_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(api_key)
    return _key_response(api_key)
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routes_keys.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/routes/keys.py tests/test_routes_keys.py tests/conftest.py
git commit -m "feat: API key CRUD endpoints with user isolation"
```

---

### Task 8: Key Verification Endpoint

**Files:**
- Modify: `app/routes/keys.py`
- Create: `tests/test_routes_verify.py`

**Step 1: Write failing test**

```python
# tests/test_routes_verify.py
import pytest
from httpx import AsyncClient, ASGITransport

AUTH = {"X-User-Email": "alice@example.com"}


@pytest.fixture
def app():
    from tests.conftest import create_test_app
    return create_test_app()


@pytest.mark.asyncio
async def test_verify_valid_key(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        create_r = await c.post("/api/keys", json={"name": "V"}, headers=AUTH)
        full_key = create_r.json()["key"]
        r = await c.post("/api/keys/verify", json={"key": full_key})
    assert r.status_code == 200
    assert r.json()["valid"] is True
    assert r.json()["user_email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_verify_invalid_key(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/keys/verify", json={"key": "sk-invalid"})
    assert r.status_code == 200
    assert r.json()["valid"] is False


@pytest.mark.asyncio
async def test_verify_revoked_key(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        create_r = await c.post("/api/keys", json={"name": "R"}, headers=AUTH)
        full_key = create_r.json()["key"]
        key_id = create_r.json()["id"]
        await c.delete(f"/api/keys/{key_id}", headers=AUTH)
        r = await c.post("/api/keys/verify", json={"key": full_key})
    assert r.status_code == 200
    assert r.json()["valid"] is False
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_routes_verify.py -v`
Expected: FAIL

**Step 3: Add verify endpoint to keys.py**

Append to `app/routes/keys.py`:

```python
class VerifyKeyRequest(BaseModel):
    key: str


@router.post("/keys/verify")
async def verify_key(
    body: VerifyKeyRequest,
    session: AsyncSession = Depends(get_session),
):
    key_hash_value = hash_key(body.key)
    result = await session.execute(
        select(APIKey).where(APIKey.key_hash == key_hash_value)
    )
    api_key = result.scalar_one_or_none()

    if not api_key or not api_key.is_active:
        return {"valid": False}

    # Check expiration if set
    if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
        return {"valid": False}

    # Load user for email
    user_result = await session.execute(
        select(User).where(User.id == api_key.user_id)
    )
    user = user_result.scalar_one()

    return {"valid": True, "user_email": user.email, "key_name": api_key.name}
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routes_verify.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/routes/keys.py tests/test_routes_verify.py
git commit -m "feat: key verification endpoint for downstream services"
```

---

### Task 9: FastAPI App Entrypoint

**Files:**
- Create: `app/main.py`

**Step 1: Write implementation**

```python
# app/main.py
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.middleware import AuthMiddleware
from app.database import init_db, create_tables
from app.routes.health import router as health_router
from app.routes.users import router as users_router
from app.routes.keys import router as keys_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db(settings.DATABASE_URL)
    await create_tables()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Signup App", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(AuthMiddleware, settings=settings)
    app.include_router(health_router)
    app.include_router(users_router)
    app.include_router(keys_router)

    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True))

    return app


app = create_app()
```

**Step 2: Verify the app starts**

Run: `python -c "from app.main import app; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add app/main.py
git commit -m "feat: FastAPI app entrypoint with lifespan and static serving"
```

---

### Task 10: Docker Setup

**Files:**
- Create: `docker-compose.yml`
- Create: `Dockerfile`

**Step 1: Create docker-compose.yml**

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: signup
      POSTGRES_PASSWORD: signup
      POSTGRES_DB: signup_app
    ports:
      - "5432:5432"
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U signup -d signup_app"]
      interval: 5s
      timeout: 3s
      retries: 5

  signup-app:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy

volumes:
  postgres-data:
```

**Step 2: Create Dockerfile**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY app/ app/
COPY static/ static/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Step 3: Commit**

```bash
git add docker-compose.yml Dockerfile
git commit -m "feat: Docker Compose with PostgreSQL and app container"
```

---

### Task 11: Frontend - HTML/CSS/JS

**Files:**
- Create: `static/index.html`
- Create: `static/style.css`
- Create: `static/app.js`

**Step 1: Create index.html**

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>API Keys</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <div class="container">
        <header>
            <h1>API Keys</h1>
            <span id="user-email"></span>
        </header>

        <div class="actions">
            <button id="create-btn" onclick="showCreateModal()">Create new key</button>
        </div>

        <table id="keys-table">
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Key</th>
                    <th>Created</th>
                    <th>Status</th>
                    <th></th>
                </tr>
            </thead>
            <tbody id="keys-body"></tbody>
        </table>

        <p id="no-keys" class="hidden">No API keys yet. Create one to get started.</p>
    </div>

    <!-- Create Key Modal -->
    <div id="create-modal" class="modal hidden">
        <div class="modal-content">
            <h2>Create API Key</h2>
            <label for="key-name">Name</label>
            <input type="text" id="key-name" placeholder="e.g. Production Server">
            <div class="modal-actions">
                <button onclick="hideCreateModal()">Cancel</button>
                <button class="primary" onclick="createKey()">Create</button>
            </div>
        </div>
    </div>

    <!-- Show Key Modal (one-time display) -->
    <div id="show-key-modal" class="modal hidden">
        <div class="modal-content">
            <h2>Your API Key</h2>
            <p class="warning">Copy this key now. You will not be able to see it again.</p>
            <div class="key-display">
                <code id="full-key"></code>
                <button onclick="copyKey()">Copy</button>
            </div>
            <div class="modal-actions">
                <button class="primary" onclick="hideShowKeyModal()">Done</button>
            </div>
        </div>
    </div>

    <script src="/static/app.js"></script>
</body>
</html>
```

**Step 2: Create style.css**

```css
* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f5f5f5;
    color: #1a1a1a;
    line-height: 1.5;
}

.container {
    max-width: 900px;
    margin: 40px auto;
    padding: 0 20px;
}

header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
}

header h1 {
    font-size: 24px;
    font-weight: 600;
}

#user-email {
    color: #666;
    font-size: 14px;
}

.actions {
    margin-bottom: 16px;
}

button {
    padding: 8px 16px;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    background: white;
    cursor: pointer;
    font-size: 14px;
}

button:hover {
    background: #f9fafb;
}

button.primary {
    background: #111;
    color: white;
    border-color: #111;
}

button.primary:hover {
    background: #333;
}

button.danger {
    color: #dc2626;
    border-color: #dc2626;
}

button.danger:hover {
    background: #fef2f2;
}

table {
    width: 100%;
    border-collapse: collapse;
    background: white;
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}

th, td {
    padding: 12px 16px;
    text-align: left;
    border-bottom: 1px solid #e5e7eb;
}

th {
    background: #f9fafb;
    font-weight: 500;
    font-size: 13px;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

td {
    font-size: 14px;
}

.key-prefix {
    font-family: monospace;
    background: #f3f4f6;
    padding: 2px 8px;
    border-radius: 4px;
}

.status-active {
    color: #059669;
}

.status-revoked {
    color: #dc2626;
}

.modal {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
}

.modal-content {
    background: white;
    padding: 24px;
    border-radius: 12px;
    width: 480px;
    max-width: 90vw;
}

.modal-content h2 {
    margin-bottom: 16px;
    font-size: 18px;
}

.modal-content label {
    display: block;
    font-size: 14px;
    margin-bottom: 4px;
    color: #374151;
}

.modal-content input {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    font-size: 14px;
    margin-bottom: 16px;
}

.modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
}

.warning {
    background: #fef3c7;
    border: 1px solid #fbbf24;
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 13px;
    margin-bottom: 12px;
}

.key-display {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
}

.key-display code {
    flex: 1;
    padding: 8px 12px;
    background: #f3f4f6;
    border-radius: 6px;
    font-size: 13px;
    word-break: break-all;
}

.hidden {
    display: none !important;
}

#no-keys {
    text-align: center;
    color: #6b7280;
    padding: 40px;
}
```

**Step 3: Create app.js**

```javascript
let currentKeys = [];

async function loadUser() {
    const r = await fetch("/api/me");
    if (r.ok) {
        const data = await r.json();
        document.getElementById("user-email").textContent = data.email;
    }
}

async function loadKeys() {
    const r = await fetch("/api/keys");
    if (!r.ok) return;
    currentKeys = await r.json();
    renderKeys();
}

function renderKeys() {
    const tbody = document.getElementById("keys-body");
    const noKeys = document.getElementById("no-keys");
    const table = document.getElementById("keys-table");

    if (currentKeys.length === 0) {
        table.classList.add("hidden");
        noKeys.classList.remove("hidden");
        return;
    }

    table.classList.remove("hidden");
    noKeys.classList.add("hidden");

    tbody.innerHTML = currentKeys.map(k => `
        <tr>
            <td>${escapeHtml(k.name)}</td>
            <td><span class="key-prefix">${escapeHtml(k.prefix)}...</span></td>
            <td>${formatDate(k.created_at)}</td>
            <td>
                <span class="${k.is_active ? 'status-active' : 'status-revoked'}">
                    ${k.is_active ? 'Active' : 'Revoked'}
                </span>
            </td>
            <td>
                ${k.is_active
                    ? `<button class="danger" onclick="revokeKey('${k.id}')">Revoke</button>`
                    : ''}
            </td>
        </tr>
    `).join("");
}

function showCreateModal() {
    document.getElementById("key-name").value = "";
    document.getElementById("create-modal").classList.remove("hidden");
    document.getElementById("key-name").focus();
}

function hideCreateModal() {
    document.getElementById("create-modal").classList.add("hidden");
}

async function createKey() {
    const name = document.getElementById("key-name").value.trim();
    if (!name) return;

    const r = await fetch("/api/keys", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name}),
    });

    if (!r.ok) return;

    const data = await r.json();
    hideCreateModal();

    document.getElementById("full-key").textContent = data.key;
    document.getElementById("show-key-modal").classList.remove("hidden");

    await loadKeys();
}

function hideShowKeyModal() {
    document.getElementById("show-key-modal").classList.add("hidden");
    document.getElementById("full-key").textContent = "";
}

async function copyKey() {
    const key = document.getElementById("full-key").textContent;
    await navigator.clipboard.writeText(key);
}

async function revokeKey(id) {
    if (!confirm("Revoke this API key? This cannot be undone.")) return;
    const r = await fetch(`/api/keys/${id}`, {method: "DELETE"});
    if (r.ok) await loadKeys();
}

function formatDate(iso) {
    return new Date(iso).toLocaleDateString("en-US", {
        year: "numeric", month: "short", day: "numeric",
    });
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// Init
loadUser();
loadKeys();
```

**Step 4: Test manually**

Run: `docker compose up -d postgres && cp .env.example .env && python -m uvicorn app.main:app --reload --port 8000`
Open: `http://localhost:8000` in browser. Verify you see the key management UI.

**Step 5: Commit**

```bash
git add static/
git commit -m "feat: frontend HTML/CSS/JS for API key management"
```

---

### Task 12: Run All Tests

**Step 1: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests PASS

**Step 2: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: test adjustments from integration run"
```

---

## Summary

| Task | What | Files |
|------|------|-------|
| 1 | Project scaffolding | pyproject.toml, .env.example, .gitignore |
| 2 | Pydantic settings | app/core/config.py |
| 3 | Database models | app/database.py, app/models/ |
| 4 | Key generation/hashing | app/core/security.py |
| 5 | Auth middleware | app/core/middleware.py |
| 6 | Health + user routes | app/routes/health.py, users.py |
| 7 | Key CRUD routes | app/routes/keys.py |
| 8 | Key verification | app/routes/keys.py (addition) |
| 9 | App entrypoint | app/main.py |
| 10 | Docker setup | docker-compose.yml, Dockerfile |
| 11 | Frontend | static/index.html, style.css, app.js |
| 12 | Integration test run | verify everything works |
