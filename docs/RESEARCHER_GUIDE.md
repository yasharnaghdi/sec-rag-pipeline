# Researcher Guide — sec-rag-pipeline

## Last updated: 2026-03-02

## Who this is for

This guide is written for academic researchers who want to extract structured,
citable data from SEC DEF 14A proxy statements. You do not need to understand
the code to use this system. You need to know how to run a Jupyter notebook
and read a CSV file.

---

## What the system produces

For each proxy statement filing you process, the system produces:

1. **A typed block list** — every paragraph, table, heading, footnote, and image
   in the filing, labelled by type and linked to its section heading
2. **A chunk manifest CSV** — one row per text chunk, with columns:
   - `chunk_id`: unique stable identifier
   - `chunk_index`: position in document order
   - `document_id`: identifies the filing
   - `section_id`: the section heading this chunk falls under
   - `token_count`: number of tokens (max 600)
   - `citation_string`: the citation you use in academic work, format:
     `{Company Name} | {Form Type} | {Filing Date} | {Section} | chunk {N}`
   - `text_preview`: first 120 characters of chunk text

All data is traceable back to the source HTML. The system does not paraphrase,
summarise, or infer. Every chunk is a verbatim excerpt.

---

## Running the pipeline on the ConnectOne filing

Prerequisites:
```bash
cd /path/to/sec-rag-pipeline
cp .env.example .env
# Fill in SEC_USER_AGENT with your name and email (required by SEC EDGAR terms)
poetry install
docker compose up -d postgres
```

Open the notebook:
```bash
poetry run jupyter lab notebooks/03_ingest_parse_chunk.ipynb
```

Run all cells top to bottom. The notebook will:
1. Download the ConnectOne Bancorp DEF 14A from SEC EDGAR (or load from cache)
2. Parse the HTML into typed blocks
3. Show block type distribution and sample headings
4. Show the first compensation table with footnotes
5. Chunk all blocks and export to `output/chunks_cnob_m0.csv`
6. Assert all data integrity constraints

If the final cell prints `✓ All M0 assertions passed`, the pipeline is verified.

---

## Running the full M0 batch (all 5 fixture filings)

Open:
```bash
poetry run jupyter lab notebooks/04_batch_ingest.ipynb
```

Run all cells top to bottom. The notebook will:
1. Read `fixtures/manifest.csv`
2. Resolve/download all fixture filings with cache support
3. Parse and chunk each filing
4. Write all chunks to PostgreSQL via `ChunkWriter`
5. Export `output/m0_batch_summary.csv`
6. Assert M0 gates across all filings

If the final cell prints `✓ All M0 gate assertions passed`, batch ingest is verified.

---

## Running the pipeline on a new filing

1. Add the filing to `fixtures/manifest.csv` with its CIK, accession number, and EDGAR URL
2. Open `notebooks/03_ingest_parse_chunk.ipynb`
3. Change `FILING_URL` and the `DocumentMetadata` fields in cells 2 and 5 to match the new filing
4. Run all cells
5. Export appears in `output/chunks_{cik}_m0.csv`

---

## How to use the CSV in Stata or R

The chunk manifest is a standard UTF-8 CSV. In Stata:
```stata
import delimited "output/chunks_cnob_m0.csv", encoding(UTF-8) clear
list citation_string text_preview in 1/5
```

In R:
```r
library(readr)
chunks <- read_csv("output/chunks_cnob_m0.csv")
head(chunks[, c("citation_string", "section_id", "token_count")])
```

---

## Sections the system reliably identifies

These section labels appear in the `section_id` column of the CSV.
They are detected by rule-based pattern matching with no LLM involvement:

| section_id value | What it contains |
|---|---|
| `COMPENSATION DISCUSSION AND ANALYSIS` | Full CD&A narrative including pay-for-performance rationale |
| `SUMMARY COMPENSATION TABLE` | CEO and NEO total compensation table with footnotes |
| `GRANTS OF PLAN-BASED AWARDS` | Equity and non-equity grant details for the fiscal year |
| `OUTSTANDING EQUITY AWARDS` | Unvested option and stock award schedules |
| `OPTION EXERCISES AND STOCK VESTED` | Realised value from exercise and vesting events |
| `PENSION BENEFITS` | Pension plan values and actuarial assumptions |
| `DIRECTOR COMPENSATION` | Board member compensation table |
| `CORPORATE GOVERNANCE` | Board independence, committees, governance policies |
| `SECURITY OWNERSHIP` | Beneficial ownership tables for directors and large holders |

Chunks with `section_id = "preamble"` fall before the first detected section heading.

---

## How to cite a result in academic work

Use the `citation_string` field directly. Example:

> Executive compensation data sourced from: ConnectOne Bancorp, Inc. | DEF 14A | 2025-04-11 | SUMMARY COMPENSATION TABLE | chunk 47

The `chunk_id` field provides the stable identifier if you need to programmatically
reference the same chunk across multiple analysis runs.

---

## Known limitations (M0)

- Character offset tracking is approximate in M0. Do not use `source_char_start`/`source_char_end` for production citation purposes until M1.
- The query interface (natural language questions) is not yet available. In M0 you work directly with the CSV.
- Image blocks are captured with alt text and caption only. Image content is not analysed.
