# CD&A Markdown Extraction - Design Decisions

Last updated: 2026-03-10

## Purpose

This document records the architectural decisions behind the deterministic CD&A markdown extraction flow so future agents can extend it consistently without re-opening core design choices.

Primary implementation files:
- `ingestion/cda_markdown_extractor.py`
- `scripts/run_cda_markdown_batch.py`

This flow is intentionally independent from `ingestion/cda_extractor.py`.

## Decision Summary

1. Use a hybrid extraction strategy:
   - Deterministic SEC boundary detection (ToC + anchor + heading fallbacks).
   - Markdown rendering via Docling when available, with deterministic internal fallback.
2. Prefer ToC anchor boundaries over heading-only heuristics.
3. Keep extraction output auditable with trace metadata (`strategy`, anchors, pages, warnings, confidence).
4. Batch CLI filtering is fiscal-year based (`--fiscal-year-start/--fiscal-year-end`) to match existing pipeline conventions.
5. Preserve tables in output markdown by default.

## Boundary Detection Decisions

### Start Boundary

Priority order:
1. Multi-table front-matter ToC parsing.
2. Fuzzy match ToC entry for CD&A (typo-tolerant, e.g. "Compensatoon Discussion and Analysis").
3. Resolve ToC `href` anchor target (`id`/`name`) in body and use as start.
4. Fallback to body heading-like node match.
5. Last fallback to keyword window in body blocks.

Why:
- SEC proxy filings often split ToC across multiple tables.
- Anchor targets are more stable than heading-only detection in prose-heavy layouts.

### End Boundary (Full Executive Compensation Scope)

Priority order:
1. First major non-executive-compensation ToC entry after CD&A (e.g., Item 4+, shareholder proposal blocks).
2. First top-level heading outside exec-comp family.
3. Page-hint cutoff when anchor/heading boundary is unavailable.
4. Document-tail fallback if no reliable end boundary exists.

Safety guard:
- If extracted span is suspiciously short, auto-expand and record warning.

Why:
- Product choice is to capture full executive compensation block, not only Section 1-6 narrative.

## Rendering Decisions

### Primary Renderer

- Docling conversion from extracted HTML fragment, then markdown export.

### Fallback Renderer

- Deterministic HTML->Markdown conversion for:
  - headings
  - paragraphs
  - lists
  - tables

Why:
- Keep extraction operational offline or when Docling is unavailable.
- Keep behavior deterministic and testable.

## Output Contract Decisions

Extractor API:
- `extract_cda_markdown(raw_html, metadata) -> CDAExtractionResult`

`CDAExtractionResult` includes:
- `markdown`
- `start_anchor`
- `end_anchor`
- `start_page`
- `end_page`
- `strategy`
- `warnings`
- `confidence`

Why:
- Downstream auditability and debugging require explicit boundary provenance.

## Batch CLI Flow Decisions

Script:
- `scripts/run_cda_markdown_batch.py`

Key CLI decisions:
- Input: CSV with `cik` or `folder_id` first column.
- Fiscal-year filtering only:
  - `--fiscal-year-start`
  - `--fiscal-year-end`
  - must be provided as a pair.
- Fiscal year derivation:
  - Prefer manifest `fiscal_year` when valid.
  - Otherwise infer from filing date: Jan-Aug -> previous year, Sep-Dec -> current year.
- Output artifacts:
  - `output/<batch_label>/cda_markdown.csv`
  - `output/<batch_label>/cda_markdown_log.csv`
  - `output/<batch_label>/cda_markdown/*.md` (unless disabled)

Failure behavior:
- Per-filing failures are logged and do not crash the entire batch.
- Missing local HTML can optionally fall back to EDGAR fetch unless `--no-fetch-fallback` is set.

## Non-Goals (Current State)

- No LLM-based boundary detection.
- No dependency on legacy `ingestion/cda_extractor.py` internals.
- No changes to existing compensation extraction contracts.

## Guidance for Future Agents

If modifying this flow:
1. Preserve deterministic boundary priority (ToC anchor first).
2. Keep trace metadata in output contract.
3. Keep fiscal-year filter semantics aligned with batch pipeline conventions.
4. Do not silently remove markdown table preservation.
5. Ensure fallback renderer remains functional if Docling import/conversion fails.

Recommended validation:
- `poetry run ruff check scripts/run_cda_markdown_batch.py ingestion/cda_markdown_extractor.py`
- `poetry run mypy scripts/run_cda_markdown_batch.py ingestion/cda_markdown_extractor.py --ignore-missing-imports`
- Run a one-CIK smoke batch with fiscal-year filters and confirm output rows + markdown files.
