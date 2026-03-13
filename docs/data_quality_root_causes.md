# DATA QUALITY ROOT CAUSES

## Summary

The current baseline no longer reproduces the older year/empty-column bug on the focused parser regression fixture, but the audit identified the exact code paths that previously made those failures possible.

## Root Causes

1. Year strings were too literal upstream.
   - `ingestion/comp_table_extractor.py` previously preserved values like `2022*` or `FY2023` as-is.
   - Downstream logic expected a clean four-digit year, so fallback-year logic could be triggered even when the table contained the right year.

2. Role collapse selected the "most recent" row by raw string sorting.
   - `scripts/run_batch50_key_results.py:_most_recent_row` used the raw `year` field instead of a parsed numeric year.
   - Rows with suffixes/prefixes were not guaranteed to sort correctly.

3. The batch status gate was too permissive.
   - The final `status="ok"` path only failed rows when both `ceo_name` and `ceo_total` were empty.
   - A partially populated CEO row could therefore survive longer than it should have.

4. Branch stability was undermined by an unrelated but high-impact chunking issue.
   - `chunking/splitter.py` initialized `tiktoken` at import time.
   - On machines without a warm tokenizer cache, test collection failed before extraction assertions even ran, which made branch comparison noisy and hid the real data-quality picture.

## Fixes Applied

- Normalized summary-comp year tokens to a clean four-digit year during deterministic row normalization.
- Normalized batch fiscal-year parsing so `2022*` and `FY2023` resolve to `2022` and `2023`.
- Changed row selection to prefer the highest parsed numeric year rather than a raw string sort.
- Tightened the CEO completion gate so a row cannot remain `ok` if either `ceo_name` or `ceo_total` is missing.
- Made `chunking/splitter.py` tokenizer initialization lazy and offline-safe so the test suite can exercise the extraction path reliably.
