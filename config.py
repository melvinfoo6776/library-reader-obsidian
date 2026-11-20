"""Trusted startup settings. Requests cannot select a scope or a config file."""

from dataclasses import dataclass, fields
import json
from pathlib import Path
import unicodedata

MAX_PAGE_BYTES = 32_768
OPERATOR_PATH = Path("/etc/lro/operator.json")


def components(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid relative reference.")
    parts = tuple(value.split("/"))
    if any(not part or part.startswith(".") for part in parts) or any(
        c in "\\:" or unicodedata.category(c) in {"Cc", "Cs"} for c in value
    ):
        raise ValueError("Invalid relative reference.")
    return parts


def prefixes(prefix, parts):
    return parts[:len(prefix)] == prefix


@dataclass(frozen=True)
class Ceilings:
    source_bytes: int = 1 << 20
    page_bytes: int = MAX_PAGE_BYTES
    envelope_bytes: int = 256 << 10
    query_chars: int = 256
    query_terms: int = 16
    hits: int = 50
    excerpt_bytes: int = 320
    scan_ms: int = 2000


@dataclass(frozen=True)
class Config:
    root: Path
    allow: tuple
    deny: tuple
    ceilings: Ceilings

    @classmethod
    def from_dict(cls, data):
        try:
            if type(data) is not dict or not {"root", "allow"} <= data.keys() or (
                data.keys() - {"root", "allow", "deny", "ceilings"}
            ):
                raise ValueError
            root = Path(data["root"])
            if not root.is_absolute() or root == Path(root.anchor) or ".." in root.parts:
                raise ValueError
            root = root.resolve(strict=True)
            if root == Path(root.anchor) or not root.is_dir():
                raise ValueError
            allow, deny = (data[key] if key in data else [] for key in ("allow", "deny"))
            if type(allow) is not list or not allow or type(deny) is not list:
                raise ValueError
            allow, deny = (tuple(dict.fromkeys(map(components, entries))) for entries in (allow, deny))
            # Scan only minimal allow roots; overlapping prefixes still grant the same scope.
            allow = tuple(p for p in allow if not any(q != p and prefixes(q, p) for q in allow))
            overrides = data.get("ceilings", {})
            if type(overrides) is not dict or overrides.keys() - {f.name for f in fields(Ceilings)}:
                raise ValueError
            ceilings = Ceilings(**overrides)
            for field in fields(Ceilings):
                value = getattr(ceilings, field.name)
                minimum = 4 if field.name in {"page_bytes", "excerpt_bytes"} else 1
                if type(value) is not int or not minimum <= value <= field.default:
                    raise ValueError
            return cls(root, allow, deny, ceilings)
        except (TypeError, ValueError, OSError, RuntimeError):
            raise ValueError("Invalid operator configuration.") from None


def _unique_pairs(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate configuration key.")
    return result


def load_config(path=OPERATOR_PATH):
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("Oversized configuration.")
            return Config.from_dict(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs))
    except (OSError, ValueError, RecursionError):
        raise ValueError("Invalid operator configuration.") from None
