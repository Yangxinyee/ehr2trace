"""ehr2trace: deterministic EHR -> OMOP CDM 5.4 / MEDS converter.

The public surface of this package is the CLI (:mod:`ehr2trace.cli`). Everything a
dataset needs to say about itself lives in ``datasets/<id>.yaml``; no dataset-specific
string belongs in this package.
"""

from ehr2trace.version import CODE_VERSION

__all__ = ["CODE_VERSION"]
