import json
import os
import secrets
import time
import logging
from contextlib import asynccontextmanager
from typing import Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, os.getenv("VERBOSITY", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("emerald")

# ── Config ────────────────────────────────────────────────────────────────────
REDIS_URL        = os.getenv("REDIS", "redis://redis/")
FRONTEND_PATH    = os.getenv("FRONTEND_PATH", "./frontend")
SIZE_LIMIT_BYTES = int(os.getenv("SIZE_LIMIT_BYTES", str(80 * 1024 * 1024)))  # 80 MiB
META_LIMIT_BYTES = int(os.getenv("META_LIMIT_BYTES", str(4 * 1024)))           # 4 KiB
MAX_VIEWS        = int(os.getenv("MAX_VIEWS", "100"))
MAX_EXPIRATION   = int(os.getenv("MAX_EXPIRATION", "360"))  # minutes
ALLOW_ADVANCED   = os.getenv("ALLOW_ADVANCED", "true").lower() == "true"
ALLOW_FILES      = os.getenv("ALLOW_FILES", "true").lower() == "true"
ID_LENGTH        = int(os.getenv("ID_LENGTH", "32"))
RATE_LIMIT_CREATE = int(os.getenv("RATE_LIMIT_CREATE", "20"))   # per minute per IP
RATE_LIMIT_READ   = int(os.getenv("RATE_LIMIT_READ",   "60"))   # per minute per IP
THEME_IMAGE      = os.getenv("THEME_IMAGE", "https://emerald-group.co.uk/wp-content/uploads/2022/10/Emeralds-Group-Logo-Lighter-text.svg")
THEME_TEXT       = os.getenv("THEME_TEXT", "")
THEME_PAGE_TITLE = os.getenv("THEME_PAGE_TITLE", "Emerald Password Share")
THEME_FAVICON    = os.getenv("THEME_FAVICON", "https://www.emerald-group.co.uk/wp-content/uploads/2022/08/cropped-emerald-favicon-32x32.png")
IMPRINT_URL      = os.getenv("IMPRINT_URL", "")
IMPRINT_HTML     = os.getenv("IMPRINT_HTML", "")

# Version: read from VERSION file baked in at build time, fall back to env, then default
def _read_version() -> str:
    try:
        with open(os.path.join(os.path.dirname(__file__), "VERSION")) as f:
            return f.read().strip()
    except FileNotFoundError:
        pass
    return os.getenv("APP_VERSION", "3.0.0")

VERSION = _read_version()

# ── Redis ─────────────────────────────────────────────────────────────────────
redis_client: aioredis.Redis = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis_client.ping()
        log.info("Connected to Redis at %s", REDIS_URL)
    except Exception as e:
        log.error("Cannot reach Redis: %s", e)
        raise
    yield
    await redis_client.aclose()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=512)

# ── Models ────────────────────────────────────────────────────────────────────
class NoteCreate(BaseModel):
    contents: str
    meta: str
    views: Optional[int] = None
    expiration: Optional[int] = None  # minutes

    @field_validator("meta")
    @classmethod
    def meta_size(cls, v: str) -> str:
        if len(v.encode()) > META_LIMIT_BYTES:
            raise ValueError(f"meta exceeds {META_LIMIT_BYTES} bytes")
        return v

class NotePublic(BaseModel):
    contents: str
    meta: str

class NoteInfo(BaseModel):
    meta: str

class CreateResponse(BaseModel):
    id: str

# ── Helpers ───────────────────────────────────────────────────────────────────
def generate_id() -> str:
    # token_urlsafe(n) produces ceil(n*4/3) URL-safe chars; request enough bytes
    # so we always have at least ID_LENGTH characters, then trim.
    raw = secrets.token_urlsafe(ID_LENGTH)
    return raw[:ID_LENGTH]

def get_client_ip(request: Request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip
    if request.client:
        return request.client.host
    return "unknown"

async def check_rate_limit(ip: str, action: str, limit: int) -> bool:
    """
    Sliding-window rate limiter using a Redis sorted set.
    Returns True if the request is allowed, False if the limit is exceeded.
    """
    now = time.time()
    window = 60  # 1 minute
    key = f"rl:{action}:{ip}"
    pipe = redis_client.pipeline()
    pipe.zremrangebyscore(key, 0, now - window)
    pipe.zadd(key, {str(now): now})
    pipe.zcard(key)
    pipe.expire(key, window + 1)
    results = await pipe.execute()
    count = results[2]
    return count <= limit

# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return {
        "version": VERSION,
        "max_size": SIZE_LIMIT_BYTES,
        "max_views": MAX_VIEWS,
        "max_expiration": MAX_EXPIRATION,
        "allow_advanced": ALLOW_ADVANCED,
        "allow_files": ALLOW_FILES,
        "imprint_url": IMPRINT_URL,
        "imprint_html": IMPRINT_HTML,
        "theme_image": THEME_IMAGE,
        "theme_text": THEME_TEXT,
        "theme_page_title": THEME_PAGE_TITLE,
        "theme_favicon": THEME_FAVICON,
    }

@app.get("/api/live")
async def health():
    """
    End-to-end health check: verifies Redis connectivity AND
    that the app can perform a real read/write round-trip.
    """
    try:
        probe_key = "healthcheck:probe"
        await redis_client.set(probe_key, "1", ex=5)
        val = await redis_client.get(probe_key)
        if val != "1":
            raise RuntimeError("Redis round-trip mismatch")
        return {"ok": True}
    except Exception as e:
        log.warning("Health check failed: %s", e)
        raise HTTPException(status_code=503, detail="Redis unreachable")

@app.post("/api/notes", response_model=CreateResponse)
async def create_note(note: NoteCreate, request: Request):
    ip = get_client_ip(request)

    if not await check_rate_limit(ip, "create", RATE_LIMIT_CREATE):
        log.warning("action=rate_limit_create ip=%s", ip)
        raise HTTPException(status_code=429, detail="Too many requests — slow down")

    # Size checks
    if len(note.contents.encode()) > SIZE_LIMIT_BYTES:
        raise HTTPException(status_code=413, detail="Note too large")
    # meta size is enforced by the Pydantic validator above

    if note.views is None and note.expiration is None:
        raise HTTPException(status_code=400, detail="At least views or expiration must be set")

    if not ALLOW_ADVANCED:
        note.views = 1
        note.expiration = None

    if note.views is not None:
        if note.views < 1 or note.views > MAX_VIEWS:
            raise HTTPException(status_code=400, detail=f"Views must be between 1 and {MAX_VIEWS}")
        note.expiration = None  # views takes priority

    expiry_seconds: Optional[int] = None
    if note.expiration is not None:
        if note.expiration < 1 or note.expiration > MAX_EXPIRATION:
            raise HTTPException(status_code=400, detail=f"Expiration must be between 1 and {MAX_EXPIRATION} minutes")
        expiry_seconds = note.expiration * 60

    note_id = generate_id()
    payload = {
        "contents": note.contents,
        "meta": note.meta,
        "views": note.views,
        "created": int(time.time()),
    }

    key = f"note:{note_id}"
    await redis_client.set(key, json.dumps(payload))
    if expiry_seconds:
        await redis_client.expire(key, expiry_seconds)

    log.info("action=create note_id=%s ip=%s views=%s expiration=%s", note_id, ip, note.views, note.expiration)
    return {"id": note_id}


@app.get("/api/notes/{note_id}", response_model=NoteInfo)
async def preview_note(note_id: str, request: Request):
    """Return note metadata without consuming a view."""
    ip = get_client_ip(request)

    if not await check_rate_limit(ip, "read", RATE_LIMIT_READ):
        log.warning("action=rate_limit_read ip=%s", ip)
        raise HTTPException(status_code=429, detail="Too many requests — slow down")

    key = f"note:{note_id}"
    raw = await redis_client.get(key)
    if raw is None:
        log.info("action=preview_not_found note_id=%s ip=%s", note_id, ip)
        raise HTTPException(status_code=404, detail="Note not found")
    data = json.loads(raw)
    log.info("action=preview note_id=%s ip=%s", note_id, ip)
    return {"meta": data["meta"]}


@app.delete("/api/notes/{note_id}", response_model=NotePublic)
async def consume_note(note_id: str, request: Request):
    """Consume (read + maybe destroy) a note."""
    ip = get_client_ip(request)

    if not await check_rate_limit(ip, "read", RATE_LIMIT_READ):
        log.warning("action=rate_limit_read ip=%s", ip)
        raise HTTPException(status_code=429, detail="Too many requests — slow down")

    key = f"note:{note_id}"

    # Atomic read-decrement-or-delete using a Lua script.
    # Uses KEEPTTL (Redis ≥ 6.0) so time-based expiry is preserved when
    # decrementing a view-counted note that also has a TTL set.
    lua_script = """
local key = KEYS[1]
local raw = redis.call('GET', key)
if not raw then
  return nil
end
local data = cjson.decode(raw)
if data.views then
  if data.views <= 1 then
    redis.call('DEL', key)
    data.views = 0
  else
    data.views = data.views - 1
    -- KEEPTTL preserves any existing TTL on the key (Redis >= 6.0)
    redis.call('SET', key, cjson.encode(data), 'KEEPTTL')
  end
end
return cjson.encode(data)
"""
    result = await redis_client.eval(lua_script, 1, key)
    if result is None:
        log.info("action=consume_not_found note_id=%s ip=%s", note_id, ip)
        raise HTTPException(status_code=404, detail="Note not found or already deleted")

    data = json.loads(result)
    remaining = data.get("views", "time-based")
    log.info("action=consume note_id=%s ip=%s remaining_views=%s", note_id, ip, remaining)
    return {"contents": data["contents"], "meta": data["meta"]}


# ── Static frontend (catch-all, must be last) ─────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware

class SPAMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if response.status_code == 404 and not request.url.path.startswith("/api"):
            return FileResponse(f"{FRONTEND_PATH}/index.html")
        return response

app.add_middleware(SPAMiddleware)
app.mount("/", StaticFiles(directory=FRONTEND_PATH, html=True), name="static")
