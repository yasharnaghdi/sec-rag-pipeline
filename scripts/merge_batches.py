#!/usr/bin/env python3
"""Merge all output/batch_*/master_compensation.csv into output/master_compensation_all.csv."""
from __future__ import annotations

import csv
import pathlib
import sys

OUTPUT = pathlib.Path("output")
TARGET = OUTPUT / "master_compensation_all.csv"


def main() -> None:
    """Merge per-batch master files into one combined CSV."""
    batch_dirs = sorted(OUTPUT.glob("batch_*/"))
    if not batch_dirs:
        sys.exit("No batch_* directories found in output/")

    all_rows: list[dict[str, str]] = []
    fieldnames: list[str] = []

    for batch_dir in batch_dirs:
        src = batch_dir / "master_compensation.csv"
        if not src.exists():
            print(f"  SKIP (no master): {batch_dir.name}")
            continue

        with src.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not fieldnames and reader.fieldnames:
                fieldnames = list(reader.fieldnames)
            for row in reader:
                all_rows.append(row)
        print(f"  {batch_dir.name}: {len(all_rows)} rows cumulative")

    if not all_rows:
        sys.exit("No rows found across batch directories")

    with TARGET.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nMerged {len(all_rows)} rows -> {TARGET}")


if __name__ == "__main__":
    main()
