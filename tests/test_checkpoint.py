"""Prove that a checkpoint can restore data and refuses unsafe overwrites."""

from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import checkpoint


class CheckpointTests(unittest.TestCase):
    def test_backup_and_restore_with_wal_and_local_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app.py").write_text("print('stable')\n")
            (root / ".env").write_text("BOT_TOKEN=synthetic\n")
            db = sqlite3.connect(root / "live.db")
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE records (value TEXT)")
                db.execute("INSERT INTO records VALUES ('before')")
                db.commit()
                def git_output(args, **kwargs):
                    return "abc123\n" if "rev-parse" in args else b"app.py\0.env\0"
                with patch.object(checkpoint, "ROOT", root), patch.object(subprocess, "check_output", side_effect=git_output):
                    saved = checkpoint.create(root / "live.db", root / "checkpoint")
                checkpoint.verify(saved)
                restored = root / "restored"
                checkpoint.restore(saved, restored)
                self.assertEqual((restored / "app.py").read_text(), "print('stable')\n")
                self.assertEqual((restored / ".env").stat().st_mode & 0o777, 0o600)
                with sqlite3.connect(restored / "live.db") as copy:
                    self.assertEqual(copy.execute("SELECT value FROM records").fetchone()[0], "before")
                with self.assertRaisesRegex(ValueError, "must not exist"):
                    checkpoint.restore(saved, restored)
                (saved / "source.tar.gz").write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "failed verification"):
                    checkpoint.restore(saved, root / "unsafe")
                self.assertFalse((root / "unsafe").exists())
            finally:
                db.close()
