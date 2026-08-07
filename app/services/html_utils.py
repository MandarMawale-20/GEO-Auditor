"""
Shared HTML/DOM parsing utilities.

All engines build on top of these small, pure, well-tested helpers rather
than re-parsing HTML with ad-hoc regex, keeping scoring deterministic and
explainable.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

from app.config import SPA_MARKERS

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
    soup: BeautifulSoup
    raw_html: str
    visible_text: str
    word_count: int
    json_ld_blocks: list[dict] = field(default_factory=list)


def parse_html(raw_html: str) -> ParsedPage:
    soup = BeautifulSoup(raw_html or "", "lxml")

    # Extract JSON-LD blocks before decomposing script tags (decompose
    # removes them from the tree, so we must read them first).
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
            # Handle @graph containers, common in real-world JSON-LD.
            if "@graph" in parsed and isinstance(parsed["@graph"], list):
                json_ld_blocks.extend([b for b in parsed["@graph"] if isinstance(b, dict)])
            else:
                json_ld_blocks.append(parsed)

    # Remove non-content tags for clean visible-text extraction.
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    visible_text = " ".join(soup.get_text(separator=" ").split())
    word_count = len(_WORD_RE.findall(visible_text))

    return ParsedPage(
        soup=soup,
        raw_html=raw_html or "",
        visible_text=visible_text,
        word_count=word_count,
        json_ld_blocks=json_ld_blocks,
    )


def has_spa_markers(raw_html: str) -> bool:
    lowered = (raw_html or "").lower()
    return any(marker in lowered for marker in SPA_MARKERS)


def find_entities_by_type(json_ld_blocks: list[dict], type_names: tuple[str, ...]) -> list[dict]:
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
    same_as = entity.get("sameAs", [])
    if isinstance(same_as, str):
        return [same_as]
    if isinstance(same_as, list):
        return [s for s in same_as if isinstance(s, str)]
    return []


def get_heading_pairs(soup: BeautifulSoup) -> list[tuple[Tag, str]]:
    """Return (heading_tag, following_paragraph_text) pairs for H2/H3 tags."""
    pairs: list[tuple[Tag, str]] = []
    for heading in soup.find_all(["h2", "h3"]):
        heading_text = heading.get_text(strip=True)
        sibling = heading.find_next_sibling()
        para_text = ""
        while sibling is not None and sibling.name not in (
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        ):
            if sibling.name == "p":
                para_text = sibling.get_text(strip=True)
                break
            sibling = sibling.find_next_sibling()
        pairs.append((heading, para_text or heading_text))
    return pairs


def is_question_heading(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    first_word = stripped.split(" ", 1)[0].strip(",.!?")
    return first_word in QUESTION_WORDS


def count_fluff_phrases(text: str) -> list[str]:
    lowered = text.lower()
    return [phrase for phrase in FLUFF_PHRASES if phrase in lowered]


def numeric_density_per_500_words(text: str) -> float:
    words = len(_WORD_RE.findall(text)) or 1
    numbers = len(_NUMBER_RE.findall(text))
    return round((numbers / words) * 500, 2)


def count_tables(soup: BeautifulSoup) -> int:
    return len(soup.find_all("table"))


def count_lists(soup: BeautifulSoup) -> int:
    return len(soup.find_all(["ol", "ul"]))


def count_images(soup: BeautifulSoup) -> int:
    return len(soup.find_all("img"))


def word_to_image_ratio(word_count: int, image_count: int) -> float | None:
    if image_count == 0:
        return None
    return round(word_count / image_count, 1)


def page_exists_link(soup: BeautifulSoup, keywords: tuple[str, ...]) -> str | None:
    """Find an <a> tag whose href or text suggests an about/contact/privacy page."""
    for link in soup.find_all("a", href=True):
        href = link["href"].lower()
        text = link.get_text(strip=True).lower()
        for keyword in keywords:
            if keyword in href or keyword in text:
                return link["href"]
    return None


def find_best_citation_paragraph(soup: BeautifulSoup) -> str | None:
    """Find a 20-60 word self-contained <p> — a candidate AI citation snippet."""
    best: str | None = None
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        word_count = len(_WORD_RE.findall(text))
        if 20 <= word_count <= 60:
            best = text
            break
    return best


def get_meta_content(soup: BeautifulSoup, name: str, attr: str = "name") -> str | None:
    tag = soup.find("meta", attrs={attr: name})
    if tag and tag.get("content"):
        return tag["content"]
    return None
