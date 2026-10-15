"""
db.py - all PostgreSQL (Neon) access.

Every function here is plain synchronous code. api.py calls them through
asyncio.to_thread so they never block the event loop.

Needs DATABASE_URL in your .env, for example:
    DATABASE_URL=postgresql://user:password@ep-xxxx-pooler.region.aws.neon.tech/neondb?sslmode=require
"""
import hashlib
import hmac
import logging
import math
import os
import secrets
import time
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional if the variable is already set
    pass

logger = logging.getLogger("api")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing. Add your Neon connection string to the .env file.")

MESSAGE_TTL_DAYS = 7     # messages older than this are deleted (keeps you under the free 0.5 GB)
SESSION_TTL_DAYS = 30    # login sessions last this long


# ---------------------------------------------------------------- connection
def _connect() -> psycopg.Connection:
    """Open a connection. Neon may need a moment to wake up, so retry a couple of times."""
    last_error = None
    for attempt in range(3):
        try:
            return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
        except psycopg.OperationalError as err:
            last_error = err
            logger.warning(f"database connect attempt {attempt + 1} failed")
            time.sleep(0.5 * (attempt + 1))
    raise last_error


@contextmanager
def conn():
    """Commit on success, roll back on error, always close."""
    c = _connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


# -------------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);

-- One row per guest/browser. msg_count is the server-side free-message counter.
CREATE TABLE IF NOT EXISTS devices (
    id         TEXT PRIMARY KEY,
    user_id    BIGINT REFERENCES users(id) ON DELETE SET NULL,
    msg_count  INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    device_id  TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    user_id    BIGINT REFERENCES users(id) ON DELETE SET NULL,
    body       TEXT NOT NULL,
    lat        REAL,
    lon        REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_created ON messages (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_geo ON messages (lat, lon) WHERE lat IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at);
"""


def init_db() -> None:
    with conn() as c:
        c.execute(SCHEMA)


def ping() -> bool:
    try:
        with conn() as c:
            c.execute("SELECT 1")
        return True
    except Exception:
        logger.exception("database ping failed")
        return False


# ----------------------------------------------------------------- passwords
# scrypt ships with Python, so no extra package is needed.
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


_DUMMY_HASH = hash_password("not-a-real-password")  # used so unknown emails take as long as real ones


# --------------------------------------------------------------------- users
def create_user(name: str, email: str, password: str):
    """Returns the new user id, or None if the email is already registered."""
    pw_hash = hash_password(password)
    try:
        with conn() as c:
            row = c.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (%s, %s, %s) RETURNING id",
                (name, email, pw_hash),
            ).fetchone()
        return row["id"]
    except psycopg.errors.UniqueViolation:
        return None


def check_login(email: str, password: str):
    """Returns {'id', 'name'} if the credentials are right, otherwise None."""
    with conn() as c:
        user = c.execute("SELECT id, name, password_hash FROM users WHERE email = %s", (email,)).fetchone()
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return {"id": user["id"], "name": user["name"]}


# ------------------------------------------------------------------ sessions
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    """Creates a login session. Only a hash of the token is stored in the database."""
    token = secrets.token_urlsafe(32)
    with conn() as c:
        c.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) "
            "VALUES (%s, %s, now() + make_interval(days => %s))",
            (_hash_token(token), user_id, SESSION_TTL_DAYS),
        )
    return token


def user_from_token(token: str):
    with conn() as c:
        return c.execute(
            "SELECT u.id, u.name FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = %s AND s.expires_at > now()",
            (_hash_token(token),),
        ).fetchone()


def delete_session(token: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash_token(token),))


# ------------------------------------------------------------------ messages
def device_count(device_id: str) -> int:
    with conn() as c:
        row = c.execute("SELECT msg_count FROM devices WHERE id = %s", (device_id,)).fetchone()
    return row["msg_count"] if row else 0


def record_message(device_id: str, user_id, body: str, lat, lon) -> dict:
    """Saves an approved message and bumps the device's counter in one transaction."""
    with conn() as c:
        dev = c.execute(
            "INSERT INTO devices (id, user_id, msg_count) VALUES (%s, %s, 1) "
            "ON CONFLICT (id) DO UPDATE SET msg_count = devices.msg_count + 1, "
            "user_id = COALESCE(EXCLUDED.user_id, devices.user_id) "
            "RETURNING msg_count",
            (device_id, user_id),
        ).fetchone()
        msg = c.execute(
            "INSERT INTO messages (device_id, user_id, body, lat, lon) VALUES (%s, %s, %s, %s, %s) "
            "RETURNING id, EXTRACT(EPOCH FROM created_at)::float8 AS ts",
            (device_id, user_id, body, lat, lon),
        ).fetchone()
    return {"id": msg["id"], "created_at": msg["ts"], "sent": dev["msg_count"]}


FEED_SQL = """
SELECT id, body, ts, dist, mine FROM (
    SELECT m.id,
           m.body,
           m.created_at,
           EXTRACT(EPOCH FROM m.created_at)::float8 AS ts,
           COALESCE(m.device_id = %(device)s OR m.user_id = %(uid)s::bigint, false) AS mine,
           6371 * 2 * asin(sqrt(LEAST(1.0,
               power(sin(radians(m.lat - %(lat)s::float8) / 2), 2) +
               cos(radians(%(lat)s::float8)) * cos(radians(m.lat)) *
               power(sin(radians(m.lon - %(lon)s::float8) / 2), 2)
           ))) AS dist
    FROM messages m
    WHERE m.lat BETWEEN %(lat_min)s AND %(lat_max)s
      AND m.lon BETWEEN %(lon_min)s AND %(lon_max)s
      AND m.created_at > now() - make_interval(days => %(ttl)s)
) t
WHERE dist <= %(radius)s
ORDER BY created_at DESC
LIMIT %(limit)s
"""


def feed(lat: float, lon: float, radius_km: int, limit: int, device_id, user_id) -> list:
    """Recent messages within radius_km of (lat, lon), newest first.

    A cheap bounding box narrows the rows first, then the haversine distance does the exact check.
    (Searches that cross the 180 degree meridian are not handled in this first version.)
    """
    dlat = radius_km / 111.0
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    dlon = radius_km / (111.0 * cos_lat)
    lon_min, lon_max = (-180.0, 180.0) if dlon >= 180 else (lon - dlon, lon + dlon)

    params = {
        "device": device_id or "", "uid": user_id,
        "lat": lat, "lon": lon,
        "lat_min": lat - dlat, "lat_max": lat + dlat,
        "lon_min": lon_min, "lon_max": lon_max,
        "ttl": MESSAGE_TTL_DAYS, "radius": radius_km, "limit": limit,
    }
    with conn() as c:
        rows = c.execute(FEED_SQL, params).fetchall()
    return [
        {"id": r["id"], "text": r["body"], "created_at": r["ts"], "distance_km": round(r["dist"]), "mine": r["mine"]}
        for r in rows
    ]


# ------------------------------------------------------------------- cleanup
def purge() -> None:
    """Deletes expired messages and sessions. Run regularly to stay inside the free storage."""
    with conn() as c:
        c.execute("DELETE FROM messages WHERE created_at < now() - make_interval(days => %s)", (MESSAGE_TTL_DAYS,))
        c.execute("DELETE FROM sessions WHERE expires_at < now()")
