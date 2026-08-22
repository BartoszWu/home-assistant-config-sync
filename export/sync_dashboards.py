#!/usr/bin/env python3
import hashlib
import json
import shutil
import sys
from pathlib import Path


STATE_RELATIVE = Path("state/dashboard-bases.json")


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_state(repo):
    path = repo / STATE_RELATIVE
    if not path.exists():
        return {"schema": 1, "dashboards": {}}
    value = load_json(path)
    if not isinstance(value, dict):
        raise RuntimeError("Dashboard base state must be a JSON object.")
    dashboards = value.get("dashboards")
    if not isinstance(dashboards, dict):
        dashboards = {}
    return {"schema": 1, "dashboards": dashboards}


def base_hash(entry):
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        value = entry.get("sha256")
        if isinstance(value, str):
            return value
    return None


def sync(repo, current_root):
    current_files = sorted(
        path for path in current_root.glob("*.json")
        if path.name != "index.json"
    )
    if not current_files:
        raise RuntimeError("No current dashboards were exported; refusing migration.")

    destination_root = repo / "dashboards"
    destination_root.mkdir(parents=True, exist_ok=True)
    state = load_state(repo)
    bases = state["dashboards"]

    for current_path in current_files:
        relative = current_path.name
        destination = destination_root / relative
        current = load_json(current_path)
        current_hash = digest(current)
        previous_base = base_hash(bases.get(relative))

        if not destination.exists():
            write_json(destination, current)
            bases[relative] = {"sha256": current_hash}
            print(f"MIGRATED {relative}: GitHub now matches HA current")
            continue

        github = load_json(destination)
        github_hash = digest(github)

        if github_hash == current_hash:
            bases[relative] = {"sha256": current_hash}
            print(f"SAME {relative}")
            continue

        if previous_base is None:
            print(
                f"CONFLICT {relative}: no exported base and GitHub differs from HA; "
                "left untouched"
            )
            continue

        github_changed = github_hash != previous_base
        ha_changed = current_hash != previous_base

        if github_changed and not ha_changed:
            print(f"PENDING GITHUB CHANGE {relative}: left GitHub file untouched")
            continue

        if github_changed and ha_changed:
            print(
                f"CONFLICT {relative}: both GitHub and HA changed from the base; "
                "left untouched"
            )
            continue

        if not github_changed and ha_changed:
            write_json(destination, current)
            bases[relative] = {"sha256": current_hash}
            print(f"HA CHANGE {relative}: exported to GitHub")
            continue

        raise RuntimeError(f"Unexpected sync state for {relative}")

    for legacy in (
        destination_root / "storage",
        destination_root / "desired",
    ):
        if legacy.exists():
            shutil.rmtree(legacy)
            print(f"MIGRATION removed legacy directory dashboards/{legacy.name}/")

    write_json(repo / STATE_RELATIVE, state)

    current_names = {path.name for path in current_files}
    for destination in sorted(destination_root.glob("*.json")):
        if destination.name not in current_names:
            print(
                f"NOTICE {destination.name}: not returned by HA; "
                "left untouched (deletion is never automatic)"
            )


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: sync_dashboards.py REPO CURRENT_DASHBOARDS")
    sync(Path(sys.argv[1]), Path(sys.argv[2]))


if __name__ == "__main__":
    main()
