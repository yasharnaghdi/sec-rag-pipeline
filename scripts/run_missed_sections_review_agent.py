#!/usr/bin/env python3
"""Review extracted sections against source HTML and report missed cases.

This agent is designed for debugging extraction quality after a batch run.
It reads ``cda_markdown.csv`` and produces:

1) ``missed_sections_report.csv`` with per-row diagnostics
2) ``missed_sections_summary.txt`` with aggregate counts and priorities
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from bs4.element import Tag

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([\d.]+)\s*pt", re.IGNORECASE)
_SECTION_PHRASES: dict[str, tuple[str, ...]] = {
    "compensation_discussion_and_analysis": (
        "compensation discussion and analysis",
        "compensation discussion",
        "cd&a",
        "cda",
    ),
    "executive_compensation": (
        "executive compensation",
    ),
    "director_compensation": (
        "director compensation",
        "compensation of directors",
    ),
    "pay_vs_performance": (
        "pay versus performance",
        "pay vs performance",
    ),
    "equity_compensation_plans": (
        "equity compensation plans",
        "equity compensation plan",
    ),
}


@dataclass(frozen=True)
class RowCheck:
    """Per-row diagnostic output."""

    issue_type: str
    match_ratio: float
    likely_in_html: bool
    detected_by: str
    evidence_snippet: str
    resolved_raw_html_path: str
    fetched_upstream: bool
    html_available: bool
    recommendation: str


def _set_csv_field_limit() -> None:
    size = sys.maxsize
    while True:
        try:
            csv.field_size_limit(size)
            return
        except OverflowError:
            size //= 10


def _normalized_words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def _normalize_text(text: str) -> str:
    return " ".join(_normalized_words(text))


def _build_phrase_windows(tokens: list[str], window_size: int, max_windows: int) -> list[str]:
    if len(tokens) < window_size or window_size <= 0 or max_windows <= 0:
        return []
    if len(tokens) == window_size:
        return [" ".join(tokens)]

    last_start = len(tokens) - window_size
    if max_windows == 1:
        return [" ".join(tokens[:window_size])]

    step = max(1, last_start // (max_windows - 1))
    starts = list(range(0, last_start + 1, step))
    if starts[-1] != last_start:
        starts.append(last_start)
    starts = starts[:max_windows]
    return [" ".join(tokens[i : i + window_size]) for i in starts]


def _extract_font_pt(style: str | None) -> float | None:
    if not style:
        return None
    m = _FONT_SIZE_RE.search(style)
    if m is None:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _looks_like_heading(tag: Tag, text: str) -> bool:
    if tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return True

    style = str(tag.get("style", "")).lower()
    font_pt = _extract_font_pt(style)
    if font_pt is not None and font_pt >= 14.0:
        return True

    has_bold = tag.find("b") is not None or "font-weight:bold" in style or "font-weight: bold" in style
    is_center = "text-align:center" in style or "text-align: center" in style
    if has_bold and is_center and len(text) <= 140:
        return True

    upper_len = sum(1 for ch in text if ch.isupper())
    alpha_len = sum(1 for ch in text if ch.isalpha())
    if alpha_len >= 12 and upper_len >= int(alpha_len * 0.8):
        return True

    return False


def _snippet_around(full_text: str, needle: str, radius: int = 140) -> str:
    idx = full_text.find(needle)
    if idx < 0:
        return ""
    start = max(0, idx - radius)
    end = min(len(full_text), idx + len(needle) + radius)
    return full_text[start:end]


def _resolve_raw_html_path(
    row: dict[str, str],
    allow_upstream_fetch: bool,
    upstream_cache: dict[str, str],
) -> tuple[str | None, bool]:
    raw_html_path = row.get("raw_html_path", "").strip()
    if raw_html_path and Path(raw_html_path).exists():
        return raw_html_path, False

    if not allow_upstream_fetch:
        return None, False

    cik = row.get("cik", "").strip()
    accession = row.get("accession_number", "").strip()
    if not cik or not accession:
        return None, False

    cache_key = f"{cik}|{accession}"
    cached_path = upstream_cache.get(cache_key)
    if cached_path and Path(cached_path).exists():
        return cached_path, True

    try:
        import ingestion.edgar_folder_fetcher as fetcher

        fetched = fetcher.fetch_filing(cik=cik, folder_id=accession, form_type="DEF 14A")
        resolved = str(fetched.cache_path)
        upstream_cache[cache_key] = resolved
        return resolved, True
    except Exception as exc:  # noqa: BLE001
        log.warning("upstream fetch failed for cik=%s accession=%s: %s", cik, accession, exc)
        return None, False


def _compute_match_ratio(markdown: str, html_norm: str, window_size: int, max_windows: int) -> float:
    tokens = _normalized_words(markdown)
    windows = _build_phrase_windows(tokens, window_size=window_size, max_windows=max_windows)
    if not windows:
        return 0.0
    hits = sum(1 for phrase in windows if phrase in html_norm)
    return hits / len(windows)


def _detect_section_presence(
    section_name: str,
    soup: BeautifulSoup,
    html_visible_norm: str,
) -> tuple[bool, str, str]:
    phrases = _SECTION_PHRASES.get(section_name, ())
    if not phrases:
        return False, "none", ""

    for phrase in phrases:
        phrase_norm = _normalize_text(phrase)
        if phrase_norm and phrase_norm in html_visible_norm:
            snippet = _snippet_around(html_visible_norm, phrase_norm)
            return True, "alias_text", snippet[:300]

    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "td"]):
        text = " ".join(tag.get_text(" ", strip=True).split())
        if not text:
            continue
        text_norm = _normalize_text(text)
        if not text_norm:
            continue
        for phrase in phrases:
            phrase_norm = _normalize_text(phrase)
            if phrase_norm and phrase_norm in text_norm and _looks_like_heading(tag, text):
                return True, "visual_heading", text[:300]

    return False, "none", ""


def _recommendation(issue_type: str, likely_in_html: bool) -> str:
    if issue_type == "matched_ok":
        return "no action"
    if issue_type == "ok_but_mismatch":
        return "check markdown rendering and phrase matching threshold"
    if issue_type in {"missing_html", "fetch_failed"}:
        return "ensure raw HTML is available or enable upstream fetch"
    if likely_in_html:
        return "likely false negative: inspect TOC parsing, anchor resolution, and heading fallback"
    return "likely true miss: section may be absent in filing"


def _check_row(
    row: dict[str, str],
    html_cache: dict[str, tuple[str, BeautifulSoup]],
    upstream_cache: dict[str, str],
    window_size: int,
    max_windows: int,
    min_hit_ratio: float,
    allow_upstream_fetch: bool,
) -> RowCheck:
    status = row.get("status", "").strip().lower()
    markdown = row.get("markdown", "")
    section_name = row.get("section_name", "").strip()

    resolved_path, fetched_upstream = _resolve_raw_html_path(
        row=row,
        allow_upstream_fetch=allow_upstream_fetch,
        upstream_cache=upstream_cache,
    )
    if resolved_path is None:
        issue = "fetch_failed" if allow_upstream_fetch else "missing_html"
        rec = _recommendation(issue, False)
        return RowCheck(issue, 0.0, False, "none", "", "", fetched_upstream, False, rec)

    cached = html_cache.get(resolved_path)
    if cached is None:
        path = Path(resolved_path)
        if not path.exists():
            rec = _recommendation("missing_html", False)
            return RowCheck("missing_html", 0.0, False, "none", "", resolved_path, fetched_upstream, False, rec)
        html_raw = path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html_raw, "lxml")
        visible_norm = _normalize_text(soup.get_text(" ", strip=True))
        cached = (visible_norm, soup)
        html_cache[resolved_path] = cached

    html_visible_norm, soup = cached

    if status == "ok":
        ratio = _compute_match_ratio(markdown, html_visible_norm, window_size=window_size, max_windows=max_windows)
        if ratio >= min_hit_ratio:
            rec = _recommendation("matched_ok", True)
            return RowCheck(
                "matched_ok",
                ratio,
                True,
                "phrase_match",
                "",
                resolved_path,
                fetched_upstream,
                True,
                rec,
            )
        rec = _recommendation("ok_but_mismatch", True)
        return RowCheck(
            "ok_but_mismatch",
            ratio,
            True,
            "phrase_mismatch",
            "",
            resolved_path,
            fetched_upstream,
            True,
            rec,
        )

    likely, detected_by, evidence = _detect_section_presence(
        section_name=section_name,
        soup=soup,
        html_visible_norm=html_visible_norm,
    )
    issue = "missed_but_present" if likely else "missing_and_not_found"
    rec = _recommendation(issue, likely)
    return RowCheck(
        issue,
        0.0,
        likely,
        detected_by,
        evidence,
        resolved_path,
        fetched_upstream,
        True,
        rec,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review extracted sections and report missed cases.")
    parser.add_argument("--batch-dir", default="output/cda_agent_100")
    parser.add_argument("--input-csv", default=None, help="Override path to cda_markdown.csv")
    parser.add_argument("--report-csv", default=None, help="Override output report CSV path")
    parser.add_argument("--summary-txt", default=None, help="Override output summary TXT path")
    parser.add_argument("--window-size", type=int, default=10)
    parser.add_argument("--max-windows", type=int, default=12)
    parser.add_argument("--min-hit-ratio", type=float, default=0.60)
    parser.add_argument(
        "--disable-upstream-fetch",
        action="store_true",
        help="Do not fetch missing raw_html files from upstream ingestion.",
    )
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    _set_csv_field_limit()

    args = _build_parser().parse_args()
    if not (0.0 <= args.min_hit_ratio <= 1.0):
        raise SystemExit("--min-hit-ratio must be between 0 and 1.")

    batch_dir = Path(args.batch_dir)
    input_csv = Path(args.input_csv) if args.input_csv else (batch_dir / "cda_markdown.csv")
    report_csv = Path(args.report_csv) if args.report_csv else (batch_dir / "missed_sections_report.csv")
    summary_txt = Path(args.summary_txt) if args.summary_txt else (batch_dir / "missed_sections_summary.txt")

    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    html_cache: dict[str, tuple[str, BeautifulSoup]] = {}
    upstream_cache: dict[str, str] = {}
    issue_counts: Counter[str] = Counter()
    by_section: defaultdict[str, Counter[str]] = defaultdict(Counter)
    by_strategy: Counter[str] = Counter()
    by_cik_missed: Counter[str] = Counter()
    total_rows = 0

    report_csv.parent.mkdir(parents=True, exist_ok=True)
    with (
        input_csv.open(encoding="utf-8", newline="") as in_file,
        report_csv.open("w", encoding="utf-8", newline="") as out_file,
    ):
        reader = csv.DictReader(in_file)
        fieldnames = [
            "cik",
            "accession_number",
            "section_name",
            "status",
            "confidence",
            "strategy",
            "raw_html_path",
            "resolved_raw_html_path",
            "fetched_upstream",
            "html_available",
            "issue_type",
            "match_ratio",
            "likely_in_html",
            "detected_by",
            "evidence_snippet",
            "recommendation",
            "warnings",
            "error",
        ]
        writer = csv.DictWriter(out_file, fieldnames=fieldnames)
        writer.writeheader()

        for row in reader:
            total_rows += 1
            check = _check_row(
                row=row,
                html_cache=html_cache,
                upstream_cache=upstream_cache,
                window_size=args.window_size,
                max_windows=args.max_windows,
                min_hit_ratio=args.min_hit_ratio,
                allow_upstream_fetch=not args.disable_upstream_fetch,
            )

            cik = row.get("cik", "").strip()
            section_name = row.get("section_name", "").strip()
            strategy = row.get("strategy", "").strip()
            issue_counts[check.issue_type] += 1
            by_section[section_name][check.issue_type] += 1
            if strategy:
                by_strategy[strategy] += 1

            if check.issue_type in {"missed_but_present", "ok_but_mismatch", "missing_html", "fetch_failed"}:
                by_cik_missed[cik] += 1

            writer.writerow(
                {
                    "cik": cik,
                    "accession_number": row.get("accession_number", ""),
                    "section_name": section_name,
                    "status": row.get("status", ""),
                    "confidence": row.get("confidence", ""),
                    "strategy": strategy,
                    "raw_html_path": row.get("raw_html_path", ""),
                    "resolved_raw_html_path": check.resolved_raw_html_path,
                    "fetched_upstream": str(check.fetched_upstream),
                    "html_available": str(check.html_available),
                    "issue_type": check.issue_type,
                    "match_ratio": f"{check.match_ratio:.4f}",
                    "likely_in_html": str(check.likely_in_html),
                    "detected_by": check.detected_by,
                    "evidence_snippet": check.evidence_snippet,
                    "recommendation": check.recommendation,
                    "warnings": row.get("warnings", ""),
                    "error": row.get("error", ""),
                }
            )

    lines: list[str] = [
        f"input_csv={input_csv}",
        f"report_csv={report_csv}",
        f"total_rows={total_rows}",
        "",
        "issue_counts:",
    ]
    for issue, count in issue_counts.most_common():
        lines.append(f"{issue}={count}")

    lines.append("")
    lines.append("section_breakdown:")
    for section_name in sorted(by_section):
        counts = by_section[section_name]
        detail = " ".join(f"{k}:{counts[k]}" for k in sorted(counts))
        lines.append(f"{section_name}: {detail}")

    lines.append("")
    lines.append("top_ciks_for_debug:")
    for cik, count in by_cik_missed.most_common(20):
        lines.append(f"{cik}={count}")

    lines.append("")
    lines.append("top_strategies_seen:")
    for strategy, count in by_strategy.most_common(20):
        lines.append(f"{strategy}={count}")

    summary_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("review complete | report=%s summary=%s", report_csv, summary_txt)


if __name__ == "__main__":
    main()
