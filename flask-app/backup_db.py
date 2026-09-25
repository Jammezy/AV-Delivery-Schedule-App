"""Create a consistent SQLite backup, including committed WAL data.

Usage: python backup_db.py /persistent/schedule.db /backups/schedule-2026-09-22.db
The output must not already exist. Store backups securely outside the app disk.
"""
import argparse
import sqlite3
from pathlib import Path


def backup(source, destination):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Backup destination already exists; choose a new filename.")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    backup(args.source, args.destination)
    print("Backup completed and integrity checked.")
