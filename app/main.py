"""FastAPI entrypoint for GEO Auditor."""
from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from cachetools import TTLCache
from fastapi import FastAPI, Form, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.models import AuditReport
from app.orchestrator import run_audit
from app.scoring import validate_total_weight
from app.services.http_client import shutdown_http_client, startup_http_client
from app.services.pdf_service import generate_pdf_bytes
from app.services.url_validator import normalize_and_validate

validate_total_weight()

# Frontend hosts the public UI; this service exposes the JSON API and serves
# the same static assets so legacy /report/sample/{id} stays styled.
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://geoauditor.pages.dev").rstrip("/")

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_REPORTS_DIR = BASE_DIR.parent / "sample_reports"

logger = logging.getLogger("geo_auditor")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup_http_client()
    logger.info("GEO Auditor started.")
    yield
    await shutdown_http_client()
    logger.info("GEO Auditor shut down.")


app = FastAPI(
    title="GEO Auditor",
    description="Generative Engine Optimization audit tool — is your site visible to AI answer engines?",
    version="1.0.0",
    lifespan=lifespan,
)

_FRONTEND_DIR = BASE_DIR.parent / "frontend"
_FRONTEND_STATIC_DIR = _FRONTEND_DIR / "static"
_FRONTEND_SAMPLES_DIR = _FRONTEND_DIR / "samples"
if _FRONTEND_STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_STATIC_DIR)), name="static")
if _FRONTEND_SAMPLES_DIR.exists():
    app.mount(
        "/samples",
        StaticFiles(directory=str(_FRONTEND_SAMPLES_DIR), html=True),
        name="samples",
    )

# Route logs to stdout so Uvicorn and the IDE show them the same way.
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.addHandler(handler)
logger.propagate = False

# Production Pages frontend is allowed by default. CORS_ORIGINS extends it for
# custom domains; the dev environment adds localhost variants automatically.
ALLOWED_ORIGINS: list[str] = ["https://geoauditor.pages.dev"]
if (cors_env := os.getenv("CORS_ORIGINS")):
    ALLOWED_ORIGINS.extend(o.strip() for o in cors_env.split(",") if o.strip())
if settings.app_env == "development":
    ALLOWED_ORIGINS.extend(
        ["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:3000"]
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Per-route rate limiting only; other endpoints are throttled at the edge or
# are static asset mounts.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """Branded 429 for browser requests, JSON for API clients."""
    logger.warning("Rate limit exceeded for %s on %s", request.client.host, request.url.path)

    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return HTMLResponse(
            content=(
                "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<title>Rate limited</title></head><body style=\"font-family:system-ui,sans-serif;"
                "padding:2rem;\"><h1>Too many requests</h1>"
                "<p>You have exceeded the audit rate limit (5 requests/hour). "
                "Please try again later.</p></body></html>"
            ),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "3600"},
        )
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "error": "rate_limited",
            "message": "You have exceeded the audit rate limit (5 requests/hour). Please try again later.",
            "retry_after": "3600",
        },
        headers={"Retry-After": "3600"},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """Custom 404 with the standard JSON shape; other codes pass through."""
    if exc.status_code == 404:
        logger.info("404 — %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=404,
            content={"detail": exc.detail if isinstance(exc.detail, str) else "Not Found"},
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Branded 500 for browser requests, JSON for API clients."""
    logger.exception("Unhandled exception: %s %s", request.method, request.url.path)

    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return HTMLResponse(
            content=(
                "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<title>Server error</title></head><body style=\"font-family:system-ui,sans-serif;"
                "padding:2rem;\"><h1>Internal server error</h1>"
                "<p>Something went wrong. Please try again later.</p></body></html>"
            ),
            status_code=500,
        )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info("%s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
        logger.info("%s %s -> %s", request.method, request.url.path, response.status_code)
        return response
    except Exception:
        logger.exception("Exception handling request %s %s", request.method, request.url.path)
        raise


# Cache the latest reports so the PDF/HTML download endpoints can serve them
# without re-running the audit. 1h TTL, max 1000 entries; cleared on restart.
REPORT_CACHE: TTLCache = TTLCache(maxsize=1000, ttl=3600)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "version": "1.0.0",
            "llm_enabled": settings.llm_enabled,
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> RedirectResponse:
    return RedirectResponse(url=FRONTEND_BASE_URL, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@app.post("/audit", response_class=JSONResponse)
@limiter.limit("5/hour")
async def audit_form(
    request: Request,
    url: str = Form(...),
    llm_enhanced: str | None = Form(default=None),
) -> RedirectResponse:
    """Legacy HTML form POST — delegates to the JSON runner then redirects."""
    use_llm = llm_enhanced == "on"
    report, err = await execute_and_cache_audit(url, use_llm=use_llm)
    if err:
        raise HTTPException(status_code=400, detail=err)

    encoded = encode_report_for_share(report)
    return RedirectResponse(
        url=f"{FRONTEND_BASE_URL}/report.html#{encoded}",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


async def execute_and_cache_audit(
    url: str, use_llm: bool = False
) -> tuple[AuditReport | None, str | None]:
    """Validate, run, cache. Returns (report, error)."""
    normalized, err = await normalize_and_validate(url)
    if err:
        return None, err

    report = await run_audit(normalized, llm_enabled=use_llm)

    if report.report_id:
        REPORT_CACHE[report.report_id] = report

    return report, None


@app.post("/api/render-shared")
async def render_shared_report(request: Request) -> RedirectResponse:
    """Re-encode a posted report payload and redirect to the report viewer.

    Decoding and rendering happen client-side; the server just produces a
    shareable URL hash fragment.
    """
    raw_body = await request.body()
    try:
        data = json.loads(raw_body)
        report = AuditReport.model_validate(data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid report data: {exc}") from exc

    encoded = encode_report_for_share(report)
    return RedirectResponse(
        url=f"{FRONTEND_BASE_URL}/report.html#{encoded}",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@app.get("/api/audit", response_class=JSONResponse)
@limiter.limit("5/hour")
async def audit_api(
    request: Request,
    url: str = Query(..., min_length=1, max_length=2048),
    llm_enhanced: bool = Query(default=False),
) -> JSONResponse:
    """JSON API for the static frontend.

    Returns the full report plus an absolute share_url that contains the
    encoded report in its hash fragment (no database).
    """
    report, err = await execute_and_cache_audit(url, use_llm=llm_enhanced)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if report is None:
        raise HTTPException(status_code=500, detail="Audit failed unexpectedly.")

    encoded = encode_report_for_share(report)

    return JSONResponse(
        {
            "ok": True,
            "report_id": report.report_id,
            "report": report.model_dump(),
            "share_url": f"{FRONTEND_BASE_URL}/report.html#{encoded}",
        }
    )


@app.get("/audit/pdf/{report_id}")
async def download_pdf(report_id: str) -> Response:
    report = REPORT_CACHE.get(report_id)
    if report is None:
        return _report_not_found_response()

    try:
        pdf_bytes = await run_in_threadpool(generate_pdf_bytes, report)
    except Exception:
        logger.exception("PDF generation failed for report_id=%s", report_id)
        return HTMLResponse(
            content="<h1>PDF generation failed</h1><p>Check server logs for details.</p>",
            status_code=500,
        )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=geo-audit-{report_id[:8]}.pdf"},
    )


@app.get("/audit/html/{report_id}", response_class=HTMLResponse)
async def download_html(report_id: str) -> Response:
    report = REPORT_CACHE.get(report_id)
    if report is None:
        return _report_not_found_response()

    html_content = _render_offline_html_report(report)
    return HTMLResponse(
        content=html_content,
        headers={"Content-Disposition": f"attachment; filename=geo-audit-{report_id[:8]}.html"},
    )


def _render_offline_html_report(report: AuditReport) -> str:
    from app.services.html_export import render
    return render(report)


def _report_not_found_response() -> HTMLResponse:
    return HTMLResponse(
        content="<h1>Report not found</h1><p>This report has expired or is invalid. "
        '<a href="/">Run a new audit</a>.</p>',
        status_code=404,
    )


@app.get("/report/sample/{report_id}", response_class=HTMLResponse)
async def sample_report(report_id: str) -> HTMLResponse:
    safe_id = "".join(c for c in report_id if c.isalnum() or c in ("_", "-"))
    if not safe_id:
        return HTMLResponse(
            content="<h1>Invalid sample ID</h1><p><a href='/'>Back to GEO Auditor</a></p>",
            status_code=400,
        )
    candidate = SAMPLE_REPORTS_DIR / f"{safe_id}.html"
    if not candidate.exists():
        return HTMLResponse(
            content=f"<h1>Sample report '{safe_id}' not found</h1>"
            f"<p><a href='/'>Back to GEO Auditor</a></p>",
            status_code=404,
        )
    return HTMLResponse(content=candidate.read_text(encoding="utf-8"))


def encode_report_for_share(report: AuditReport) -> str:
    """Serialize a report, gzip it, and base64url-encode it for a URL hash."""
    payload = json.dumps(report.model_dump(), separators=(",", ":"), default=str).encode("utf-8")
    compressed = gzip.compress(payload, compresslevel=9)
    return base64.urlsafe_b64encode(compressed).rstrip(b"=").decode("ascii")


@app.get("/report/share", response_class=HTMLResponse)
async def shared_report_page() -> RedirectResponse:
    """Redirect to the report viewer; the encoded payload lives in the URL hash."""
    return RedirectResponse(
        url=f"{FRONTEND_BASE_URL}/report.html",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )
