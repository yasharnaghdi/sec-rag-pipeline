# Executive Summary — sec-rag-pipeline

## Last updated: 2026-03-11

## Current release scope

The current stable extraction baseline is the `Separate-files` branch. Its purpose is not full RAG query serving yet; it is a production-style DEF 14A acquisition and extraction pipeline that produces auditable batch CSV outputs suitable for review before retrieval and embedding work is layered on top.

Today’s stable path is:

`EDGAR -> cached HTML -> SEC block parsing -> deterministic compensation extraction -> LLM fallback -> CD&A extraction -> validated CSV outputs`

## What is working now

The branch currently supports:

- CIK-only acquisition of the latest available `DEF 14A`
- SEC-aware HTML parsing into typed text, heading, and table blocks
- XBRL-aware compensation table handling and wide-header detection
- deterministic Summary Compensation Table extraction before any LLM fallback
- clean split of executive name vs title from combined cells
- role assignment using the most recent fiscal year for each executive
- CD&A extraction with token counts and pay-for-performance flagging
- batch validation that rejects silent "status=ok but empty CEO" outputs

## Stable baseline metrics

For the latest hardened 50-CIK reference run on `Separate-files`:

- `50` expected rows were written
- `42` rows had non-empty `ceo_total`
- `42` rows had `cda_token_count > 0`
- key identifier columns were populated for all rows
- normalized numeric compensation fields were emitted as digit-only strings

These metrics define a release-quality baseline for this branch. They are not a claim that every DEF 14A filing is fully solved; they are the operating benchmark the current batch and validator enforce.

## What changed over the last iteration cycle

The last week of IDE-agent iterations converged on a few specific invariants:

1. Fetch by CIK only, and always target the latest `DEF 14A`
2. Treat deterministic extraction as the primary source of truth
3. Fix extraction defects in the parser or extractor, not by patching downstream CSVs
4. Collapse multi-year executive rows by taking the most recent fiscal year for the selected role
5. Reject apparently successful rows when CEO identity and total compensation are both missing
6. Preserve enough CD&A and compensation text through chunking so later retrieval work has intact evidence

## Branch meaning

The repo contains several task branches that reflect the recent iteration history:

- `Separate-files`: stable integration branch and current release baseline
- `main`: conservative branch that may lag the latest extraction hardening until merged
- `fix/cik-only-acquisition`: acquisition-only correction branch
- `feat/task4-hardening`: parser, validator, and edge-case hardening branch
- `feat/llm-comp-extractor-and-batch50`: LLM fallback and batch-runner branch
- `feat/ollama-fallback-and-docker-compose`: local runtime, fallback routing, and compose support

## What should not be committed

Generated outputs under `output/` are run artifacts, not source files. In particular:

- do not commit `output/<batch_label>/key_results.csv`
- do not commit `output/<batch_label>/batch_log.csv`
- do not use generated CSVs as a substitute for tests or validators

The repo already ignores `output/`; this policy should stay in place.

## Where to look first

For the most relevant files that explain the current reasoning and end result:

- `ingestion/comp_table_extractor.py`
- `scripts/run_batch50_key_results.py`
- `scripts/validate_key_results.py`
- `ingestion/cda_extractor.py`
- `ingestion/sec_chunker.py`
- `docs/PIPELINE_AGENT_HANDOFF.md`

For local evidence only, inspect:

- `output/b01/key_results.csv`
- `output/b01/batch_log.csv`

## What is next

The next major step is to keep this extraction baseline stable while finishing the chunk embedding and storage path. Retrieval and answer generation should build on these validated outputs rather than bypass them.
