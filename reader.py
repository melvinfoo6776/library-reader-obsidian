"""Literal retrieval and paging of independently authorized byte buffers."""

from bisect import insort
from collections import Counter
from contextlib import closing
import hashlib
import re
import time
import unicodedata

from access import AccessError, ScanExpired, expired

MESSAGES = {
    "unavailable": "The note is unavailable.",
    "source_changed": "The supplied fingerprint does not match the current source, or a change was "
                      "detected during reading. Search again for a fresh reference.",
    "invalid_digest": "Supply a full SHA-256 fingerprint.",
    "too_large": "The note exceeds the source byte limit.",
    "invalid_encoding": "The note is not valid UTF-8.",
    "invalid_range": "Supply a valid byte boundary and a full fingerprint for nonzero offsets.",
    "io_error": "The operation could not read the source.",
    "response_limit": "The result does not fit the response limit.",
    "busy": "Another library operation is active.",
}


def failure(status):
    return {"status": status, "message": MESSAGES[status]}


def normalize(text):
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())


def safe_boundary(raw, pos):
    return pos == len(raw) or (
        raw[pos] & 0xC0 != 0x80 and not (pos and raw[pos - 1] == 13 and raw[pos] == 10)
    )


def boundary_at_or_before(raw, pos):
    while not safe_boundary(raw, pos):
        pos -= 1
    return pos


def search_terms(query, ceilings):
    if not isinstance(query, str) or len(query) > ceilings.query_chars:
        raise ValueError("Invalid search query.")
    terms = query.split()
    if not 1 <= len(terms) <= ceilings.query_terms or any(
        unicodedata.category(c) == "Cs" for c in query
    ):
        raise ValueError("Invalid search query.")
    return tuple(dict.fromkeys(map(normalize, terms)))


def original_span(line, folded, start, end, deadline):
    """Map through starter/mark chunks; verify separability against whole-line NFC."""
    if line.isascii():
        return start, end
    left = folded_pos = 0
    first = last = None
    for right in range(1, len(line) + 1):
        if right % 4096 == 0:
            expired(deadline)
        if right != len(line) and unicodedata.category(line[right]).startswith("M"):
            continue
        chunk = normalize(line[left:right])
        following = folded_pos + len(chunk)
        if not folded.startswith(chunk, folded_pos):
            return None
        if folded_pos <= start < following:
            first = left
        if folded_pos < end <= following:
            last = right
        folded_pos, left = following, right
    if folded_pos == len(folded) and first is not None and last is not None:
        return first, last
    # E.g. compositions crossing chunk boundaries. The caller omits an excerpt
    # when a long line's visible prefix cannot be confirmed to contain a match.
    return None


def excerpt(raw, terms, cap, deadline):
    start = 0
    number = 1
    while start < len(raw):
        expired(deadline)
        lf = raw.find(b"\n", start)
        end = len(raw) if lf < 0 else lf + 1
        line = raw[start:end].decode("utf-8")
        folded = normalize(line)
        found = min(((pos, -len(term), term) for term in terms
                     if (pos := folded.find(term)) >= 0), default=None)
        if found is not None:
            pos, _, term = found
            if end - start <= cap:
                return {"text": line, "line": number, "offset": start, "truncated": False}
            span = original_span(line, folded, pos, pos + len(term), deadline)
            offset = start
            if span is not None:
                # Keep line-start context when possible; otherwise begin at the match.
                first, last = span
                if len(line[:last].encode("utf-8")) > cap:
                    offset += len(line[:first].encode("utf-8"))
            stop = boundary_at_or_before(raw, min(end, offset + cap))
            snippet = raw[offset:stop].decode("utf-8")
            if span is None and not any(term in normalize(snippet) for term in terms):
                return None
            return {"text": snippet, "line": number, "offset": offset, "truncated": True}
        start = end
        number += 1
    return None


class Reader:
    def __init__(self, access, fits):
        self.access = access
        self.ceilings = access.config.ceilings
        self.fits = fits

    def search(self, query, limit):
        terms = search_terms(query, self.ceilings)
        if type(limit) is not int or not 1 <= limit <= self.ceilings.hits:
            raise ValueError("Invalid result limit.")
        deadline = time.monotonic() + self.ceilings.scan_ms / 1000
        best, skips = [], Counter()
        scanned = matched = 0
        stop = None
        try:
            with closing(self.access.scan(deadline)) as entries:
                for note_id, source in entries:
                    expired(deadline)
                    if note_id is None:
                        skips[source] += 1
                        continue
                    raw, text = source
                    del source
                    stem = note_id.rsplit("/", 1)[-1][:-3]
                    stem_folded, body_folded = normalize(stem), normalize(text)
                    score = sum(term in stem_folded for term in terms)
                    conforms = all(term in stem_folded or term in body_folded for term in terms)
                    hit = None
                    rank = (-score, note_id)
                    if conforms and (len(best) < limit or rank < best[-1][0]):
                        hit = {"note_id": note_id, "title": stem, "digest": hashlib.sha256(raw).hexdigest()}
                        if any(term in body_folded for term in terms):
                            snippet = excerpt(raw, terms, self.ceilings.excerpt_bytes, deadline)
                            if snippet is not None:
                                hit["excerpt"] = snippet
                    expired(deadline)
                    scanned += 1
                    matched += conforms
                    if hit is not None:
                        insort(best, (rank, hit))
                        if len(best) > limit:
                            best.pop()
                    del raw, text, body_folded
        except ScanExpired:
            stop = "scan_budget"
        hits = [hit for _, hit in best]
        count_limited = matched > len(hits)
        trimmed = False
        while True:
            reason = ("count_and_response_budget" if count_limited and trimmed else
                      "count" if count_limited else "response_budget" if trimmed else None)
            result = {"status": "ok", "hits": hits, "coverage": "partial" if stop or skips else "complete",
                      "stop_reason": stop, "scanned_notes": scanned, "skips": dict(skips),
                      "matched_scanned": matched, "limited": reason is not None, "limit_reason": reason}
            if self.fits(result):
                return result
            if len(hits) <= 1:
                return failure("response_limit")
            hits.pop()
            trimmed = True

    def read(self, note_id, expected_digest, offset, max_bytes):
        try:
            raw, _ = self.access.read(note_id)
        except AccessError as error:
            return failure(error.status)
        digest = hashlib.sha256(raw).hexdigest()
        if expected_digest is not None:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_digest):
                return failure("invalid_digest")
            if digest != expected_digest.lower():
                return failure("source_changed")
        total = len(raw)
        if (type(offset) is not int or type(max_bytes) is not int
                or not 4 <= max_bytes <= self.ceilings.page_bytes or not 0 <= offset <= total
                or (offset and expected_digest is None) or not safe_boundary(raw, offset)):
            return failure("invalid_range")
        total_lines = raw.count(b"\n") + bool(raw and not raw.endswith(b"\n"))
        start_line = 1 + raw.count(b"\n", 0, offset)
        upper = min(total, offset + max_bytes)

        def page(end):
            return {
                "status": "ok", "note_id": note_id, "digest": digest,
                "text": raw[offset:end].decode("utf-8"), "returned_bytes": end - offset,
                "range": {"start_byte": offset, "end_byte": end,
                          "start_line": start_line if end > offset else None,
                          "end_line": start_line + raw.count(b"\n", offset, end - 1)
                          if end > offset else None},
                "total_bytes": total, "total_lines": total_lines,
                "next_offset": end if end < total else None, "has_more": end < total,
                "complete_note": offset == 0 and end == total,
            }

        if upper == total:
            result = page(total)
            if self.fits(result):
                return result
            upper -= 1
        # Among progressing non-EOF boundaries, all flags/types stay fixed,
        # integer widths only grow, and JSON escaping appends bytes per character
        # in both representations. EOF may change flags and nullability.
        lo, hi = offset + 1, upper
        end = offset
        while lo <= hi:
            mid = (lo + hi) // 2
            boundary = boundary_at_or_before(raw, mid)
            if boundary == offset:
                lo = mid + 1
            elif self.fits(page(boundary)):
                end = boundary
                lo = mid + 1
            else:
                hi = boundary - 1
        if end == offset:
            return failure("response_limit")
        newline = raw.rfind(b"\n", offset, end) + 1
        if newline > offset and 2 * (newline - offset) >= end - offset:
            result = page(newline)
            if self.fits(result):
                return result
        return page(end)
