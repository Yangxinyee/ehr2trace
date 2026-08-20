# -*- coding: utf-8 -*-
"""
make_local_baselines.py — generate tools/baselines.local.json.

That file holds real MRNs and patient-level service dates (CT anchors). It is PHI and is
**never committed** (.gitignore excludes it). The documents and the verification script in
this repository refer only to the pseudonyms PT-A / PT-B / PT-C.

Run once on a machine that has the raw data. The first run needs the pseudonym-to-MRN
correspondence:
    python3 tools/make_local_baselines.py PT-A=<MRN> PT-B=<MRN> PT-C=<MRN>

Later runs reuse the mapping already stored in the generated file:
    python3 tools/make_local_baselines.py
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(os.environ.get("EHR_DATA_ROOT", ""))
OUT = Path(__file__).with_name("baselines.local.json")

ANCHOR_PATIENT = "PT-B"          # the patient used for anchor regression
ANCHOR_PARTITIONS = {
    "29_has":  "29 - pulmonary embolism/has_embolism",
    "29b_has": "29b - pulmonary embolism/has_pulmonary_embolism",
    "29b_no":  "29b - pulmonary embolism/no_pulmonary_embolism",
}


def load_alias_map(args: list[str]) -> dict[str, str]:
    """Pseudonym -> real MRN. Real MRNs are PHI and are never written into this file.

    Command-line arguments win; with no arguments, reuse the existing
    baselines.local.json.
    """
    if args:
        m = {}
        for a in args:
            if "=" not in a:
                raise SystemExit(f"Arguments must look like ALIAS=MRN, got: {a}")
            alias, mrn = a.split("=", 1)
            m[alias.strip()] = mrn.strip()
        return m
    if OUT.exists():
        return json.loads(OUT.read_text(encoding="utf-8"))["alias_to_mrn"]
    raise SystemExit(
        "The first run needs the pseudonym-to-MRN correspondence, for example:\n"
        "    python3 tools/make_local_baselines.py PT-A=<MRN> PT-B=<MRN> PT-C=<MRN>\n"
        "Later runs reuse the stored mapping automatically.")


def main(argv: list[str]) -> int:
    if not ROOT.exists():
        print(f"Data root not found: {ROOT}. Set EHR_DATA_ROOT and retry.")
        return 2

    alias_to_mrn = load_alias_map(argv)
    mrn = alias_to_mrn[ANCHOR_PATIENT]
    anchors: set[str] = set()
    for pid, rel in ANCHOR_PARTITIONS.items():
        f = next((ROOT / rel).glob("*echo.txt"))
        r = subprocess.run(["grep", f"^{mrn}\t", str(f)], capture_output=True, text=True)
        # Batch 29 carries a full timestamp and 29b a bare date; take the first 10
        # characters so both land at date granularity.
        anchors |= {line.split("\t")[3][:10] for line in r.stdout.splitlines()}

    OUT.write_text(json.dumps({
        "_warning": "Contains real MRNs and patient-level service dates. "
                    "Never commit. Never share.",
        "alias_to_mrn": alias_to_mrn,
        "ptb_anchors": sorted(anchors),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT.chmod(0o600)
    print(f"Wrote {OUT} ({len(alias_to_mrn)} alias mappings, "
          f"{len(anchors)} anchor dates, mode 600)")
    print("Reminder: this file is excluded by .gitignore. Do not force-add it.")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
