"""Measure the existing search core inside the disposable benchmark container."""

from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from functools import wraps
import inspect
import json
import sys
import time
from unittest.mock import patch

import access as access_module
from config import load_config
from reader import Reader
from server import result_bytes, result_for


class PhaseTimes:
    def __init__(self):
        self.nanoseconds = Counter()
        self.calls = Counter()

    def wrap(self, phase, function):
        @wraps(function)
        def timed(*args, **kwargs):
            start = time.perf_counter_ns()
            try:
                return function(*args, **kwargs)
            finally:
                self.nanoseconds[phase] += time.perf_counter_ns() - start
                self.calls[phase] += 1
        return timed


class AccessOS:
    """Attribute note opens and observe POSIX metadata calls in access.py."""

    def __init__(self, original, times):
        self.original = original
        self.open_note = times.wrap("file_open", original.open)
        self.stat = times.wrap("stat", original.stat)
        self.fstat = times.wrap("fstat", original.fstat)

    def __getattr__(self, name):
        return getattr(self.original, name)

    def open(self, path, flags, *args, **kwargs):
        function = self.open_note if flags == access_module.NOTE_FLAGS else self.original.open
        return function(path, flags, *args, **kwargs)


def profile(config, query, limit, label):
    access = access_module.Access(config)
    reader = Reader(access, lambda payload: len(result_bytes(result_for(payload))) <= config.ceilings.envelope_bytes)
    times = PhaseTimes()
    original_scan = access.scan
    original_directory = access._directory

    def timed_scan(deadline):
        entries = original_scan(deadline)
        advance = times.wrap("scan_iteration", next)
        end = object()
        try:
            while (entry := advance(entries, end)) is not end:
                yield entry
        finally:
            times.wrap("scan_iteration", entries.close)()

    @contextmanager
    def timed_directory(*args, **kwargs):
        # Measure context entry, which performs the descriptor and ancestor checks.
        original = original_directory(*args, **kwargs)
        with ExitStack() as stack:
            yield times.wrap("directory_enter", stack.enter_context)(original)

    # Observe the three existing statements that normalize the stem and body
    # and run membership tests. Monitor only this code object, not its callees.
    search = Reader.search
    source, first_line = inspect.getsourcelines(search)
    markers = ("stem_folded, body_folded = normalize", "hit = None")
    matching_lines = [[first_line + i for i, line in enumerate(source) if line.lstrip().startswith(marker)]
                      for marker in markers]
    if any(len(lines) != 1 for lines in matching_lines) or matching_lines[1][0] - matching_lines[0][0] != 3:
        raise RuntimeError("Search normalization/matching lines changed. Review profiling markers.")
    (match_start,), (match_end,) = matching_lines
    matching_started = 0

    def measure_match(_code, line):
        nonlocal matching_started
        if line == match_start:
            matching_started = time.perf_counter_ns()
        elif line == match_end:
            times.nanoseconds["normalize_match"] += time.perf_counter_ns() - matching_started
            times.calls["normalize_match"] += 1

    monitoring = sys.monitoring
    tool_id = monitoring.PROFILER_ID
    ancestor_lookup = access._scan_directory_identity
    descriptor_lookup = access._descriptor_statx
    with ExitStack() as cleanup:
        cleanup.callback(access.close)
        monitoring.use_tool_id(tool_id, "synthetic-search-phases")
        cleanup.callback(monitoring.free_tool_id, tool_id)
        monitoring.register_callback(tool_id, monitoring.events.LINE, measure_match)
        cleanup.callback(monitoring.register_callback, tool_id, monitoring.events.LINE, None)
        monitoring.set_local_events(tool_id, search.__code__, monitoring.events.LINE)
        cleanup.callback(monitoring.set_local_events, tool_id, search.__code__, 0)
        cleanup.enter_context(patch.object(access, "scan", timed_scan))
        cleanup.enter_context(patch.object(access, "_directory", timed_directory))
        cleanup.enter_context(patch.object(
            access, "_verify_scan_route", times.wrap("ancestor_verify", access._verify_scan_route)))
        cleanup.enter_context(patch.object(access, "_source", times.wrap("source", access._source)))
        cleanup.enter_context(patch.object(access, "_exact", times.wrap("exact", access._exact)))
        if ancestor_lookup is not None:
            cleanup.enter_context(patch.object(
                access, "_scan_directory_identity", times.wrap("ancestor_statx", ancestor_lookup)))
        if descriptor_lookup is not None:
            cleanup.enter_context(patch.object(
                access, "_descriptor_statx", times.wrap("descriptor_statx", descriptor_lookup)))
        cleanup.enter_context(patch.object(access_module, "mount_id", times.wrap("mount_id", access_module.mount_id)))
        cleanup.enter_context(patch.object(access_module, "os", AccessOS(access_module.os, times)))
        reader_module = sys.modules[search.__module__]
        cleanup.enter_context(patch.object(reader_module, "excerpt", times.wrap("excerpt", reader_module.excerpt)))

        start = time.perf_counter_ns()
        payload = reader.search(query, limit)
        total = time.perf_counter_ns() - start

    ns = times.nanoseconds
    source_io = ns["file_open"] + ns["source"]
    traversal = ns["scan_iteration"] - source_io
    other = total - ns["scan_iteration"] - ns["normalize_match"] - ns["excerpt"]
    print(json.dumps({
        "event": "scan_profile", "run": label, "diagnostic_scan_deadline_ms": config.ceilings.scan_ms,
        "ancestor_revalidation": "statx" if ancestor_lookup is not None else "descriptor_walk",
        "opened_metadata": "statx" if descriptor_lookup is not None else "fstat_fdinfo",
        **{key: payload[key] for key in ("status", "coverage", "scanned_notes", "stop_reason", "skips",
                                         "matched_scanned")},
        "elapsed_ms": total / 1e6,
        "phase_ms": {
            "traversal_and_ancestor_metadata": traversal / 1e6,
            "file_open_read_decode": source_io / 1e6,
            "normalize_and_match": ns["normalize_match"] / 1e6,
            "excerpt": ns["excerpt"] / 1e6,
            "other_core_work_and_instrumentation": other / 1e6,
        },
        "nested_detail_ms": {phase: ns[phase] / 1e6 for phase in
                             ("directory_enter", "ancestor_verify", "ancestor_statx", "descriptor_statx",
                              "exact", "stat", "fstat", "mount_id",
                              "file_open", "source")},
        "calls": dict(times.calls),
    }, sort_keys=True), flush=True)
    return payload


if __name__ == "__main__":
    query, limit, expected_notes = sys.argv[1:]
    config = load_config()
    profile(config, query, int(limit), "configured_deadline")
    # This in-process control captures the tail even when the 2 s scan is partial.
    # The mounted config and the running HTTP server retain their trial limits.
    diagnostic = replace(config, ceilings=replace(config.ceilings, scan_ms=30_000))
    full = profile(diagnostic, query, int(limit), "full_fixture_diagnostic")
    if full["coverage"] != "complete" or full["scanned_notes"] != int(expected_notes):
        sys.exit("Full-fixture diagnostic did not complete.")
