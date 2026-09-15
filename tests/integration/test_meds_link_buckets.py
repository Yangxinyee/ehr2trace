"""Collapsing lineage links in buckets must publish exactly what one pass publishes.

On MIMIC-IV's 801 million links the single grouped list() exhausted 195 GiB, so the
links are collapsed in hash buckets of event_id. The bucket count is a memory decision
and must never reach the output.
"""

from __future__ import annotations

import pyarrow.parquet as pq

from ehr2trace import meds
from tests.integration.hand_built_layer import config, meds_frame, the_layer, write_canonical


def test_links_collapsed_in_buckets_publish_the_same_rows_as_one_pass(tmp_path, monkeypatch):
    whole = write_canonical(tmp_path / "one", the_layer())
    meds.build_meds(config(), whole)
    monkeypatch.setattr(meds, "LINKS_PER_BUCKET", 2)
    split = write_canonical(tmp_path / "many", the_layer())
    meds.build_meds(config(), split)
    # The stage removes its scratch directory on exit, so the buckets cannot be counted
    # afterwards; with two links per bucket, more than two links forces several buckets.
    assert pq.ParquetFile(split.canonical_path("event_source")).metadata.num_rows > 2
    a, b = meds_frame(whole), meds_frame(split)
    assert a.height > 0 and a.columns == b.columns
    assert a.sort(a.columns[:4]).equals(b.sort(b.columns[:4]))
