"""Local, verified project checkpoints. Never overwrites a running installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def configured_database() -> Path:
    from dotenv import dotenv_values
    from sqlalchemy.engine import make_url
    settings = dotenv_values(ROOT / ".env")
    url = make_url(os.environ.get("DB_URL") or settings.get("DB_URL") or "sqlite+aiosqlite:///tracker_v3.db")
    if not url.drivername.startswith("sqlite") or url.database in {None, ":memory:"}:
        raise ValueError("Full checkpoint requires a local SQLite database")
    path = Path(url.database)
    return path if path.is_absolute() else ROOT / path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(folder: Path) -> dict:
    manifest = json.loads((folder / "manifest.json").read_text())
    for name, expected in manifest["sha256"].items():
        if Path(name).name != name or digest(folder / name) != expected:
            raise ValueError(f"Checkpoint file failed verification: {name}")
    with sqlite3.connect((folder / "database.db").resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
    return manifest


def create(database: Path, output: Path | None = None) -> Path:
    if not database.is_file():
        raise ValueError(f"Database does not exist: {database}")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    stamp = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d_%H-%M-%S")
    folder = output or ROOT / "backups" / "checkpoints" / f"{stamp}-{commit[:7]}"
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    paths = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT).split(b"\0")
    with tarfile.open(folder / "source.tar.gz", "w:gz") as archive:
        for raw in sorted(set(paths)):
            if not raw:
                continue
            path = Path(os.fsdecode(raw))
            if path.is_absolute() or ".." in path.parts or path.parts[0] in {".git", ".env", "backups"}:
                continue
            source = ROOT / path
            if source.is_file() and not source.is_symlink():
                archive.add(source, arcname=str(path), recursive=False)
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=30) as source:
        with sqlite3.connect(folder / "database.db") as destination:
            source.backup(destination)
    names = ["source.tar.gz", "database.db"]
    if (ROOT / ".env").is_file():
        shutil.copyfile(ROOT / ".env", folder / "config.env")
        names.append("config.env")
    for name in names:
        (folder / name).chmod(0o600)
    try:
        database_path = str(database.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        database_path = "tracker_v4.db"
    manifest = {"commit": commit, "created_moscow": stamp, "database_path": database_path,
                "sha256": {name: digest(folder / name) for name in names}}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    verify(folder)
    return folder


def restore(folder: Path, target: Path) -> None:
    """Restore into a new folder only; caller explicitly stops/replaces the live bot."""
    manifest = verify(folder)
    if target.exists():
        raise ValueError("Restore destination must not exist. Use a new folder.")
    database_path = Path(manifest.get("database_path", "tracker_v4.db"))
    if database_path.is_absolute() or ".." in database_path.parts:
        raise ValueError("Unsafe database path")
    with tarfile.open(folder / "source.tar.gz", "r:gz") as archive:
        members = archive.getmembers()
        if any(Path(m.name).is_absolute() or ".." in Path(m.name).parts or not m.isfile() for m in members):
            raise ValueError("Unsafe archive contents")
        target.mkdir(parents=True, mode=0o700)
        archive.extractall(target, members=members, filter="data")
    destination = target / database_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(folder / "database.db", destination)
    destination.chmod(0o600)
    if "config.env" in manifest["sha256"]:
        config = (folder / "config.env").read_text()
        # Restored settings always point inside the new folder, even if the
        # original deployment used an absolute path outside the project.
        setting = "DB_URL=sqlite+aiosqlite:///" + database_path.as_posix()
        config, replacements = re.subn(r"(?m)^(?:export\s+)?DB_URL\s*=.*$", lambda _: setting, config)
        if not replacements:
            config = config.rstrip() + "\n" + setting + "\n"
        (target / ".env").write_text(config)
        (target / ".env").chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    new = sub.add_parser("create")
    new.add_argument("--database", type=Path)
    new.add_argument("--output", type=Path)
    for action in ("verify", "restore"):
        command = sub.add_parser(action)
        command.add_argument("checkpoint", type=Path)
        if action == "restore":
            command.add_argument("target", type=Path)
    args = parser.parse_args()
    if args.action == "create":
        print(create(args.database or configured_database(), args.output))
    elif args.action == "verify":
        print("Verified:", verify(args.checkpoint)["commit"])
    else:
        restore(args.checkpoint, args.target)
        print("Restored to:", args.target)


if __name__ == "__main__":
    main()
