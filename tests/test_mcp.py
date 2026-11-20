import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from starlette.testclient import TestClient

from config import Config
from reader import Reader
from server import HOST, build_app
from fixture import BASE, EDGES, operator, setup

URL = f"http://{HOST}/mcp"
CAP = 262144


class MCP(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "library"
        setup(root, {**BASE, **EDGES})
        self.config = Config.from_dict(operator(root))
        self.app = build_app(self.config)
        self.addCleanup(self.app.state.library_access.close)
        self.client = self.enterContext(TestClient(self.app))

    def rpc(self, method, params, version="2025-11-25"):
        response = self.client.post(URL, headers={
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": version,
        }, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        self.assertEqual(response.status_code, 200, response.text[:300])
        self.assertNotIn("mcp-session-id", response.headers)
        return response

    def call(self, name, arguments, version="2025-11-25"):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments}, version)
        result = response.json()["result"]
        # Measure the actual wire result value, excluding the JSON-RPC framing.
        json_text = response.content.split(b'"result":', 1)[1][:-1]
        self.assertEqual(json.loads(json_text), result)
        self.assertLessEqual(len(json_text), CAP)
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")
        payload = result["structuredContent"]
        self.assertEqual(json.loads(result["content"][0]["text"]), payload)
        self.assertEqual(result["isError"], payload["status"] != "ok")
        return payload, json_text

    def test_protocol_discovery_and_strict_inputs(self):
        for version in ("2025-06-18", "2025-11-25"):
            init = self.rpc("initialize", {"protocolVersion": version, "capabilities": {},
                                           "clientInfo": {"name": "independent", "version": "1"}}, version)
            self.assertEqual(init.json()["result"]["protocolVersion"], version)
            tools = self.rpc("tools/list", {}, version).json()["result"]["tools"]
            self.assertEqual([t["name"] for t in tools], ["library_search", "library_read"])
            for spec, fields in zip(tools, ({"query", "limit"}, {"note_id", "expected_digest", "offset", "max_bytes"})):
                self.assertEqual(set(spec["inputSchema"]["properties"]), fields)
                self.assertFalse(spec["inputSchema"]["additionalProperties"])
                self.assertEqual(spec["annotations"],
                                 {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
                Draft202012Validator.check_schema(spec["inputSchema"])
                Draft202012Validator.check_schema(spec["outputSchema"])
            page_budget = tools[1]["inputSchema"]["properties"]["max_bytes"]
            self.assertEqual((page_budget["default"], page_budget["maximum"]), (32_768, 32_768))
            payload, _ = self.call("library_search", {"query": "restore backup"}, version)
            Draft202012Validator(tools[0]["outputSchema"]).validate(payload)
            hit, = payload["hits"]
            raw = BASE[hit["note_id"]]
            self.assertEqual(hit["digest"], hashlib.sha256(raw).hexdigest())
            for note in (hit["note_id"], "Guides/.hidden/Secret.md"):
                result, _ = self.call("library_read", {"note_id": note}, version)
                Draft202012Validator(tools[1]["outputSchema"]).validate(result)
                self.assertEqual(result.get("text", "").encode(), raw if note == hit["note_id"] else b"")
        for args in ({"query": " "}, {"query": "a" * 257}, {"query": "a " * 17},
                     {"query": "a", "limit": 1.0}, {"query": "a", "limit": True},
                     {"query": "a", "limit": 51}, {"query": "a", "root": "/do-not-echo"},
                     {"query": "a", "allow": ["GuidesExtra"]}):
            self.invalid("library_search", args)
        for args in ({"offset": False}, {"offset": 1.0}, {"offset": -1}, {"max_bytes": 3},
                     {"max_bytes": 32_769}, {"expected_digest": 1}, {"config": "/do-not-echo"}):
            self.invalid("library_read", {"note_id": "Guides/Atlas.md", **args})

    def invalid(self, name, arguments):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments})
        self.assertEqual(response.json()["error"]["code"], -32602)
        self.assertNotIn("do-not-echo", response.text)

    def test_actual_wire_envelopes_and_paging(self):
        for version in ("2025-06-18", "2025-11-25"):
            ordinary, _ = self.call("library_read", {
                "note_id": "Guides/Ordinary.md", "max_bytes": 32_768,
            }, version)
            self.assertEqual(ordinary["returned_bytes"], 32_768)
            self.assertTrue(ordinary["has_more"])
            self.assertEqual(ordinary["next_offset"], 32_768)
            for note in ("Guides/Escapes.md", "Guides/Page.md"):
                raw = EDGES[note]
                digest = hashlib.sha256(raw).hexdigest()
                offset, pages = 0, []
                while offset < len(raw):
                    payload, wire = self.call("library_read", {
                        "note_id": note, "offset": offset, "max_bytes": 32_768, "expected_digest": digest,
                    }, version)
                    page = payload["text"].encode()
                    self.assertEqual(payload["digest"], digest)
                    self.assertGreater(len(page), 0)
                    if note.endswith("Escapes.md") and offset == 0:
                        self.assertLess(len(page), 32_768)
                        self.assertGreater(len(wire), CAP - 32)
                    pages.append(page)
                    offset += len(page)
                    self.assertEqual(payload["next_offset"], offset if offset < len(raw) else None)
                self.assertEqual(b"".join(pages), raw)

    def test_long_exact_path_and_operator_ceilings(self):
        note = "/".join(["Guides", *(["long" * 40] * 5), "Long.md"])
        path = self.config.root / note
        path.parent.mkdir(parents=True)
        path.write_bytes(b"long-path-keyword")
        hit, = self.call("library_search", {"query": "long-path-keyword"})[0]["hits"]
        self.assertEqual(hit["note_id"], note)
        self.assertEqual(self.call("library_read", {"note_id": note})[0]["text"], "long-path-keyword")

        config = Config.from_dict(operator(self.config.root, hits=1, page_bytes=4, envelope_bytes=512))
        app = build_app(config)
        self.addCleanup(app.state.library_access.close)
        with TestClient(app) as client:
            original, self.client = self.client, client
            try:
                specs = self.rpc("tools/list", {}).json()["result"]["tools"]
                for tool, key, cap in zip(specs, ("limit", "max_bytes"), (1, 4)):
                    schema = tool["inputSchema"]["properties"][key]
                    self.assertEqual((schema["default"], schema["maximum"]), (cap, cap))
                self.invalid("library_search", {"query": "long", "limit": 2})
                self.invalid("library_read", {"note_id": note, "max_bytes": 5})
                for name, args in (("library_read", {"note_id": note}),
                                   ("library_search", {"query": "long-path-keyword"})):
                    result, wire = self.call(name, args)
                    self.assertEqual(result["status"], "response_limit")
                    self.assertNotIn("Guides", wire.decode())
                    self.assertLessEqual(len(wire), 512)
            finally:
                self.client = original

    def test_transport_restrictions(self):
        for host, origin, code in (("other.invalid:8000", None, 421),
                                   (HOST, "http://other.invalid", 403)):
            headers = {"Host": host, "Accept": "application/json, text/event-stream"}
            if origin:
                headers["Origin"] = origin
            response = self.client.post(URL, headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(response.status_code, code)
        response = self.client.post(URL, headers={"Accept": "application/json, text/event-stream"},
                                    json={"padding": "x" * 9000})
        self.assertEqual(response.status_code, 413)
        modern = self.client.post(URL, headers={"MCP-Protocol-Version": "2026-07-28"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(modern.status_code, 400)

    def test_busy_for_parallel_read_and_search(self):
        started, release = threading.Event(), threading.Event()
        read = Reader.read

        def hold(reader, **args):
            started.set()
            if not release.wait(5):
                raise TimeoutError
            return read(reader, **args)

        args = {"note_id": "Guides/Restore.md"}
        with patch.object(Reader, "read", hold), ThreadPoolExecutor(1) as pool:
            first = pool.submit(self.call, "library_read", args)
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(self.call("library_read", args)[0]["status"], "busy")
                self.assertEqual(self.call("library_search", {"query": "backup"})[0]["status"], "busy")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)[0]["status"], "ok")
        self.assertEqual(self.call("library_search", {"query": "backup"})[0]["status"], "ok")

    def test_gate_stays_held_after_cancellation(self):
        started, release = threading.Event(), threading.Event()
        read = Reader.read

        def hold(reader, **args):
            started.set()
            if not release.wait(5):
                raise TimeoutError
            return read(reader, **args)

        class CaptureServer:
            def __init__(self, name, on_list_tools, on_call_tool):
                self.call = on_call_tool
                self.middleware = []

            def streamable_http_app(self, **kwargs):
                return SimpleNamespace(state=SimpleNamespace(), call=self.call, add_middleware=lambda _: None)

        with patch("server.Server", CaptureServer):
            app = build_app(self.config)
        self.addCleanup(app.state.library_access.close)

        async def check():
            ctx = SimpleNamespace(protocol_version="2025-11-25")
            params = SimpleNamespace(name="library_read", arguments={"note_id": "Guides/Restore.md"})
            task = asyncio.create_task(app.call(ctx, params))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual((await app.call(ctx, params)).structured_content["status"], "busy")
            finally:
                release.set()

        with patch.object(Reader, "read", hold):
            asyncio.run(check())

    def test_invalid_output_and_too_small_startup_envelope(self):
        with patch.object(Reader, "read", return_value={"status": "ok", "text": "/do-not-echo"}):
            result = self.rpc("tools/call", {
                "name": "library_read", "arguments": {"note_id": "Guides/Restore.md"},
            }).json()
        self.assertEqual(result["error"]["code"], -32603)
        self.assertNotIn("do-not-echo", json.dumps(result))
        config = Config.from_dict(operator(self.config.root, envelope_bytes=16))
        with self.assertRaisesRegex(ValueError, "envelope"):
            build_app(config)
