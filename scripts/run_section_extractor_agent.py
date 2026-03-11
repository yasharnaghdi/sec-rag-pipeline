#!/usr/bin/env python3
"""Run section extraction batch and audit markdown-to-HTML matching.

This script orchestrates:
1) running ``scripts/run_cda_markdown_batch.py`` for the first N CIKs, and
2) validating that each extracted ``status=ok`` section text can be matched
   back to the associated raw HTML filing.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import subprocess
import sys
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class MatchStats:
    """Per-row phrase matching statistics."""

    phrase_windows: int
    phrase_hits: int
    hit_ratio: float
    matched: bool
    issue: str
    resolved_raw_html_path: str
    fetched_upstream: bool


def _normalized_words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def _to_searchable_text(text: str) -> str:
    return " ".join(_normalized_words(text))


def _build_phrase_windows(tokens: list[str], window_size: int, max_windows: int) -> list[str]:
    if window_size <= 0 or max_windows <= 0:
        return []
    if len(tokens) < window_size:
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


def _set_csv_field_limit() -> None:
    """Raise csv field-size limit for large markdown columns."""
    max_size = sys.maxsize
    while True:
        try:
            csv.field_size_limit(max_size)
            return
        except OverflowError:
            max_size //= 10


def _audit_row(
    row: dict[str, str],
    html_cache: dict[str, str],
    upstream_fetch_cache: dict[str, str],
    window_size: int,
    max_windows: int,
    min_hit_ratio: float,
    allow_upstream_fetch: bool,
) -> MatchStats:
    status = row.get("status", "").strip().lower()
    if status != "ok":
        return MatchStats(0, 0, 0.0, False, "skipped_non_ok", "", False)

    markdown = row.get("markdown", "")
    if not markdown.strip():
        return MatchStats(0, 0, 0.0, False, "empty_markdown", "", False)

    raw_html_path, fetched_upstream = _resolve_raw_html_path(
        row=row,
        upstream_fetch_cache=upstream_fetch_cache,
        allow_upstream_fetch=allow_upstream_fetch,
    )
    if raw_html_path is None:
        issue = "raw_html_missing"
        if allow_upstream_fetch:
            issue = "raw_html_missing_or_fetch_failed"
        return MatchStats(0, 0, 0.0, False, issue, "", fetched_upstream)

    html_text = html_cache.get(raw_html_path)
    if html_text is None:
        path = Path(raw_html_path)
        if not path.exists():
            return MatchStats(0, 0, 0.0, False, "raw_html_missing", raw_html_path, fetched_upstream)
        html_raw = path.read_text(encoding="utf-8", errors="replace")
        html_visible = BeautifulSoup(html_raw, "lxml").get_text(" ", strip=True)
        html_text = _to_searchable_text(html_visible)
        html_cache[raw_html_path] = html_text

    md_tokens = _normalized_words(markdown)
    windows = _build_phrase_windows(md_tokens, window_size=window_size, max_windows=max_windows)
    if not windows:
        return MatchStats(0, 0, 0.0, False, "markdown_too_short", raw_html_path, fetched_upstream)

    hits = sum(1 for phrase in windows if phrase in html_text)
    ratio = hits / len(windows)
    matched = ratio >= min_hit_ratio
    issue = "" if matched else "phrase_mismatch"
    return MatchStats(len(windows), hits, ratio, matched, issue, raw_html_path, fetched_upstream)


def _resolve_raw_html_path(
    row: dict[str, str],
    upstream_fetch_cache: dict[str, str],
    allow_upstream_fetch: bool,
) -> tuple[str | None, bool]:
    """Resolve raw HTML path for an audit row, fetching from upstream if needed."""
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
    cached = upstream_fetch_cache.get(cache_key)
    if cached and Path(cached).exists():
        return cached, True

    try:
        import ingestion.edgar_folder_fetcher as fetcher

        fetched = fetcher.fetch_filing(cik=cik, folder_id=accession, form_type="DEF 14A")
        resolved = str(fetched.cache_path)
        upstream_fetch_cache[cache_key] = resolved
        return resolved, True
    except Exception as exc:  # noqa: BLE001
        log.warning("upstream fetch failed for cik=%s accession=%s: %s", cik, accession, exc)
        return None, False


def _run_batch(args: argparse.Namespace) -> Path:
    out_dir = Path(str(args.output_base)) / str(args.batch_label)
    cmd: list[str] = [
        sys.executable,
        "scripts/run_cda_markdown_batch.py",
        "--input",
        args.input,
        "--batch-label",
        args.batch_label,
        "--output-base",
        args.output_base,
        "--limit",
        str(args.limit_ciks),
        "--max-filings-per-cik",
        str(args.max_filings_per_cik),
        "--no-markdown-files",
    ]
    if args.fiscal_year_start is not None and args.fiscal_year_end is not None:
        cmd.extend(
            [
                "--fiscal-year-start",
                str(args.fiscal_year_start),
                "--fiscal-year-end",
                str(args.fiscal_year_end),
            ]
        )
    batch_fetch_enabled = bool(args.allow_fetch_fallback or not args.no_fetch_fallback)
    if not batch_fetch_enabled:
        cmd.append("--no-fetch-fallback")

    log.info("running batch: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return Path(out_dir)


def _audit_output(
    cda_csv_path: Path,
    out_dir: Path,
    window_size: int,
    max_windows: int,
    min_hit_ratio: float,
    allow_upstream_fetch: bool,
) -> tuple[Path, Path]:
    if not cda_csv_path.exists():
        raise FileNotFoundError(f"Batch output not found: {cda_csv_path}")

    audit_csv_path = out_dir / "html_match_audit.csv"
    summary_path = out_dir / "html_match_summary.txt"

    html_cache: dict[str, str] = {}
    upstream_fetch_cache: dict[str, str] = {}
    totals: Counter[str] = Counter()
    by_section: defaultdict[str, Counter[str]] = defaultdict(Counter)
    by_cik: defaultdict[str, Counter[str]] = defaultdict(Counter)

    with (
        cda_csv_path.open(encoding="utf-8", newline="") as in_file,
        audit_csv_path.open("w", encoding="utf-8", newline="") as out_file,
    ):
        reader = csv.DictReader(in_file)
        fieldnames = [
            "cik",
            "accession_number",
            "section_name",
            "status",
            "raw_html_path",
            "resolved_raw_html_path",
            "fetched_upstream",
            "strategy",
            "confidence",
            "markdown_len",
            "phrase_windows",
            "phrase_hits",
            "hit_ratio",
            "matched",
            "issue",
        ]
        writer = csv.DictWriter(out_file, fieldnames=fieldnames)
        writer.writeheader()

        for row in reader:
            totals["total_rows"] += 1
            cik = row.get("cik", "").strip()
            section_name = row.get("section_name", "").strip()
            status = row.get("status", "").strip().lower()
            if status == "ok":
                totals["ok_rows"] += 1
                by_section[section_name]["ok"] += 1
                by_cik[cik]["ok"] += 1
            elif status:
                totals["non_ok_rows"] += 1
                by_section[section_name]["non_ok"] += 1
                by_cik[cik]["non_ok"] += 1

            stats = _audit_row(
                row=row,
                html_cache=html_cache,
                upstream_fetch_cache=upstream_fetch_cache,
                window_size=window_size,
                max_windows=max_windows,
                min_hit_ratio=min_hit_ratio,
                allow_upstream_fetch=allow_upstream_fetch,
            )

            if stats.issue == "skipped_non_ok":
                totals["skipped_non_ok"] += 1
            else:
                totals["audited_rows"] += 1
                by_section[section_name]["audited"] += 1
                by_cik[cik]["audited"] += 1
                if stats.issue in {"raw_html_missing", "raw_html_missing_or_fetch_failed"}:
                    totals["raw_html_missing"] += 1
                if stats.fetched_upstream:
                    totals["fetched_upstream_rows"] += 1
                if stats.matched:
                    totals["matched_rows"] += 1
                    by_section[section_name]["matched"] += 1
                    by_cik[cik]["matched"] += 1
                else:
                    totals["unmatched_rows"] += 1
                    by_section[section_name]["unmatched"] += 1
                    by_cik[cik]["unmatched"] += 1

            writer.writerow(
                {
                    "cik": cik,
                    "accession_number": row.get("accession_number", ""),
                    "section_name": section_name,
                    "status": row.get("status", ""),
                    "raw_html_path": row.get("raw_html_path", ""),
                    "resolved_raw_html_path": stats.resolved_raw_html_path,
                    "fetched_upstream": str(stats.fetched_upstream),
                    "strategy": row.get("strategy", ""),
                    "confidence": row.get("confidence", ""),
                    "markdown_len": len(row.get("markdown", "")),
                    "phrase_windows": stats.phrase_windows,
                    "phrase_hits": stats.phrase_hits,
                    "hit_ratio": f"{stats.hit_ratio:.4f}",
                    "matched": str(stats.matched),
                    "issue": stats.issue,
                }
            )

    audited = totals["audited_rows"]
    matched = totals["matched_rows"]
    matched_pct = (100.0 * matched / audited) if audited else 0.0
    cik_fully_matched = sum(
        (c["audited"] > 0 and c["audited"] == c["matched"]) for c in by_cik.values()
    )
    cik_with_audit = sum((c["audited"] > 0) for c in by_cik.values())

    lines: list[str] = [
        f"total_rows={totals['total_rows']}",
        f"ok_rows={totals['ok_rows']}",
        f"non_ok_rows={totals['non_ok_rows']}",
        f"audited_rows={audited}",
        f"matched_rows={matched}",
        f"unmatched_rows={totals['unmatched_rows']}",
        f"raw_html_missing={totals['raw_html_missing']}",
        f"fetched_upstream_rows={totals['fetched_upstream_rows']}",
        f"skipped_non_ok={totals['skipped_non_ok']}",
        f"matched_percent={matched_pct:.2f}",
        f"cik_with_audited_rows={cik_with_audit}",
        f"cik_fully_matched={cik_fully_matched}",
        "",
        "section_breakdown:",
    ]
    for section_name in sorted(by_section):
        sec = by_section[section_name]
        lines.append(
            (
                f"{section_name}: ok={sec['ok']} audited={sec['audited']} "
                f"matched={sec['matched']} unmatched={sec['unmatched']} non_ok={sec['non_ok']}"
            )
        )

    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return audit_csv_path, summary_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run section extractor batch + HTML match audit.")
    parser.add_argument("--input", default="fixtures/client_input.csv")
    parser.add_argument("--batch-label", default="cda_agent_100")
    parser.add_argument("--output-base", default="output")
    parser.add_argument("--limit-ciks", type=int, default=100)
    parser.add_argument("--max-filings-per-cik", type=int, default=1)
    parser.add_argument("--fiscal-year-start", type=int, default=None)
    parser.add_argument("--fiscal-year-end", type=int, default=None)
    parser.add_argument(
        "--allow-fetch-fallback",
        action="store_true",
        help="Legacy switch. Batch fetch fallback is enabled by default.",
    )
    parser.add_argument(
        "--no-fetch-fallback",
        action="store_true",
        help="Disable EDGAR fetch fallback in the batch run.",
    )
    parser.add_argument(
        "--skip-batch",
        action="store_true",
        help="Skip batch run and audit an existing output folder.",
    )
    parser.add_argument(
        "--disable-upstream-fetch",
        action="store_true",
        help="Do not fetch missing raw_html files during audit; mark as missing instead.",
    )
    parser.add_argument("--window-size", type=int, default=10)
    parser.add_argument("--max-windows", type=int, default=12)
    parser.add_argument("--min-hit-ratio", type=float, default=0.60)
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args()
    _set_csv_field_limit()
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

    has_fy_start = args.fiscal_year_start is not None
    has_fy_end = args.fiscal_year_end is not None
    if has_fy_start != has_fy_end:
        parser.error("Both --fiscal-year-start and --fiscal-year-end must be provided together.")
    if not (0.0 <= args.min_hit_ratio <= 1.0):
        parser.error("--min-hit-ratio must be between 0 and 1.")
    if args.allow_fetch_fallback and args.no_fetch_fallback:
        parser.error("Cannot set both --allow-fetch-fallback and --no-fetch-fallback.")

    out_dir = Path(args.output_base) / args.batch_label
    if not args.skip_batch:
        out_dir = _run_batch(args)

    cda_csv_path = out_dir / "cda_markdown.csv"
    audit_csv_path, summary_path = _audit_output(
        cda_csv_path=cda_csv_path,
        out_dir=out_dir,
        window_size=args.window_size,
        max_windows=args.max_windows,
        min_hit_ratio=args.min_hit_ratio,
        allow_upstream_fetch=not args.disable_upstream_fetch,
    )
    log.info("audit complete | audit_csv=%s summary=%s", audit_csv_path, summary_path)


if __name__ == "__main__":
    main()
