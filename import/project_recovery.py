"""Private recovery checkpoint shared by every reviewed application profile."""

import base64
import hashlib
import json
import os
import re
from pathlib import Path

from dashboard_logic import digest as content_digest


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def snapshot_hash(snapshot):
    if snapshot["encoding"] == "base64":
        data = snapshot["data"]
        return (
            None
            if data is None
            else hashlib.sha256(base64.b64decode(data, validate=True)).hexdigest()
        )
    if snapshot["encoding"] == "json":
        return content_digest(snapshot["data"])
    raise ValueError("Unknown recovery snapshot format")


class RecoveryCheckpoint:
    def __init__(self, directory, installer, dashboard):
        self.directory = Path(directory) / "recovery"
        self.installer = installer
        self.dashboard = dashboard

    def prepare(self, job, save):
        if not re.fullmatch(r"[a-f0-9]{32}", job["id"]):
            raise ValueError("Invalid recovery identity")
        path = self.directory / (job["id"] + ".json")
        if path.is_symlink() or self.directory.is_symlink():
            raise ValueError("Invalid recovery path")
        if job["stage"] != "backup" and (
            not path.exists() or not job.get("recovery_checkpoint")
        ):
            raise ValueError("Missing recovery checkpoint for an approved stage")
        if path.exists():
            checkpoint = json.loads(path.read_text())
        else:
            if job.get("recovery_checkpoint"):
                raise ValueError("Missing recovery checkpoint")
            checkpoint = {
                "schema_version": 1,
                "job_id": job["id"],
                "profile_hash": job["profile_hash"],
                "source_sha": job["source_sha"],
                "integration": self.installer.recovery_snapshot(),
                "dashboard": self.dashboard.snapshot(job),
            }
        self.verify(checkpoint, job)
        checksum = digest(checkpoint)
        if job.get("recovery_checkpoint") not in (None, checksum):
            raise ValueError("Recovery checkpoint changed")
        if not path.exists():
            atomic_json(path, checkpoint)
        job["recovery_checkpoint"] = checksum
        save(job)

    @staticmethod
    def verify(checkpoint, job):
        if any(
            checkpoint.get(k) != value
            for k, value in (
                ("schema_version", 1),
                ("job_id", job["id"]),
                ("profile_hash", job["profile_hash"]),
                ("source_sha", job["source_sha"]),
            )
        ):
            raise ValueError("Recovery checkpoint belongs to another plan")
        integration = checkpoint["integration"]
        actual = digest(
            {
                name: hashlib.sha256(base64.b64decode(data, validate=True)).hexdigest()
                for name, data in integration.items()
            }
        )
        if actual != job["integration_before"]:
            raise ValueError("Integration changed before recovery capture")
        expected = {
            key: item
            for key, item in job["dashboard"].get("expected", {}).items()
            if item["before"] != item["after"]
        }
        if set(checkpoint["dashboard"]) != set(expected):
            raise ValueError("Incomplete dashboard recovery checkpoint")
        for key, item in expected.items():
            if snapshot_hash(checkpoint["dashboard"][key]) != item["before"]:
                raise ValueError("Dashboard changed before recovery capture")
