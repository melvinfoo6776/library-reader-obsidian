"""Independent, non-truncating write-open probes against read-only mounts."""

import argparse
import errno
import os
from pathlib import Path


def check(note_id="Guides/Restore.md"):
    assert (os.getuid(), os.getgid()) == (10001, 10001)
    relative_note = Path(note_id)
    assert not relative_note.is_absolute() and ".." not in relative_note.parts
    note = Path("/library") / relative_note
    for name in ("/library", "/etc/lro/operator.json", "/app",
                 "/usr/local/lib/python3.12/site-packages"):
        assert os.statvfs(name).f_flag & os.ST_RDONLY, name
    for name in (note, "/etc/lro/operator.json", "/app/server.py",
                 "/usr/local/lib/python3.12/site-packages/mcp/__init__.py"):
        # No O_TRUNC and no write; an unexpected success leaves bytes untouched.
        path = Path(name)
        assert path.is_file(), name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
        except OSError as error:
            assert error.errno in {errno.EROFS, errno.EACCES, errno.EPERM}, (name, error.errno)
        else:
            os.close(fd)
            raise AssertionError(f"Write access unexpectedly granted: {name}")
    try:
        fd = os.open("/library/.lro-write-probe", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        assert error.errno in {errno.EROFS, errno.EACCES, errno.EPERM}
    else:
        os.close(fd)
        os.unlink("/library/.lro-write-probe")
        raise AssertionError("Library directory creation unexpectedly granted.")
    print("PASS: non-root identity and OS write denial for library, config, code and dependencies")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("note_id", nargs="?", default="Guides/Restore.md",
                        help="existing Markdown path relative to /library")
    check(parser.parse_args().note_id)
