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
from pydantic import BaseModel

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
MAX_VIEWS        = int(os.getenv("MAX_VIEWS", "100"))
MAX_EXPIRATION   = int(os.getenv("MAX_EXPIRATION", "360"))  # minutes
ALLOW_ADVANCED   = os.getenv("ALLOW_ADVANCED", "true").lower() == "true"
ALLOW_FILES      = os.getenv("ALLOW_FILES", "true").lower() == "true"
ID_LENGTH        = int(os.getenv("ID_LENGTH", "32"))
THEME_IMAGE      = os.getenv("THEME_IMAGE", "https://emerald-group.co.uk/wp-content/uploads/2022/10/Emeralds-Group-Logo-Lighter-text.svg")
THEME_TEXT       = os.getenv("THEME_TEXT", "")
THEME_PAGE_TITLE = os.getenv("THEME_PAGE_TITLE", "Emerald Password Share")
THEME_FAVICON    = os.getenv("THEME_FAVICON", "https://www.emerald-group.co.uk/wp-content/uploads/2022/08/cropped-emerald-favicon-32x32.png")
IMPRINT_URL      = os.getenv("IMPRINT_URL", "")
IMPRINT_HTML     = os.getenv("IMPRINT_HTML", "")
VERSION          = "3.0.0"

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

class NotePublic(BaseModel):
    contents: str
    meta: str

class NoteInfo(BaseModel):
    meta: str

class CreateResponse(BaseModel):
    id: str

# ── Helpers ───────────────────────────────────────────────────────────────────
def generate_id() -> str:
    return secrets.token_urlsafe(ID_LENGTH)[:ID_LENGTH]

def get_client_ip(request: Request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip
    if request.client:
        return request.client.host
    return "unknown"

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
    try:
        await redis_client.ping()
        return {"ok": True}
    except Exception:
        raise HTTPException(status_code=503, detail="Redis unreachable")

@app.post("/api/notes", response_model=CreateResponse)
async def create_note(note: NoteCreate, request: Request):
    ip = get_client_ip(request)

    # Size check
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

    import json
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
    import json
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
    import json
    key = f"note:{note_id}"

    # Use a Lua script for atomic read-decrement-or-delete
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
    redis.call('SET', key, cjson.encode(data))
    -- preserve TTL if expiration-based
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
# Serve the SPA index.html for all unknown routes so /note/[id] works
from starlette.middleware.base import BaseHTTPMiddleware

class SPAMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # If a static file wasn't found, serve index.html
        if response.status_code == 404 and not request.url.path.startswith("/api"):
            return FileResponse(f"{FRONTEND_PATH}/index.html")
        return response

app.add_middleware(SPAMiddleware)
app.mount("/", StaticFiles(directory=FRONTEND_PATH, html=True), name="static")
