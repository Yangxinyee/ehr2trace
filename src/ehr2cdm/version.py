"""Code version, recorded in every content-addressed path and run report.

Bump ``CODE_VERSION`` whenever a change alters produced bytes. Because it feeds the
task hash, bumping it invalidates cached artifacts and forces a recompute.
"""

CODE_VERSION = "0.3.0"

# Version of the canonical-serialization + id rules (design section 5.1). Kept separate
# from CODE_VERSION so that a pure bugfix elsewhere does not renumber every id.
HASH_RULE_VERSION = "1"

# Version of the canonical event schema (design section 5.2).
CANONICAL_SCHEMA_VERSION = "1"

# Version of the deterministic mapping rules; recorded on every canonical event.
DEFAULT_MAPPING_VERSION = "0"
