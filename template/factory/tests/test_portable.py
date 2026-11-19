"""Tests for factory/bin/portable.py. They take the platform's own path, so they prove
Windows only when run on Windows (.github/workflows/test.yml of the template does that).

  python3 -m unittest discover -s factory/tests -v
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import portable  # noqa: E402

# Starts a child of its own that outlives it unless the whole group is killed, and says when both are up.
PARENT = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""


class PortableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "x.lock"

    def open(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(lambda: self.close(fd))
        return fd

    @staticmethod
    def close(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    def test_second_holder_is_refused_until_the_first_closes(self):
        first, second = self.open(), self.open()
        portable.lock(first)
        with self.assertRaises(BlockingIOError):
            portable.lock(second, wait=False)
        os.close(first)
        portable.lock(second, wait=False)

    def test_lock_leaves_the_descriptor_at_the_start(self):
        fd = self.open()
        portable.lock(fd)
        os.write(fd, b"holder")
        self.assertEqual(self.path.read_bytes(), b"holder")

    def test_lock_named_reports_the_holder_then_takes_over(self):
        first = portable.lock_named(self.path, "first (pid 1)", self.fail)
        self.addCleanup(self.close, first)
        seen, got = [], []
        thread = threading.Thread(target=lambda: got.append(portable.lock_named(self.path, "second", seen.append)))
        thread.start()
        deadline = time.monotonic() + 10
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(seen, ["first (pid 1)"])
        self.assertEqual(got, [])
        os.close(first)
        thread.join(10)
        self.addCleanup(self.close, got[0])
        self.assertEqual(self.path.read_bytes(), b"second")

    def test_replace_overwrites(self):
        tmp, path = Path(self.tmp.name) / "a.tmp", Path(self.tmp.name) / "a"
        path.write_text("old")
        tmp.write_text("new")
        portable.replace(tmp, path)
        self.assertEqual(path.read_text(), "new")
        self.assertFalse(tmp.exists())

    def test_kill_group_stops_the_process_and_its_children(self):
        proc = portable.popen_group([sys.executable, "-c", PARENT], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.stdout.close)
        child = int(proc.stdout.readline())
        portable.kill_group(proc)
        proc.wait(10)
        deadline = time.monotonic() + 10
        while self.alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.alive(child))

    @staticmethod
    def alive(pid):
        if portable.WINDOWS:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
            return str(pid) in out
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        # A killed child stays a zombie until its parent is reaped; that still answers kill(pid, 0).
        stat = Path(f"/proc/{pid}/stat")
        return not (stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z")

    def test_bash_is_found(self):
        self.assertTrue(Path(portable.bash()).is_file())

    def test_signals_exist_on_this_platform(self):
        self.assertTrue(portable.SIGNALS)


if __name__ == "__main__":
    unittest.main()
