# Library Reader Obsidian (`library-reader-obsidian`)

A read-only Model Context Protocol (MCP) server for literal keyword search and source-linked paged reads of Markdown notes in local Obsidian vaults.

## Overview

`library-reader-obsidian` gives an authorized MCP client two read-only operations over an operator-bound Markdown library:

- **`library_search`** — Find approved Markdown notes using Unicode-normalized, case-folded literal keywords across filename stems and raw note bodies. Returns relative note references, whole-file SHA-256 digests, bounded original-text excerpts with line numbers and byte offsets, and explicit scan coverage (`complete` vs `partial`).
- **`library_read`** — Read an exact UTF-8 byte page of an approved Markdown note with revision verification (`expected_digest`), half-open byte coordinates, inclusive line numbers, continuation offsets (`next_offset`, `has_more`, `complete_note`), and strict serialized envelope bounds.

### Security & Operator Boundary

1. **Operator-bound scope:** The operator configures the vault root and permitted directory prefixes (`allow` / `deny`) in an immutable configuration file (`/etc/lro/operator.json`). The MCP client cannot supply or override the vault root, scope rules, or configuration file path.
2. **Descriptor-pinned traversal:** The reader pins the configured root directory descriptor at startup and traverses exact directory entry spellings using `O_RDONLY | O_DIRECTORY | O_NOFOLLOW` and `O_NONBLOCK` file opens. Symlinks, hardlinked files (`st_nlink != 1`), non-regular files (FIFOs, devices, sockets), hidden dot-directories/files, and cross-mount traversals are rejected.
3. **Linux `statx` revalidation:** On Linux kernels supporting `statx`, the reader verifies ancestor directory identity and mount IDs and checks opened file descriptors before and after reading to detect concurrent mutations or ancestor swaps.
4. **Read-only container isolation:** Designed to run as non-root `UID:GID 10001:10001` with a read-only root filesystem (`--read-only`), dropped capabilities (`--cap-drop ALL`), `no-new-privileges`, and read-only bind mounts for `/library` and `/etc/lro/operator.json`.
5. **Sanitized logs and errors:** Operational logs record only MCP method names, negotiated protocol versions, and stable status codes—never search queries, file paths, or note contents.

---

## Repository Layout

| Path | Description |
| --- | --- |
| `server.py` | MCP Streamable HTTP server (`2025-11-25` and `2025-06-18`), tool schemas, serialized envelope enforcement, and single-operation admission gate |
| `config.py` | Startup validation for `/etc/lro/operator.json` (`root`, `allow`, `deny`, and optional `ceilings`) |
| `access.py` | Shared descriptor-relative access boundary, scope checks, safe directory walk, and bounded file reads |
| `reader.py` | Unicode-normalized literal search, deterministic ranking, coordinate mapping, and byte-offset paging |
| `Dockerfile` | Pinned Python 3.12 container image running as `10001:10001` with hash-verified wheels |
| `requirements.in` / `requirements.lock` | Direct dependencies and full hash-locked wheel manifest |
| `operator.example.json` | Example operator configuration |
| `tests/` | Unit tests (`test_reader.py`, `test_mcp.py`), synthetic fixtures (`fixture.py`), container/mount verification (`container_trial.sh`, `container_check.py`, `write_denial.py`), and the 1,000-note synthetic benchmark (`benchmark.py`, `profile_scan.py`) |
| [Architecture.md](Architecture.md) | Detailed architecture, access boundary, paging contract, and error semantics |
| [TOOL_DESCRIPTIONS.md](TOOL_DESCRIPTIONS.md) | Agent-facing MCP tool descriptions and parameter specifications |
| [TEST_PLAN.md](TEST_PLAN.md) | Synthetic acceptance test cases and release gates |
| [DATA_QUALITY.md](DATA_QUALITY.md) | Provenance, scan coverage semantics, and corpus hygiene guidance |

---

## Operator Configuration

Create an `operator.json` file (see [operator.example.json](operator.example.json)) and mount it read-only at `/etc/lro/operator.json`:

```json
{
  "root": "/library",
  "allow": [
    "Guides",
    "Notes"
  ],
  "deny": [
    "Guides/Restricted"
  ],
  "ceilings": {
    "source_bytes": 1048576,
    "page_bytes": 32768,
    "envelope_bytes": 262144,
    "query_chars": 256,
    "query_terms": 16,
    "hits": 50,
    "excerpt_bytes": 320,
    "scan_ms": 2000
  }
}
```

- `root` (required): Absolute path to the mounted vault inside the container (typically `"/library"`).
- `allow` (required): Non-empty list of permitted relative directory prefixes.
- `deny` (optional): List of denied relative directory prefixes (`deny` takes precedence over `allow`).
- `ceilings` (optional): Operator overrides that may lower (never raise) any built-in budget ceiling.

---

## Quick Start (Docker)

1. **Build the image:**

```sh
docker build -t library-reader-obsidian:latest .
```

2. **Create an internal Docker network (recommended for container-to-container MCP):**

```sh
docker network create --driver bridge --internal lro-net
```

3. **Run the read-only MCP server container:**

```sh
docker run -d --name library-reader \
  --network lro-net --network-alias library-reader \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user 10001:10001 --memory 256m --cpus 1 --pids-limit 64 \
  --mount "type=bind,src=/abs/path/to/vault,dst=/library,readonly" \
  --mount "type=bind,src=/abs/path/to/operator.json,dst=/etc/lro/operator.json,readonly" \
  library-reader-obsidian:latest
```

The server listens on `0.0.0.0:8000` at `/mcp` using stateless Streamable HTTP with JSON responses. By default, DNS rebinding protection allows `Host: library-reader:8000` and `Origin: http://library-reader:8000` (configurable via the `LRO_HOST` environment variable).

### Security Considerations

- **Network reachability equals read access:** The server enforces `Host` and `Origin` header validation but has no built-in client authentication or per-user permissions. Keep it on a private `--internal` Docker network with no host-published ports, or place an authenticating reverse proxy in front of it if broader reachability is required.
- **No content redaction:** `library_search` and `library_read` return original Markdown verbatim (including YAML frontmatter and relative note paths). Only include directories in `allow` whose full contents are safe for the connected MCP client to read.

---

## Running the Test Suite

### 1. Local Python Unit & MCP Tests

Using Python 3.12+ in a virtual environment with `requirements.lock` installed (run as a non-root user):

```sh
python -m pip install --only-binary=:all: --require-hashes -r requirements.lock
PYTHONPATH=.:tests python -B -m unittest discover -s tests -v
```

### 2. Full Container, Bind-Mount & Write-Denial Suite

Run the end-to-end isolated Linux container and read-only bind-mount verification suite:

```sh
sh tests/container_trial.sh local
```

### 3. Synthetic 1,000-Note (10 MiB) Performance Benchmark

On a Docker Desktop host, run the five-call MCP search performance gate and mount write-denial check:

```sh
python3 -B tests/benchmark.py
```

---

## License

Released under the [MIT License](LICENSE).
