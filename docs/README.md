# Docs Overview

## Production Status

- `cleanup-finalize` has been merged into `main` via PR 22, and `main` is now the stable branch to build from.
- Cached 50-CIK reference output validates cleanly: `output/b01/key_results.csv` passes `scripts/validate_key_results.py`.
- CEO coverage on the current cached reference output is `43/50`.
- Chunking is now offline-safe; `tests/test_chunk_boundaries.py` and `tests/unit/test_sec_chunker.py` are green.
- Batch rows now include rule-based critical-section flags and token counts for executive-compensation coverage.

## Project Audit & Lessons

For the narrative summary of the branch audit, root causes, and merged stabilization work, see [audit_and_lessons.md](./audit_and_lessons.md).

## Known Issues

- A fresh online 50-CIK rerun on merged `main` has not been regenerated in this offline environment; the existing `output/b01/` artifact is still the validated reference batch.
- `scripts/debug_data_quality.py` is offline-repeatable via cached filings, but some sampled filings still require the network-only LLM fallback if deterministic extraction cannot resolve a row from cache alone.
