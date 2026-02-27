# Researcher Guide

## Current Milestone State
- Task 3 is complete.
- Use `SECHTMLParser.parse(raw_html, metadata)` to transform a DEF 14A HTML string into `BaseBlock` subclasses from `ingestion.metadata_model`.
- Parser output now includes section-aware headings, table rows with merged-cell expansion, linked table footnotes, inline XBRL annotations, and image blocks with position tokens.
- Blocks retain deterministic order indices and source offsets to support auditability in later retrieval/debug workflows.
