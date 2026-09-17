#!/usr/bin/env python3
"""Remove licensed vocabulary strings from the recorded component results.

The terminology experiments in the appendix read concept names out of a local
Athena vocabulary build. Those names are the input the models rank, so they end
up in the saved records -- and the concept names in OMOP's standard Condition,
Measurement and Drug domains come from SNOMED CT, LOINC and RxNorm, none of
which this project may redistribute. The aggregate numbers the manuscript cites
do not need them.

So the committed records keep every measured quantity, the OHDSI concept
identifiers, the ranks and the hit flags, and drop the vocabulary strings:

  * ``concept_name`` and ``true_concept_name`` everywhere;
  * ``text`` in the Measurement records, where the query string is itself a
    LOINC synonym. The Condition records keep ``text``: those are ICD-10-CM
    descriptions, which are US public domain.

Anyone with the vocabulary build named in ``vocabulary_version`` can restore the
full records by rerunning ``tools/measure_retrieval.py`` and
``tools/measure_terminology_llm.py``; nothing else in the repository reads the
removed fields.

    python3 tools/strip_vocabulary_strings.py            # strip in place
    python3 tools/strip_vocabulary_strings.py --check    # fail if any remain

``--check`` is the half that keeps holding after this script has run once. It is
wired into CI, so a future experiment that writes names back into ``results/``
fails the build instead of quietly republishing licensed content.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

# Fields carrying a vocabulary string, and the domains they must be dropped in.
# "*" means every domain.
LICENSED_FIELDS = {
    "concept_name": "*",
    "true_concept_name": "*",
    "text": "Measurement",
}

NOTE = (
    "Licensed vocabulary strings (SNOMED CT / LOINC / RxNorm concept names, and "
    "LOINC synonyms in the Measurement records) were removed before publication; "
    "see tools/strip_vocabulary_strings.py. Concept identifiers, ranks and all "
    "measured quantities are unchanged."
)


def _strip(node: object, domain: str | None, removed: dict[str, int]) -> object:
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            scope = LICENSED_FIELDS.get(key)
            if scope is not None and (scope == "*" or scope == domain):
                removed[key] = removed.get(key, 0) + 1
                continue
            out[key] = _strip(value, domain, removed)
        return out
    if isinstance(node, list):
        return [_strip(item, domain, removed) for item in node]
    return node


def process(path: Path, check_only: bool) -> tuple[int, dict[str, int]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0, {}
    if not isinstance(document, dict):
        return 0, {}

    domain = document.get("domain")
    removed: dict[str, int] = {}
    stripped = _strip(document, domain, removed)
    total = sum(removed.values())
    if not total:
        return 0, {}
    if check_only:
        return total, removed

    stripped["vocabulary_strings_removed"] = NOTE
    path.write_text(json.dumps(stripped, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return total, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="report files that still carry vocabulary strings and exit 1")
    args = parser.parse_args()

    offenders = 0
    grand_total = 0
    for path in sorted(RESULTS.glob("*.json")):
        total, removed = process(path, args.check)
        if not total:
            continue
        offenders += 1
        grand_total += total
        detail = ", ".join(f"{field} x{count}" for field, count in sorted(removed.items()))
        verb = "still carries" if args.check else "stripped"
        print(f"  {verb} {total:>6} strings  {path.relative_to(RESULTS.parent)}  ({detail})")

    if args.check:
        if offenders:
            print(f"\n{grand_total} licensed vocabulary strings in {offenders} file(s). "
                  f"Run: python3 tools/strip_vocabulary_strings.py", file=sys.stderr)
            return 1
        print("No licensed vocabulary strings in results/.")
        return 0

    print(f"\nRemoved {grand_total} vocabulary strings from {offenders} file(s)."
          if offenders else "Nothing to remove.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
