# Audit And Lessons

## 1. What The Project Does

`sec-rag-pipeline` ingests SEC filings, parses them into typed blocks, extracts executive-compensation data, and prepares the resulting artifacts for retrieval and downstream generation. The current production path is deterministic-first: fetch the latest proxy filing, parse headings/tables/text, extract compensation rows, and write auditable CSV outputs that can be validated before any retrieval or LLM layer is used.

The stabilization work merged from `cleanup-finalize` through PR 22 turned `main` back into the single source of truth for that flow. The repo now has one coherent branch for the extraction contract, the batch validator, and the chunking and embedding path that feeds later storage and retrieval phases.

## 2. What Went Wrong

The project drifted across multiple active branches without a clean merge discipline. That produced duplicate implementations of the same responsibilities, especially around year parsing, role collapse, and batch serialization. Branch comparisons were also noisy because an unrelated tokenizer initialization problem in `chunking/splitter.py` could fail tests before the data-quality checks even ran.

The extraction bugs were concentrated in a few behavioral gaps:

- summary-compensation year values were preserved too literally, so strings like `2022*` and `FY2023` did not behave like clean fiscal years downstream
- role collapse logic could choose rows by raw string ordering instead of parsed numeric year ordering
- the batch writer allowed rows to remain `status=ok` even when critical CEO fields were still missing
- contributors had to infer the current source of truth by reading several branches and several docs instead of starting from one release branch and one guardrail document

## 3. What `cleanup-finalize` Fixed

At a behavioral level, the merged stabilization branch fixed the failure modes that mattered most to the 50-CIK batch:

- year normalization now turns inputs such as `2022*` and `FY2023` into clean four-digit years before downstream selection logic runs
- role collapse now picks the most recent row using parsed year values rather than raw string sorting
- the CEO completeness gate now blocks silent `status=ok` rows when required CEO identity or compensation fields are missing
- chunking is now offline-safe, so local test collection no longer depends on fetching tokenizer state at import time
- the embedding path now uses Voyage Finance-2 as the default model and preserves empty-batch and input-order guarantees
- rule-based critical section labeling now adds compensation coverage flags and token counts without depending on an LLM
- the repo now includes a focused debug runner, a multi-year fixture, and regression coverage for the data-quality edge cases that previously slipped through

## 4. Guardrails For The Future

Any change to the files below should be treated as extraction-critical and should not merge on intuition alone:

- `ingestion/comp_table_extractor.py`
- `ingestion/critical_section_labeler.py`
- `chunking/splitter.py`
- `indexing/embedder.py`
- `scripts/run_batch50_key_results.py`

Minimum guardrails for those changes:

1. Add or update tests for the behavioral contract you touched.
2. Run `poetry run python scripts/debug_data_quality.py`.
3. Run `poetry run ruff check .`.
4. Run `poetry run mypy . --ignore-missing-imports`.
5. Run `env DB_URL='' poetry run pytest tests/ -q`.
6. If a 50-CIK batch artifact is available, run `poetry run python scripts/validate_key_results.py --input output/<batch_label>/key_results.csv --expected-rows 50`.

The practical lesson from the audit is simple: the project does not need more alternate implementations. It needs one stable path on `main`, explicit verification, and narrow follow-up branches that merge quickly.

## 5. Recommended Release Process

Use `main` as the only release branch. Open short-lived PRs from `feat/...`, `fix/...`, or `chore/...` branches, and keep the verification commands in the PR body so the release record remains auditable.

For a release candidate:

1. Pull the latest `main`.
2. Run the local quality gate (`ruff`, `mypy`, `pytest`).
3. Run `scripts/debug_data_quality.py` for extraction-sensitive work.
4. When network access is available, rerun the 50-CIK batch and validate the resulting `key_results.csv`.
5. Merge via PR, then tag the merged `main` state that you want downstream users to treat as stable.

The cached `output/b01/key_results.csv` artifact remains a useful reference, but it should not replace a fresh online rerun when the environment allows one.

## Source Docs

This summary condenses the findings from:

- `docs/branch_audit.md`
- `docs/core_functions.md`
- `docs/data_quality_root_causes.md`
- `docs/README.md`
- PR 22 (`finalize: stable pipeline with data quality + section labeling`)
