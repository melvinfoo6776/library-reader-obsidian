"""Run the synthetic 1,000-note (10 MiB) performance gate on a local Docker Desktop host."""

import argparse
from contextlib import ExitStack
import hashlib
import http.client
import json
from pathlib import Path
import platform
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid


IMAGE = "library-reader-obsidian:test"
ENDPOINT = "http://library-reader:8000/mcp"
PROTOCOL = "2025-11-25"
NOTE_COUNT = 1000
FIXTURE_BYTES = 10_485_760
SCAN_MS = 2000
SEARCH = {"name": "library_search", "arguments": {"query": "benchmarkmarker", "limit": 20}}
LATENCY_TARGET = 2.0
HARDENING = ("--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
             "--user", "10001:10001")


def report(**fields):
    print(json.dumps(fields, sort_keys=True), flush=True)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def request(method, params=None, request_id=None):
    endpoint = urlsplit(ENDPOINT)
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if request_id is not None:
        message["id"] = request_id
    body = json.dumps(message)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": PROTOCOL}
    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=10)
    started = time.perf_counter()
    try:
        connection.request("POST", endpoint.path, body, headers)
        response = connection.getresponse()
        wire = response.read()
        elapsed = time.perf_counter() - started
        return elapsed, response.status, wire
    finally:
        connection.close()


def rpc_result(status, wire, request_id):
    require(status == 200, f"HTTP {status}")
    message = json.loads(wire)
    require(isinstance(message, dict) and message.get("jsonrpc") == "2.0" and message.get("id") == request_id,
            "Mismatched JSON-RPC response")
    require(isinstance(message.get("result"), dict), f"JSON-RPC failure: {message.get('error')}")
    return message["result"]


def benchmark_client():
    for attempt in range(60):
        try:
            _, status, wire = request("initialize", {
                "protocolVersion": PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "lro-performance-benchmark", "version": "1"},
            }, request_id=0)
            break
        except ConnectionRefusedError:
            if attempt == 59:
                raise
            time.sleep(0.25)
    require(rpc_result(status, wire, 0)["protocolVersion"] == PROTOCOL, "Unexpected MCP protocol")
    _, status, _ = request("notifications/initialized")
    require(status == 202, "MCP initialized notification rejected")

    timings = []
    passed = 0
    for run in range(1, 6):
        started = time.perf_counter()
        outcome = {"run": run}
        try:
            elapsed, status, wire = request("tools/call", SEARCH, request_id=run)
            outcome.update(seconds=elapsed, http_status=status, response_bytes=len(wire))
            result = rpc_result(status, wire, run)
            payload = result["structuredContent"]
            require(isinstance(payload, dict), "Invalid structured search result")
            for key in ("status", "coverage", "scanned_notes", "stop_reason", "skips", "matched_scanned"):
                outcome[key] = payload.get(key)
            require(result.get("isError") is False and payload.get("status") == "ok"
                    and payload.get("coverage") == "complete" and payload.get("scanned_notes") == NOTE_COUNT,
                    "Search did not complete successfully over all 1000 notes")
            # The server scan is contained within the measured request interval.
            outcome["scan_headroom_lower_bound_seconds"] = max(0, SCAN_MS / 1000 - elapsed)
            passed += 1
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, RuntimeError) as error:
            if "seconds" not in outcome:
                outcome["seconds"] = time.perf_counter() - started
            outcome["error"] = f"{type(error).__name__}: {error}"
        timings.append(outcome["seconds"])
        report(**outcome)

    median = statistics.median(timings)
    success = passed == 5 and median < LATENCY_TARGET
    report(performance_gate="passed" if success else "failed", complete_runs=passed, total_runs=5,
           min_seconds=min(timings), median_seconds=median, max_seconds=max(timings),
           median_target_seconds=LATENCY_TARGET)
    return 0 if success else 1


def fixture_manifest(library):
    digest = hashlib.sha256()
    count = total = 0
    for path in sorted(library.glob("Guides/*.md")):
        raw = path.read_bytes()
        raw.decode("utf-8", errors="strict")
        count += 1
        total += len(raw)
        entry = [path.relative_to(library).as_posix(), len(raw), hashlib.sha256(raw).hexdigest()]
        digest.update(json.dumps(entry).encode("ascii") + b"\n")
    return count, total, digest.hexdigest()


def generate_fixture(library):
    guides = library / "Guides"
    guides.mkdir(parents=True)
    base, extra = divmod(FIXTURE_BYTES, NOTE_COUNT)
    filler = b"Record of a fictional archive and its ordinary retention schedule.\n"
    for index in range(NOTE_COUNT):
        size = base + (index < extra)
        marker = "benchmarkmarker" if index % 40 == 0 else "archive"
        header = f"# Synthetic archive {index:04d}\n\nCafé inventory checkpoint: {marker}.\n\n".encode("utf-8")
        padding = size - len(header) - 1
        name = "Restore.md" if index == 0 else f"Note-{index:04d}.md"
        path = guides / name
        path.write_bytes(header + (filler * (padding // len(filler) + 1))[:padding] + b"\n")
        path.chmod(0o644)
    guides.chmod(0o755)
    library.chmod(0o755)


def docker(*args, capture=True, timeout=120, **kwargs):
    result = subprocess.run(["docker", *args], check=True, text=True, timeout=timeout,
                            stdout=subprocess.PIPE if capture else None, **kwargs)
    return result.stdout.strip() if capture else None


def image_id():
    return docker("image", "ls", "--no-trunc", "--quiet", IMAGE)


def remove_built_tag(expected_id):
    if image_id() == expected_id:
        docker("image", "rm", IMAGE)


def report_paired_timings(client_output, server_log):
    route = re.search(r"benchmark_ancestor_revalidation=(statx|descriptor_walk)$", server_log, re.MULTILINE)
    opened = re.search(r"benchmark_opened_metadata=(statx|fstat_fdinfo)$", server_log, re.MULTILINE)
    report(event="server_setup", ancestor_revalidation=route[1] if route else "unknown",
           opened_metadata=opened[1] if opened else "unknown")
    timings = {}
    for request_id, elapsed in re.findall(r"benchmark_search request_id=(\d+) core_ns=(\d+)$",
                                          server_log, flags=re.MULTILINE):
        request_id = int(request_id)
        require(request_id not in timings, f"Duplicate core timing for MCP request {request_id}")
        timings[request_id] = int(elapsed) / 1e6
    missing = []
    for line in client_output.splitlines():
        outcome = json.loads(line)
        if "run" in outcome:
            core_ms = timings.get(outcome["run"])
            outcome["client_end_to_end_ms"] = outcome["seconds"] * 1000
            outcome["server_core_ms"] = core_ms
            if outcome.get("http_status") == 200 and outcome.get("status") == "ok":
                if core_ms is None:
                    missing.append(outcome["run"])
                else:
                    outcome["outside_core_ms"] = outcome["client_end_to_end_ms"] - core_ms
        else:
            outcome["paired_core_runs"] = len(timings)
            if len(timings) == outcome["total_runs"]:
                outcome.update(server_core_min_ms=min(timings.values()),
                               server_core_median_ms=statistics.median(timings.values()),
                               server_core_max_ms=max(timings.values()))
        report(**outcome)
    require(not missing, f"Missing server core timings for MCP requests {missing}")


def run_benchmark(file_sharing_backend, profiling=False):
    source = Path(__file__).resolve()
    docker_info = json.loads(docker("info", "--format", "{{json .}}"))
    require(docker_info["OperatingSystem"] == "Docker Desktop", "Use the local Docker Desktop engine")
    with ExitStack() as cleanup:
        selected_image = image_id()
        if not selected_image:
            docker("build", "--tag", IMAGE, str(source.parents[1]), capture=False, timeout=900)
            selected_image = image_id()
            cleanup.callback(remove_built_tag, selected_image)
        require(bool(selected_image), f"Image unavailable: {IMAGE}")

        prefix = f"lro-bench-{uuid.uuid4().hex[:12]}"
        scratch = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix=prefix + "-", dir="/tmp"))).resolve()
        library = scratch / "library"
        generate_fixture(library)
        manifest = fixture_manifest(library)
        require(manifest[:2] == (NOTE_COUNT, FIXTURE_BYTES), "Incorrect fixture count or byte size")
        config = scratch / "operator.json"
        config.write_text(json.dumps({"root": "/library", "allow": ["Guides"], "ceilings": {"scan_ms": SCAN_MS}}),
                          encoding="utf-8")
        config.chmod(0o644)

        network = docker("network", "create", "--internal", prefix)
        cleanup.callback(docker, "network", "rm", network)
        reader = docker(
            "create", "--name", prefix + "-reader", "--network", prefix, "--network-alias", "library-reader",
            *HARDENING, "--memory", "256m", "--cpus", "1", "--pids-limit", "64",
            "--env", f"LRO_BENCHMARK_TIMING={int(not profiling)}",
            "--mount", f"type=bind,source={library},target=/library,readonly",
            "--mount", f"type=bind,source={config},target=/etc/lro/operator.json,readonly", selected_image,
        )
        cleanup.callback(docker, "container", "rm", "--force", reader)
        mounts = json.loads(docker("inspect", "--format", "{{json .Mounts}}", reader))
        require(len(mounts) == 2 and {(m["Type"], m["Destination"], m["RW"]) for m in mounts}
                == {("bind", "/library", False), ("bind", "/etc/lro/operator.json", False)},
                "Missing read-only fixture or operator bind mount")
        docker("start", reader)

        sdk = docker("exec", reader, "python", "-c",
                     "from importlib.metadata import version; print(version('mcp'))")
        mount_driver = docker("exec", reader, "awk", '$5 == "/library" {split($0, fs, " - "); '
                              'split(fs[2], fields, " "); print fields[1]}', "/proc/self/mountinfo")
        report(event="setup", mode="profile" if profiling else "performance_gate",
               endpoint=ENDPOINT, image=IMAGE, image_id=selected_image, mcp_sdk=sdk,
               protocol=PROTOCOL, fixture_notes=manifest[0], fixture_bytes=manifest[1],
               manifest_sha256=manifest[2], scan_budget_ms=SCAN_MS, search=SEARCH,
               mount_filesystem=mount_driver,
               file_sharing_backend=file_sharing_backend, hostname="redacted", host=platform.system(),
               docker_version=docker_info["ServerVersion"], vm_cpus=docker_info["NCPU"],
               vm_memory_bytes=docker_info["MemTotal"], vm_arch=docker_info["Architecture"],
               reader_cpus=1, reader_memory_mib=256,
               cache_state="fresh reader; fixture verified on host; caches not flushed; no warm-up search",
               connection=("direct search core in a separate process inside the reader container" if profiling
                           else "fresh TCP connection per call from an isolated MCP client container"))

        performance_exit = 0
        if profiling:
            arguments = SEARCH["arguments"]
            docker("exec", "-i", reader, "python", "-B", "-", arguments["query"], str(arguments["limit"]),
                   str(NOTE_COUNT), input=source.with_name("profile_scan.py").read_text(encoding="utf-8"),
                   capture=False)
        else:
            client = docker("create", "-i", "--name", prefix + "-client", "--network", prefix,
                            *HARDENING, selected_image, "python", "-B", "-", "--client")
            cleanup.callback(docker, "container", "rm", "--force", client)
            try:
                client_output = docker("start", "-ai", client, input=source.read_text(encoding="utf-8"))
            except subprocess.CalledProcessError as error:
                performance_exit = error.returncode
                client_output = error.stdout
            server_log = docker("logs", reader, stderr=subprocess.STDOUT)
            report_paired_timings(client_output, server_log)
        docker("exec", reader, "python", "-B", "/app/tests/write_denial.py", capture=False)
        require(fixture_manifest(library) == manifest, "Fixture manifest changed during the benchmark")
        report(event="mount_gate", outcome="passed", fixture_manifest_unchanged=True)

    report(event="cleanup", outcome="passed")
    return performance_exit


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-sharing-backend", default="unverified", help="Observed Docker Desktop setting")
    parser.add_argument("--profile", action="store_true", help="Time search core phases on the synthetic bind mount")
    parser.add_argument("--client", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        sys.exit(benchmark_client() if args.client else run_benchmark(args.file_sharing_backend, args.profile))
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        sys.exit(f"Benchmark failed: {error}")
