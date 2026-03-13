# BRANCH AUDIT

Timestamp: 2026-03-13 01:23:00 CET

## Branches Checked

- `Separate-files`
- `origin/main` (same commit as `Separate-files`: `3525a80`)
- `main`
- `feat/task4-hardening`
- `feat/llm-comp-extractor-and-batch50`
- `feat/demo-batch50-key-results`
- `feat/ollama-fallback-and-docker-compose`

## Best Candidate

`Separate-files` (equivalent to current `origin/main`) is the best baseline.

Why:

- It is the only branch where a local 50-row reference artifact already exists and validates cleanly.
- `output/b01/key_results.csv` has `43/50` populated `ceo_total` values.
- `scripts/validate_key_results.py --input output/b01/key_results.csv --expected-rows 50` passes all checks.
- The blocking test failure on this baseline was isolated to the offline tokenizer path in `chunking/splitter.py`, which is a contained stabilization fix rather than a broad extraction regression.

## Test Findings

Audit method:

- Used the existing local Python 3.11 Poetry environment directly inside temporary `/tmp` worktrees.
- Plain `poetry run` inside the worktrees was not reliable under sandbox constraints because Poetry attempted to create new virtualenvs in cache.

Results:

- `Separate-files`: `pytest tests/ -q` failed during collection because `chunking/splitter.py` initialized `tiktoken` at import time and attempted a network fetch for `cl100k_base`.
- `main`: same offline `tiktoken` collection failure.
- `feat/task4-hardening`: same offline `tiktoken` collection failure.
- `feat/llm-comp-extractor-and-batch50`: same offline `tiktoken` collection failure.
- `feat/ollama-fallback-and-docker-compose`: same offline `tiktoken` collection failure.
- `feat/demo-batch50-key-results`: `69 passed, 3 failed, 3 skipped, 1 xfailed, 1 xpassed`; failures were in `tests/test_chunk_boundaries.py` because that branch's `core.config.Settings` required env values at chunker construction time.

## Problems Seen

- Newer integration branches shared one common blocker: offline test collection was unstable because `chunking/splitter.py` tried to load tokenizer state from the network at import time.
- Older `feat/demo-batch50-key-results` used a chunker/config path that was still env-required, so chunker construction failed without populated secrets.
- No alternate branch had stronger evidence than `Separate-files` once the validated 50-row output and current extraction features were considered together.
