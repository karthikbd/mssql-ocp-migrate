# Validation and reports

Validation answers one question: **is the target the same as the source?** It runs at the end of
`migrate` and `finalize`, on its own with `mssql-migrate validate` (any time, against any two
databases), and in a lighter physical form with `lift verify`.

## Modes

| `validation.mode` | What's compared | Cost |
|---|---|---|
| `count` | exact `COUNT_BIG(*)` per table on both sides; schema parity; objects; identity current values | one count scan per table |
| `checksum` (default) | the `count` checks, plus per chunk: `COUNT_BIG`, `SUM(BINARY_CHECKSUM(...))` and `CHECKSUM_AGG(BINARY_CHECKSUM(...))` computed **inside both servers**, plus an exact row-by-row SHA-256 comparison of a random `sample_full_pct` % of chunks | a server-side scan of each side; only small results cross the network |
| `full` | every row of every chunk hashed with SHA-256 on both sides and compared by key | reads all data from both sides |

Any chunk whose checksum differs is **automatically compared row by row**, so even in
`checksum` mode the report lists the exact keys that differ.

### Schema parity (`compare_schema = true`)

- columns: name, type, length, precision, scale, nullability, identity, computed, collation;
- primary key columns and order;
- index, foreign key and check constraint names;
- views, procedures, functions, triggers, synonyms, sequences and types present on the target;
- identity `IDENT_CURRENT` on both sides;
- index storage options: fill factor, `PAD_INDEX`, `IGNORE_DUP_KEY`, lock options and compression,
  for every index and primary key, and heap compression (`copy_index_options`);
- user-created statistics: name and columns (`copy_statistics`);
- extended properties: every name and value (`copy_extended_properties`);
- users and roles, role memberships, schema owners and every `GRANT`/`DENY` (`copy_security`);
- compatibility level, read-committed snapshot and snapshot isolation (`copy_database_settings`),
  every database option, scoped configuration, change tracking and Query Store settings;
- sparse columns, column sets, masks, typed XML, `GENERATED ALWAYS` and hidden columns, and
  Always Encrypted settings, per column;
- memory-optimized tables and their durability, partition schemes, system versioning with its
  history table and retention, change tracking per table, XML, spatial and full-text indexes;
- row-level security policies and their predicates, certificates, module signatures and CDC
  capture instances;
- SQL Agent jobs and linked servers, when copied;
- the text of every view, procedure, function and trigger (ignoring line endings and trailing
  spaces), its `ANSI_NULLS` / `QUOTED_IDENTIFIER` settings, disabled state and trigger order, and
  object owners;
- references in code that resolve on the source but not on the target (for example to a table
  left out by `include` / `exclude`): SQL Server creates such code without complaint and fails when
  it runs.

Database-level items are compared for the scope being migrated: object permissions and properties
of tables outside `include`/`exclude` are ignored. Each check follows its `copy_*` switch, so
turning a copy off also stops it being compared.

### How the row hashes stay driver-independent

The source might be read with pyodbc and the target with the bundled driver. Each value is
normalised by the **column's SQL type** before hashing:

- GUIDs, whether text or `uuid.UUID`, become the same canonical form;
- decimals and money are normalised (`10.5000` = `10.5`);
- integers, bit/bool and floats each have a single canonical form;
- NULL is distinct from every value, including empty strings and `False`.

Rows are fingerprinted with SHA-256 over a length-safe encoding.

### Columns excluded by design

`rowversion` / `timestamp` values are assigned by the target, so they always differ. They are
excluded from every comparison.

### Tables without a key

These are compared with an **order-independent multiset digest**: the sum of row hashes modulo
2²⁵⁶, plus the row count. Duplicate rows count, so losing one of two identical rows is
detected. Individual differing rows can't be named, because there is no key.

### Always Encrypted columns

The server can't compute checksums over ciphertext in a meaningful way, so tables with encrypted
columns are always compared row by row, as ciphertext, whatever the mode.

### LOB, xml and CLR columns in checksums

`BINARY_CHECKSUM` ignores `text`, `ntext`, `image` and `xml`, and only partly covers some large
types. These columns are hashed with `HASHBYTES('SHA2_256', ...)` inside the checksum (SQL Server
2016+). Older servers fall back to `DATALENGTH`, and `full` mode remains exact.

## What validation proves, and what it doesn't

- `full` proves every row of every compared column is identical.
- `checksum` proves counts and a strong aggregate per chunk, plus exact equality on the sampled
  chunks. A deliberate collision is theoretically possible in the aggregate; an accidental one is
  very unlikely.
- `lift verify` relies on the restore's page checksums, metadata row counts and sampled windows.

For audit sign-off on a live source, run `validate` **after** the cut-over freeze, when the
source can no longer change.

## The report

Every run writes two files to `report_dir`:

`report.html`
: self-contained, so you can open it anywhere or attach it to a ticket. It shows PASSED or FAILED;
  the run summary (rows, duration, rows/s, server versions); the identity strategy used; the
  validation summary; and a per-table table (source rows, target rows, chunks, copy seconds,
  result) where each problem names the chunk and the keys. It ends with object differences, DDL
  errors, warnings, and the estimate if there is one.

`report.json`
: the same content in machine-readable form for pipelines: `run_ok`, `validation_summary`, and the
  full per-table and per-chunk results. Passwords are redacted.

```python
import json
r = json.load(open("migration_report/report.json"))
assert r["run_ok"] and r["validation_summary"]["ok"], r["validation_summary"]
```

## Exit codes

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `migrate`, `finalize` | copy and validation passed | something failed (see the report) | configuration error |
| `validate` | identical | differences found | configuration error |
| `lift plan` | no blockers | blockers | configuration error |
| `lift verify` | match | mismatch | configuration error |
| `lift cutover` | done | error | `--confirm` missing |

Interrupting with Ctrl-C exits with 130 and a reminder to use `--resume`.
