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
RATE_LIMIT_CREATE = int(os.getenv("RATE_LIMIT_CREATE", "20"))    # per minute per IP
RATE_LIMIT_READ   = int(os.getenv("RATE_LIMIT_READ",   "60"))    # per minute per IP
# Chunks get their own generous bucket so multi-chunk uploads don't exhaust the
# note-creation limit. Default 600 = 10 chunks/sec sustained, well above any
# realistic upload speed while still blocking abuse.
RATE_LIMIT_CHUNK  = int(os.getenv("RATE_LIMIT_CHUNK",  "600"))   # per minute per IP
THEME_IMAGE      = os.getenv("THEME_IMAGE", "https://emerald-group.co.uk/wp-content/uploads/2022/10/Emeralds-Group-Logo-Lighter-text.svg")
THEME_TEXT       = os.getenv("THEME_TEXT", "")
THEME_PAGE_TITLE = os.getenv("THEME_PAGE_TITLE", "Emerald Password Share")
THEME_FAVICON    = os.getenv("THEME_FAVICON", "https://www.emerald-group.co.uk/wp-content/uploads/2022/08/cropped-emerald-favicon-32x32.png")
IMPRINT_URL      = os.getenv("IMPRINT_URL", "")
IMPRINT_HTML     = os.getenv("IMPRINT_HTML", "")

# Chunked upload config
CHUNK_SIZE_LIMIT = int(os.getenv("CHUNK_SIZE_LIMIT", str(90 * 1024 * 1024)))
CHUNK_TTL        = int(os.getenv("CHUNK_TTL", "3600"))  # 1 hour

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

# ── Chunked upload models ─────────────────────────────────────────────────────
class ChunkUpload(BaseModel):
    upload_id: str
    chunk_index: int
    total_chunks: int
    data: str        # hex-encoded chunk data

class ChunkComplete(BaseModel):
    upload_id: str
    meta: str
    views: Optional[int] = None
    expiration: Optional[int] = None

    @field_validator("meta")
    @classmethod
    def meta_size(cls, v: str) -> str:
        if len(v.encode()) > META_LIMIT_BYTES:
            raise ValueError(f"meta exceeds {META_LIMIT_BYTES} bytes")
        return v

# ── Helpers ───────────────────────────────────────────────────────────────────
def generate_id() -> str:
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
    now = time.time()
    window = 60
    key = f"rl:{action}:{ip}"
    pipe = redis_client.pipeline()
    pipe.zremrangebyscore(key, 0, now - window)
    pipe.zadd(key, {str(now): now})
    pipe.zcard(key)
    pipe.expire(key, window + 1)
    results = await pipe.execute()
    count = results[2]
    return count <= limit

# ── Storage helpers ───────────────────────────────────────────────────────────
# Notes are stored as TWO Redis keys to avoid cjson's 8 MB string limit in Lua:
#
#   note:{id}          – small JSON: { meta, views, created }  (no contents)
#   note:{id}:contents – raw hex ciphertext string (arbitrarily large)
#
# The Lua script in consume_note only decodes the small JSON key, then fetches
# the contents key directly — completely bypassing the cjson ceiling.

async def _store_note(
    note_id: str,
    contents: str,
    meta: str,
    views: Optional[int],
    expiry_seconds: Optional[int],
) -> None:
    meta_key     = f"note:{note_id}"
    contents_key = f"note:{note_id}:contents"

    payload = {
        "meta": meta,
        "views": views,
        "created": int(time.time()),
    }

    pipe = redis_client.pipeline()
    pipe.set(meta_key,     json.dumps(payload))
    pipe.set(contents_key, contents)
    if expiry_seconds:
        pipe.expire(meta_key,     expiry_seconds)
        pipe.expire(contents_key, expiry_seconds)
    await pipe.execute()


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
        "chunk_size": CHUNK_SIZE_LIMIT,
        "rate_limit_chunk": RATE_LIMIT_CHUNK,
    }

@app.get("/api/live")
async def health():
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

    if len(note.contents.encode()) > SIZE_LIMIT_BYTES:
        raise HTTPException(status_code=413, detail="Note too large")

    if note.views is None and note.expiration is None:
        raise HTTPException(status_code=400, detail="At least views or expiration must be set")

    if not ALLOW_ADVANCED:
        note.views = 1
        note.expiration = None

    if note.views is not None:
        if note.views < 1 or note.views > MAX_VIEWS:
            raise HTTPException(status_code=400, detail=f"Views must be between 1 and {MAX_VIEWS}")
        note.expiration = None

    expiry_seconds: Optional[int] = None
    if note.expiration is not None:
        if note.expiration < 1 or note.expiration > MAX_EXPIRATION:
            raise HTTPException(status_code=400, detail=f"Expiration must be between 1 and {MAX_EXPIRATION} minutes")
        expiry_seconds = note.expiration * 60

    note_id = generate_id()
    await _store_note(note_id, note.contents, note.meta, note.views, expiry_seconds)

    log.info("action=create note_id=%s ip=%s views=%s expiration=%s", note_id, ip, note.views, note.expiration)
    return {"id": note_id}


# ── Chunked upload endpoints ───────────────────────────────────────────────────

@app.post("/api/chunks")
async def upload_chunk(chunk: ChunkUpload, request: Request):
    ip = get_client_ip(request)

    if not await check_rate_limit(ip, "chunk", RATE_LIMIT_CHUNK):
        log.warning("action=rate_limit_chunk ip=%s", ip)
        raise HTTPException(status_code=429, detail="Too many requests — slow down")

    if chunk.chunk_index < 0 or chunk.chunk_index >= chunk.total_chunks:
        raise HTTPException(status_code=400, detail="Invalid chunk_index")

    if not chunk.upload_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid upload_id")

    chunk_bytes = len(chunk.data.encode())
    if chunk_bytes > CHUNK_SIZE_LIMIT * 2:  # *2 because hex encoding doubles size
        raise HTTPException(status_code=413, detail="Chunk too large")

    chunk_key = f"chunk:{chunk.upload_id}:{chunk.chunk_index}"
    meta_key  = f"chunk_meta:{chunk.upload_id}"

    pipe = redis_client.pipeline()
    pipe.set(chunk_key, chunk.data, ex=CHUNK_TTL)
    pipe.set(meta_key, json.dumps({"total_chunks": chunk.total_chunks, "ip": ip}), ex=CHUNK_TTL)
    await pipe.execute()

    log.info(
        "action=chunk_received upload_id=%s chunk=%d/%d ip=%s",
        chunk.upload_id, chunk.chunk_index + 1, chunk.total_chunks, ip,
    )
    return {"ok": True, "chunk_index": chunk.chunk_index}


@app.post("/api/chunks/complete", response_model=CreateResponse)
async def complete_chunked_upload(body: ChunkComplete, request: Request):
    ip = get_client_ip(request)

    if not await check_rate_limit(ip, "create", RATE_LIMIT_CREATE):
        log.warning("action=rate_limit_complete ip=%s", ip)
        raise HTTPException(status_code=429, detail="Too many requests — slow down")

    if not body.upload_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid upload_id")

    meta_key = f"chunk_meta:{body.upload_id}"
    meta_raw = await redis_client.get(meta_key)
    if meta_raw is None:
        raise HTTPException(status_code=404, detail="Upload session not found or expired")

    upload_meta = json.loads(meta_raw)
    total_chunks = upload_meta["total_chunks"]

    chunk_keys = [f"chunk:{body.upload_id}:{i}" for i in range(total_chunks)]
    chunk_values = await redis_client.mget(*chunk_keys)

    missing = [i for i, v in enumerate(chunk_values) if v is None]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing chunks: {missing}")

    contents = "".join(chunk_values)

    # Total size check (hex string length / 2 ≈ bytes)
    approx_bytes = len(contents) // 2
    if approx_bytes > SIZE_LIMIT_BYTES:
        raise HTTPException(status_code=413, detail="Assembled payload too large")

    # Clean up chunk keys
    pipe = redis_client.pipeline()
    for k in chunk_keys:
        pipe.delete(k)
    pipe.delete(meta_key)
    await pipe.execute()

    if body.views is None and body.expiration is None:
        raise HTTPException(status_code=400, detail="At least views or expiration must be set")

    if not ALLOW_ADVANCED:
        body.views = 1
        body.expiration = None

    expiry_seconds: Optional[int] = None
    if body.views is not None:
        if body.views < 1 or body.views > MAX_VIEWS:
            raise HTTPException(status_code=400, detail=f"Views must be between 1 and {MAX_VIEWS}")
        body.expiration = None
    if body.expiration is not None:
        if body.expiration < 1 or body.expiration > MAX_EXPIRATION:
            raise HTTPException(status_code=400, detail=f"Expiration must be between 1 and {MAX_EXPIRATION} minutes")
        expiry_seconds = body.expiration * 60

    note_id = generate_id()
    await _store_note(note_id, contents, body.meta, body.views, expiry_seconds)

    log.info(
        "action=chunked_create note_id=%s ip=%s chunks=%d views=%s expiration=%s",
        note_id, ip, total_chunks, body.views, body.expiration,
    )
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

    meta_key     = f"note:{note_id}"
    contents_key = f"note:{note_id}:contents"

    # Atomic read-decrement-or-delete on the small metadata key only.
    # The contents key is fetched separately — no cjson involved for large payloads.
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
    redis.call('SET', key, cjson.encode(data), 'KEEPTTL')
  end
end
return cjson.encode(data)
"""
    result = await redis_client.eval(lua_script, 1, meta_key)
    if result is None:
        log.info("action=consume_not_found note_id=%s ip=%s", note_id, ip)
        raise HTTPException(status_code=404, detail="Note not found or already deleted")

    meta_data = json.loads(result)
    remaining  = meta_data.get("views", "time-based")

    # Fetch contents separately (bypasses cjson entirely)
    contents = await redis_client.get(contents_key)

    # If the note was view-based and just got deleted, clean up the contents key too
    if meta_data.get("views") == 0:
        await redis_client.delete(contents_key)

    if contents is None:
        # Shouldn't happen in normal operation, but handle gracefully
        log.warning("action=consume_missing_contents note_id=%s ip=%s", note_id, ip)
        raise HTTPException(status_code=404, detail="Note not found or already deleted")

    log.info("action=consume note_id=%s ip=%s remaining_views=%s", note_id, ip, remaining)
    return {"contents": contents, "meta": meta_data["meta"]}


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
