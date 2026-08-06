"""
PDF report generator using ReportLab Platypus.

Produces a text-first, print-quality A4 report: title, URL, score & grade,
executive summary, score table, top priorities, detailed findings,
recommendations, and copy-paste fixes as plain text blocks.

No HTML, CSS, progress bars, or decorative UI elements are rendered.
"""
from __future__ import annotations

import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    KeepTogether,
    Preformatted,
)

from app.models import AuditReport
import logging
import traceback

logger = logging.getLogger("geo_auditor.pdf")

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------
_PAGE_WIDTH, _PAGE_HEIGHT = A4
_MARGIN = inch  # 1-inch margins on all sides

_HEADER_TEXT = "GEO Auditor"

# ---------------------------------------------------------------------------
# Styles  —  modify built-in names in place; add new unique names
# ---------------------------------------------------------------------------
_styles = getSampleStyleSheet()

# Override built-in styles (getSampleStyleSheet pre-defines these).
_styles["Title"].fontSize = 24
_styles["Title"].leading = 28
_styles["Title"].spaceAfter = 6
_styles["Title"].alignment = TA_CENTER
_styles["Title"].textColor = colors.black

_styles.add(
    ParagraphStyle(
        name="Subtitle",
        parent=_styles["Normal"],
        fontSize=12,
        leading=16,
        spaceAfter=18,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#666666"),
    )
)

_styles["Normal"].fontSize = 10
_styles["Normal"].leading = 14
_styles["Normal"].spaceAfter = 4
_styles["Normal"].textColor = colors.black
_styles["Normal"].alignment = TA_LEFT

# Add new styles with unique names.
_styles.add(
    ParagraphStyle(
        name="SectionHeading",
        parent=_styles["Heading1"],
        fontSize=16,
        leading=22,
        spaceBefore=18,
        spaceAfter=10,
        textColor=colors.black,
        backColor=colors.white,
    )
)
_styles["SectionHeading"].underline = True

_styles.add(
    ParagraphStyle(
        name="SmallText",
        parent=_styles["Normal"],
        fontSize=8,
        leading=12,
        spaceAfter=2,
        textColor=colors.HexColor("#555555"),
        alignment=TA_LEFT,
    )
)
_styles.add(
    ParagraphStyle(
        name="CodeBlock",
        parent=_styles["Normal"],
        fontName="Courier",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#333333"),
        backColor=colors.HexColor("#f4f4f4"),
        leftIndent=8,
        rightIndent=8,
        spaceBefore=4,
        spaceAfter=4,
    )
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _escape_xml(text: str) -> str:
    """Escape characters that are special in reportlab's Paragraph XML markup."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _wrap_url(url: str) -> str:
    """Escape a URL for use inside a Paragraph.

    ReportLab's Paragraph wraps text naturally at ``/`` and ``.`` characters,
    so no special break tags are needed — we just escape XML metacharacters.
    """
    escaped = _escape_xml(url)
    return escaped


def _table_text(text: str) -> str:
    """Prepare plain text for a ReportLab Table cell.

    Table cells render as plain text (not XML), so we must NOT escape entities.
    Instead we strip any markup characters that ReportLab might interpret.
    """
    return str(text).replace("\n", " ").replace("\r", "")


def _append_finding(story: list, finding, numbered: int, include_points: bool = False):
    """Append a single finding as a KeepTogether block to the story."""
    blocks: list = []

    sev_label = finding.severity.value.upper()
    blocks.append(
        Paragraph(
            f"{numbered}. <b>{_escape_xml(finding.title)}</b>  <i>[{sev_label}]</i>",
            _styles["BodyText"],
        )
    )

    blocks.append(
        Paragraph(f"<b>Category:</b> {_escape_xml(finding.category)}", _styles["SmallText"])
    )

    blocks.append(
        Paragraph(
            f"<b>Why it matters:</b> {_escape_xml(finding.why_it_matters)}",
            _styles["BodyText"],
        )
    )

    blocks.append(
        Paragraph(f"<b>Evidence:</b> {_escape_xml(finding.evidence)}", _styles["BodyText"])
    )

    blocks.append(
        Paragraph(
            f"<b>Impact:</b> {finding.impact.value}  |  "
            f"<b>Effort:</b> {finding.effort.value}",
            _styles["SmallText"],
        )
    )

    if include_points:
        blocks.append(
            Paragraph(
                f"<b>Points:</b> {finding.points_awarded:.1f} / "
                f"{finding.points_possible:.1f} awarded",
                _styles["SmallText"],
            )
        )

    blocks.append(
        Paragraph(
            f"<b>Recommendation:</b> {_escape_xml(finding.recommendation)}",
            _styles["BodyText"],
        )
    )

    if finding.copy_paste_fix:
        blocks.append(Paragraph("<b>Copy-paste fix:</b>", _styles["SmallText"]))
        blocks.append(Preformatted(finding.copy_paste_fix, _styles["CodeBlock"]))

    blocks.append(Spacer(1, 6))
    story.append(KeepTogether(blocks))


# ---------------------------------------------------------------------------
# Page callback
# ---------------------------------------------------------------------------
def _make_page_canvas(canvas, doc):
    """Draw header and footer on every page."""
    canvas.saveState()
    # Header
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#888888"))
    canvas.drawString(_MARGIN, _PAGE_HEIGHT - _MARGIN + 6, _HEADER_TEXT)
    # Footer — page number (right) + timestamp (center)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#888888"))
    canvas.drawRightString(
        _PAGE_WIDTH - _MARGIN, _MARGIN - 6, f"Page {canvas.getPageNumber()}"
    )
    canvas.setFont("Helvetica-Oblique", 7)
    canvas.setFillColor(colors.HexColor("#999999"))
    canvas.drawCentredString(
        _PAGE_WIDTH / 2,
        _MARGIN - 16,
        f"Generated {doc._geo_report_ts}",
    )
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Story builder
# ---------------------------------------------------------------------------
def _build_story(report: AuditReport) -> list:
    """Build the list of flowables for the PDF body."""
    story: list = []

    display_url = report.final_url or report.url

    # ---- Title / URL -------------------------------------------------------
    story.append(Paragraph("GEO Audit Report", _styles["Title"]))
    story.append(Paragraph(_wrap_url(display_url), _styles["Subtitle"]))
    story.append(Spacer(1, 12))

    # ---- Executive Summary ------------------------------------------------
    story.append(Paragraph("Executive Summary", _styles["SectionHeading"]))
    summary_parts = [
        f"<b>Overall Score:</b> {report.total_score} / {report.max_score}",
        f"<b>Grade:</b> {report.grade} — {report.grade_label}",
        f"<b>URL:</b> {_wrap_url(display_url)}",
        f"<b>Generated:</b> {report.generated_at}",
        f"<b>Audit Duration:</b> {report.audit_duration_ms} ms",
    ]
    if report.llm_enhanced:
        summary_parts.append("<b>Scoring Mode:</b> Gemini-enhanced")
    elif report.llm_error:
        summary_parts.append(f"<b>Scoring Mode:</b> Deterministic fallback")
    else:
        summary_parts.append("<b>Scoring Mode:</b> Deterministic")
    for part in summary_parts:
        story.append(Paragraph(part, _styles["BodyText"]))

    story.append(Spacer(1, 8))

    # ---- Calibration note -------------------------------------------------
    story.append(
        Paragraph(
            (
                "<b>Calibration Note:</b> This audit measures on-page GEO readiness. "
                "Large brands may receive lower scores than their real-world AI visibility "
                "because off-page authority signals (Wikipedia, Knowledge Graph, "
                "backlinks, brand mentions) are intentionally excluded from this MVP."
            ),
            _styles["SmallText"],
        )
    )
    story.append(Spacer(1, 8))

    # ---- Score Breakdown Table ------------------------------------------
    story.append(Paragraph("Score Breakdown", _styles["SectionHeading"]))
    table_data = [
        ["Category", "Group", "Score", "%"],
    ]
    for section in report.sections:
        table_data.append(
            [
                _table_text(section.label),
                _table_text(section.group),
                f"{section.points_awarded:.1f} / {section.points_possible:.0f}",
                f"{section.percentage:.0f}%",
            ]
        )

    score_table = Table(
        table_data,
        colWidths=[2.8 * inch, 2.2 * inch, 0.9 * inch, 0.5 * inch],
        hAlign=TA_LEFT,
    )
    score_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("LEADING", (0, 0), (-1, -1), 12),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(score_table)
    story.append(Spacer(1, 10))

    # ---- Top Priorities ----------------------------------------------------
    story.append(Paragraph("Top Priorities", _styles["SectionHeading"]))
    if report.top_priorities:
        for i, finding in enumerate(report.top_priorities, 1):
            _append_finding(story, finding, numbered=i)
    else:
        story.append(Paragraph("No priority findings identified.", _styles["BodyText"]))

    story.append(Spacer(1, 8))

    # ---- Detailed Findings ----------------------------------------------
    story.append(Paragraph("Detailed Findings", _styles["SectionHeading"]))
    if report.all_findings:
        for i, finding in enumerate(report.all_findings, 1):
            _append_finding(story, finding, numbered=i, include_points=True)
    else:
        story.append(Paragraph("No detailed findings.", _styles["BodyText"]))

    # ---- AI Answerability Matrix (if present) ---------------------------
    if report.answerability_matrix:
        story.append(PageBreak())
        story.append(Paragraph("AI Answerability Matrix", _styles["SectionHeading"]))
        matrix_data = [["Question", "Status", "Evidence"]]
        for row in report.answerability_matrix:
            matrix_data.append(
                [
                    _table_text(row.question),
                    row.status.value.upper(),
                    _table_text(row.evidence or ""),
                ]
            )
        matrix_table = Table(
            matrix_data, colWidths=[2.2 * inch, 1.0 * inch, 3.8 * inch]
        )
        matrix_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("LEADING", (0, 0), (-1, -1), 10),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(matrix_table)

    story.append(Spacer(1, 8))

    # ---- Recommendations Recap -----------------------------------------
    story.append(Paragraph("Recommendations Recap", _styles["SectionHeading"]))
    seen_recs: set[str] = set()
    for finding in report.all_findings:
        if finding.severity.value in ("critical", "warning"):
            rec = finding.recommendation.strip()
            if rec and rec not in seen_recs:
                seen_recs.add(rec)
                story.append(
                    Paragraph(
                        f"<b>{_escape_xml(finding.title)}</b>: {_escape_xml(rec)}",
                        _styles["BodyText"],
                    )
                )

    return story


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def generate_pdf_bytes(report: AuditReport) -> bytes:
    """Render an AuditReport into a PDF document and return the raw bytes."""
    logger.info("Generating PDF for url=%s report_id=%s", report.url, getattr(report, "report_id", None))
    print(f"GENERATING PDF for url={report.url} report_id={getattr(report, 'report_id', None)}", flush=True)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN + 14,
        bottomMargin=_MARGIN + 14,
        title=f"GEO Audit Report — {report.final_url or report.url}",
        author="GEO Auditor",
    )

    # Stash the timestamp on the doc so the page canvas hook can render it.
    doc._geo_report_ts = report.generated_at

    story = _build_story(report)

    try:
        doc.build(
            story,
            onFirstPage=_make_page_canvas,
            onLaterPages=_make_page_canvas,
        )
    except Exception:  # pragma: no cover - surface traceback to logs
        tb = traceback.format_exc()
        logger.error("PDF build failed for url=%s report_id=%s:\n%s", report.url, getattr(report, "report_id", None), tb)
        # Also print the traceback to stdout so it's visible even when logging
        # configuration prevents logger output from appearing in the terminal.
        print("PDF build failed:\n" + tb, flush=True)
        # Re-raise so the caller can return a 500 to the client.
        raise

    logger.info("PDF generation complete for report_id=%s", getattr(report, "report_id", None))
    print(f"PDF generation complete for report_id={getattr(report, 'report_id', None)}", flush=True)
    return buffer.getvalue()
