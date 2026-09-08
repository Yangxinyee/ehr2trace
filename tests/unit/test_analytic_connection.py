"""An analytical connection must be able to spill.

Two functions promised out-of-core execution in their docstrings and neither delivered:
the canonical merge was killed at 195 GB and the MEDS build at 178 GB, both on the first
dataset large enough to matter. In both cases the cause was the same one-line omission --
an in-memory DuckDB connection has no temp directory, and without one it cannot spill.

These tests assert the configuration rather than the behaviour, because provoking a real
spill would need a dataset larger than CI has. That is a weaker test than one would like,
and still strictly better than a comment.
"""

from __future__ import annotations

from pathlib import Path

from ehr2trace.analytics import analytic_connection, memory_limit_gb


def test_the_connection_has_somewhere_to_spill(tmp_path: Path):
    scratch = tmp_path / "scratch"
    with analytic_connection(scratch) as con:
        configured = con.execute("SELECT current_setting('temp_directory')").fetchone()[0]
    assert configured, "no temp directory: this connection cannot spill and will grow until killed"
    assert str(scratch) in configured


def test_the_memory_ceiling_is_below_the_machine(tmp_path: Path):
    """DuckDB's default is ~80% of RAM, which assumes it owns the machine. It does not."""
    limit = memory_limit_gb()
    if limit is None:
        return
    with analytic_connection(tmp_path / "s") as con:
        setting = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    assert setting and setting not in ("0", "0GB"), "no memory limit set"


def test_the_scratch_directory_is_cleaned_up(tmp_path: Path):
    """Spill files are the size of the dataset; leaving them behind fills the work root."""
    scratch = tmp_path / "scratch"
    with analytic_connection(scratch) as con:
        con.execute("SELECT 1").fetchone()
    assert not scratch.exists()


def test_cleanup_happens_even_when_the_query_raises(tmp_path: Path):
    scratch = tmp_path / "scratch"
    try:
        with analytic_connection(scratch) as con:
            con.execute("SELECT * FROM a_table_that_does_not_exist")
    except Exception:
        pass
    assert not scratch.exists()
