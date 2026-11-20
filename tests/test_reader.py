import hashlib
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from access import Access, AccessError
from config import Config, load_config
from reader import Reader
from server import result_bytes, result_for
from fixture import BASE, EDGES, MIB, PARTIAL, manifest, operator, put, setup


class Core(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.tmp / "library"
        setup(self.root)
        self.reader = self.make_reader(self.root)

    def make_reader(self, root, **ceilings):
        config = Config.from_dict(operator(root, **ceilings))
        access = Access(config)
        self.addCleanup(access.close)
        return Reader(access, lambda p: len(result_bytes(result_for(p))) <= config.ceilings.envelope_bytes)

    def search(self, query, limit=20):
        return self.reader.search(query, limit)

    def read(self, note, digest=None, offset=0, size=32768):
        return self.reader.read(note, digest, offset, size)

    def test_reference_queries_and_original_evidence(self):
        queries = {
            "restore backup": ["Restore.md"], "weekly": ["Backup.md"],
            "backup": ["Backup.md", "Restore.md"],
            "atlas": ["Archive/Atlas.md", "Atlas.md", "Plain.md"],
            "cartography": ["Archive/Atlas.md"], "draft map": ["Atlas.md"],
            "STRASSE": ["Unicode.md"], "unicode audit": ["Unicode.md"],
            "example.invalid": ["Plain.md"], "cold storage": ["Restore.md"],
        }
        before = manifest(self.root)
        for query, names in queries.items():
            with self.subTest(query=query):
                first = self.search(query)
                self.assertEqual(first, self.search(query))
                self.assertEqual(first["coverage"], "complete")
                self.assertEqual(first["scanned_notes"], 6)
                self.assertEqual([h["note_id"] for h in first["hits"]], ["Guides/" + n for n in names])
                for hit in first["hits"]:
                    note = hit["note_id"]
                    raw = BASE[note]
                    digest = hashlib.sha256(raw).hexdigest()
                    self.assertEqual(hit["digest"], digest)
                    self.assertEqual(self.read(note, digest)["text"].encode(), raw)
                    if "excerpt" in hit:
                        self.assertEqual(hit["excerpt"], {
                            "text": raw.decode(), "offset": 0, "line": 1, "truncated": False})
        self.assertNotIn("excerpt", self.search("atlas")["hits"][0])
        self.assertEqual(self.search("absent")["hits"], [])
        limited = self.search("backup", 1)
        self.assertEqual(limited["matched_scanned"], 2)
        self.assertEqual(limited["limit_reason"], "count")
        self.assertEqual(limited["coverage"], "complete")
        self.assertEqual(manifest(self.root), before)

    def test_scope_links_nonregular_and_exact_spelling(self):
        guides = self.root / "Guides"
        os.mkfifo(guides / "FIFO.md")
        (guides / "LinkedDir").symlink_to(self.root / "GuidesExtra")
        (guides / "External.md").symlink_to(put(self.tmp, "Out.md", b"external-sentinel"))
        (guides / "Internal.md").symlink_to(guides / "Plain.md")
        os.link(self.root / "GuidesExtra/Secret.md", guides / "Hard.md")
        put(self.root, "Guides/Draft.txt", b"other-sentinel")
        put(self.root, "Guides/.dot.md", b"dot-sentinel")
        put(self.root, "Guides/ExactCase.md", b"case-sentinel")
        (guides / "Folder.md").mkdir()
        baseline = self.read("Guides/Missing.md")
        for ref in (
            "Guides/Restricted/Secret.md", "Guides/.hidden/Secret.md", "GuidesExtra/Secret.md",
            "Guides/../Guides/Plain.md", "./Guides/Plain.md", "Guides//Plain.md",
            "/Guides/Plain.md", "C:/Guides/Plain.md", "Guides\\Plain.md", "Guides/Plain.md\0",
            "Guides/FIFO.md", "Guides/Folder.md", "Guides/LinkedDir/Secret.md", "Guides/External.md",
            "Guides/Internal.md", "Guides/Hard.md", "Guides/Draft.txt", "Guides/.dot.md",
            "guides/Plain.md", "Guides/exactcase.md",
        ):
            self.assertEqual(self.read(ref, "broken"), baseline, ref)
        result = self.search("sentinel")
        self.assertEqual([h["note_id"] for h in result["hits"]], ["Guides/ExactCase.md"])
        self.assertEqual(result["coverage"], "complete")
        self.assertFalse(result["skips"])

    def test_partial_and_exact_source_cap(self):
        for note, data in PARTIAL.items():
            if "/Locked/" not in note:
                put(self.root, note, data)
        put(self.root, "Guides/Restricted/Secret.md", b"\xff" * (MIB + 1))
        result = self.search("not-present")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["coverage"], "partial")
        self.assertIsNone(result["stop_reason"])
        self.assertEqual(result["skips"], {"invalid_encoding": 1, "too_large": 1})
        self.assertEqual(result["scanned_notes"], 7)
        self.assertEqual(self.read("Guides/Invalid.md")["status"], "invalid_encoding")
        self.assertEqual(self.read("Guides/Oversize.md")["status"], "too_large")
        self.assertEqual(self.read("Guides/Restricted/Secret.md")["status"], "unavailable")
        self.assertEqual(self.read("Guides/Exact.md")["total_bytes"], MIB)
        self.reconstruct("Guides/Exact.md", PARTIAL["Guides/Exact.md"], 32_768)

    def test_unreadable_in_scope_directory(self):
        self.assertNotEqual(os.getuid(), 0, "Run boundary tests as a non-root user.")
        path = put(self.root, "Guides/Locked/Canary.md", b"blocked-value")
        path.parent.chmod(0)
        self.addCleanup(path.parent.chmod, 0o755)
        with self.assertRaises(PermissionError):
            list(path.parent.iterdir())
        result = self.search("blocked-value")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["skips"], {"io_error": 1})
        self.assertEqual(self.read("Guides/Locked/Canary.md")["status"], "io_error")

    def reconstruct(self, note, raw, budget):
        digest = hashlib.sha256(raw).hexdigest()
        offset, chunks = 0, []
        for _ in range(len(raw) + 1):
            result = self.read(note, digest.upper(), offset, budget)
            self.assertEqual(result["status"], "ok")
            chunk = result["text"].encode()
            end = offset + len(chunk)
            self.assertEqual(result["digest"], digest)
            self.assertEqual(result["returned_bytes"], len(chunk))
            self.assertEqual(result["total_bytes"], len(raw))
            self.assertEqual(result["total_lines"], len(raw.split(b"\n")) - int(not raw or raw.endswith(b"\n")))
            self.assertEqual(result["range"], {
                "start_byte": offset, "end_byte": end,
                "start_line": 1 + raw[:offset].count(b"\n") if chunk else None,
                "end_line": 1 + raw[:end-1].count(b"\n") if chunk else None,
            })
            self.assertEqual(result["has_more"], end < len(raw))
            self.assertEqual(result["complete_note"], offset == 0 and end == len(raw))
            self.assertEqual(chunk, raw[offset:end])
            self.assertLessEqual(len(chunk), budget)
            self.assertNotEqual(raw[end-1:end+1], b"\r\n")
            chunks.append(chunk)
            if end == len(raw):
                self.assertIsNone(result["next_offset"])
                break
            self.assertGreater(end, offset)
            self.assertEqual(result["next_offset"], end)
            offset = end
        self.assertEqual(b"".join(chunks), raw)

    def test_bom_crlf_unicode_mapping_and_ranges(self):
        for note, data in EDGES.items():
            put(self.root, note, data)
        raw = EDGES["Guides/Page.md"]
        note = "Guides/Page.md"
        digest = hashlib.sha256(raw).hexdigest()
        for query, visible in [("STRASSE", "Straße"), ("É", "e\u0301"), ("İ", "İ")]:
            hit = next(h for h in self.search(query)["hits"] if h["note_id"] == note)
            ex = hit["excerpt"]
            self.assertEqual(ex["line"], 2)
            self.assertGreater(ex["offset"], 160)
            self.assertIn(visible, ex["text"])
            self.assertEqual(ex["text"].encode(), raw[ex["offset"]:][:len(ex["text"].encode())])
            self.assertTrue(self.read(note, digest, ex["offset"])["text"].startswith(ex["text"]))
        for offset in (1, 2, raw.index(b"\n"), raw.index("🎉".encode()) + 1, len(raw) + 1):
            self.assertEqual(self.read(note, digest, offset)["status"], "invalid_range")
        self.assertEqual(self.read(note, digest, len(raw))["range"]["end_line"], None)
        self.assertEqual(self.read(note, None, 3)["status"], "invalid_range")
        self.assertEqual(self.read(note, "0" * 16)["status"], "invalid_digest")
        self.assertEqual(self.read(note, "0" * 64, len(raw) + 1)["status"], "source_changed")
        for size in (24_576, 32_768):
            self.reconstruct(note, raw, size)
        for ref in ("Guides/Empty.md", "Guides/Mixed.MD", "Guides/Escapes.md", "Guides/Inert.md"):
            self.reconstruct(ref, EDGES[ref], 4 if ref != "Guides/Escapes.md" else 32_768)

    def test_changes_between_and_during_reads(self):
        note = "Guides/Restore.md"
        path = self.root / note
        old = hashlib.sha256(BASE[note]).hexdigest()
        for replace in (False, True):
            page = self.read(note, old, size=4)
            if replace:
                put(self.root, "Guides/Temp.md", b"z").replace(path)
            else:
                path.write_bytes(b"changed\n")
            self.assertEqual(self.read(note, old, page["next_offset"])["status"], "source_changed")
            path.write_bytes(BASE[note])

        replacement = put(self.root, "Guides/Temp.md", b"replacement")
        original = self.reader.access._source

        def race(fd, before):
            replacement.replace(path)
            return original(fd, before)

        with patch.object(self.reader.access, "_source", side_effect=race):
            self.assertEqual(self.read(note)["status"], "source_changed")

        replacement = put(self.root, "Guides/Temp.md", b"another replacement")

        def replace_after_read(fd, before):
            source = original(fd, before)
            replacement.replace(path)
            return source

        with patch.object(self.reader.access, "_source", side_effect=replace_after_read):
            self.assertEqual(self.read(note)["status"], "source_changed")
        path.unlink()
        self.assertEqual(self.read(note, old)["status"], "unavailable")

    def test_ancestor_swap_revokes_open_descriptor(self):
        note = "Guides/Archive/Atlas.md"
        archive = self.root / "Guides/Archive"
        original = self.reader.access._source

        def swap(fd, before):
            source = original(fd, before)
            archive.rename(self.root / "Guides/Restricted/Archive")
            archive.symlink_to(self.root / "GuidesExtra")
            return source

        with patch.object(self.reader.access, "_source", side_effect=swap):
            self.assertEqual(self.read(note)["status"], "source_changed")
        self.assertEqual(self.read(note)["status"], "unavailable")

    def test_scan_rejects_replaced_or_respelt_ancestor_after_source_read(self):
        access = self.reader.access
        original = access._source
        for directory, use_symlink in (("ScanSwap", True), ("ScanCase", False)):
            with self.subTest(directory=directory):
                folder = self.root / "Guides" / directory
                filename = directory + ".md"
                note_path = put(self.root, f"Guides/{directory}/{filename}", b"scan-race-marker")
                inode = note_path.stat().st_ino

                def swap(fd, before):
                    source = original(fd, before)
                    if before.st_ino == inode:
                        moved = folder.with_name(directory.lower())
                        folder.rename(moved)
                        if use_symlink:
                            folder.symlink_to(moved, target_is_directory=True)
                    return source

                with patch.object(access, "_source", side_effect=swap):
                    result = self.reader.search("scan-race-marker", 10)
                self.assertEqual(result["coverage"], "partial")
                self.assertEqual(result["skips"]["source_changed"], 1)
                self.assertNotIn(f"Guides/{directory}/{filename}", [hit["note_id"] for hit in result["hits"]])

        with self.subTest(directory="Guides", reuse_root_cursor=True):
            root = self.tmp / "respelled-allow-root"
            setup(root, {"Guides/First.md": b"same marker", "Guides/Second.md": b"same marker"})
            reader = self.make_reader(root)
            original = reader.access._source
            reads = 0

            def respell_after_first_verification(fd, before):
                nonlocal reads
                source = original(fd, before)
                reads += 1
                if reads == 2:
                    (root / "Guides").rename(root / "moving")
                    (root / "moving").rename(root / "guides")
                return source

            with patch.object(reader.access, "_source", side_effect=respell_after_first_verification):
                result = reader.search("same marker", 10)
            self.assertEqual(reads, 2)
            self.assertEqual(result["scanned_notes"], 1)
            self.assertEqual(result["coverage"], "partial")
            self.assertEqual(result["skips"]["source_changed"], 1)

    def test_config_and_root_independence(self):
        second = self.tmp / "another"
        setup(second, {"Guides/Atlas.md": b"different"})
        self.assertEqual(self.make_reader(second).read("Guides/Atlas.md", None, 0, 4)["text"], "diff")
        self.assertEqual(self.read("Guides/Atlas.md")["text"], "draft map\n")
        for bad in ({}, {"root": str(second)}, operator(self.tmp / "missing"),
                    {**operator(second), "allow": ["../Guides"]}, operator(second, hits=True),
                    operator(second, source_bytes=MIB + 1), operator(second, page_bytes=3),
                    operator(second, page_bytes=32_769)):
            with self.assertRaisesRegex(ValueError, "Invalid operator"):
                Config.from_dict(bad)
        config_file = self.tmp / "operator.json"
        config_file.write_text('{"root":"/a","root":"/b","allow":["Guides"]}')
        with self.assertRaises(ValueError):
            load_config(config_file)

    def test_minimum_fill_and_response_trimming(self):
        note = "Guides/Fill.md"
        for newline in (40, 16383, 16384, 16385):
            put(self.root, note, b"a" * (newline - 1) + b"\n" + b"x" * 100000)
            self.assertEqual(self.read(note)["returned_bytes"], 32768 if newline < 16384 else newline)
        limited = self.make_reader(self.root, envelope_bytes=1300)
        # Use a tab as a same-cost stand-in for LF in both JSON representations.
        put(self.root, note, b"x" * 141 + b"\t" + b"x" * (70000 - 142))
        capacity = limited.read(note, None, 0, 32768)["returned_bytes"]
        for delta in (-1, 0, 1):
            newline = (capacity + 1) // 2 + delta
            put(self.root, note, b"x" * (newline - 1) + b"\n" + b"x" * (70000 - newline))
            self.assertEqual(limited.read(note, None, 0, 32768)["returned_bytes"],
                             capacity if delta == -1 else newline)
        for i in range(6):
            put(self.root, f"Guides/Hit{i}.md", b"budget " + b"x" * 314)
        for limit, reason in ((6, "response_budget"), (5, "count_and_response_budget")):
            result = self.make_reader(self.root, envelope_bytes=1700).search("budget", limit)
            self.assertEqual(result["coverage"], "complete")
            self.assertEqual(result["limit_reason"], reason)
            self.assertEqual([h["note_id"] for h in result["hits"]], ["Guides/Hit0.md"])
        tiny = self.make_reader(self.root, envelope_bytes=500)
        self.assertEqual(tiny.search("budget", 6)["status"], "response_limit")
        put(self.root, note, b"")
        self.assertEqual(tiny.read(note, None, 0, 4)["status"], "response_limit")

    def test_scan_budget_and_crowded_ranking(self):
        second = self.tmp / "crowded"
        setup(second, {**{f"Guides/Crowded/Entry{i:03}.md": b"backup inventory entry only\n"
                          for i in range(1, 51)},
                       "Guides/Crowded/Zulu.md": b"restore backup from cold storage: verify checksum before recovery.\n"})
        reader = self.make_reader(second)
        for limit in (20, 50):
            result = reader.search("backup", limit)
            self.assertEqual(result, reader.search("backup", limit))
            self.assertEqual(result["matched_scanned"], 51)
            self.assertEqual(result["coverage"], "complete")
            self.assertEqual(result["limit_reason"], "count")
            self.assertEqual([h["note_id"] for h in result["hits"]],
                             [f"Guides/Crowded/Entry{i:03}.md" for i in range(1, limit + 1)])
        self.assertEqual(reader.search("restore backup", 20)["hits"][0]["note_id"], "Guides/Crowded/Zulu.md")
        with patch("access.time.monotonic", side_effect=[0, 10]):
            result = reader.search("backup", 20)
        self.assertEqual((result["coverage"], result["stop_reason"], result["scanned_notes"]),
                         ("partial", "scan_budget", 0))
