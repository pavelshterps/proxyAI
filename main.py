import time
import uuid
from pathlib import Path
import structlog
import redis.asyncio as redis_async
import httpx
from urllib.parse import urlparse

from fastapi import (
    FastAPI,
    UploadFile,
    File,
    Form,
    Body,                  # ⟵ NEW
    HTTPException,
    Header,
    Depends,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.security.api_key import APIKeyHeader
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from contextlib import asynccontextmanager

from config.settings import settings
from database import get_db, engine, init_models
from crud import create_upload_record, get_upload_for_user
from dependencies import get_current_user
from routes import router as api_router
from admin_routes import router as admin_router

from tasks import transcribe_segments, diarize_full, external_transcribe

# —————————————————————————————————————————————
# async‐contextmanager для старта/остановки приложения
# —————————————————————————————————————————————
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_models(engine)
    except Exception as e:
        structlog.get_logger().warning(f"init_models failed: {e}")
    yield

app = FastAPI(
    title="proxyAI",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# structlog
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

# rate limiter
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# CORS и TrustedHost
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        *settings.ALLOWED_ORIGINS_LIST,
        "https://transcriber-next.vercel.app",    # ⟵ NEW
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost"] + settings.ALLOWED_ORIGINS_LIST
)

# Redis Pub/Sub + key/value
redis = redis_async.from_url(settings.CELERY_BROKER_URL, decode_responses=True)

# API-Key → User
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Создание директорий
for d in (
    settings.UPLOAD_FOLDER,
    settings.RESULTS_FOLDER,
    settings.DIARIZER_CACHE_DIR,
):
    Path(d).mkdir(parents=True, exist_ok=True)

# Метрики Prometheus
HTTP_REQ_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path"],
)
HTTP_REQ_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["path"],
)

# Middleware для метрик
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    HTTP_REQ_COUNT.labels(request.method, request.url.path).inc()
    HTTP_REQ_LATENCY.labels(request.url.path).observe(time.time() - start)
    return response

# Здоровье и готовность
@app.get("/health")
@limiter.limit("30/minute")
async def health(request: Request):
    return {"status": "ok", "version": app.version}

@app.get("/ready")
async def ready():
    return {"status": "ready", "version": app.version}

@app.get("/metrics")
@limiter.limit("10/minute")
async def metrics(request: Request):
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)

# Отдача фронтенда
@app.get("/", include_in_schema=False)
async def root():
    return FileResponse("static/index.html")

# Проверка статуса обработки
@app.get(
    "/status/{upload_id}",
    summary="Check processing status",
    responses={401: {"description": "Invalid X-API-Key"}, 404: {"description": "Not found"}},
)
async def get_status(
    upload_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    rec = await get_upload_for_user(db, current_user.id, upload_id)
    if not rec:
        raise HTTPException(status_code=404, detail="upload_id not found")

    base = Path(settings.RESULTS_FOLDER) / upload_id
    done = (base / "transcript.json").exists() and (base / "diarization.json").exists()

    status_str = "done" if done else "processing"
    progress = await redis.get(f"progress:{upload_id}") or "0%"
    return {"status": status_str, "progress": progress}

# Приём и запуск фоновых задач
@app.post(
    "/upload/",
    dependencies=[Depends(get_current_user)],
    responses={401: {"description": "Invalid X-API-Key"}},
)
@limiter.limit("10/minute")
async def upload(
    request: Request,
    # — multipart/form-data (как раньше) —
    file: UploadFile | None = File(None),
    s3_url: str | None = Form(None),
    # — JSON-режим для внешнего фронта —
    id: str | None = Body(None),
    upload: str | None = Body(None),
    x_correlation_id: str | None = Header(None),
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    cid = x_correlation_id or str(uuid.uuid4())

    # 1) JSON-поход
    if upload is not None:
        if not id:
            raise HTTPException(status_code=400, detail="JSON must include 'id'")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(upload)
                resp.raise_for_status()
                data = resp.content
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to fetch '{upload}': {e}")
        ext = Path(urlparse(upload).path).suffix or ""
        upload_id = id
        dest = Path(settings.UPLOAD_FOLDER) / upload_id
        dest.write_bytes(data)

    # 2) S3-URL из формы
    elif s3_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(s3_url)
                resp.raise_for_status()
                data = resp.content
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to fetch from s3_url: {e}")
        ext = Path(urlparse(s3_url).path).suffix or ""
        upload_id = f"{uuid.uuid4().hex}{ext}"
        dest = Path(settings.UPLOAD_FOLDER) / upload_id
        dest.write_bytes(data)

    # 3) Обычная загрузка файла
    elif file:
        data = await file.read()
        if len(data) == 0:
            raise HTTPException(status_code=400, detail="File is empty")
        if len(data) > settings.MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail="File too large")
        ext = Path(file.filename).suffix or ""
        upload_id = f"{uuid.uuid4().hex}{ext}"
        dest = Path(settings.UPLOAD_FOLDER) / upload_id
        dest.write_bytes(data)

    else:
        raise HTTPException(status_code=400, detail="Must provide file, s3_url or JSON {'id','upload'}")

    # запись в БД и лог
    await create_upload_record(db, current_user.id, upload_id)
    log.bind(
        correlation_id=cid,
        upload_id=upload_id,
        user_id=current_user.id,
    ).info("upload accepted")

    # выбор внутренней/внешней транскрипции
    mode = settings.DEFAULT_TRANSCRIBE_MODE.lower()
    if mode == "external" and settings.EXTERNAL_API_URL:
        transcribe_segments.delay(upload_id, cid)
        diarize_full.delay(upload_id, cid)
    else:
        transcribe_segments.delay(upload_id, cid)
        diarize_full.delay(upload_id, cid)

    # инициализация прогресса
    await redis.publish(f"progress:{upload_id}", "0%")
    await redis.set(f"progress:{upload_id}", "0%")

    return JSONResponse(
        content={"upload_id": upload_id},
        headers={"X-Correlation-ID": cid},
    )

# Роутеры и статика
app.include_router(
    api_router,
    tags=["proxyAI"],
    dependencies=[Depends(get_current_user)],
)
app.include_router(admin_router)
app.mount("/static", StaticFiles(directory="static"), name="static")