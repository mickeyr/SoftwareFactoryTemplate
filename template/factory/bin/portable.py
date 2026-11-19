"""What differs between Linux, macOS and Windows, in one place for job, gate and store."""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl

# Windows refuses reads of a locked byte through any other handle, and lock files
# start with their holder's name, so the locked byte sits far past it. Locking
# beyond the end of a file is allowed there.
_LOCK_BYTE = 1 << 30
_LOCK_POLL = 0.1
_REPLACE_WAIT = 5

# The signals that end a run. SIGHUP is POSIX only; SIGBREAK is Windows only.
SIGNALS = [getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK") if hasattr(signal, name)]


def utf8_mode():
    """Run the calling script again in UTF-8 mode when it isn't in it.

    Before Python 3.15, Windows reads and writes text in the machine's ANSI code
    page, and the factory's files and its tools' output are UTF-8.
    """
    if WINDOWS and not sys.flags.utf8_mode:
        sys.exit(subprocess.call([sys.executable, "-X", "utf8", *sys.argv]))


def lock(file, wait=True):
    """Take an exclusive lock on an open file or descriptor, held until it is closed or the process ends.

    Raises BlockingIOError when wait is False and another holder has it.
    """
    fd = file if isinstance(file, int) else file.fileno()
    if not WINDOWS:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        return
    try:
        while True:
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if not wait:
                    raise BlockingIOError("the lock is held by another process") from None
                time.sleep(_LOCK_POLL)
    finally:
        os.lseek(fd, 0, os.SEEK_SET)


def lock_named(path, holder, waiting):
    """Take the lock file at path and write holder into it. Returns its descriptor.

    When another holder has it, calls waiting(<that holder's name>) once, then waits.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        lock(fd, wait=False)
    except BlockingIOError:
        os.lseek(fd, 0, os.SEEK_SET)
        waiting(os.read(fd, 200).decode(errors="replace").strip() or "unknown holder")
        lock(fd)
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, holder.encode())
    return fd


def replace(tmp, path):
    """Move tmp over path. Windows refuses while a reader has path open (the dashboard's server, say), so wait for it."""
    deadline = time.monotonic() + _REPLACE_WAIT
    while True:
        try:
            Path(tmp).replace(path)
            return
        except PermissionError:
            if not WINDOWS or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def popen_group(cmd, **kwargs):
    """Start cmd at the head of its own process group, so kill_group can stop everything it starts."""
    if WINDOWS:
        return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, **kwargs)
    return subprocess.Popen(cmd, start_new_session=True, **kwargs)


def kill_group(proc):
    """Kill a popen_group process and every process it started."""
    if WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        os.killpg(proc.pid, signal.SIGKILL)


def bash():
    """The path of bash, which runs factory/checks.sh, or None.

    On Windows a bare `bash` can start WSL's launcher in System32 rather than
    Git for Windows' bash, so Git's own is looked for first, next to git.
    """
    if WINDOWS:
        git = shutil.which("git")
        for up in (1, 2) if git else ():
            candidate = Path(git).resolve().parents[up] / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
    return shutil.which("bash")
