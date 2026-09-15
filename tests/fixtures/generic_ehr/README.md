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

It also carries six of the conversion traps of remediation plan T1.12, each a shape a
real export delivered and a converter mishandled: a vital-sign table delivered wide and
declared as components (`vitals_wide.csv`), Fahrenheit temperatures under a Celsius label
(`results.csv`), one note filed under two encounter ids (`notes.csv`), one procedure billed
by the facility and by the professional (`procedures.csv`), two intensive-care stays begun
on the same day (`icu_stays.csv`), and a delivered column nothing reads (`orders.csv`). The
declarations that settle them are in the YAML, and
`tests/integration/test_fixture_traps_generic_ehr.py` builds each with and without its
declaration.
