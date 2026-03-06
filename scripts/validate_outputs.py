#!/usr/bin/env python3
"""Validate compensation output CSVs after ingestion."""
from __future__ import annotations

import glob
import os
import sys

import pandas as pd

OUTPUT = "output"
errors: list[str] = []


# Test 1: Summary comp has float salary & total
sc = pd.read_csv(f"{OUTPUT}/comp_summary_table.csv")
for col in ["salary", "total"]:
    if col in sc.columns:
        dollar_violations = sc[col].astype(str).str.contains(r"\$", na=False).sum()
        if dollar_violations > 0:
            errors.append(f"FAIL T1: {col} has {dollar_violations} $ signs")
        populated_pct = sc[col].notna().mean() * 100
        print(f"  {col}: {dollar_violations} violations | {populated_pct:.1f}% populated")

# Test 2: No $ signs in any numeric-like object column across all CSVs
for csv_path in glob.glob(f"{OUTPUT}/*.csv"):
    df = pd.read_csv(csv_path)
    for col in df.select_dtypes(include="object").columns:
        hits = df[col].astype(str).str.fullmatch(r"\$[\d,\.]+", na=False).sum()
        if hits > 0:
            filename = os.path.basename(csv_path)
            errors.append(f"FAIL T2: {filename} col={col} bare $ values={hits}")

# Test 3: CDA text completeness
cda = pd.read_csv(f"{OUTPUT}/cda_full_text.csv")
if "cda_full_text" in cda.columns:
    truncated = cda["cda_full_text"].dropna().apply(
        lambda text: not str(text).rstrip().endswith((".", "!", "?"))
    ).sum()
    if truncated > 0:
        errors.append(f"FAIL T3: {truncated} CDA rows truncated mid-sentence")
if "cda_token_count" in cda.columns:
    short_cda = (pd.to_numeric(cda["cda_token_count"], errors="coerce") < 500).sum()
    if short_cda > 0:
        errors.append(f"FAIL T3b: {short_cda} CDA rows under 500 tokens (truncation suspected)")

# Test 4: master_compensation.csv exists and has rows
mc_path = f"{OUTPUT}/master_compensation.csv"
if not os.path.exists(mc_path):
    errors.append("FAIL T4: master_compensation.csv missing")
else:
    mc = pd.read_csv(mc_path)
    if len(mc) == 0:
        errors.append("FAIL T4: master_compensation.csv is empty")
    required_cols = {"ticker", "fiscal_year", "exec_name", "salary", "total"}
    missing = required_cols - set(mc.columns)
    if missing:
        errors.append(f"FAIL T4: master_compensation.csv missing columns: {missing}")

# Test 5: No orphan filings — every folder_id in log appears in master
log = pd.read_csv(f"{OUTPUT}/folder_ingest_log.csv")
if "folder_id" not in log.columns and "cik" in log.columns:
    log["folder_id"] = log["cik"]
if os.path.exists(mc_path):
    mc = pd.read_csv(mc_path)
    if "folder_id" not in mc.columns and "cik" in mc.columns:
        mc["folder_id"] = mc["cik"]
    if "folder_id" in log.columns and "folder_id" in mc.columns:
        orphans = set(log["folder_id"].astype(str)) - set(mc["folder_id"].astype(str))
        if orphans:
            errors.append(f"FAIL T5: {len(orphans)} folder_ids in log not in master: {orphans}")

# Test 6: Numeric sanity — no negative present_value or grant_fair_value
for filename, col in [("pension_benefits.csv", "present_value"), ("grants_plan_based.csv", "grant_fair_value")]:
    path = f"{OUTPUT}/{filename}"
    if os.path.exists(path):
        df = pd.read_csv(path)
        if col in df.columns:
            negatives = (pd.to_numeric(df[col], errors="coerce") < 0).sum()
            if negatives > 0:
                errors.append(f"FAIL T6: {filename} {col} has {negatives} negative values")

# Summary
print("\n--- VALIDATION SUMMARY ---")
if errors:
    for err in errors:
        print(err)
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
