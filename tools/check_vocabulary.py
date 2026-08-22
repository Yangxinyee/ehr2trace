# -*- coding: utf-8 -*-
"""Check an Athena vocabulary download before trusting it.

A vocabulary is a licensed multi-gigabyte download that arrives by email, and the two
ways it goes wrong are quiet: a truncated file, or a bundle that omits a vocabulary the
dataset actually needs. Both surface later as "everything is concept_id 0", which looks
exactly like having no vocabulary at all.

This answers three questions before a single row is mapped:

  1. are the required tables present and readable?
  2. which vocabularies did the bundle actually include?
  3. how much of *this* dataset's terminology would map with it?

The third one is the useful one -- it estimates the answer without running the ETL.

    python3 tools/check_vocabulary.py /path/to/unzipped/vocab [--dataset ctpe]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

REQUIRED = ("CONCEPT", "CONCEPT_RELATIONSHIP", "VOCABULARY", "DOMAIN", "CONCEPT_CLASS", "RELATIONSHIP")
RECOMMENDED = ("CONCEPT_ANCESTOR", "DRUG_STRENGTH")

#: What this converter's code systems need to be present as vocabulary_id values.
NEEDED_FOR_MAPPING = {
    "ICD10CM": "problem list diagnosis codes",
    "SNOMED": "the standard target for conditions",
    "LOINC": "the standard target for lab measurements",
    "RxNorm": "the standard target for drugs",
}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--dataset", default="ctpe")
    parser.add_argument("--sample", type=int, default=2000, help="terms to test-map")
    args = parser.parse_args(argv)

    directory: Path = args.directory
    if not directory.is_dir():
        print(f"not a directory: {directory}")
        return 2

    print(f"vocabulary at {directory}\n")

    # 1. files present and non-trivial
    missing = []
    for table in REQUIRED + RECOMMENDED:
        hit = next((p for p in directory.glob("*") if p.stem.upper() == table), None)
        if hit is None:
            missing.append(table)
            print(f"  MISSING   {table}")
        else:
            size = hit.stat().st_size
            print(f"  ok        {table:<22} {size / 1e6:>10.1f} MB")
    hard = [t for t in missing if t in REQUIRED]
    if hard:
        print(f"\nrequired tables missing: {hard}. Re-download including them.")
        return 1

    # 2. what the bundle actually contains
    import duckdb

    con = duckdb.connect()
    concept = next(p for p in directory.glob("*") if p.stem.upper() == "CONCEPT")
    con.register(
        "concept",
        con.read_csv(str(concept), sep="\t", header=True, quotechar="", all_varchar=True),
    )
    total = con.execute("SELECT count(*) FROM concept").fetchone()[0]
    print(f"\n{total:,} concepts\n")
    rows = con.execute(
        "SELECT vocabulary_id, count(*) n FROM concept GROUP BY 1 ORDER BY n DESC LIMIT 15"
    ).fetchall()
    print("  included vocabularies (top 15)")
    for vocab, n in rows:
        print(f"    {vocab:<24} {n:>12,}")

    present = {r[0] for r in con.execute("SELECT DISTINCT vocabulary_id FROM concept").fetchall()}
    print()
    gaps = []
    for vocab, why in NEEDED_FOR_MAPPING.items():
        if vocab in present:
            print(f"  ok        {vocab:<12} present ({why})")
        else:
            gaps.append(vocab)
            print(f"  MISSING   {vocab:<12} needed for {why}")
    if gaps:
        print(f"\n  -> re-download including {gaps}, or those terms stay unmapped.")

    # 3. how much of this dataset would actually map
    try:
        _estimate(con, args)
    except Exception as exc:
        print(f"\n(skipped the coverage estimate: {exc})")
    con.close()
    print("\nIf this all looks right:")
    print(f"    export OMOP_VOCAB_DIR={directory}")
    print(f"    ehr2cdm omop --dataset {args.dataset}")
    return 0


def _estimate(con, args) -> None:
    """Test-map a sample of the dataset's own pending terms against this bundle."""
    from ehr2cdm.config import find_dataset_config, load_dataset_config
    from ehr2cdm.paths import WorkLayout
    from ehr2cdm.review import read_pending

    if not os.environ.get("EHR_WORK_ROOT"):
        return
    cfg = load_dataset_config(find_dataset_config(args.dataset))
    layout = WorkLayout.from_env(cfg.dataset_id)
    pending = read_pending(layout)
    if not pending:
        return

    by_system: dict[str, list[str]] = {}
    for row in pending:
        by_system.setdefault(row.get("code_system", "SOURCE"), []).append(row.get("source_string", ""))

    print(f"\ncoverage estimate against {len(pending):,} pending terms")
    for system, codes in sorted(by_system.items(), key=lambda kv: -len(kv[1])):
        sample = [c for c in codes if c][: args.sample]
        if not sample:
            continue
        if system == "SOURCE":
            print(f"  {system:<10} {len(codes):>7,} terms   (free text, needs lexical recall + review)")
            continue
        placeholders = ", ".join("?" for _ in sample)
        hit = con.execute(
            f"SELECT count(DISTINCT concept_code) FROM concept "
            f"WHERE vocabulary_id = ? AND concept_code IN ({placeholders})",
            [system] + sample,
        ).fetchone()[0]
        pct = hit / len(sample)
        print(
            f"  {system:<10} {len(codes):>7,} terms   {pct:>6.1%} of a {len(sample)}-term sample "
            f"match by exact code"
        )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
