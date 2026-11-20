# Acceptance Test Plan

`library-reader-obsidian` includes a self-contained synthetic test suite verifying search ranking, provenance digests, byte-offset paging, descriptor-relative scope containment, OS write denial, and container bind-mount semantics.

## Base Synthetic Fixture

Defined in `tests/fixture.py` (`BASE`), each note body is UTF-8 followed by a single LF:

| Path | Body |
| --- | --- |
| `Guides/Restore.md` | `restore backup from cold storage` |
| `Guides/Backup.md` | `rotate backup weekly` |
| `Guides/Atlas.md` | `draft map` |
| `Guides/Archive/Atlas.md` | `verified cartography` |
| `Guides/Unicode.md` | `Straße audit` |
| `Guides/Plain.md` | `[[Atlas]] and https://example.invalid` |
| `Guides/Restricted/Secret.md` | `denied-sentinel` |
| `Guides/.hidden/Secret.md` | `hidden-sentinel` |
| `GuidesExtra/Secret.md` | `sibling-sentinel` |

With `allow: ["Guides"]` and `deny: ["Guides/Restricted"]`, the ten reference queries must return these exact ordered hits:

| Query | Expected hits under `Guides/` |
| --- | --- |
| `restore backup` | `Restore.md` |
| `weekly` | `Backup.md` |
| `backup` | `Backup.md`, `Restore.md` |
| `atlas` | `Archive/Atlas.md`, `Atlas.md`, `Plain.md` |
| `cartography` | `Archive/Atlas.md` |
| `draft map` | `Atlas.md` |
| `STRASSE` | `Unicode.md` |
| `unicode audit` | `Unicode.md` |
| `example.invalid` | `Plain.md` |
| `cold storage` | `Restore.md` |

## Automated Test Matrix

| Suite / Case | Verification |
| --- | --- |
| **Reference queries & provenance** (`test_reader.py`) | Verifies all 10 reference queries, deterministic tie-breaking, stem vs body scoring, independent SHA-256 verification, filename-only match excerpt omission, and unchanged fixture manifest. |
| **Scope, links & non-regular files** (`test_reader.py`, `container_check.py`) | Verifies rejection of denied subdirectories, hidden dot-entries, sibling directories, `..` traversal, backslashes, drive prefixes, FIFOs, directories named `.md`, symlinks (internal, external, and directory links), hardlinks (`st_nlink > 1`), non-Markdown files, and case/normalization aliases. |
| **Partial coverage & source cap** (`test_reader.py`, `container_check.py`) | Verifies that a `1 MiB` note is searchable and reconstructable across pages, a `1 MiB + 1 byte` in-scope candidate yields `too_large` and marks search `coverage="partial"`, invalid UTF-8 yields `invalid_encoding`, and an unreadable mode-`000` in-scope directory yields `io_error` as `UID 10001`. |
| **BOM, CRLF, Unicode mapping & ranges** (`test_reader.py`) | Exercises UTF-8 BOM preservation (`U+FEFF`), CRLF boundary safety, Unicode casefold expansion (`ß` -> `ss`, decomposed accents, `İ`), matches beyond column 160, invalid offsets/digests, and full multi-page byte reconstruction at `4`, `24,576`, and `32,768` byte budgets. |
| **Concurrent mutations & ancestor swaps** (`test_reader.py`, `container_trial.sh`) | Verifies `source_changed` detection across in-place writes, temp-file-plus-rename replacements, mid-read file replacements, and ancestor directory swaps/renames during both direct reads and scans. |
| **Minimum fill & envelope trimming** (`test_reader.py`, `test_mcp.py`) | Verifies the 50% newline minimum-fill rule, serialized `256 KiB` MCP `CallToolResult` enforcement on escape-heavy content (`\u0000`), hit list trimming under tight operator envelopes, and `response_limit` errors without path leakage. |
| **MCP protocol & admission control** (`test_mcp.py`) | Verifies `2025-11-25` and `2025-06-18` initialization, tool discovery, JSON Schema validation, `Host`/`Origin` enforcement (`421`/`403`), `8,192`-byte request cap (`413`), single-operation `busy` gate under parallel requests, and cancellation safety (`asyncio.shield`). |
| **OS write denial** (`write_denial.py`) | Independently verifies `UID:GID 10001:10001`, `ST_RDONLY` mount flags, and `EROFS`/`EACCES`/`EPERM` on write-open attempts against `/library`, `/etc/lro/operator.json`, `/app/server.py`, and installed Python packages. |
| **Performance gate** (`benchmark.py`) | Generates 1,000 synthetic Markdown notes totaling `10 MiB` (`10,485,760` bytes) on a host bind mount and requires 5/5 complete MCP search runs with a median end-to-end latency below `2.0` seconds under a `2,000` ms scan budget, `1` CPU, and `256 MiB` memory. |
