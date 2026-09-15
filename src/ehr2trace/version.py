"""Code version, recorded in every content-addressed path and run report.

Bump ``CODE_VERSION`` whenever a change alters produced bytes. Because it feeds the
task hash, bumping it invalidates cached artifacts and forces a recompute.
"""

# 0.7.1 on 2026-09-14: a result's reference range reaches canonical range_low/range_high and
# OMOP, and a merge rule keyed by a role that shares its field with another role applies.
CODE_VERSION = "0.7.1"

# Version of the canonical-serialization + id rules (design section 5.1). Kept separate
# from CODE_VERSION so that a pure bugfix elsewhere does not renumber every id.
# Bumped to "2" on 2026-09-13: a drug event's identity now folds in its dose, unit,
# route, status and end time, and a note may leave its encounter out (see the
# conversion remediation plan, T1.1). Every event id changes; that is the point.
HASH_RULE_VERSION = "2"

# Version of the canonical event schema (design section 5.2).
# "2" on 2026-09-13: normalized value and unit, infusion rate, action and discharge
# destination columns were added (remediation plan T1.11).
# "3" on 2026-09-14: range_low and range_high, the reference interval reported with a result.
CANONICAL_SCHEMA_VERSION = "3"

# Version of the deterministic mapping rules; recorded on every canonical event.
DEFAULT_MAPPING_VERSION = "0"
