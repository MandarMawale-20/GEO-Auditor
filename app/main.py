"""FastAPI application entrypoint for GEO Auditor.

Public-launch upgrade:
  * CORS configured for the Cloudflare Pages frontend domain.
  * Rate limiting via slowapi (5 requests/hour/IP on /audit).
  * SSRF-safe URL validation before any fetch.
  * JSON API endpoint at POST /audit (with optional LLM recommendations).
  * /health returns the spec-required schema.
  * GET /audit/html/{report_id} returns a standalone HTML snapshot.
  * Stateless share links: report JSON is compressed + base64url-encoded
    into a URL hash fragment (no database required).

Lifespan:
  * On startup, the shared ``httpx.AsyncClient`` is initialized with
    connection pooling and SSRF-safe defaults.
  * On shutdown, the client is gracefully closed.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import gzip
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

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

# Public frontend (Cloudflare Pages). Used for redirects now that this API
# service no longer renders HTML itself.
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://geoauditor.pages.dev").rstrip("/")
from app.orchestrator import run_audit
from app.scoring import validate_total_weight
from app.services.http_client import shutdown_http_client, startup_http_client
from app.services.pdf_service import generate_pdf_bytes
from app.services.url_validator import normalize_and_validate

from cachetools import TTLCache

validate_total_weight()

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_REPORTS_DIR = BASE_DIR.parent / "sample_reports"

logger = logging.getLogger("geo_auditor.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the shared HTTP client lifecycle."""
    await startup_http_client()
    logger.info("GEO Auditor application started.")
    yield
    await shutdown_http_client()
    logger.info("GEO Auditor application shut down.")


app = FastAPI(
    title="GEO Auditor",
    description="Generative Engine Optimization audit tool — is your site visible to AI answer engines?",
    version="1.0.0",
    lifespan=lifespan,
)

# The Cloudflare Pages frontend is the primary host for the site, but this
# backend also serves the same static assets (CSS/JS/favicons) and the
# pre-generated sample reports so the legacy /report/sample/{id} route and any
# HTML served here directly stay fully styled without depending on Pages.
_FRONTEND_DIR = BASE_DIR.parent / "frontend"
_FRONTEND_STATIC_DIR = _FRONTEND_DIR / "static"
_FRONTEND_SAMPLES_DIR = _FRONTEND_DIR / "samples"
if _FRONTEND_STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_STATIC_DIR)), name="static")
if _FRONTEND_SAMPLES_DIR.exists():
    app.mount("/samples", StaticFiles(directory=str(_FRONTEND_SAMPLES_DIR), html=True), name="samples")

# Keep logs on stdout so they show up the same way in Uvicorn and the IDE.
logger = logging.getLogger("geo_auditor")
logger.setLevel(logging.INFO)
logger.handlers.clear()
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)
logger.addHandler(handler)
logger.propagate = False


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Hardcoded default keeps the production Pages frontend working out of the box.
# Override via CORS_ORIGINS (comma-separated) if your Pages domain differs.
ALLOWED_ORIGINS: list[str] = [
    "https://geoauditor.pages.dev",
]
_cors_env = os.getenv("CORS_ORIGINS")
if _cors_env:
    ALLOWED_ORIGINS.extend(
        [o.strip() for o in _cors_env.split(",") if o.strip()]
    )
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


# ---------------------------------------------------------------------------
# Rate limiting (slowapi)
# ---------------------------------------------------------------------------
# Rate limiting is applied PER-ROUTE via @limiter.limit(...) and only on the
# audit endpoints (/audit and /api/audit). No default_limits is configured, so
# the SlowAPIMiddleware passes every other route (/, /health, /static, /samples,
# /report/sample/{id}, /api/render-shared) through unthrottled.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """Return a branded 429 page for browser requests, JSON for API clients."""
    logger.warning("Rate limit exceeded for %s on %s", request.client.host, request.url.path)

    # Browser requests get a short HTML notice; API requests get JSON.
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


# ---------------------------------------------------------------------------
# 404 — custom HTML error page (replaces FastAPI's default JSON 404)
# ---------------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """Render a branded 404 page for missing routes; pass through other codes."""
    if exc.status_code == 404:
        logger.info("404 — %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=404,
            content={"detail": exc.detail if isinstance(exc.detail, str) else "Not Found"},
        )
    # Non-404 HTTP exceptions fall back to the default JSON response.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


# ---------------------------------------------------------------------------
# 500 — custom HTML error page for unhandled exceptions
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Render a branded 500 page for browser requests; JSON for API clients."""
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
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# Log each request and surface failures with a traceback.
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info("%s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
        logger.info("%s %s -> %s", request.method, request.url.path, response.status_code)
        return response
    except Exception:  # pragma: no cover - logs and re-raises
        logger.exception(
            "Exception handling request %s %s", request.method, request.url.path
        )
        raise


# In-memory cache for reports used by the PDF and HTML download endpoints.
REPORT_CACHE: TTLCache = TTLCache(maxsize=1000, ttl=3600)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "version": "1.0.0",
            "llm_enabled": settings.llm_enabled,
        }
    )


# ---------------------------------------------------------------------------
# Landing page — redirect to the Cloudflare Pages frontend
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index() -> RedirectResponse:
    return RedirectResponse(url=FRONTEND_BASE_URL, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


# ---------------------------------------------------------------------------
# Audit endpoints
# ---------------------------------------------------------------------------
@app.post("/audit", response_class=JSONResponse)
@limiter.limit("5/hour")
async def audit_form(
    request: Request,
    url: str = Form(...),
    llm_enhanced: Optional[str] = Form(default=None),
) -> JSONResponse:
    """Legacy HTML form POST endpoint.

    Delegates to the shared ``execute_and_cache_audit`` helper so the JSON API
    and form endpoint behave identically, then redirects the browser to a
    shareable report URL on the frontend.
    """
    use_llm = llm_enhanced == "on"
    report, err = await execute_and_cache_audit(url, use_llm=use_llm)
    if err:
        raise HTTPException(status_code=400, detail=err)

    encoded = encode_report_for_share(report)
    return RedirectResponse(
        url=f"{FRONTEND_BASE_URL}/report.html#{encoded}",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


async def execute_and_cache_audit(url: str, use_llm: bool = False) -> tuple[Optional[AuditReport], Optional[str]]:
    """Shared audit runner: validate, run, cache. Returns (report, error)."""
    normalized, err = await normalize_and_validate(url)
    if err:
        return None, err

    report = await run_audit(normalized, llm_enabled=use_llm)

    if report.report_id:
        REPORT_CACHE[report.report_id] = report

    return report, None


# ---------------------------------------------------------------------------
# API endpoint to render a shared report from a posted JSON payload
# ---------------------------------------------------------------------------
@app.post("/api/render-shared")
async def render_shared_report(request: Request) -> RedirectResponse:
    """Accept a decoded report JSON and redirect to a shareable frontend URL.

    The client-side share-link decoder now renders the report entirely in the
    browser, so this endpoint just re-encodes the payload into a hash fragment
    on the frontend and redirects there. No server rendering is involved.
    """
    raw_body = await request.body()
    try:
        data = json.loads(raw_body)
        report = AuditReport.model_validate(data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid report data: {exc}")

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
    """JSON API for the Cloudflare Pages frontend.

    Returns the full report as JSON, plus an absolute ``share_url`` pointing at
    the report viewer on the frontend with the stateless, encoded copy of the
    report in its URL hash fragment (no database).
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


# ---------------------------------------------------------------------------
# PDF download
# ---------------------------------------------------------------------------
@app.get("/audit/pdf/{report_id}")
async def download_pdf(request: Request, report_id: str) -> Response:
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
        headers={
            "Content-Disposition": f"attachment; filename=geo-audit-{report_id[:8]}.pdf",
        },
    )


# ---------------------------------------------------------------------------
# HTML download (standalone, embedded CSS)
# ---------------------------------------------------------------------------
@app.get("/audit/html/{report_id}", response_class=HTMLResponse)
async def download_html(request: Request, report_id: str) -> Response:
    report = REPORT_CACHE.get(report_id)
    if report is None:
        return _report_not_found_response()

    html_content = _render_offline_html_report(report)
    return HTMLResponse(
        content=html_content,
        headers={
            "Content-Disposition": f"attachment; filename=geo-audit-{report_id[:8]}.html",
        },
    )


def _render_offline_html_report(report: AuditReport) -> str:
    """Render a self-contained HTML snapshot with embedded CSS for offline sharing."""
    from app.services.html_export import render

    return render(report)


def _report_not_found_response() -> HTMLResponse:
    return HTMLResponse(
        content="<h1>Report not found</h1><p>This report has expired or is invalid. "
        '<a href="/">Run a new audit</a>.</p>',
        status_code=404,
    )


# ---------------------------------------------------------------------------
# Sample reports (legacy route)
# ---------------------------------------------------------------------------
@app.get("/report/sample/{report_id}", response_class=HTMLResponse)
async def sample_report(request: Request, report_id: str) -> HTMLResponse:
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


# ---------------------------------------------------------------------------
# Stateless share links (no database)
# ---------------------------------------------------------------------------
def encode_report_for_share(report: AuditReport) -> str:
    """Serialize a report to JSON, gzip-compress, and base64url-encode it.

    The result is safe to place in a URL hash fragment so the report can be
    shared without any server-side storage.
    """
    payload = json.dumps(report.model_dump(), separators=(",", ":"), default=str).encode("utf-8")
    compressed = gzip.compress(payload, compresslevel=9)
    return base64.urlsafe_b64encode(compressed).rstrip(b"=").decode("ascii")


@app.get("/report/share", response_class=HTMLResponse)
async def shared_report_page(request: Request) -> RedirectResponse:
    """Redirect to the report viewer on the frontend.

    The encoded payload lives in the URL hash fragment; the frontend JS decodes
    and renders it client-side, so this endpoint just hands off to the SPA.
    """
    return RedirectResponse(
        url=f"{FRONTEND_BASE_URL}/report.html",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )
