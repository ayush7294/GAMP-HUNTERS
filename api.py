"""
api.py - FastAPI server: moderation + Neon PostgreSQL storage + accounts

Run from the repo root (so `log/` and the .env file resolve):
    pip install -r requirements.txt
    uvicorn api:app --reload --port 8000

Logs go to log/api.log (rotating) and the console. Message text, emails and
passwords are never logged.
"""
import asyncio
import ipaddress
import logging
import os
import re
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import psycopg
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import db  # loads .env and checks DATABASE_URL
import iplocation
from moderation import moderate_text

# ---------------- Config ----------------
FREE_MESSAGES = 3                    # guest messages allowed without location (UI reads this from /session)
MAX_LEN = 500
ALLOWED_RADII_KM = {10, 100, 1000}
ALLOWED_ORIGINS = ["*"]              # TODO: put your real UI address here before going public
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# IP-location backup. Behind a reverse proxy (Render, Nginx, Cloudflare...) set TRUST_PROXY_HEADERS=true.
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "").lower() in ("1", "true", "yes")
# Set to true to allow LAN/Docker addresses (192.168.x.x, 172.17.x.x) during local testing.
IP_LOCATION_ALLOW_PRIVATE = os.getenv("IP_LOCATION_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")

# ---------------- Logging ----------------
Path("log").mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("api")
logger.setLevel(logging.INFO)
logger.propagate = False
_fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
if not logger.handlers:
    _file = RotatingFileHandler("log/api.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _file.setFormatter(_fmt)
    _console = logging.StreamHandler()
    _console.setFormatter(_fmt)
    logger.addHandler(_file)
    logger.addHandler(_console)


# ---------------- App ----------------
async def purge_loop():
    while True:
        try:
            await asyncio.to_thread(db.purge)
            logger.info("purge finished")
        except Exception:
            logger.exception("purge failed")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(db.init_db)
    logger.info("API started, database ready")
    task = asyncio.create_task(purge_loop())
    yield
    task.cancel()


app = FastAPI(title="Nearby Chat API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    req_id = uuid.uuid4().hex[:8]
    request.state.req_id = req_id
    start = time.perf_counter()
    client = request.client.host if request.client else "-"
    try:
        response = await call_next(request)
    except Exception:
        ms = (time.perf_counter() - start) * 1000
        logger.exception(f"[{req_id}] {request.method} {request.url.path} | client: {client} | unhandled error | {ms:.0f} ms")
        raise
    ms = (time.perf_counter() - start) * 1000
    logger.info(f"[{req_id}] {request.method} {request.url.path} | client: {client} | status: {response.status_code} | {ms:.0f} ms")
    response.headers["X-Request-Id"] = req_id
    return response


# ---------------- Helpers ----------------
_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(key: str, limit: int, window_s: int = 60) -> None:
    now = time.time()
    q = _hits[key]
    while q and now - q[0] > window_s:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many requests. Please slow down.")
    q.append(now)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def valid_device(value: Optional[str]) -> str:
    if not value or not DEVICE_RE.match(value):
        raise HTTPException(400, "Missing or invalid X-Device-Id header.")
    return value


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


async def run_db(fn, *args):
    """Run a blocking db function in a thread; turn database errors into a clean 503."""
    try:
        return await asyncio.to_thread(fn, *args)
    except psycopg.Error:
        logger.exception(f"database error in {fn.__name__}")
        raise HTTPException(503, "The database is temporarily unavailable.")


async def current_user(authorization: Optional[str]):
    token = bearer_token(authorization)
    if not token:
        return None
    return await run_db(db.user_from_token, token)


# ---------------- Schemas ----------------
class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_LEN)
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)


class MessageOut(BaseModel):
    allowed: bool
    reason: str = ""
    id: Optional[int] = None
    text: Optional[str] = None
    created_at: Optional[float] = None
    sent: Optional[int] = None


class SignupIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)


# ---------------- Routes: status ----------------
@app.get("/health")
async def health():
    return {"ok": True, "db": await asyncio.to_thread(db.ping)}


@app.get("/session")
async def session(x_device_id: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
    """Lets the UI sync its free-message counter and login state with the server."""
    device = valid_device(x_device_id)
    user = await current_user(authorization)
    sent = await run_db(db.device_count, device)
    return {
        "account": {"name": user["name"]} if user else None,
        "sent": sent,
        "free_messages": FREE_MESSAGES,
    }


# ---------------- Routes: accounts ----------------
@app.post("/signup")
async def signup(body: SignupIn, request: Request):
    rate_limit("auth:" + client_ip(request), 10)
    email = body.email.strip().lower()
    user_id = await run_db(db.create_user, body.name.strip(), email, body.password)
    if user_id is None:
        raise HTTPException(409, "That email already has an account.")
    token = await run_db(db.create_session, user_id)
    logger.info(f"account created | user: {user_id}")
    return {"token": token, "name": body.name.strip()}


@app.post("/login")
async def login(body: LoginIn, request: Request):
    rate_limit("auth:" + client_ip(request), 10)
    user = await run_db(db.check_login, body.email.strip().lower(), body.password)
    if user is None:
        logger.warning(f"login failed | client: {client_ip(request)}")
        raise HTTPException(401, "Email or password is incorrect.")
    token = await run_db(db.create_session, user["id"])
    logger.info(f"login | user: {user['id']}")
    return {"token": token, "name": user["name"]}


@app.post("/logout")
async def logout(authorization: Optional[str] = Header(default=None)):
    token = bearer_token(authorization)
    if token:
        await run_db(db.delete_session, token)
    return {"ok": True}


# ---------------- Routes: messages ----------------
@app.post("/messages", response_model=MessageOut)
async def post_message(
    body: MessageIn,
    request: Request,
    x_device_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
):
    req_id = getattr(request.state, "req_id", "-")
    device = valid_device(x_device_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Message is empty.")
    if (body.lat is None) != (body.lon is None):
        raise HTTPException(422, "Send both lat and lon, or neither.")
    has_location = body.lat is not None

    rate_limit("msg-device:" + device, 10)
    rate_limit("msg-ip:" + client_ip(request), 30)

    user = await current_user(authorization)
    user_id = user["id"] if user else None
    who = f"user {user_id}" if user_id else "guest"

    # Server-side rule: after the free messages, location is mandatory.
    sent = await run_db(db.device_count, device)
    if sent >= FREE_MESSAGES and not has_location:
        logger.info(f"[{req_id}] location required | {who} | sent: {sent}")
        raise HTTPException(428, "location_required")

    # moderate_text is blocking (network + retries), so run it in a thread
    t0 = time.perf_counter()
    mod = await asyncio.to_thread(moderate_text, text)
    mod_ms = (time.perf_counter() - t0) * 1000
    status = mod["status"]

    if status == "blocked":
        logger.warning(f"[{req_id}] BLOCKED by Gemini safety | {who} | chars: {len(text)} | moderation: {mod_ms:.0f} ms | {mod.get('reason')}")
        return MessageOut(allowed=False, reason="Your message violates our content rules.", sent=sent)

    if status != "ok":
        # Fail closed: never publish unchecked text
        logger.error(f"[{req_id}] moderation FAILED (fail-closed) | {who} | chars: {len(text)} | moderation: {mod_ms:.0f} ms | {mod.get('reason')}")
        raise HTTPException(503, "Moderation is temporarily unavailable. Please try again.")

    r = mod["result"]
    if r["is_harmful"] or r["is_illegal"] or r["is_abusive"]:
        label = ", ".join(r["categories"]) or "policy violation"
        logger.warning(
            f"[{req_id}] BLOCKED | {who} | chars: {len(text)} | severity: {r['severity']} | categories: {label} "
            f"| harmful: {r['is_harmful']} illegal: {r['is_illegal']} abusive: {r['is_abusive']} | moderation: {mod_ms:.0f} ms"
        )
        return MessageOut(allowed=False, reason=f"Message blocked ({label}).", sent=sent)

    # Approved: store it. Coordinates are rounded to about 1 km before saving.
    lat = round(body.lat, 2) if has_location else None
    lon = round(body.lon, 2) if has_location else None
    saved = await run_db(db.record_message, device, user_id, text, lat, lon)
    logger.info(f"[{req_id}] ALLOWED + SAVED | {who} | chars: {len(text)} | severity: {r['severity']} | moderation: {mod_ms:.0f} ms | located: {has_location}")
    return MessageOut(allowed=True, text=text, id=saved["id"], created_at=saved["created_at"], sent=saved["sent"])


@app.get("/feed")
async def feed(
    request: Request,
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    radius_km: int = Query(10),
    limit: int = Query(50, ge=1, le=100),
    x_device_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
):
    if radius_km not in ALLOWED_RADII_KM:
        raise HTTPException(422, f"radius_km must be one of {sorted(ALLOWED_RADII_KM)}")
    device = valid_device(x_device_id)
    rate_limit("feed:" + client_ip(request), 60)

    user = await current_user(authorization)
    messages = await run_db(db.feed, lat, lon, radius_km, limit, device, user["id"] if user else None)
    return {"radius_km": radius_km, "messages": messages}
