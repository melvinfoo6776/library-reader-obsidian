"""Independent real HTTP and Linux mount probes. Run only on synthetic fixtures."""

import argparse
import errno
import hashlib
import http.client
import json
import os
import socket
from pathlib import Path
import time

from fixture import BASE, EDGES, PARTIAL

HOST = os.environ.get("LRO_HOST", "library-reader:8000")


def rpc(method, params):
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=10)
    try:
        connection.request("POST", "/mcp", json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        }), {"Host": HOST, "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"})
        response = connection.getresponse()
        wire = response.read()
        assert response.status == 200 and response.getheader("Content-Type") == "application/json"
        obj = json.loads(wire)
        if "error" in obj:
            return obj["error"]
        result = obj["result"]
        if method == "tools/call":
            assert len(wire.split(b'"result":', 1)[1][:-1]) <= 262144
            payload = result["structuredContent"]
            assert json.loads(result["content"][0]["text"]) == payload
            assert result["isError"] == (payload["status"] != "ok")
            return payload
        return result
    finally:
        connection.close()


def call(name, **args):
    return rpc("tools/call", {"name": "library_" + name, "arguments": args})


def check():
    for i in range(50):
        try:
            tools = rpc("tools/list", {})["tools"]
            break
        except ConnectionRefusedError:
            if i == 49:
                raise
            time.sleep(0.1)
    assert [tool["name"] for tool in tools] == ["library_search", "library_read"]
    interfaces = {name for _, name in socket.if_nameindex()}
    assert "lo" in interfaces and "eth0" not in interfaces, interfaces
    assert len(Path("/proc/net/route").read_text().splitlines()) == 1
    assert (os.getuid(), os.getgid()) == (10001, 10001)
    with open("/library/Guides/Restore.md", "rb") as note:
        assert note.read() == BASE["Guides/Restore.md"]
    with open("/etc/lro/operator.json", encoding="utf-8") as config:
        assert json.load(config)["root"] == "/library"
    try:
        os.listdir("/library/Guides/Locked")
    except PermissionError as error:
        assert error.errno == errno.EACCES
    else:
        raise AssertionError("Mode 000 did not deny directory enumeration.")
    target = Path("/library/GuidesExtra/Secret.md")
    assert target.stat().st_nlink == Path("/library/Guides/Hardlink.md").stat().st_nlink == 2
    assert call("search", query="sentinel")["hits"] == [{
        "note_id": "Guides/ExactCase.md", "title": "ExactCase",
        "digest": hashlib.sha256(EDGES["Guides/ExactCase.md"]).hexdigest(),
        "excerpt": {"text": "case-sentinel\n", "line": 1, "offset": 0, "truncated": False},
    }]
    result = call("search", query="no-match-here")
    assert result["hits"] == [] and result["coverage"] == "partial" and result["stop_reason"] is None
    assert result["skips"] == {"too_large": 1, "invalid_encoding": 1, "io_error": 1}, result
    for note in ("Guides/Link.md", "Guides/Hardlink.md", "Guides/Pipe.md", "Guides/DirectoryLink/Secret.md",
                 "Guides/Restricted/Secret.md", "Guides/.hidden/Secret.md", "GuidesExtra/Secret.md",
                 "Guides/exactcase.md", "guides/ExactCase.md"):
        assert call("read", note_id=note) == {"status": "unavailable", "message": "The note is unavailable."}
    assert call("read", note_id="Guides/Invalid.md")["status"] == "invalid_encoding"
    assert call("read", note_id="Guides/Oversize.md")["status"] == "too_large"
    assert call("read", note_id="Guides/Ordinary.md", max_bytes=32_769)["code"] == -32602
    locked = call("read", note_id="Guides/Locked/Canary.md")
    assert locked["status"] == "io_error", locked
    assert call("search", query="backup", root="/not-accepted")["code"] == -32602

    # Independently enumerate actual names; both canonical spellings must round-trip
    # if present, otherwise deliberate aliases must be unavailable.
    names = {"Guides/Caf\u00e9.md", "Guides/Cafe\u0301.md"}
    entries = {"Guides/" + n for n in os.listdir("/library/Guides")}
    hits = call("search", query="cafe\u0301")["hits"]
    assert names & entries and {hit["note_id"] for hit in hits} == names & entries
    for hit in hits:
        note_id = hit["note_id"]
        raw = (Path("/library") / note_id).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == hit["digest"]
        assert call("read", note_id=note_id, expected_digest=hit["digest"])["text"].encode() == raw
    for absent in names - entries:
        assert call("read", note_id=absent)["status"] == "unavailable"
    print(f"Observed {len(hits)} exact canonical spellings on the mount")

    for note, raw in {"Guides/Exact.md": PARTIAL["Guides/Exact.md"],
                      **{n: EDGES[n] for n in ("Guides/Page.md", "Guides/Escapes.md")}}.items():
        digest = hashlib.sha256(raw).hexdigest()
        offset = 0
        while offset < len(raw):
            page = call("read", note_id=note, expected_digest=digest, offset=offset, max_bytes=32_768)
            chunk = page["text"].encode()
            assert 0 < len(chunk) <= 32_768 and page["digest"] == digest
            end = offset + len(chunk)
            assert chunk == raw[offset:end]
            assert page["range"]["start_byte"] == offset and page["range"]["end_byte"] == end
            assert page["next_offset"] == (end if end < len(raw) else None)
            offset = end
    print("PASS: actual HTTP, bind scope, filesystem exclusions, UID permission denial, envelopes and page bytes")


def revision_check(old_hex):
    result = call("read", note_id="Guides/Restore.md", offset=8, expected_digest=old_hex)
    assert result["status"] == "source_changed" and "text" not in result
    raw = Path("/library/Guides/Restore.md").read_bytes()
    new = call("read", note_id="Guides/Restore.md")
    assert new["text"].encode() == raw and new["digest"] == hashlib.sha256(raw).hexdigest() != old_hex
    print("PASS: stale continuation rejected and fresh revision verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--old")
    args = parser.parse_args()
    if args.old:
        revision_check(args.old)
    else:
        check()
