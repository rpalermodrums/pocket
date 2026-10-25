# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import errno
import functools
import hashlib
import hmac
import json
import math
import os
import platform
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .errors import PocketError

COREAUDIO_PROFILE = "coreaudio-hal-input/v1"
SYNTHETIC_PROFILE = "listen-synthetic/v1"
ADAPTER_PROFILES = {"synthetic": SYNTHETIC_PROFILE, "coreaudio": COREAUDIO_PROFILE}
BUILD_SCHEMA = "pocket.listen-build/v1"
DEVICES_SCHEMA = "pocket.listen-helper-devices/v1"
SOURCE_NAME = "capture.swift"
HELPER_NAME = "listen-capture"
WIRE = "PLH1"
MAGIC = WIRE.encode()
HEADER = struct.Struct("<4sHHII")
BLOCK_HEADER = struct.Struct("<QdQIIHHIQQQQ")
HEARTBEAT = struct.Struct("<QQQQQ")
HELLO, BLOCK, EVENT, HEARTBEAT_TYPE, EXIT, STARTED = 1, 2, 3, 4, 5, 6
HELPER_SCHEMAS = {HELLO: "pocket.listen-helper-hello/v1", EVENT: "pocket.listen-helper-event/v1",
                  EXIT: "pocket.listen-helper-exit/v1", STARTED: "pocket.listen-helper-started/v1"}
ORDERLY_EXIT_CODES = {"refused": 3, "device_event": 4, "device_changed_before_start": 4}
HELLO_REFUSALS = ("device_not_found", "device_not_alive", "device_hogged", "microphone_not_authorized",
                  "sample_rate_mismatch")
START_REFUSALS = (*HELLO_REFUSALS, "format_not_float32")
# Swift 5 language mode: in Swift 6 mode the top-level IOProc constant is main-actor isolated.
SWIFTC_FLAGS = ("-O", "-enforce-exclusivity=unchecked", "-swift-version", "5")
MAX_BLOCK_FRAMES = 8192
HELPER_RING_SECONDS = 2
ADAPTER_QUEUE_FRAMES = 256
SAMPLE_RATES = (44100, 48000, 88200, 96000)
MICROPHONE_STATES = ("authorized", "denied", "restricted", "not_determined", "unknown")
MAX_DEVICES = 64
PROBE_TIMEOUT_S = 10
BUILD_TIMEOUT_S = 600
PROBE_STDOUT_BYTES = 256 * 1024
STDERR_TAIL_BYTES = 2048
REGISTRY_ENV = "POCKET_LISTEN_DIR"
REGISTRY_DEFAULT = "~/.pocket/listen"
REGISTRATION_SCHEMA = "pocket.listen-build-registration/v1"
REGISTRY_KEY_NAME = "registry.key"
REGISTRY_BUILDS_NAME = "builds"
REGISTRY_KEY_BYTES = 32
REGISTRATION_MAX_BYTES = 4096
BUILD_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}")
HELPER_COPY_PREFIX = "pocket-listen-helper-"

_TOOLCHAIN_HINT = "The native helper builds on macOS with the Swift compiler (swiftc); the synthetic adapter needs neither."
_MACOS_HINT = "The native helper needs macOS 15 or later. The synthetic adapter needs neither."
_COMPILE_HINT = ("swiftc ran but didn't build the helper; the error ends with its messages. Run "
                 "listen_build_helper again; a failed build leaves nothing behind.")
_TIMEOUT_HINT = ("The compile didn't finish within its time limit, which can happen on a busy Mac. Run "
                 "listen_build_helper again; a failed build leaves nothing behind.")
_BUILD_HINT = ("Build the helper with listen_build_helper and use the build_id its receipt names, then prepare "
               "a new session. Listen runs only builds it compiled into its own folder, so a build copied, "
               "moved or changed since is refused.")
_REGISTRY_HINT = ("Listen keeps the helper builds it compiles in ~/.pocket/listen, or in POCKET_LISTEN_DIR when "
                  "that is set. Make that folder and everything in it private to you (chmod -R go-rwx, and "
                  "chmod -R -N to remove any access list), on a volume that enforces ownership, then run "
                  "listen_build_helper again.")
_COPY_HINT = ("Listen runs the helper from a fresh copy in a new folder in /private/tmp that only you can "
              "open. /private/tmp must be root's with the sticky bit set and have room for the copy; check "
              "it with ls -ld /private/tmp, then try again.")
_ACL_TYPE_EXTENDED = 0x00000100         # sys/acl.h
_ACL_FIRST_ENTRY, _ACL_NEXT_ENTRY = 0, -1
_ACL_EXTENDED_DENY = 2


def helper_source():
    return Path(__file__).parent / "devices" / "listen" / SOURCE_NAME


def macos_major():
    try:
        return int(platform.mac_ver()[0].split(".")[0] or 0)
    except ValueError:
        return 0


SWIFTC_SHIM = "/usr/bin/swiftc"
XCODE_SELECT_LINK = Path("/var/db/xcode_select_link")
DEFAULT_DEVELOPER_DIRS = (Path("/Applications/Xcode.app/Contents/Developer"),
                          Path("/Library/Developer/CommandLineTools"))
_TOOLCHAIN_SWIFTC = ("usr/bin/swiftc", "Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc")


def swift_compiler():
    found = shutil.which("swiftc")
    if found != SWIFTC_SHIM:
        return found
    selected = os.environ.get("DEVELOPER_DIR")
    developers = ((Path(selected),) if selected else (XCODE_SELECT_LINK,) if XCODE_SELECT_LINK.is_symlink()
                  else DEFAULT_DEVELOPER_DIRS)
    real = any((developer / tool).is_file() for developer in developers for tool in _TOOLCHAIN_SWIFTC)
    return found if real else None


def helper_ring_frames(sample_rate, seconds=HELPER_RING_SECONDS):
    frames = 1
    while frames < seconds * sample_rate:
        frames <<= 1
    return frames


def encode_frame(kind, payload, serial):
    if isinstance(payload, dict):
        payload = json.dumps(payload, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    return HEADER.pack(MAGIC, kind, 0, len(payload), serial) + payload


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seal(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    with Path(path).open("xb") as stream:
        stream.write(data)
    Path(str(path) + ".sha256").write_text(hashlib.sha256(data).hexdigest() + "\n")
    return hashlib.sha256(data).hexdigest()


def _seal_build(output, swiftc_version):
    record = {"schema": BUILD_SCHEMA, "profile": COREAUDIO_PROFILE,
              "files": {name: file_sha256(output / name) for name in (SOURCE_NAME, HELPER_NAME)},
              "swiftc_version": swiftc_version, "swift_language_mode": "5", "swiftc_flags": list(SWIFTC_FLAGS),
              "wire": WIRE, "native_acceptance": "separate_from_build"}
    return record, _seal(output / "build.json", record)


def registry_dir(environ=None):
    base = os.environ if environ is None else environ
    return Path(base.get(REGISTRY_ENV) or REGISTRY_DEFAULT).expanduser().resolve()


# Access lists. On macOS an extended ACL can let others in whatever the mode bits say, and a new file
# or directory takes inheritable entries from its parent whatever mode it is created with. So the
# checks on Listen's own folder read the ACL as well, and that folder starts with none when Listen
# creates it. Linux needs neither: a POSIX ACL's mask shows in the group bits, so 0700 leaves its
# named entries no access.

@functools.cache
def _acl_api():
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.acl_get_link_np.restype = libc.acl_get_fd_np.restype = libc.acl_init.restype = ctypes.c_void_p
    libc.acl_get_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    libc.acl_get_tag_type.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    libc.acl_set_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    libc.acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    libc.acl_init.argtypes = [ctypes.c_int]
    libc.acl_free.argtypes = [ctypes.c_void_p]
    return ctypes, libc


_HOST_IS_MACOS = sys.platform == "darwin"


def acl_grants_access(where):
    if not _HOST_IS_MACOS:
        return False
    ctypes, libc = _acl_api()
    ctypes.set_errno(0)
    acl = (libc.acl_get_fd_np(where, _ACL_TYPE_EXTENDED) if isinstance(where, int)
           else libc.acl_get_link_np(os.fsencode(where), _ACL_TYPE_EXTENDED))
    if not acl:
        return ctypes.get_errno() != errno.ENOENT       # ENOENT: the file has no extended ACL
    try:
        entry, tag = ctypes.c_void_p(), ctypes.c_int()
        which = _ACL_FIRST_ENTRY
        while libc.acl_get_entry(acl, which, ctypes.byref(entry)) == 0:
            if libc.acl_get_tag_type(entry, ctypes.byref(tag)) != 0 or tag.value != _ACL_EXTENDED_DENY:
                return True
            which = _ACL_NEXT_ENTRY
        return False
    finally:
        libc.acl_free(acl)


def clear_acl(path):
    if not _HOST_IS_MACOS:
        return
    ctypes, libc = _acl_api()
    empty = libc.acl_init(0)
    if not empty:
        raise OSError(ctypes.get_errno(), "Can't build an empty access list")
    try:
        result = (libc.acl_set_fd_np(path, empty, _ACL_TYPE_EXTENDED) if isinstance(path, int)
                  else libc.acl_set_link_np(os.fsencode(path), _ACL_TYPE_EXTENDED, empty))
        if result != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(path))
    finally:
        libc.acl_free(empty)


# Volumes that ignore ownership. macOS can mount a volume with "Ignore ownership on this volume" (mount
# shows noowners; it is common on external drives). There stat reports whoever asks as the owner of every
# file, and the kernel grants that caller owner access, so a 0700 directory passes the owner and mode
# checks for you and is still open to every other account. The privacy checks refuse such a volume.

_MNT_IGNORE_OWNERSHIP = 0x00200000      # sys/mount.h (MNT_UNKNOWNPERMISSIONS)


@functools.cache
def _statfs_api():
    import ctypes
    import ctypes.util

    class StatFS(ctypes.Structure):     # struct statfs with 64-bit inodes, <sys/mount.h>
        _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32), ("f_blocks", ctypes.c_uint64),
                    ("f_bfree", ctypes.c_uint64), ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                    ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2), ("f_owner", ctypes.c_uint32),
                    ("f_type", ctypes.c_uint32), ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                    ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
                    ("f_mntfromname", ctypes.c_char * 1024), ("f_flags_ext", ctypes.c_uint32),
                    ("f_reserved", ctypes.c_uint32 * 7)]

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

    def symbol(name):
        try:
            return libc[name + "$INODE64"]     # x86_64; arm64 has only the 64-bit-inode name
        except AttributeError:
            return libc[name]
    statfs, fstatfs = symbol("statfs"), symbol("fstatfs")
    statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(StatFS)]
    fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(StatFS)]
    return ctypes, StatFS, statfs, fstatfs


def _volume_flags(where):
    ctypes, StatFS, statfs, fstatfs = _statfs_api()
    info = StatFS()
    result = (fstatfs(where, ctypes.byref(info)) if isinstance(where, int)
              else statfs(os.fsencode(where), ctypes.byref(info)))
    return None if result != 0 else info.f_flags


def volume_ignores_ownership(where):
    if not _HOST_IS_MACOS:
        return False
    flags = _volume_flags(where)
    return flags is None or bool(flags & _MNT_IGNORE_OWNERSHIP)


def is_private(info, shape, where):
    return (shape(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
            and not acl_grants_access(where) and not volume_ignores_ownership(where))


def _registry_error(message, code="evidence_mismatch"):
    return PocketError(message, code=code, hint=_REGISTRY_HINT)


def _registry_folder(create):
    folder = registry_dir()
    if create:
        try:
            folder.mkdir(parents=True, mode=0o700)
            clear_acl(folder)
        except FileExistsError:
            pass
        except OSError as error:
            raise _registry_error(f"Can't create Listen's build registry {folder}: {error}",
                                  "io_error") from error
    try:
        info = os.lstat(folder)
    except FileNotFoundError:
        return None
    if not is_private(info, stat.S_ISDIR, folder):
        raise _registry_error(f"Listen's build registry {folder} isn't a directory private to you",
                              "io_error" if create else "evidence_mismatch")
    return folder


def _read_key(folder, code="evidence_mismatch"):
    try:
        descriptor = os.open(folder / REGISTRY_KEY_NAME, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _registry_error(f"Can't read Listen's build registry key in {folder}", code) from error
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        private = is_private(info, stat.S_ISREG, stream.fileno())
        key = stream.read(REGISTRY_KEY_BYTES + 1)
    if not private or len(key) != REGISTRY_KEY_BYTES:
        raise _registry_error(f"Listen's build registry key in {folder} isn't private to you or has changed",
                              code)
    return key


def _create_key(folder):
    key = _read_key(folder, "io_error")
    if key is not None:
        return key
    temporary = folder / f".{REGISTRY_KEY_NAME}.{secrets.token_hex(6)}"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(secrets.token_bytes(REGISTRY_KEY_BYTES))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, folder / REGISTRY_KEY_NAME)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return _read_key(folder, "io_error")


def _builds_folder(folder, create):
    builds = folder / REGISTRY_BUILDS_NAME
    if create:
        builds.mkdir(mode=0o700, exist_ok=True)
    try:
        info = os.lstat(builds)
    except FileNotFoundError:
        return None
    if not is_private(info, stat.S_ISDIR, builds):
        raise _registry_error(f"Listen's build folder {builds} isn't a directory private to you",
                              "io_error" if create else "evidence_mismatch")
    return builds


def _registration_path(builds, build_id):
    return builds / f"{build_id}.json"


def _registration_mac(key, registration):
    body = {name: value for name, value in registration.items() if name != "mac"}
    message = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _new_build():
    folder = _registry_folder(create=True)
    try:
        _create_key(folder)
        builds = _builds_folder(folder, create=True)
        build_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)
        path = builds / build_id
        path.mkdir(mode=0o700)
    except OSError as error:
        raise _registry_error(f"Can't create a helper build in Listen's build registry {folder}: {error}",
                              "io_error") from error
    return build_id, path


def _register(builds, build_id, seal, files):
    folder = builds.parent
    try:
        key = _create_key(folder)
        body = {"schema": REGISTRATION_SCHEMA, "build_id": build_id, "build_json_sha256": seal,
                "helper_sha256": files[HELPER_NAME], "source_sha256": files[SOURCE_NAME]}
        registration = {**body, "mac": _registration_mac(key, body)}
        path = _registration_path(builds, build_id)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write((json.dumps(registration, sort_keys=True, indent=2) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise _registry_error(f"Can't record the helper build in Listen's build registry {folder}: {error}",
                              "io_error") from error


def _registration(builds, build_id, key, unknown):
    try:
        descriptor = os.open(_registration_path(builds, build_id), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as error:
        raise _mismatch(unknown) from error
    except OSError as error:
        raise _mismatch(f"Can't read Listen's record of the helper build {build_id}") from error
    with os.fdopen(descriptor, "rb") as stream:
        private = is_private(os.fstat(stream.fileno()), stat.S_ISREG, stream.fileno())
        data = stream.read(REGISTRATION_MAX_BYTES + 1)
    if not private:
        raise _mismatch(f"Listen's record of the helper build {build_id} isn't private to you")
    try:
        registration = json.loads(data)
    except ValueError:
        registration = None
    if (len(data) > REGISTRATION_MAX_BYTES or not isinstance(registration, dict)
            or registration.get("schema") != REGISTRATION_SCHEMA or registration.get("build_id") != build_id
            or not isinstance(registration.get("mac"), str)
            or not hmac.compare_digest(registration["mac"], _registration_mac(key, registration))):
        raise _mismatch(f"Listen's record of the helper build {build_id} doesn't verify")
    return registration


def _seal_and_register(build_id, output, swiftc_version, expected_source_sha256=None):
    record, seal = _seal_build(output, swiftc_version)
    if expected_source_sha256 is not None and record["files"][SOURCE_NAME] != expected_source_sha256:
        raise _mismatch(f"The helper source in {output} changed while it was compiled, so Listen didn't record "
                        "the build")
    _register(output.parent, build_id, seal, record["files"])
    return record, seal


def build_helper():
    if sys.platform != "darwin":
        raise PocketError("The Listen helper builds only on macOS", code="unsupported_profile", hint=_TOOLCHAIN_HINT)
    if macos_major() < 15:
        raise PocketError("The Listen helper needs macOS 15 or later", code="unsupported_profile", hint=_MACOS_HINT)
    if not swift_compiler():
        raise PocketError("The Swift compiler (swiftc) isn't installed", code="unsupported_profile",
                          hint=_TOOLCHAIN_HINT)
    build_id, output = _new_build()
    try:
        source = output / SOURCE_NAME
        packaged = file_sha256(helper_source())
        shutil.copyfile(helper_source(), source)
        try:
            with tempfile.TemporaryDirectory(prefix="listen-swift-", dir=output) as cache:
                result = subprocess.run(["swiftc", *SWIFTC_FLAGS, "-module-cache-path", cache, str(source),
                                         "-o", str(output / HELPER_NAME)], check=False, capture_output=True,
                                        text=True, timeout=BUILD_TIMEOUT_S)
            if result.returncode or not (output / HELPER_NAME).is_file():
                raise PocketError("Listen helper build failed: " + (result.stderr or "")[-2000:],
                                  code="unsupported_profile", hint=_COMPILE_HINT)
            version = subprocess.run(["swiftc", "--version"], check=False, capture_output=True, text=True,
                                     timeout=30)
        except subprocess.TimeoutExpired as error:
            raise PocketError(f"Listen helper build timed out: {error}"[:2000], code="unsupported_profile",
                              hint=_TIMEOUT_HINT) from error
        except OSError as error:
            raise PocketError(f"Listen helper build failed: {error}"[:2000], code="unsupported_profile",
                              hint=_COMPILE_HINT) from error
        first_line = ((version.stdout or version.stderr or "").strip().splitlines() or ["unknown"])[0][:300]
        try:
            record, seal = _seal_and_register(build_id, output, first_line, packaged)
        except OSError as error:
            raise PocketError(f"Can't seal the helper build in {output}: {error}", code="io_error",
                              hint=_REGISTRY_HINT) from error
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return {"build_id": build_id, "build_dir": str(output), "build": record, "build_json_sha256": seal,
            "compiler_messages": (result.stderr or "")[-2000:] or None}


def _mismatch(message):
    return PocketError(message, code="evidence_mismatch", hint=_BUILD_HINT)


def _read_regular(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        if volume_ignores_ownership(stream.fileno()):
            raise OSError(errno.EPERM, "on a volume that ignores ownership", str(path))
        return stream.read()


def verify_build(build_id):
    if not isinstance(build_id, str) or not BUILD_ID.fullmatch(build_id):
        raise _mismatch("build_id must be one listen_build_helper returned")
    unknown = f"Listen has no helper build {build_id}; listen_build_helper didn't record one for you"
    folder = _registry_folder(create=False)
    builds = None if folder is None else _builds_folder(folder, create=False)
    key = None if builds is None else _read_key(folder)
    if key is None:
        raise _mismatch(unknown)
    registration = _registration(builds, build_id, key, unknown)
    root = builds / build_id
    try:
        info = os.lstat(root)
    except OSError as error:
        raise _mismatch(f"The helper build folder {root} is missing") from error
    if not is_private(info, stat.S_ISDIR, root):
        raise _mismatch(f"The helper build folder {root} isn't a directory private to you")
    try:
        data = _read_regular(root / "build.json")
        seal = _read_regular(root / "build.json.sha256").decode().strip()
    except (OSError, ValueError) as error:
        raise _mismatch(f"Can't read the sealed Listen helper build in {root}") from error
    if hashlib.sha256(data).hexdigest() != seal or seal != registration["build_json_sha256"]:
        raise _mismatch("The Listen helper build record changed since listen_build_helper recorded it")
    try:
        record = json.loads(data)
    except ValueError as error:
        raise _mismatch("The Listen helper build record isn't a Listen build") from error
    files = record.get("files") if isinstance(record, dict) else None
    if (not isinstance(files, dict) or record.get("schema") != BUILD_SCHEMA
            or record.get("profile") != COREAUDIO_PROFILE or record.get("wire") != WIRE
            or set(files) != {SOURCE_NAME, HELPER_NAME} or files[HELPER_NAME] != registration["helper_sha256"]
            or files[SOURCE_NAME] != registration["source_sha256"]):
        raise _mismatch("The Listen helper build record changed or isn't a Listen build")
    for name, sha in files.items():
        try:
            unchanged = hashlib.sha256(_read_regular(root / name)).hexdigest() == sha
        except OSError as error:
            raise _mismatch(f"The sealed helper file {name} is missing, isn't a regular file or is on a "
                            "volume that ignores ownership") from error
        if not unchanged:
            raise _mismatch(f"The sealed helper file {name} changed")
    return {"build_id": build_id, "build_dir": str(root), "build_json_sha256": seal,
            "helper_sha256": files[HELPER_NAME], "source_sha256": files[SOURCE_NAME],
            "swiftc_version": record.get("swiftc_version")}


def _copy_parent():
    return Path("/private/tmp" if sys.platform == "darwin" else tempfile.gettempdir())


def copy_helper(build):
    parent = _copy_parent()
    try:
        info = os.lstat(parent)
    except OSError:
        info = None
    if info is None or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or not info.st_mode & stat.S_ISVTX:
        raise PocketError(f"{parent} isn't a folder of root's with the sticky bit set, so Listen didn't copy "
                          "the helper there", code="io_error", hint=_COPY_HINT)
    changed = _mismatch("The helper changed after its build was verified, so Listen didn't run it")
    try:
        data = _read_regular(Path(build["build_dir"]) / HELPER_NAME)
    except OSError as error:
        raise changed from error
    try:
        folder = Path(tempfile.mkdtemp(prefix=HELPER_COPY_PREFIX, dir=parent))
    except OSError as error:
        raise PocketError(f"Can't create a folder for the helper copy in {parent}: {error}", code="io_error",
                          hint=_COPY_HINT) from error
    path = folder / HELPER_NAME
    try:
        os.chmod(folder, 0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o700)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(descriptor, 0o700)
            stream.write(data)
        copied = hashlib.sha256(_read_regular(path)).hexdigest()
    except OSError as error:
        shutil.rmtree(folder, ignore_errors=True)
        raise PocketError(f"Can't write the helper copy in {folder}: {error}", code="io_error",
                          hint=_COPY_HINT) from error
    if copied != build["helper_sha256"]:
        shutil.rmtree(folder, ignore_errors=True)
        raise changed
    return path


def remove_helper_copy(path):
    shutil.rmtree(Path(path).parent, ignore_errors=True)


def _run_bounded(argv, cwd, timeout_s):
    out, err = bytearray(), bytearray()

    def read_out(stream):
        out.extend(stream.read(PROBE_STDOUT_BYTES + 1))
        while stream.read(65536):
            pass

    def read_err(stream):
        for chunk in iter(lambda: stream.read(4096), b""):
            err.extend(chunk)
            del err[:-STDERR_TAIL_BYTES]

    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               close_fds=True, cwd=cwd)
    readers = [threading.Thread(target=read_out, args=(process.stdout,), daemon=True),
               threading.Thread(target=read_err, args=(process.stderr,), daemon=True)]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait(timeout=5)
    for reader in readers:
        reader.join(timeout=2)
    process.stdout.close()
    process.stderr.close()
    return {"exit_code": process.returncode, "timed_out": timed_out, "stdout": bytes(out),
            "stderr_tail": bytes(err[-STDERR_TAIL_BYTES:]).decode("utf-8", "replace") or None}


def probe_devices(build_id, *, timeout_s=PROBE_TIMEOUT_S):
    build = verify_build(build_id)
    helper = copy_helper(build)
    try:
        result = _run_bounded([str(helper), "list-devices"], helper.parent, timeout_s)
    finally:
        remove_helper_copy(helper)
    reason = ("timeout" if result["timed_out"] else "nonzero_exit" if result["exit_code"]
              else "output_too_large" if len(result["stdout"]) > PROBE_STDOUT_BYTES else None)
    inventory = None
    if reason is None:
        try:
            inventory = validate_device_inventory(json.loads(result["stdout"], parse_constant=_reject_constant))
        except (ValueError, UnicodeError):
            reason = "invalid_output"
    if reason is not None:
        return {"status": "failed", "build": build,
                "probe": {"reason": reason, "exit_code": result["exit_code"], "stderr_tail": result["stderr_tail"]}}
    return {"status": "ok", "build": build, **inventory}


class InventoryError(ValueError):
    pass


def _reject_constant(name):
    raise InventoryError(f"non-finite JSON constant {name}")


def is_float32(virtual, rate):
    return (isinstance(virtual, dict) and virtual.get("format_id") == "lpcm" and virtual.get("float") is True
            and virtual.get("bits_per_channel") == 32 and virtual.get("packed") is True
            and virtual.get("native_endian") is True and _number(virtual.get("sample_rate"))
            and virtual["sample_rate"] == rate)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _integer(value, low=None):
    return type(value) is int and (low is None or value >= low)


def _text(value, limit=256):
    return value[:limit] if isinstance(value, str) else None


def _stream_row(stream):
    if not isinstance(stream, dict):
        raise InventoryError("input stream must be an object")
    start, channels, virtual = stream.get("starting_channel"), stream.get("channels"), stream.get("virtual_format")
    if not (start is None or _integer(start, 0)) or not _integer(channels, 0) \
            or not (virtual is None or isinstance(virtual, dict)):
        raise InventoryError("input stream fields have the wrong types")
    keys = ("format_id", "float", "bits_per_channel", "channels_per_frame", "interleaved", "packed",
            "native_endian", "sample_rate")
    return {"starting_channel": start, "channels": channels,
            "virtual_format": None if virtual is None else {k: virtual.get(k) for k in keys}}


def has_four_byte_samples(virtual):
    return (isinstance(virtual, dict) and virtual.get("format_id") == "lpcm"
            and virtual.get("bits_per_channel") == 32)


def _float32_channels(row):
    rate = row["nominal_sample_rate"]
    if not all(has_four_byte_samples(s["virtual_format"]) for s in row["input_streams"]):
        return []
    return [c for s in row["input_streams"] if s["starting_channel"] is not None
            and is_float32(s["virtual_format"], rate)
            for c in range(s["starting_channel"], s["starting_channel"] + s["channels"])]


def _profile_fit(row):
    rate = row["nominal_sample_rate"]
    if row["input_channels"] < 1:
        return "unsupported:no_input_channels"
    if not row["alive"]:
        return "unsupported:not_alive"
    if row["hog_mode_pid"] is None:
        return "unsupported:hog_mode_unreadable"
    if row["hog_mode_pid"] != -1:
        return "unsupported:hogged_by_other_process"
    if rate not in SAMPLE_RATES:
        return "unsupported:rate_not_in_profile"
    if not row["input_streams"] or not all(is_float32(s["virtual_format"], rate) for s in row["input_streams"]):
        return "partial:not_float32_streams" if _float32_channels(row) else "unsupported:not_float32"
    return "supported"


def _device_row(device):
    if not isinstance(device, dict) or not isinstance(device.get("uid"), str) or not device["uid"]:
        raise InventoryError("device rows need a uid")
    names = device.get("channel_names", [])
    streams = device.get("input_streams", [])
    if (not isinstance(device.get("name"), str) or type(device.get("alive")) is not bool
            or not (device.get("hog_mode_pid") is None or _integer(device["hog_mode_pid"]))
            or not _number(device.get("nominal_sample_rate"))
            or not _integer(device.get("input_channels"), 0) or not _integer(device.get("audio_object_id"), 0)
            or not isinstance(streams, list) or len(streams) > 64 or not isinstance(names, list)
            or not all(isinstance(n, str) for n in names)
            or not (device.get("buffer_frame_size") is None or _integer(device["buffer_frame_size"], 0))):
        raise InventoryError("device row fields have the wrong types")
    rate = device["nominal_sample_rate"]
    row = {"uid": device["uid"][:256], "name": device["name"][:256], "manufacturer": _text(device.get("manufacturer")),
           "model_uid": _text(device.get("model_uid")), "transport": _text(device.get("transport"), 32) or "other",
           "audio_object_id": device["audio_object_id"], "alive": device["alive"],
           "hog_mode_pid": device["hog_mode_pid"], "nominal_sample_rate": int(rate) if rate == int(rate) else rate,
           "input_channels": device["input_channels"], "input_streams": [_stream_row(s) for s in streams],
           "channel_names": [n[:128] for n in names[:64]], "buffer_frame_size": device.get("buffer_frame_size")}
    return {**row, "profile_fit": _profile_fit(row), "supported_channels": _float32_channels(row)}


def validate_device_inventory(document):
    if not isinstance(document, dict) or document.get("schema") != DEVICES_SCHEMA:
        raise InventoryError("not a Listen device inventory")
    authorization = document.get("microphone_authorization")
    devices = document.get("devices")
    if authorization not in MICROPHONE_STATES or not isinstance(devices, list) or len(devices) > MAX_DEVICES:
        raise InventoryError("inventory fields have the wrong types")
    rows = [_device_row(d) for d in devices]
    names = [row["name"] for row in rows]
    rows = [{**row, "display_name_shared": names.count(row["name"]) > 1,
             "name_shared_with": names.count(row["name"]) - 1} for row in rows]
    omitted, unreadable = document.get("devices_omitted", 0), document.get("devices_unreadable")
    return {"microphone_authorization": authorization, "macos_version": _text(document.get("macos_version"), 32),
            "devices": rows, "devices_omitted": omitted if _integer(omitted, 0) else 0,
            "devices_unreadable": unreadable if _integer(unreadable, 0) else None}
