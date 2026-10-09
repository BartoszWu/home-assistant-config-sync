"""Reviewed project definitions; Git selects versions, never installation privileges."""

import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class RecoveryPolicy:
    ha_backup: str = "targeted"
    backend_data: str = "none"

    def __post_init__(self):
        if self.ha_backup not in {"targeted", "ha_configuration", "full_ha"}:
            raise ValueError("Invalid HA recovery policy")
        if self.backend_data not in {"none", "executor"}:
            raise ValueError("Invalid backend data recovery policy")

    def identity(self):
        return asdict(self)


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
    recovery: RecoveryPolicy
    legacy_hacs_repository: str | None = None

    def __post_init__(self):
        if not isinstance(self.recovery, RecoveryPolicy):
            raise TypeError("Recovery policy must be reviewed App code")
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

    def legacy_identity(self):
        value = self.identity()
        del value["recovery"]
        return value

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
    recovery=RecoveryPolicy(backend_data="executor"),
)
HOME_JOURNAL = Project(
    id="home_journal",
    title="Dziennik domu",
    domain="home_journal",
    files=frozenset(
        {"__init__.py", "client.py", "config_flow.py", "manifest.json", "strings.json"}
    ),
    dashboards=("dashboard-home-journal.json",),
    bundles=("www/dashboard/home-journal.mjs",),
    resources=("/local/dashboard/home-journal.mjs",),
    minimum_backend=(0, 2, 0),
    minimum_integration=(0, 2, 0),
    recovery=RecoveryPolicy(backend_data="executor"),
)
PROJECTS = {p.id: p for p in (JDG, HOME_JOURNAL)}
