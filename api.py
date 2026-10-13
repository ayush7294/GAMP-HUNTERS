"""
api.py - minimal FastAPI connection between the UI and moderation.py (with logging)

Run from the repo root:
    pip install fastapi "uvicorn[standard]"
    uvicorn api:app --reload --port 8000

Logs go to log/api.log (rotating, ~5 MB x 3 files) and to the console.
Message text is never logged, only metadata (length, outcome, timing).
"""
import asyncio
import logging
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from moderation import moderate_text

# ---------------- Logging ----------------
Path("log").mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("api")
logger.setLevel(logging.INFO)
logger.propagate = False  # keep separate from moderation.log

_fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
_file = RotatingFileHandler("log/api.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_file.setFormatter(_fmt)
_console = logging.StreamHandler()
_console.setFormatter(_fmt)
if not logger.handlers:
    logger.addHandler(_file)
    logger.addHandler(_console)

# ---------------- App ----------------
app = FastAPI(title="Moderation API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict to your UI's address in production
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    logger.info("API started")


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


# ---------------- Schemas ----------------
class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class MessageOut(BaseModel):
    allowed: bool
    reason: str = ""
    text: str | None = None


# ---------------- Routes ----------------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/messages", response_model=MessageOut)
async def post_message(body: MessageIn, request: Request):
    req_id = getattr(request.state, "req_id", "-")
    text = body.text.strip()
    if not text:
        logger.warning(f"[{req_id}] rejected | empty message")
        raise HTTPException(422, "Message is empty.")

    # moderate_text is blocking (network + retries), so run it in a thread
    t0 = time.perf_counter()
    mod = await asyncio.to_thread(moderate_text, text)
    mod_ms = (time.perf_counter() - t0) * 1000

    status = mod["status"]
    if status == "blocked":
        logger.warning(f"[{req_id}] BLOCKED by Gemini safety | chars: {len(text)} | moderation: {mod_ms:.0f} ms | {mod.get('reason')}")
        return MessageOut(allowed=False, reason="Your message violates our content rules.")

    if status != "ok":
        # Fail closed: don't let unchecked text through
        logger.error(f"[{req_id}] moderation FAILED (fail-closed) | chars: {len(text)} | moderation: {mod_ms:.0f} ms | {mod.get('reason')}")
        raise HTTPException(503, "Moderation is temporarily unavailable. Please try again.")

    r = mod["result"]
    if r["is_harmful"] or r["is_illegal"] or r["is_abusive"]:
        label = ", ".join(r["categories"]) or "policy violation"
        logger.warning(
            f"[{req_id}] BLOCKED | chars: {len(text)} | severity: {r['severity']} | categories: {label} "
            f"| harmful: {r['is_harmful']} illegal: {r['is_illegal']} abusive: {r['is_abusive']} | moderation: {mod_ms:.0f} ms"
        )
        return MessageOut(allowed=False, reason=f"Message blocked ({label}).")

    logger.info(f"[{req_id}] ALLOWED | chars: {len(text)} | severity: {r['severity']} | moderation: {mod_ms:.0f} ms")
    return MessageOut(allowed=True, text=text)
