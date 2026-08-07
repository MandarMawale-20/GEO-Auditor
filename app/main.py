"""FastAPI application entrypoint for GEO Auditor."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
import logging
import sys

from app.config import TOTAL_POSSIBLE_SCORE, settings
from app.models import AuditReport, AuditRequest
from app.orchestrator import run_audit
from app.scoring import validate_total_weight
from app.services import llm_service
from app.services.pdf_service import generate_pdf_bytes

from cachetools import TTLCache

validate_total_weight()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
SAMPLE_REPORTS_DIR = BASE_DIR.parent / "sample_reports"

app = FastAPI(
    title="GEO Auditor",
    description="Generative Engine Optimization audit tool — is your site visible to AI answer engines?",
    version="1.0.0",
)

# Keep logs on stdout so they show up the same way in Uvicorn and the IDE.
logger = logging.getLogger("geo_auditor")
logger.setLevel(logging.INFO)
# Drop inherited handlers so reloads do not duplicate log lines.
logger.handlers.clear()
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)
logger.addHandler(handler)
logger.propagate = False


# Log each request and surface failures with a traceback.
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info("%s %s", request.method, request.url.path)
    try:
        response = await call_next(request)
        logger.info(
            "%s %s -> %s",
            request.method,
            request.url.path,
            response.status_code,
        )
        return response
    except Exception:  # pragma: no cover - logs and re-raises
        logger.exception(
            "Exception handling request %s %s",
            request.method,
            request.url.path,
        )
        raise

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# In-memory cache for reports used by the PDF endpoint.
# Stores up to 1000 reports, expires them after 1 hour (3600 seconds).
REPORT_CACHE: TTLCache = TTLCache(maxsize=1000, ttl=3600)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "llm_enabled": settings.llm_enabled,
            "max_score": TOTAL_POSSIBLE_SCORE,
        },
    )


@app.post("/audit", response_class=HTMLResponse)
async def audit(request: Request, url: str = Form(...)) -> HTMLResponse:
    validated_request = AuditRequest(url=url)
    report = await run_audit(validated_request.url)

    # Cache the report so the PDF endpoint can retrieve it without re-running.
    if report.report_id:
        REPORT_CACHE[report.report_id] = report

    return templates.TemplateResponse(
        "report.html",
        {
            "request": request,
            "report": report,
            "is_sample": False,
        },
    )


@app.get("/audit/pdf/{report_id}")
async def download_pdf(report_id: str) -> Response:
    report = REPORT_CACHE.get(report_id)
    if report is None:
        return HTMLResponse(
            content="<h1>Report not found</h1><p><a href='/'>Back to GEO Auditor</a></p>",
            status_code=404,
        )

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


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "llm_enabled": settings.llm_enabled,
            "groq_model": settings.groq_model if settings.llm_enabled else None,
            "scoring_total": TOTAL_POSSIBLE_SCORE,
            "app_env": settings.app_env,
        }
    )
