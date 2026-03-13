# Contributing

## 1. First-Time Setup

A clean contributor setup should get to a green test run in five commands:

```bash
git clone https://github.com/yasharnaghdi/sec-rag-pipeline.git
cd sec-rag-pipeline
cp .env.example .env
docker compose up -d postgres qdrant
poetry install && poetry run pytest tests/ -v --tb=short
```

Before running SEC-facing scripts, fill in `.env` with a real `EDGAR_USER_AGENT` and `SEC_USER_AGENT`.

## 2. Branching Model

Base all work on `main`. Create short-lived branches from `main`, keep them narrowly scoped, and merge them back through a pull request once the required checks are green.

Preferred branch prefixes:

- `feat/<scope>`
- `fix/<scope>`
- `chore/<scope>`

Optional when they describe the work more clearly:

- `docs/<scope>`
- `test/<scope>`

Examples:

- `feat/voyage-embedder`
- `fix/edgar-rate-limit`
- `chore/release-docs`

## 3. Commit Message Format

Use Conventional Commits:

```text
<type>(<scope>): <description>
```

Examples:

- `feat(indexing): implement Voyage Finance-2 batch embedding`
- `fix(ingestion): preserve table row order in parser`
- `docs(release): v0.1.0`

## 4. Coding Standards

| Rule | Expectation |
| --- | --- |
| Typing | New and modified Python code should satisfy strict mypy expectations. |
| Lint | `ruff check .` must pass before opening a PR. |
| Logging | Use `logging.getLogger(__name__)`; do not add `print()`. |
| Docstrings | Public functions and classes need docstrings when behavior is non-trivial. |
| Tests | Add or update tests with behavior changes. |
| Style | Match existing naming, file structure, and parser conventions. |

## 5. TDD Workflow

Use a test-first loop for behavior changes:

1. Start with a failing test or extend an existing one.
2. Confirm the failure is for the behavior you intend to change.
3. Implement the smallest code change that makes the test pass.
4. Re-run targeted tests first, then the full gate.
5. Refactor only after the tests are green.

Do not skip the failing-test step for parser, chunking, or extraction logic unless the change is docs-only.

## 6. Required Local Checks Before PR

Run this minimum gate before opening or updating a pull request:

```bash
poetry run ruff check .
poetry run mypy . --ignore-missing-imports
env DB_URL='' poetry run pytest tests/ -q
```

This is the baseline local gate expected to match the project CI path for routine changes.

## 7. Data-Extraction-Critical Changes

If your change touches the extraction-critical path, do more than the baseline gate. This applies in particular to:

- `ingestion/comp_table_extractor.py`
- `ingestion/critical_section_labeler.py`
- `chunking/splitter.py`
- `indexing/embedder.py`
- `scripts/run_batch50_key_results.py`

Required follow-up checks:

```bash
poetry run python scripts/debug_data_quality.py
```

If a 50-CIK batch artifact is available locally, also run:

```bash
poetry run python scripts/validate_key_results.py \
  --input output/<batch_label>/key_results.csv --expected-rows 50
```

Use this checklist before requesting review:

1. Rebase or merge the latest `main`.
2. Keep the PR scope narrow enough to review in one sitting.
3. Run the required local checks above.
4. Update docs if CLI flags, env vars, outputs, or validation expectations changed.
5. Paste the verification commands and outcomes into the PR description.
6. Call out any known risks, skipped tests, or network-dependent steps.
7. Do not stage generated outputs from `output/`, especially `key_results.csv` and `batch_log.csv`.

## 8. Protected Files

These files or paths need extra care because they define contracts, CI, or storage shape:

| Path | Why it is protected |
| --- | --- |
| `tests/` | Tests are the spec. Change only when the spec itself must evolve. |
| `storage/schema.sql` | Database schema contract. |
| `.github/workflows/` | CI gate definitions. |
| `fixtures/` | Shared research inputs and release fixtures. |
| `AGENTS.md` | Agent operating contract for the repo. |

## 9. Environment Variables

Minimum variables for active development:

| Variable | Required | Notes |
| --- | --- | --- |
| `EDGAR_USER_AGENT` | yes | Primary SEC identity string. |
| `SEC_USER_AGENT` | yes | Some batch scripts still read this name directly. |
| `OPENAI_API_KEY` | conditional | Required for the OpenAI extraction path. Use `dummy` to force Ollama fallback. |
| `VOYAGE_API_KEY` | conditional | Required for Voyage embedding work. |
| `DB_URL` | conditional | Required for Postgres-backed chunk writes. |
| `QDRANT_URL` | conditional | Required for vector indexing and retrieval work. |
| `OLLAMA_BASE_URL` | conditional | Needed when using local Ollama instead of OpenAI. |
| `OLLAMA_MODEL` | conditional | Defaults to `llama3.1`. |

## 10. Running a Batch

Three-company smoke run:

```bash
poetry run python scripts/run_batch50_key_results.py \
  --input fixtures/client_input.csv --batch-label smoke --limit 3
poetry run python scripts/validate_key_results.py \
  --input output/smoke/key_results.csv --expected-rows 3
```

Full S&P 500 prep:

```bash
poetry run python scripts/build_sp500_manifest.py
poetry run python scripts/batch_download.py
poetry run python scripts/batch_ingest.py --limit 50
```

If you want the full local stack, use:

```bash
docker compose --profile full up --build -d
```

## 11. Stable Baseline And Review Files

Treat `main` as the single source of truth and read these files first when you need the shortest path to the current extraction contract:

- `ingestion/comp_table_extractor.py`
- `ingestion/critical_section_labeler.py`
- `chunking/splitter.py`
- `indexing/embedder.py`
- `scripts/run_batch50_key_results.py`
- `scripts/validate_key_results.py`

Those files define the current reasoning, validation contract, and release-critical behavior.

## 12. Getting Help

Start with these repo-local sources:

- `README.md` for setup, architecture, and runtime commands
- `AGENTS.md` for active milestone scope and agent guardrails
- `docs/DECISION_LOG.md` for architectural context
- `docs/TECHNICAL_AUDIT.md` and `docs/RESEARCHER_GUIDE.md` for project history and research-facing context

If the docs do not answer the question, open a GitHub issue with the failing command, exact error, and the commit SHA you tested.
