# docs/standards/

Local copies of the specifications this converter builds against, in a form ripgrep can
search. Design section 8.3 is explicit that specification lookup is grep over local
documents, not a retrieval index — there is no corpus here large enough to justify one,
and an index would be one more thing that can be silently stale.

| File | Derived from | Regenerate |
|---|---|---|
| `omop_cdm_5.4_fields.md` | the pinned DDL in `sql/omop_5.4/` | `python3 tools/make_standards_reference.py` |
| `meds_schema.md` | the installed `meds` package, pinned in `requirements.lock` | same |

Both are generated rather than written, so they cannot drift from what the code actually
builds against. If one disagrees with its source, the source is right.

Larger specification PDFs can be dropped in here too; `.gitignore` excludes them, because
they are big, licensed differently, and easy to re-download.

```bash
rg -i "not null" docs/standards/omop_cdm_5.4_fields.md
rg -i "available_time|death_code" docs/standards/
```
