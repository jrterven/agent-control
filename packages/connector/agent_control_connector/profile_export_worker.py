"""Isolated compatibility adapter for the audited Hermes named-profile exporter.

Hermes 0.21.2 copies Unix sockets when exporting named profiles. Keep its
credential redaction and archive format, but copy only portable profile data.
This file also runs with Hermes's own Python, without connector dependencies.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import stat
import sys
import types

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 20_000
CREDENTIAL_FILES = frozenset({".env", "auth.json", ".anthropic_oauth.json", "credentials.json"})
RUNTIME_FILES = frozenset({
    ("gateway.pid",), ("gateway_state.json",), ("processes.json",),
    (".agent-control", "background", "runtime.json"),
})


def credential_path(parts) -> bool:
    return any(part.casefold() in CREDENTIAL_FILES or part.casefold().startswith(".env.") for part in parts)


def portable_copytree(source, destination, *, symlinks=False, ignore=None):
    """Bounded, no-follow copy; never open sockets or alter source files.

    Runtime markers belong to the source process. Regular files merely named
    *.sock are still user data; only actual sockets are omitted. Unknown special
    files and links fail closed, including replacements during the copy.
    """
    count = size = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def copy_directory(fd, output, parts):
        nonlocal count, size
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise ValueError("Profile directory ownership changed")
        output.mkdir(mode=0o700)
        with os.scandir(fd) as entries:
            names = []
            for entry in entries:
                names.append(entry.name)
                if len(names) + count > MAX_MEMBERS:
                    raise ValueError("Profile has too many files")
        omitted = set(ignore(str(Path(source).joinpath(*parts)), names)) if ignore else set()
        for name in names:
            count += 1
            if count > MAX_MEMBERS:
                raise ValueError("Profile has too many files")
            relative = (*parts, name)
            if name in omitted or credential_path(relative) or relative in RUNTIME_FILES:
                continue
            item = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISSOCK(item.st_mode):
                continue
            if item.st_uid != os.getuid():
                raise ValueError("Profile file ownership changed")
            if stat.S_ISDIR(item.st_mode):
                child = os.open(name, directory_flags, dir_fd=fd)
                try:
                    copy_directory(child, output / name, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(item.st_mode):
                handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(handle, "rb") as incoming:
                    actual = os.fstat(incoming.fileno())
                    if (not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.getuid()
                            or (actual.st_dev, actual.st_ino) != (item.st_dev, item.st_ino)):
                        raise ValueError("Profile file changed during export")
                    if size + actual.st_size > MAX_EXPANDED_BYTES:
                        raise ValueError("Expanded profile archive exceeds limit")
                    with (output / name).open("xb") as outgoing:
                        while chunk := incoming.read(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_EXPANDED_BYTES:
                                raise ValueError("Expanded profile archive exceeds limit")
                            outgoing.write(chunk)
                    (output / name).chmod(stat.S_IMODE(actual.st_mode) & 0o777)
            else:
                raise ValueError("Profile contains a link or unsupported special file")

    fd = os.open(source, directory_flags)
    try:
        copy_directory(fd, Path(destination), ())
    finally:
        os.close(fd)
    return str(destination)


def export(request):
    source = Path(request["source"])
    home = Path(request["home"])
    output = Path(request["output"])
    sys.path.insert(0, str(source))
    from hermes_cli import profiles

    name, directory = profiles._existing_profile_dir(request["name"])
    if name == "default" or directory != home / "profiles" / name or output.exists():
        raise ValueError("Invalid named-profile export boundary")
    # This process performs only an export. The running Hermes service and its
    # on-disk source are untouched; native filtering/redaction remain in use.
    profiles.shutil = types.SimpleNamespace(**{**vars(profiles.shutil), "copytree": portable_copytree})
    result = profiles.export_profile(name, str(output))
    if result != output or not 0 < output.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise ValueError("Invalid exported profile archive")
    output.chmod(0o600)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        export(json.loads(sys.stdin.buffer.read(16385)))
    except Exception:
        # Native exception text can contain paths or secrets. The parent only
        # needs a success/failure result; no source details leave this machine.
        sys.exit(1)
