"""PDF report generator using ReportLab Platypus.

Produces a text-first, print-quality A4 report: title, URL, score & grade,
executive summary, score table, top priorities, detailed findings,
recommendations, and copy-paste fixes as plain text blocks.

No HTML, CSS, progress bars, or decorative UI elements are rendered.
"""
from __future__ import annotations

import io
import logging
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.models import AuditReport

logger = logging.getLogger("geo_auditor.pdf")

_PAGE_WIDTH, _PAGE_HEIGHT = A4
_MARGIN = inch  # 1-inch margins on all sides
_HEADER_TEXT = "GEO Auditor"


def _build_styles():
    """Create a fresh copy of ReportLab styles per PDF generation.

    Avoids mutating the global :func:`getSampleStyleSheet` at import time,
    which would side-affect any other code relying on the default styles.
    """
    styles = getSampleStyleSheet()

    styles["Title"].fontSize = 24
    styles["Title"].leading = 28
    styles["Title"].spaceAfter = 6
    styles["Title"].alignment = TA_CENTER
    styles["Title"].textColor = colors.black

    styles.add(
        ParagraphStyle(
            name="Subtitle",
            parent=styles["Normal"],
            fontSize=12,
            leading=16,
            spaceAfter=18,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#666666"),
        )
    )

    styles["Normal"].fontSize = 10
    styles["Normal"].leading = 14
    styles["Normal"].spaceAfter = 4
    styles["Normal"].textColor = colors.black
    styles["Normal"].alignment = TA_LEFT

    styles.add(
        ParagraphStyle(
            name="SectionHeading",
            parent=styles["Heading1"],
            fontSize=16,
            leading=22,
            spaceBefore=18,
            spaceAfter=10,
            textColor=colors.black,
            backColor=colors.white,
        )
    )
    styles["SectionHeading"].underline = True

    styles.add(
        ParagraphStyle(
            name="SmallText",
            parent=styles["Normal"],
            fontSize=8,
            leading=12,
            spaceAfter=2,
            textColor=colors.HexColor("#555555"),
            alignment=TA_LEFT,
        )
    )
    styles.add(
        ParagraphStyle(
            name="CodeBlock",
            parent=styles["Normal"],
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

    return styles


# Module-level default styles for functions that don't receive them as parameters.
_styles = _build_styles()


def _escape_xml(text: str) -> str:
    """Escape characters that are special in ReportLab's Paragraph XML markup."""
    return xml_escape(str(text))


def _wrap_url(url: str) -> str:
    """Escape a URL for use inside a Paragraph.

    ReportLab's Paragraph wraps text naturally at ``/`` and ``.`` characters,
    so no special break tags are needed — XML metacharacters are escaped.
    """
    return _escape_xml(url)


def _table_text(text: str) -> str:
    """Prepare plain text for a ReportLab Table cell.

    Table cells render as plain text (not XML), so we don't escape entities;
    instead we strip newlines so multi-line strings fit on a single row.
    """
    return str(text).replace("\n", " ").replace("\r", "")


def _append_finding(story: list, finding, numbered: int, include_points: bool = False) -> None:
    """Append a single finding as a KeepTogether block to *story*."""
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


def _make_page_canvas(canvas, doc) -> None:
    """Draw header and footer on every page."""
    canvas.saveState()

    # Header
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#888888"))
    canvas.drawString(_MARGIN, _PAGE_HEIGHT - _MARGIN + 6, _HEADER_TEXT)

    # Footer: page number (right) + timestamp (center)
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


def _build_story(report: AuditReport) -> list:
    """Build the list of flowables for the PDF body."""
    story: list = []

    display_url = report.final_url or report.url

    # Title and URL
    story.append(Paragraph("GEO Audit Report", _styles["Title"]))
    story.append(Paragraph(_wrap_url(display_url), _styles["Subtitle"]))
    story.append(Spacer(1, 12))

    # Executive Summary
    story.append(Paragraph("Executive Summary", _styles["SectionHeading"]))
    summary_parts = [
        f"<b>Overall Score:</b> {report.total_score} / {report.max_score}",
        f"<b>Grade:</b> {report.grade} — {report.grade_label}",
        f"<b>URL:</b> {_wrap_url(display_url)}",
        f"<b>Generated:</b> {report.generated_at}",
        f"<b>Audit Duration:</b> {report.audit_duration_ms} ms",
    ]
    if report.audit_scope == "site" or report.partial:
        summary_parts.extend(
            [
                f"<b>Audit Scope:</b> {'Site crawl' if report.audit_scope == 'site' else 'Single page'}",
                f"<b>Pages Audited:</b> {report.pages_audited}",
            ]
        )
        if report.pages_discovered:
            summary_parts.append(
                f"<b>Internal Candidates Discovered:</b> {report.pages_discovered}"
            )
        summary_parts.append(
            f"<b>Points Evaluated:</b> {report.points_evaluated:g} / {report.max_score}"
        )
    if report.partial:
        summary_parts.append(
            "<b>Coverage:</b> PARTIAL — some pages or checks could not be evaluated. "
            "Unknown checks are disclosed, not treated as failures."
        )
    if report.llm_enhanced:
        summary_parts.append("<b>Scoring Mode:</b> Groq-enhanced")
    elif report.llm_error:
        summary_parts.append("<b>Scoring Mode:</b> Deterministic fallback")
    else:
        summary_parts.append("<b>Scoring Mode:</b> Deterministic")
    for part in summary_parts:
        story.append(Paragraph(part, _styles["BodyText"]))
    story.append(Spacer(1, 8))

    # Audited pages (multipage reports only).
    if report.audit_scope == "site" and report.pages:
        story.append(Paragraph("Audited Pages", _styles["SectionHeading"]))
        page_rows = [["Page", "Role", "Page Checks", "Status", "Weight"]]
        for page in report.pages:
            if page.error:
                status = (
                    "Excluded (noindex)"
                    if page.error == "noindex_excluded"
                    else f"Not evaluated: {page.error}"
                )
            else:
                status = "Audited"
            page_rows.append(
                [
                    _table_text(page.final_url or page.url),
                    _table_text(page.role),
                    f"{page.points_awarded:.1f} / {page.points_possible:.0f}",
                    _table_text(status),
                    "2x" if page.role == "homepage" else "1x",
                ]
            )
        pages_table = Table(
            page_rows,
            colWidths=[3.0 * inch, 0.7 * inch, 0.9 * inch, 1.3 * inch, 0.5 * inch],
            hAlign=TA_LEFT,
        )
        pages_table.setStyle(
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
        story.append(pages_table)
        story.append(Spacer(1, 10))

    # Calibration note
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

    # Score Breakdown Table
    story.append(Paragraph("Score Breakdown", _styles["SectionHeading"]))
    table_data = [["Category", "Group", "Score", "%"]]
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

    # Top Priorities
    story.append(Paragraph("Top Priorities", _styles["SectionHeading"]))
    if report.top_priorities:
        for i, finding in enumerate(report.top_priorities, 1):
            _append_finding(story, finding, numbered=i)
    else:
        story.append(Paragraph("No priority findings identified.", _styles["BodyText"]))
    story.append(Spacer(1, 8))

    # Detailed Findings
    story.append(Paragraph("Detailed Findings", _styles["SectionHeading"]))
    if report.all_findings:
        for i, finding in enumerate(report.all_findings, 1):
            _append_finding(story, finding, numbered=i, include_points=True)
    else:
        story.append(Paragraph("No detailed findings.", _styles["BodyText"]))

    # Per-page detail (internal PDF only; not included in share payloads).
    if report.audit_scope == "site" and report.pages:
        story.append(PageBreak())
        story.append(Paragraph("Per-Page Detail", _styles["SectionHeading"]))
        for page in report.pages:
            if not page.sections:
                continue
            story.append(
                Paragraph(
                    f"<b>{_escape_xml(page.final_url or page.url)}</b> "
                    f"({_escape_xml(page.role)}) — "
                    f"{page.points_awarded:.1f} / {page.points_possible:.0f} page points",
                    _styles["BodyText"],
                )
            )
            page_rows = [["Category", "Score", "Status"]]
            for section in page.sections:
                statuses = sorted({check.status.value for check in section.checks})
                page_rows.append(
                    [
                        _table_text(section.label),
                        f"{section.points_awarded:.1f} / {section.points_possible:.0f}",
                        _table_text(", ".join(statuses)),
                    ]
                )
            detail_table = Table(
                page_rows,
                colWidths=[3.6 * inch, 1.0 * inch, 1.8 * inch],
                hAlign=TA_LEFT,
            )
            detail_table.setStyle(
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
            story.append(detail_table)
            story.append(Spacer(1, 8))

    # AI Answerability Matrix (if present)
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

    # Recommendations Recap
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


def generate_pdf_bytes(report: AuditReport) -> bytes:
    """Render an *AuditReport* into a PDF document and return the raw bytes."""
    logger.info(
        "Generating PDF for url=%s report_id=%s",
        report.url, getattr(report, "report_id", None),
    )
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
    doc._geo_report_ts = report.generated_at

    story = _build_story(report)

    try:
        doc.build(
            story,
            onFirstPage=_make_page_canvas,
            onLaterPages=_make_page_canvas,
        )
    except Exception:
        # Surface the failure in logs and re-raise so the API layer can
        # return a 500 with a meaningful message.
        logger.error(
            "PDF build failed for url=%s report_id=%s",
            report.url, getattr(report, "report_id", None),
            exc_info=True,
        )
        raise

    logger.info(
        "PDF generation complete for report_id=%s",
        getattr(report, "report_id", None),
    )
    return buffer.getvalue()
