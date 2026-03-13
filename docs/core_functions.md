# CORE FUNCTIONS INVENTORY

## Pipeline Map

1. Acquisition
   - `ingestion/edgar_folder_fetcher.py:fetch_latest_def14a`
   - Status: working baseline for latest DEF 14A retrieval, cache-aware once the accession is known

2. HTML parsing
   - `ingestion/sec_html_parser.py:SECHTMLParser.parse`
   - Status: working; produces typed blocks with deterministic ids, sections, tables, and footnotes

3. Summary compensation extraction (deterministic)
   - `ingestion/comp_table_extractor.py:extract_summary_compensation`
   - Status: working; hardened for carried-forward exec names/titles and normalized year tokens

4. LLM fallback
   - `ingestion/llm_comp_extractor.py:extract_company_comp_from_summary_table`
   - Status: working fallback path, but network/provider dependent by design

5. CD&A extraction
   - `ingestion/cda_extractor.py:extract_cda`
   - Status: working; supplies `cda_full_text`, `cda_token_count`, and `pay_for_performance_flag`

6. Chunking
   - `ingestion/sec_chunker.py:SECChunker`
   - `chunking/splitter.py:SECChunker`
   - Status: working after offline-safe tokenizer initialization and monotonic chunk indexing fix

7. Batch orchestration
   - `scripts/run_batch50_key_results.py:process_cik`
   - `scripts/run_batch50_key_results.py:main`
   - Status: working baseline; deterministic-first extraction, role collapse, validation-friendly CSV writing

8. Validation
   - `scripts/validate_key_results.py:validate`
   - `scripts/validate_key_results.py:main`
   - Status: working; current reference `output/b01/key_results.csv` passes all checks

## Year Handling Hotspots

Year parsing and selection currently live in these functions:

- `ingestion/comp_table_extractor.py:_normalize_summary_year_value`
- `scripts/run_batch50_key_results.py:_normalize_compensation_year`
- `scripts/run_batch50_key_results.py:_most_recent_row`
- `scripts/run_batch50_key_results.py:_collapse_to_roles`
- `scripts/run_batch50_key_results.py:_role_fiscal_year`

These are the functions to audit first whenever fiscal-year mixups reappear.
