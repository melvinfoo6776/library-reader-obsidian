"""The shared exact-path, descriptor-relative read boundary."""

from contextlib import ExitStack, contextmanager
import ctypes
import errno
import os
import stat
import sys
import time

from config import components, prefixes

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
NOTE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
STATX_IDENTITY = 0x0001 | 0x0100 | 0x1000  # TYPE | INO | MNT_ID
STATX_REVISION = STATX_IDENTITY | 0x0002 | 0x0004 | 0x0040 | 0x0080 | 0x0200
AT_SYMLINK_NOFOLLOW = 0x100
AT_EMPTY_PATH = 0x1000


class StatxTimestamp(ctypes.Structure):
    _fields_ = [("seconds", ctypes.c_int64), ("nanoseconds", ctypes.c_uint32),
                ("_reserved", ctypes.c_int32)]

    @property
    def total_ns(self):
        return self.seconds * 1_000_000_000 + self.nanoseconds


class Statx(ctypes.Structure):
    # Linux UAPI struct statx. Unused fields occupy their original slots.
    _fields_ = [
        ("mask", ctypes.c_uint32),
        ("_before_nlink", ctypes.c_byte * 12),
        ("st_nlink", ctypes.c_uint32),
        ("_before_mode", ctypes.c_byte * 8),
        ("st_mode", ctypes.c_uint16),
        ("_mode_pad", ctypes.c_uint16),
        ("st_ino", ctypes.c_uint64),
        ("st_size", ctypes.c_uint64),
        ("_before_times", ctypes.c_byte * 48),
        ("_ctime", StatxTimestamp),
        ("_mtime", StatxTimestamp),
        ("_before_device", ctypes.c_byte * 8),
        ("device_major", ctypes.c_uint32),
        ("device_minor", ctypes.c_uint32),
        ("mount", ctypes.c_uint64),
        ("_tail", ctypes.c_byte * 104),
    ]

    @property
    def st_dev(self):
        return os.makedev(self.device_major, self.device_minor)

    @property
    def st_ctime_ns(self):
        return self._ctime.total_ns

    @property
    def st_mtime_ns(self):
        return self._mtime.total_ns


def linux_statx():
    if sys.platform != "linux":
        return None
    lookup = getattr(ctypes.CDLL(None, use_errno=True), "statx", None)
    if lookup is None:
        return None
    lookup.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Statx))
    lookup.restype = ctypes.c_int

    def stat_at(fd, name, flags, required):
        info = Statx()
        if lookup(fd, name, flags, required, ctypes.byref(info)) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if info.mask & required != required:
            raise OSError(errno.ENOTSUP, "Missing requested statx fields")
        return info

    return stat_at


def scan_directory_identity(stat_at, root, expected_root):
    """Look up directory identity and mount together, without reopening it."""
    if stat_at is None or os.access not in os.supports_effective_ids:
        return None

    def directory_identity(parent, name):
        info = stat_at(parent, os.fsencode(name), AT_SYMLINK_NOFOLLOW, STATX_IDENTITY)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "Ancestor no longer names a directory")
        return (*identity(info), info.mount)

    try:
        # Some Linux emulators expose the access flags but reject their
        # combination. Use the checked descriptor walk on those hosts.
        if (os.access(".", os.R_OK, dir_fd=root, follow_symlinks=False, effective_ids=True)
                and directory_identity(root, ".") == expected_root):
            return directory_identity
    except OSError:
        pass
    # Keep the checked descriptor walk on unsupported/filtered kernels.
    return None


class AccessError(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


class ScanExpired(Exception):
    pass


def identity(info):
    return info.st_dev, info.st_ino


def revision(info):
    return identity(info), info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def expired(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise ScanExpired


def mount_id(fd):
    # st_dev alone cannot distinguish Linux bind mounts of the same filesystem.
    if sys.platform == "linux":
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as data:
            for line in data:
                if line.startswith("mnt_id:"):
                    return int(line.split()[1])
        raise OSError("Missing descriptor mount ID.")
    return None


class Access:
    def __init__(self, config):
        self.config = config
        self.root = os.open(config.root, DIR_FLAGS)
        try:
            root_info = os.fstat(self.root)
            self.device = root_info.st_dev
            self.mount = mount_id(self.root)
            stat_at = linux_statx()
            self._descriptor_statx = None
            if stat_at is not None:
                try:
                    probe = stat_at(self.root, b"", AT_EMPTY_PATH, STATX_REVISION)
                    if probe.mount == self.mount and revision(probe) == revision(root_info):
                        self._descriptor_statx = stat_at
                except OSError:
                    pass
            self._scan_directory_identity = scan_directory_identity(
                stat_at, self.root, (*identity(root_info), self.mount))
            # An allow prefix must name a directory, if it presently exists. Absence
            # and permission failures are handled per operation, including revocation.
            for parts in config.allow:
                if self._denied(parts):
                    continue
                try:
                    with self._directory(parts):
                        pass
                except AccessError as error:
                    if error.status != "io_error":
                        raise ValueError("Invalid allow directory.") from None
        except BaseException:
            self.close()
            raise

    def close(self):
        os.close(self.root)

    def _denied(self, parts):
        return any(prefixes(p, parts) for p in self.config.deny)

    def _permitted(self, parts):
        return not self._denied(parts) and any(len(parts) > len(p) and prefixes(p, parts) for p in self.config.allow)

    def _exact(self, fd, name, deadline, *, reuse_cursor=False):
        # Lookup alone would accept case/normalization aliases on some host shares.
        try:
            with ExitStack() as context:
                if reuse_cursor:
                    # The scan owns this cursor and never shares it with _walk.
                    # Recheck permission and start a new enumeration at offset 0.
                    if not os.access(".", os.R_OK | os.X_OK, dir_fd=fd, effective_ids=True):
                        raise AccessError("io_error")
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                    except OSError as error:
                        if error.errno not in {errno.EINVAL, errno.ESPIPE}:
                            raise
                        reuse_cursor = False
                scan_fd = fd
                if not reuse_cursor:
                    scan_fd = os.open(".", DIR_FLAGS, dir_fd=fd)
                    context.callback(os.close, scan_fd)
                with os.scandir(scan_fd) as entries:
                    for entry in entries:
                        expired(deadline)
                        if entry.name == name:
                            return
        except OSError:
            raise AccessError("io_error") from None
        raise AccessError("unavailable")

    def _opened_metadata(self, fd):
        if self._descriptor_statx is not None:
            try:
                info = self._descriptor_statx(fd, b"", AT_EMPTY_PATH, STATX_REVISION)
                return info, info.mount
            except OSError as error:
                if error.errno not in {errno.ENOTSUP, errno.ENOSYS, errno.EINVAL, errno.EPERM}:
                    raise
        return os.fstat(fd), mount_id(fd)

    @contextmanager
    def _opened(self, parent, name, directory, *, observed_info=None, deadline=None):
        expired(deadline)
        if observed_info is None:
            self._exact(parent, name, deadline)
        try:
            before = observed_info
            if before is None:
                before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            valid = stat.S_ISDIR if directory else stat.S_ISREG
            if (not valid(before.st_mode) or before.st_dev != self.device
                    or (not directory and before.st_nlink != 1)):
                raise AccessError("unavailable")
            fd = os.open(name, DIR_FLAGS if directory else NOTE_FLAGS, dir_fd=parent)
        except OSError as error:
            # An exact entry was observed. Some shares return ENOENT for a
            # listed but unreadable entry; this is not an ordinary missing ID.
            status = "unavailable" if error.errno in {errno.ENOTDIR, errno.ELOOP} else "io_error"
            raise AccessError(status) from None
        try:
            opened, opened_mount = self._opened_metadata(fd)
            if (not valid(opened.st_mode) or opened.st_dev != self.device or opened_mount != self.mount
                    or (not directory and opened.st_nlink != 1)):
                raise AccessError("unavailable")
            if identity(before) != identity(opened):
                raise AccessError("source_changed")
            yield fd, opened
        except OSError:
            raise AccessError("io_error") from None
        finally:
            os.close(fd)

    @contextmanager
    def _directory(self, parts, deadline=None):
        fd = os.dup(self.root)
        try:
            for part in parts:
                with self._opened(fd, part, True, deadline=deadline) as (child, _):
                    next_fd = os.dup(child)
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)

    def _source(self, fd, before):
        cap = self.config.ceilings.source_bytes
        try:
            if before.st_size > cap:
                raise AccessError("too_large")
            remaining = cap + 1
            chunks = []
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
            # Report growth past the cap, but never serve a detectably changed buffer.
            if len(raw) > cap:
                raise AccessError("too_large")
            if revision(before) != revision(after):
                raise AccessError("source_changed")
            try:
                return raw, raw.decode("utf-8")
            except UnicodeDecodeError:
                raise AccessError("invalid_encoding") from None
        except OSError:
            raise AccessError("io_error") from None

    @contextmanager
    def _scan_base(self, parts, deadline):
        if self._scan_directory_identity is None:
            with self._directory(parts, deadline) as fd:
                yield fd, None
            return
        with ExitStack() as context:
            fd = os.open(".", DIR_FLAGS, dir_fd=self.root)
            context.callback(os.close, fd)
            route = []
            for name in parts:
                child, info = context.enter_context(self._opened(fd, name, True, deadline=deadline))
                route.append((fd, name, identity(info)))
                fd = child
            yield fd, tuple(route)

    def _verify_scan_route(self, route, deadline):
        # Reobserve spelling and attachment after every read. Reuse pinned
        # parents, never cached names or timestamps, and reject any mount hop.
        for depth, (parent, name, expected_identity) in enumerate(route, start=1):
            expired(deadline)
            self._exact(parent, name, deadline, reuse_cursor=depth == 1)
            try:
                current = self._scan_directory_identity(parent, name)
            except OSError as error:
                if error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EXDEV}:
                    raise AccessError("source_changed") from None
                raise
            if current != (*expected_identity, self.mount):
                raise AccessError("source_changed")
            # The next edge opens this directory afresh for its exact-name
            # check. Only the last edge needs a separate read-permission check.
            if (depth == len(route)
                    and not os.access(name, os.R_OK, dir_fd=parent, follow_symlinks=False, effective_ids=True)):
                raise AccessError("io_error")

    @contextmanager
    def _current_parent(self, parts, parent, route, deadline):
        if route is not None:
            self._verify_scan_route(route, deadline)
            yield parent
        else:
            with self._directory(parts, deadline) as current:
                if identity(os.fstat(current)) != identity(os.fstat(parent)):
                    raise AccessError("source_changed")
                yield current

    def _read(self, parts, parent, *, observed_info=None, deadline=None, route=None):
        name = parts[-1]
        with self._opened(parent, name, False, observed_info=observed_info, deadline=deadline) as (fd, before):
            source = self._source(fd, before)
            # Reject an ancestor detached or replaced while its descriptor was held.
            try:
                with self._current_parent(parts[:-1], parent, route, deadline) as current:
                    named = os.stat(name, dir_fd=current, follow_symlinks=False)
                    if revision(named) != revision(before):
                        raise AccessError("source_changed")
                    if observed_info is None:
                        self._exact(current, name, deadline)
            except FileNotFoundError:
                raise AccessError("source_changed") from None
            except AccessError as error:
                if error.status == "unavailable":
                    raise AccessError("source_changed") from None
                raise
            return source

    def read(self, note_id):
        try:
            parts = components(note_id)
        except ValueError:
            raise AccessError("unavailable") from None
        if not parts[-1].lower().endswith(".md") or not self._permitted(parts):
            raise AccessError("unavailable")
        with self._directory(parts[:-1]) as parent:
            return self._read(parts, parent)

    def scan(self, deadline):
        """Yield (exact ID, source) or (None, aggregate failure category)."""
        for base in self.config.allow:
            expired(deadline)
            if self._denied(base):
                continue
            try:
                with self._scan_base(base, deadline) as (fd, route):
                    yield from self._walk(fd, base, route, deadline)
            except AccessError as error:
                if error.status != "unavailable":
                    yield None, "io_error"
            except OSError:
                yield None, "io_error"

    def _walk(self, root, base, route, deadline):
        # At most one iterator and one directory descriptor per active depth.
        stack = []

        def push(context, fd, prefix, route):
            try:
                entries = context.enter_context(os.scandir(fd))
            except OSError:
                context.close()
                return False
            stack.append((context, entries, fd, prefix, route))
            return True

        try:
            if not push(ExitStack(), root, base, route):
                yield None, "io_error"
            while stack:
                expired(deadline)
                context, entries, fd, prefix, route = stack[-1]
                try:
                    entry = next(entries, None)
                except OSError:
                    yield None, "io_error"
                    entry = None
                if entry is None:
                    stack.pop()
                    context.close()
                    continue
                try:
                    parts = prefix + components(entry.name)
                except ValueError:
                    continue
                if self._denied(parts):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        with ExitStack() as child:
                            child_fd, child_info = child.enter_context(
                                self._opened(fd, entry.name, True, observed_info=info, deadline=deadline)
                            )
                            child_route = (None if route is None else
                                           (*route, (fd, entry.name, identity(child_info))))
                            if not push(child.pop_all(), child_fd, parts, child_route):
                                yield None, "io_error"
                    elif entry.name.lower().endswith(".md") and stat.S_ISREG(info.st_mode):
                        source = self._read(parts, fd, observed_info=info, deadline=deadline, route=route)
                        yield "/".join(parts), source
                        del source
                except AccessError as error:
                    if error.status != "unavailable":
                        yield None, error.status
                except OSError:
                    yield None, "io_error"
        finally:
            while stack:
                stack.pop()[0].close()
