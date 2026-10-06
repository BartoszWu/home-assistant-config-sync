"""Reviewed project definitions; Git selects versions, never installation privileges."""

import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    domain: str
    files: frozenset[str]
    dashboards: tuple[str, ...]
    bundles: tuple[str, ...]
    resources: tuple[str, ...]
    minimum_backend: tuple[int, int, int]
    minimum_integration: tuple[int, int, int]
    legacy_hacs_repository: str | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.id) or not re.fullmatch(
            r"[a-z][a-z0-9_]*", self.domain
        ):
            raise ValueError("Invalid project identity")
        if "manifest.json" not in self.files or any(
            PurePosixPath(f).is_absolute()
            or any(p in ("", ".", "..") for p in f.split("/"))
            or not f.endswith((".py", ".json"))
            for f in self.files
        ):
            raise ValueError("Invalid integration file policy")
        if any(
            not re.fullmatch(r"www/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+[.]mjs", f)
            for f in self.bundles
        ) or any(
            not f.startswith("/local/") or ".." in f or "?" in f or "#" in f
            for f in self.resources
        ):
            raise ValueError("Invalid frontend selection")
        if not self.dashboards or any(
            not re.fullmatch(r"dashboard-[a-z0-9_-]+[.]json", f)
            for f in self.dashboards
        ):
            raise ValueError("Invalid dashboard selection")
        if any(
            len(v) != len(set(v))
            for v in (self.dashboards, self.bundles, self.resources)
        ):
            raise ValueError("Duplicate project artifacts")

    def identity(self):
        value = asdict(self)
        value["files"] = sorted(self.files)
        return value


JDG_FILES = frozenset(
    {
        "__init__.py",
        "binary_sensor.py",
        "button.py",
        "client.py",
        "config_flow.py",
        "const.py",
        "entity.py",
        "local.py",
        "manifest.json",
        "review.py",
        "sensor.py",
        "strings.json",
        "translations/en.json",
        "translations/pl.json",
        "websocket.py",
        "version.py",
    }
)

JDG = Project(
    id="jdg",
    title="JDG",
    domain="jdg_ksiegowy",
    files=JDG_FILES,
    dashboards=("dashboard-faktury.json",),
    bundles=("www/dashboard/faktury.mjs",),
    resources=("/local/dashboard/faktury.mjs",),
    minimum_backend=(0, 3, 0),
    minimum_integration=(0, 7, 0),
    legacy_hacs_repository="BartoszWu/ha-jdg-ksiegowy",
)
PROJECTS = {JDG.id: JDG}
