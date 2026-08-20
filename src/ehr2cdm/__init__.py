"""ehr2cdm: deterministic EHR -> OMOP CDM 5.4 / MEDS converter.

The public surface of this package is the CLI (:mod:`ehr2cdm.cli`). Everything a
dataset needs to say about itself lives in ``datasets/<id>.yaml``; no dataset-specific
string belongs in this package.
"""

from ehr2cdm.version import CODE_VERSION

__all__ = ["CODE_VERSION"]
