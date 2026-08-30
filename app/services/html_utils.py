"""
Shared HTML/DOM parsing utilities.

All engines build on top of the immutable :class:`ParsedPage` data structure
rather than sharing a mutable BeautifulSoup tree.  The parsing happens once
in :func:`parse_html`, which extracts all required data into plain Python
structures.  Engines then operate on these structures safely from worker
threads — no concurrent traversal of a shared parser tree occurs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from app.config import SPA_MARKERS, TRUSTED_SAMEAS_DOMAINS

# Re-export TRUSTED_SAMEAS_DOMAINS for backward compatibility with imports
# that previously came from this module.  New code should import from
# app.config directly.
__all__ = [
    "ParsedPage",
    "parse_html",
    "has_spa_markers",
    "FLUFF_PHRASES",
    "QUESTION_WORDS",
    "count_fluff_phrases",
    "numeric_density_per_500_words",
    "word_to_image_ratio",
    "is_question_heading",
    "find_citation_candidate_paragraph",
    "find_best_citation_paragraph",  # deprecated alias
    "extract_sameas",
    "find_entities_by_type",
    "get_meta_content",
    "page_exists_link",
]

_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?%?\b")

FLUFF_PHRASES: tuple[str, ...] = (
    "welcome to",
    "we are passionate about",
    "in today's fast-paced world",
    "in today's digital age",
    "at the end of the day",
    "unlock your potential",
    "best-in-class",
    "world-class",
    "cutting-edge solutions",
    "revolutionize the way",
    "take your business to the next level",
    "we believe",
    "our mission is to",
    "synergy",
    "game-changing",
    "state-of-the-art",
)

QUESTION_WORDS: tuple[str, ...] = (
    "what",
    "how",
    "why",
    "when",
    "where",
    "who",
    "which",
    "can",
    "does",
    "is",
    "are",
)


@dataclass
class ParsedPage:
    """Immutable snapshot of all data extracted from an HTML document.

    All fields are plain Python types — no BeautifulSoup tree is retained,
    so multiple engines can safely read this object from concurrent threads.
    """

    raw_html: str
    visible_text: str
    word_count: int
    json_ld_blocks: list[dict] = field(default_factory=list)
    # Pre-extracted for thread-safe engine usage:
    heading_pairs: list[tuple[str, str]] = field(default_factory=list)
    table_count: int = 0
    list_count: int = 0
    image_count: int = 0
    citation_paragraph: str | None = None
    spa_markers_present: bool = False
    meta_contents: dict[str, str] = field(default_factory=dict)
    about_href: str | None = None
    contact_href: str | None = None


def parse_html(raw_html: str) -> ParsedPage:
    """Parse raw HTML once, extracting all engine-relevant data into plain Python.

    The returned :class:`ParsedPage` contains no reference to the BeautifulSoup
    tree, so it can be shared safely across worker threads.
    """
    html = raw_html or ""
    soup = BeautifulSoup(html, "lxml")

    # --- JSON-LD extraction (before decomposing script tags) ---
    json_ld_blocks: list[dict] = []
    for script_tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script_tag.string or script_tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            json_ld_blocks.extend([b for b in parsed if isinstance(b, dict)])
        elif isinstance(parsed, dict):
            if "@graph" in parsed and isinstance(parsed["@graph"], list):
                json_ld_blocks.extend([b for b in parsed["@graph"] if isinstance(b, dict)])
            else:
                json_ld_blocks.append(parsed)

    # --- Meta content extraction ---
    meta_contents: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property")
        content = tag.get("content")
        if name and content:
            meta_contents[name.lower()] = content

    # --- Heading + paragraph pairs (H2/H3) ---
    heading_pairs: list[tuple[str, str]] = []
    for heading in soup.find_all(["h2", "h3"]):
        heading_text = heading.get_text(strip=True)
        sibling = heading.find_next_sibling()
        para_text = ""
        while sibling is not None and sibling.name not in (
            "h1", "h2", "h3", "h4", "h5", "h6"
        ):
            if sibling.name == "p":
                para_text = sibling.get_text(strip=True)
                break
            sibling = sibling.find_next_sibling()
        heading_pairs.append((heading_text, para_text or heading_text))

    # --- Citation candidate paragraph (20-60 words) ---
    citation_paragraph = _find_citation_candidate(soup)

    # --- Simple counts ---
    table_count = len(soup.find_all("table"))
    list_count = len(soup.find_all(["ol", "ul"]))
    image_count = len(soup.find_all("img"))

    # --- SPA marker detection ---
    spa_markers_present = _has_spa_markers(html)

    # --- Trust page links (about / contact) ---
    about_href = _page_exists_link(soup, ("about", "/about", "about-us"))
    contact_href = _page_exists_link(soup, ("contact", "/contact", "contact-us"))

    # --- Clean visible-text extraction ---
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible_text = " ".join(soup.get_text(separator=" ").split())
    word_count = len(_WORD_RE.findall(visible_text))

    return ParsedPage(
        raw_html=html,
        visible_text=visible_text,
        word_count=word_count,
        json_ld_blocks=json_ld_blocks,
        heading_pairs=heading_pairs,
        table_count=table_count,
        list_count=list_count,
        image_count=image_count,
        citation_paragraph=citation_paragraph,
        spa_markers_present=spa_markers_present,
        meta_contents=meta_contents,
        about_href=about_href,
        contact_href=contact_href,
    )


# ---------------------------------------------------------------------------
# Pure helper functions (work on strings / plain data, not soup)
# ---------------------------------------------------------------------------
def has_spa_markers(raw_html: str) -> bool:
    """Check whether raw HTML contains client-side framework markers."""
    lowered = (raw_html or "").lower()
    return any(marker in lowered for marker in SPA_MARKERS)


def _has_spa_markers(raw_html: str) -> bool:
    """Internal: same as :func:`has_spa_markers` but operates on the already-parsed HTML."""
    return has_spa_markers(raw_html)


def count_fluff_phrases(text: str) -> list[str]:
    """Return all fluff phrases found in *text* (case-insensitive)."""
    lowered = text.lower()
    return [phrase for phrase in FLUFF_PHRASES if phrase in lowered]


def numeric_density_per_500_words(text: str) -> float:
    """Count numeric tokens per 500 words of *text*."""
    words = len(_WORD_RE.findall(text)) or 1
    numbers = len(_NUMBER_RE.findall(text))
    return round((numbers / words) * 500, 2)


def word_to_image_ratio(word_count: int, image_count: int) -> float | None:
    """Return the words-per-image ratio, or ``None`` when there are no images."""
    if image_count == 0:
        return None
    return round(word_count / image_count, 1)


def is_question_heading(text: str) -> bool:
    """Return True if *text* looks like a question (ends with '?' or starts with a question word)."""
    stripped = text.strip().lower()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    first_word = stripped.split(" ", 1)[0].strip(",.!?")
    return first_word in QUESTION_WORDS


def _find_citation_candidate(soup: BeautifulSoup) -> str | None:
    """Find a 20-60 word self-contained <p> — a candidate AI citation snippet."""
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        word_count = len(_WORD_RE.findall(text))
        if 20 <= word_count <= 60:
            return text
    return None


def find_citation_candidate_paragraph(soup: BeautifulSoup) -> str | None:
    """Find a 20-60 word self-contained <p> — a candidate AI citation snippet.

    .. deprecated::
        Use :attr:`ParsedPage.citation_paragraph` instead.  This function is
        retained for any external callers that still operate on a raw soup.
    """
    return _find_citation_candidate(soup)


# Backward-compat alias (old name referenced in tasks.md).
find_best_citation_paragraph = find_citation_candidate_paragraph


def _page_exists_link(soup: BeautifulSoup, keywords: tuple[str, ...]) -> str | None:
    """Find an <a> tag whose href or text suggests an about/contact/privacy page."""
    for link in soup.find_all("a", href=True):
        href = link["href"].lower()
        text = link.get_text(strip=True).lower()
        for keyword in keywords:
            if keyword in href or keyword in text:
                return link["href"]
    return None


def page_exists_link(soup: BeautifulSoup, keywords: tuple[str, ...]) -> str | None:
    """Public wrapper around :func:`_page_exists_link`."""
    return _page_exists_link(soup, keywords)


# ---------------------------------------------------------------------------
# JSON-LD helpers (operate on extracted dicts, not soup)
# ---------------------------------------------------------------------------
def find_entities_by_type(json_ld_blocks: list[dict], type_names: tuple[str, ...]) -> list[dict]:
    """Filter JSON-LD blocks to those whose @type matches one of *type_names*."""
    matches = []
    lowered_targets = {t.lower() for t in type_names}
    for block in json_ld_blocks:
        raw_type = block.get("@type")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        types_lower = {str(t).lower() for t in types if t}
        if types_lower & lowered_targets:
            matches.append(block)
    return matches


def extract_sameas(entity: dict) -> list[str]:
    """Extract sameAs URLs from a JSON-LD entity dict."""
    same_as = entity.get("sameAs", [])
    if isinstance(same_as, str):
        return [same_as]
    if isinstance(same_as, list):
        return [s for s in same_as if isinstance(s, str)]
    return []


def get_meta_content(soup: BeautifulSoup, name: str, attr: str = "name") -> str | None:
    """Extract the ``content`` attribute of a ``<meta>`` tag by *name* or *property*.

    .. deprecated::
        Use :attr:`ParsedPage.meta_contents` instead.  Retained for
        backward compatibility.
    """
    tag = soup.find("meta", attrs={attr: name})
    if tag and tag.get("content"):
        return tag["content"]
    return None


def get_meta_content_from_map(meta_contents: dict[str, str], name: str) -> str | None:
    """Look up a meta tag value from the pre-extracted :attr:`ParsedPage.meta_contents` dict."""
    return meta_contents.get(name.lower())
