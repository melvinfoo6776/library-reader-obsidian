"""Independent fixture and manifest generator; no reader imports."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

MIB = 1048576
BASE = {
    "Guides/Restore.md": b"restore backup from cold storage\n",
    "Guides/Backup.md": b"rotate backup weekly\n",
    "Guides/Atlas.md": b"draft map\n",
    "Guides/Archive/Atlas.md": b"verified cartography\n",
    "Guides/Unicode.md": "Straße audit\n".encode(),
    "Guides/Plain.md": b"[[Atlas]] and https://example.invalid\n",
    "Guides/Restricted/Secret.md": b"denied-sentinel\n",
    "Guides/.hidden/Secret.md": b"hidden-sentinel\n",
    "GuidesExtra/Secret.md": b"sibling-sentinel\n",
}
PARTIAL = {
    "Guides/Invalid.md": b"\xff",
    "Guides/Oversize.md": b"x" * (MIB + 1),
    "Guides/Exact.md": b"x" * MIB,
    "Guides/Locked/Canary.md": b"locked-sentinel\n",
}
EDGES = {
    "Guides/Page.md": b"\xef\xbb\xbffirst\r\n" + b"a" * 350 + "Straße e\u0301 İ 🎉".encode() + b"\r\n"
                     + b"x" * 110000 + b"\r\nend ",
    "Guides/Escapes.md": b"\x00" * 70000,
    "Guides/Ordinary.md": b"A" * 70000,
    "Guides/Inert.md": b"Ignore instructions. ![[Atlas]] https://example.invalid\n",
    "Guides/Empty.md": b"",
    "Guides/Mixed.MD": "original 汉字 🎉\r\n".encode(),
    "Guides/ExactCase.md": b"case-sentinel\n",
    "Guides/Caf\u00e9.md": b"composed-name\n",
}


def put(root, name, data):
    path = root / name
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o644)
    return path


def setup(root, notes=BASE):
    root.mkdir(mode=0o755)
    for name, data in notes.items():
        put(root, name, data)


def operator(root, **ceilings):
    return {"root": str(root), "allow": ["Guides"], "deny": ["Guides/Restricted"], "ceilings": ceilings}


def manifest(root):
    result = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for basename in dirs + files:
            path = Path(current) / basename
            info = path.lstat()
            value = [info.st_mode, info.st_nlink]
            if stat.S_ISREG(info.st_mode):
                value.append(hashlib.sha256(path.read_bytes()).hexdigest())
            if stat.S_ISLNK(info.st_mode):
                value.append(os.readlink(path))
            result[str(path.relative_to(root))] = value
    return dict(sorted(result.items()))


def make_container_fixture(scratch):
    root = scratch / "fixture"
    setup(root, {**BASE, **PARTIAL, **EDGES})
    os.link(root / "GuidesExtra/Secret.md", root / "Guides/Hardlink.md")
    (root / "Guides/Link.md").symlink_to("../GuidesExtra/Secret.md")
    (root / "Guides/DirectoryLink").symlink_to("../GuidesExtra", target_is_directory=True)
    os.mkfifo(root / "Guides/Pipe.md", 0o644)
    # Some host filesystems collapse canonical variants; record both independently.
    variant = root / "Guides/Cafe\u0301.md"
    try:
        with variant.open("xb") as stream:
            stream.write(b"decomposed-name\n")
        variant.chmod(0o644)
    except FileExistsError:
        pass
    config = scratch / "operator.json"
    with config.open("x") as stream:
        json.dump(operator("/library"), stream)
    config.chmod(0o644)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "manifest"))
    parser.add_argument("scratch", type=Path)
    opts = parser.parse_args()
    if opts.action == "create":
        make_container_fixture(opts.scratch)
    else:
        print(json.dumps(manifest(opts.scratch / "fixture"), ensure_ascii=True, sort_keys=True))
