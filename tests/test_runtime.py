"""Offline process-lock regressions; no bot imports, configuration or database."""

import errno
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tracker_runtime import AlreadyRunning, single_instance


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESS_TIMEOUT = 5
REFUSED_EXIT_CODE = 73
CHILD = """
import sys

sys.path.insert(0, sys.argv[1])
from tracker_runtime import AlreadyRunning, single_instance

try:
    with single_instance(sys.argv[2]):
        print('LOCKED', flush=True)
        if sys.argv[3] == 'hold':
            sys.stdin.read(1)
except AlreadyRunning as exc:
    print(str(exc), file=sys.stderr, flush=True)
    sys.exit(73)
"""


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.lock_path = Path(self.directory.name) / "tracker.lock"

    def command(self, mode):
        # Isolated Python, a temporary cwd and only the runtime import keep the
        # subprocess independent of bot configuration, secrets and live data.
        return [sys.executable, "-I", "-B", "-c", CHILD,
                str(PROJECT_ROOT), str(self.lock_path), mode]

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=PROCESS_TIMEOUT)

    def start_holder(self):
        process = subprocess.Popen(
            self.command("hold"), cwd=self.directory.name,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_process, process)
        ready = queue.Queue()
        reader = threading.Thread(
            target=lambda: ready.put(process.stdout.readline()), daemon=True,
        )
        reader.start()
        try:
            line = ready.get(timeout=PROCESS_TIMEOUT)
        except queue.Empty:
            self.fail("Lock holder did not report readiness within five seconds")
        if line != "LOCKED\n":
            _, stderr = process.communicate(timeout=PROCESS_TIMEOUT)
            self.fail(f"Lock holder failed: {line!r}, {stderr!r}")
        reader.join(timeout=PROCESS_TIMEOUT)
        return process

    def probe(self):
        return subprocess.run(
            self.command("once"), cwd=self.directory.name,
            capture_output=True, text=True, timeout=PROCESS_TIMEOUT,
        )

    def assert_acquired(self):
        result = self.probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "LOCKED\n")

    def assert_refused(self):
        result = self.probe()
        self.assertEqual(result.returncode, REFUSED_EXIT_CODE, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("already running", result.stderr.lower())
        self.assertIn(str(self.lock_path), result.stderr)

    def test_concurrent_process_refused_then_reacquired_after_normal_exit(self):
        holder = self.start_holder()
        original_inode = self.lock_path.stat().st_ino

        self.assert_refused()
        self.assertIsNone(holder.poll(), "The losing process stopped the holder")

        _, stderr = holder.communicate(input="\n", timeout=PROCESS_TIMEOUT)
        self.assertEqual(holder.returncode, 0, stderr)
        self.assertTrue(self.lock_path.exists())
        self.assertEqual(self.lock_path.stat().st_ino, original_inode)
        self.assert_acquired()
        self.assertEqual(self.lock_path.stat().st_ino, original_inode)

    def test_lock_reacquired_after_owner_is_killed_without_cleanup(self):
        holder = self.start_holder()
        original_inode = self.lock_path.stat().st_ino
        self.assert_refused()

        holder.kill()
        holder.communicate(timeout=PROCESS_TIMEOUT)
        self.assertNotEqual(holder.returncode, 0)

        self.assert_acquired()
        self.assertEqual(self.lock_path.stat().st_ino, original_inode)

    def test_body_exceptions_and_interrupts_release_lock(self):
        for error in (ValueError("body failed"), KeyboardInterrupt(), SystemExit(2)):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)) as caught:
                    with single_instance(self.lock_path):
                        raise error
                self.assertIs(caught.exception, error)
                self.assert_acquired()

    def test_failed_nested_acquisition_does_not_release_original_lock(self):
        with single_instance(self.lock_path):
            with self.assertRaises(AlreadyRunning) as caught:
                with single_instance(str(self.lock_path)):
                    self.fail("A second context acquired the same lock")
            self.assertEqual(caught.exception.lock_path, str(self.lock_path))
            self.assert_refused()
        self.assert_acquired()

    def test_existing_lockfile_contents_and_inode_are_preserved(self):
        contents = b"Pre-existing lockfile; existence is not ownership.\n"
        self.lock_path.write_bytes(contents)
        original_inode = self.lock_path.stat().st_ino

        with single_instance(self.lock_path):
            # A Windows range lock also blocks reads of the locked byte.
            self.assertEqual(self.lock_path.stat().st_ino, original_inode)
            self.assert_refused()

        self.assert_acquired()
        self.assertEqual(self.lock_path.read_bytes(), contents)
        self.assertEqual(self.lock_path.stat().st_ino, original_inode)

    def test_different_lock_paths_can_be_held_independently(self):
        other_path = self.lock_path.with_name("other.lock")
        with single_instance(self.lock_path), single_instance(other_path):
            self.assert_refused()
        self.assert_acquired()

    def test_open_failure_is_not_misreported_as_another_instance(self):
        self.lock_path.mkdir()
        with self.assertRaises(OSError) as caught:
            with single_instance(self.lock_path):
                self.fail("A directory was accepted as a lockfile")
        self.assertNotIsInstance(caught.exception, AlreadyRunning)

    def test_missing_parent_is_not_created(self):
        missing_path = self.lock_path / "missing.lock"
        with self.assertRaises(FileNotFoundError):
            with single_instance(missing_path):
                self.fail("A missing parent directory was silently created")
        self.assertFalse(self.lock_path.exists())

    def test_failed_acquisition_closes_descriptor_and_preserves_os_error(self):
        real_open = os.open
        descriptors = []

        def record_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            descriptors.append(descriptor)
            return descriptor

        failure = OSError(errno.EIO, "Synthetic filesystem failure")
        with patch("tracker_runtime.os.open", side_effect=record_open):
            with patch("tracker_runtime._acquire", side_effect=failure):
                with self.assertRaises(OSError) as caught:
                    with single_instance(self.lock_path):
                        self.fail("The context body ran after acquisition failed")
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError) as closed:
            os.fstat(descriptors[0])
        self.assertEqual(closed.exception.errno, errno.EBADF)
        self.assert_acquired()

    def test_failed_unlock_still_closes_descriptor_and_releases_os_lock(self):
        failure = OSError(errno.EIO, "Synthetic unlock failure")
        with patch("tracker_runtime._release", side_effect=failure):
            with self.assertRaises(OSError) as caught:
                with single_instance(self.lock_path):
                    self.assert_refused()
        self.assertIs(caught.exception, failure)
        self.assert_acquired()


if __name__ == "__main__":
    unittest.main()
