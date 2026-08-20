# generic_ehr — synthetic generalization fixture

Entirely synthetic. No patient data, real or derived, appears here.

Structurally unlike the reference export on purpose, because a converter that only
works on the export it was written against has not been generalized:

* comma-separated, not tab-separated, and no byte-order marks;
* different column names for every field role, and a different file per domain;
* two sites instead of cohort partitions, with no membership label;
* **no anchor concept at all** — nothing here is duplicated per extraction anchor;
* one patient (`PX-2`) appears at both sites and must resolve to one subject.

It is converted by `datasets/generic_ehr.yaml` and no core code change. That file is
the entire cost of onboarding a new source, which is the only claim of generality this
project makes.
