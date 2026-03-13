# Docs Overview

## Production Status

- Cached 50-CIK reference output validates cleanly: `output/b01/key_results.csv` passes `scripts/validate_key_results.py`.
- CEO coverage on the current cached reference output is `43/50`.
- Chunking is now offline-safe; `tests/test_chunk_boundaries.py` and `tests/unit/test_sec_chunker.py` are green.
- Batch rows now include rule-based critical-section flags and token counts for executive-compensation coverage.

## Known Issues

- A fresh 50-CIK rerun on the updated code has not been regenerated in this turn; the existing `output/b01/` artifact predates the new section-label columns.
- `scripts/debug_data_quality.py` is offline-repeatable via cached filings, but some sampled filings still require the network-only LLM fallback if deterministic extraction cannot resolve a row from cache alone.
- Push/PR/tag operations were not executed from this environment.
