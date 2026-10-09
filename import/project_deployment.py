"""Durable reviewed project deployment. Ordinary managed-file policy stays unchanged."""

import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request

from project_recovery import RecoveryCheckpoint, atomic_json, digest

VERSION = re.compile(r"\d+\.\d+\.\d+\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
MAX_ARCHIVE = 2 * 1024 * 1024


def release_set(value, project):
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema_version", "project", "backend_version", "integration_version"}
        or value["schema_version"] != 2
        or value["project"] != project.id
    ):
        raise ValueError("Nieprawidłowy zestaw wydań projektu")
    if any(
        not isinstance(value[k], str) or not VERSION.fullmatch(value[k])
        for k in ("backend_version", "integration_version")
    ):
        raise ValueError("Nieprawidłowa wersja projektu")
    if (
        tuple(map(int, value["backend_version"].split("."))) < project.minimum_backend
        or tuple(map(int, value["integration_version"].split(".")))
        < project.minimum_integration
    ):
        raise ValueError("Wydania nie obsługują wspólnego importu projektu")
    return value


def unpack(data, expected_hash, version, project):
    if (
        len(data) > MAX_ARCHIVE
        or not HASH.fullmatch(expected_hash)
        or hashlib.sha256(data).hexdigest() != expected_hash
    ):
        raise ValueError("Nieprawidłowa suma paczki integracji")
    result = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for item in archive.infolist():
            total += item.file_size
            mode = item.external_attr >> 16
            if (
                item.filename not in project.files
                or item.filename in result
                or stat.S_ISLNK(mode)
                or total > MAX_ARCHIVE
            ):
                raise ValueError("Nieprawidłowy plik w paczce integracji")
            result[item.filename] = archive.read(item)
    if set(result) != project.files:
        raise ValueError("Niekompletna paczka integracji")
    manifest = json.loads(result["manifest.json"])
    if (
        manifest.get("domain") != project.domain
        or manifest.get("version") != version
        or manifest.get("requirements") != []
    ):
        raise ValueError("Nieprawidłowy manifest integracji")
    return result


class Installer:
    def __init__(self, ha_root, project):
        self.project = project
        self.parent = Path(ha_root) / "custom_components"
        self.target = self.parent / project.domain
        # HA scans hidden directories too. Recovery manifests must never be
        # discovered as a second integration with the same domain.
        self.recovery = Path(ha_root) / ".config-sync-integrations"
        self.stage = self.recovery / (project.domain + "-stage")
        self.previous = self.recovery / (project.domain + "-previous")

    def snapshot(self, target=None):
        target = self.target if target is None else target
        if (
            self.parent.parent.is_symlink()
            or self.parent.is_symlink()
            or self.recovery.is_symlink()
            or target.is_symlink()
        ):
            raise ValueError("Nieprawidłowa ścieżka integracji")
        if not target.exists():
            return digest({})
        if not target.is_dir():
            raise ValueError("Katalog integracji nie jest katalogiem")
        files = {}
        for path in target.rglob("*"):
            if path.is_symlink():
                raise ValueError("Dowiązanie w integracji")
            if "__pycache__" in path.parts or not path.is_file():
                continue
            relative = path.relative_to(target).as_posix()
            if relative not in self.project.files:
                raise ValueError("Niezarządzany plik w integracji")
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return digest(files)

    def recovery_snapshot(self):
        before = self.snapshot()
        files = {
            path.relative_to(self.target).as_posix(): base64.b64encode(
                path.read_bytes()
            ).decode()
            for path in self.target.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        if self.snapshot() != before:
            raise ValueError("Integration changed during recovery capture")
        return files

    def install(self, files, reviewed_hash):
        expected = digest({k: hashlib.sha256(v).hexdigest() for k, v in files.items()})
        current = self.snapshot()
        if current == expected:
            if self.previous.exists() and self.snapshot(self.previous) != reviewed_hash:
                raise ValueError("Zmieniona kopia poprzedniej integracji")
            return False
        # Recover a crash between the two directory renames, using reviewed hashes.
        if not self.target.exists() and self.previous.exists():
            if (
                self.snapshot(self.previous) != reviewed_hash
                or self.snapshot(self.stage) != expected
            ):
                raise ValueError("Nie można potwierdzić przerwanej instalacji")
            self.stage.rename(self.target)
            return True
        if current != reviewed_hash:
            raise ValueError("Integracja zmieniła się po review")
        self.parent.mkdir(exist_ok=True)
        self.recovery.mkdir(exist_ok=True, mode=0o700)
        for path in (self.stage, self.previous):
            if path.is_symlink():
                raise ValueError("Nieprawidłowy katalog instalatora")
        # Never silently overwrite recovery evidence from an interrupted installation.
        if self.previous.exists():
            raise ValueError("Poprzednia instalacja wymaga sprawdzenia")
        if self.stage.exists():
            # Only an exact, verified stage may be reused after an interruption.
            if self.snapshot(self.stage) != expected:
                raise ValueError("Niekompletna instalacja wymaga sprawdzenia")
        else:
            self.stage.mkdir(mode=0o755)
            for relative, data in files.items():
                path = self.stage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                path.chmod(0o644)
        if self.target.exists():
            self.target.rename(self.previous)
        try:
            self.stage.rename(self.target)
        except BaseException:
            if self.previous.exists():
                self.previous.rename(self.target)
            raise
        return True

    def finish(self):
        if self.previous.exists() and not self.previous.is_symlink():
            self.snapshot(self.previous)
            shutil.rmtree(self.previous)


class Backend:
    def __init__(self, config_path):
        config = json.loads(config_path.read_text())
        if set(config) != {"backend_url", "token_file"}:
            raise ValueError("Nieprawidłowa lokalna konfiguracja wykonawcy")
        self.url = config["backend_url"].rstrip("/")
        from urllib.parse import urlsplit

        parts = urlsplit(self.url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Nieprawidłowy adres wykonawcy")
        token_path = Path(config["token_file"])
        if (
            not token_path.is_absolute()
            or token_path.is_symlink()
            or token_path.parent != config_path.parent
        ):
            raise ValueError("Nieprawidłowa ścieżka klucza deploy")
        self.token = token_path.read_text().strip()
        if len(self.token) < 32:
            raise ValueError("Brak dedykowanego klucza deploy")

    def request(self, path, value=None, *, maximum=65536, timeout=30):
        request = Request(
            self.url + path,
            data=None if value is None else json.dumps(value).encode(),
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        # Reject redirects: a deploy credential must never follow an arbitrary Location.
        from urllib.request import HTTPRedirectHandler, build_opener

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            data = response.read(maximum + 1)
            if len(data) > maximum:
                raise ValueError("Nieprawidłowa odpowiedź wykonawcy")
            return json.loads(data)

    def integration(self, backend_release, integration_version):
        value = self.request(
            "/integration?version=" + backend_release["version"],
            maximum=3 * 1024 * 1024,
        )
        if (
            value.get("sha") != backend_release["sha"]
            or value.get("version") != integration_version
        ):
            raise ValueError(
                "Integration does not belong to the reviewed backend release"
            )
        data = base64.b64decode(value["archive"], validate=True)
        return data, value["sha256"]


class DeploymentBusy(ValueError):
    """Another approved project owns HA mutation privileges."""


class Coordinator:
    """One persistent HA mutation owner across all project workers and ordinary Apply."""

    def __init__(self, directory):
        self.path = Path(directory) / "owner.json"
        self.lock = threading.RLock()

    def owner(self):
        if not self.path.exists():
            return None
        owner = json.loads(self.path.read_text())
        if (
            not isinstance(owner, dict)
            or set(owner) != {"project", "id"}
            or not isinstance(owner["project"], str)
            or not re.fullmatch(r"[a-z][a-z0-9_]*", owner["project"])
            or not isinstance(owner["id"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", owner["id"])
        ):
            raise ValueError("Corrupt HA update owner; inspect recovery state")
        return owner

    def claim(self, project, job_id):
        with self.lock:
            expected = {"project": project, "id": job_id}
            owner = self.owner()
            if owner is not None and owner != expected:
                raise ValueError(
                    "Another project owns the HA update; finish or recover it first"
                )
            atomic_json(self.path, expected)

    def release(self, project, job_id):
        with self.lock:
            if self.owner() != {"project": project, "id": job_id}:
                raise ValueError("HA update ownership changed")
            self.path.unlink()

    @contextmanager
    def operation(self, project=None, job_id=None):
        with self.lock:
            owner = self.owner()
            if owner is not None and owner != {"project": project, "id": job_id}:
                raise DeploymentBusy("HA is reserved by an approved project update")
            yield


class Deployments:
    """One serialized job; stored plans survive browser, Core and Import restarts."""

    def __init__(
        self,
        directory,
        installer,
        backend,
        ha,
        dashboard,
        project,
        coordinator,
        sleep=time.sleep,
    ):
        self.directory = Path(directory) / project.id
        self.project = project
        self.coordinator = coordinator
        self.installer = installer
        self.backend = backend
        self.ha = ha
        self.dashboard = dashboard
        self.sleep = sleep
        self.lock = threading.RLock()
        self.running = False
        self.recovery = RecoveryCheckpoint(self.directory, installer, dashboard)

    def read(self):
        path = self.directory / "job.json"
        job = json.loads(path.read_text()) if path.exists() else None
        if job and (
            job.get("project") != self.project.id
            or (
                "recovery_policy" in job
                and job["recovery_policy"] != self.project.recovery.identity()
            )
            or (
                job.get("profile_hash") != digest(self.project.identity())
                and not (
                    "recovery_policy" not in job
                    and job.get("profile_hash")
                    == digest(self.project.legacy_identity())
                )
            )
        ):
            raise ValueError(
                "Stored plan requires its original reviewed project profile"
            )
        return job

    def save(self, job):
        atomic_json(self.directory / "job.json", job)

    def review(self, releases, source_sha, dashboard_plan):
        with self.lock:
            old = self.read()
            if self.running or (
                old and old["status"] in ("running", "review", "failed")
            ):
                raise ValueError(
                    "Zadanie projektu już istnieje; dokończ je lub anuluj review"
                )
            release_set(releases, self.project)
            if not SHA.fullmatch(source_sha):
                raise ValueError("Nieprawidłowa rewizja dashboardu")
            backend = self.backend.request(
                "/release?version=" + releases["backend_version"]
            )
            if backend.get("version") != releases[
                "backend_version"
            ] or not SHA.fullmatch(backend.get("sha", "")):
                raise ValueError("Nieprawidłowe wydanie backendu")
            active = self.backend.request("/status")
            if not active.get("healthy") or not SHA.fullmatch(active.get("sha", "")):
                raise ValueError("Backend nie jest gotowy do aktualizacji")
            data, checksum = self.backend.integration(
                backend, releases["integration_version"]
            )
            job = {
                "id": uuid.uuid4().hex,
                "project": self.project.id,
                "profile_hash": digest(self.project.identity()),
                "status": "review",
                "stage": "backup",
                "recovery_policy": self.project.recovery.identity(),
                "releases": releases,
                "source_sha": source_sha,
                "dashboard": dashboard_plan,
                "backend": backend,
                "backend_before": active,
                "backend_changed": any(
                    active.get(k) != backend[k] for k in ("sha", "version")
                ),
                "integration_before": self.installer.snapshot(),
                "archive_hash": checksum,
                "restart": False,
            }
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            archive = self.directory / "integration.zip"
            archive.write_bytes(data)
            archive.chmod(0o600)
            files = unpack(
                data, checksum, releases["integration_version"], self.project
            )
            job["restart"] = (
                digest({k: hashlib.sha256(v).hexdigest() for k, v in files.items()})
                != job["integration_before"]
            )
            loaded = self.ha("current_version", job)
            current_version = (
                loaded.get("version")
                if isinstance(loaded, dict) and loaded.get("loaded") is True
                else None
            )
            job["integration_version_before"] = (
                current_version
                if isinstance(current_version, str)
                and VERSION.fullmatch(current_version)
                else None
            )
            job["restart"] = (
                job["restart"]
                or job["integration_version_before"] != releases["integration_version"]
            )
            self.ha("check", job)
            job["review_hash"] = digest(job)
            self.save(job)
            return job

    def approve(self, job_id, review_hash):
        with self.lock:
            job = self.read()
            if not job or job["id"] != job_id or job["review_hash"] != review_hash:
                raise ValueError("Review wygasło")
            if job["status"] == "review":
                if (
                    self.installer.snapshot() != job["integration_before"]
                    or self.backend.request("/status") != job["backend_before"]
                ):
                    raise ValueError("Stan zmienił się po review")
                self.coordinator.claim(self.project.id, job["id"])
                job["status"] = "running"
                self.save(job)
            elif job["status"] != "running":
                return job
            self.coordinator.claim(self.project.id, job["id"])
            self.launch()
            return job

    def launch(self):
        with self.lock:
            if self.running:
                return
            self.running = True
            threading.Thread(target=self.run, daemon=True).start()

    def advance(self, job, stage):
        job["stage"] = stage
        self.save(job)

    def run(self):
        job = None
        try:
            job = self.read()
            if not job or job["status"] != "running":
                return
            self.coordinator.claim(self.project.id, job["id"])
            if "recovery_policy" in job and job["stage"] != "backup":
                self.recovery.prepare(job, self.save)
            if job["stage"] == "backup":
                self.ha("check", job)
                if "recovery_policy" in job:
                    self.recovery.prepare(job, self.save)
                    if job["recovery_policy"]["ha_backup"] != "targeted":
                        self.ha("backup", job)
                else:
                    # An approved legacy plan keeps its original HA backup.
                    self.ha("backup", job)
                self.advance(job, "backend")
            if job["stage"] == "backend":
                if job.get("backend_changed", True):
                    value = {"id": job["id"], **job["backend"]}
                    self.backend.request("/jobs", value)
                    for _ in range(360):
                        result = self.backend.request("/jobs/" + job["id"])
                        if result["status"] == "success":
                            break
                        if result["status"] != "running":
                            raise ValueError("Aktualizacja backendu nie powiodła się")
                        self.sleep(5)
                    else:
                        raise TimeoutError("Aktualizacja backendu nie zakończyła się")
                self.advance(job, "integration")
            if job["stage"] == "integration":
                data = (self.directory / "integration.zip").read_bytes()
                files = unpack(
                    data,
                    job["archive_hash"],
                    job["releases"]["integration_version"],
                    self.project,
                )
                self.ha("takeover", job)
                self.installer.install(files, job["integration_before"])
                self.advance(job, "restart")
            if job["stage"] == "restart":
                # Persist before request; do not issue a second restart after recovery.
                self.advance(job, "wait_ha")
                if job["restart"]:
                    try:
                        self.ha("restart", job)
                    except Exception:  # noqa: BLE001, S110 - Core may close its socket during restart.
                        pass  # Core may close the socket; loaded-version verification decides success.
            if job["stage"] == "wait_ha":
                for _ in range(120):
                    try:
                        self.ha("verify", job)
                        break
                    except Exception:  # noqa: BLE001 - preserve durable recovery without leaking errors.
                        self.sleep(5)
                else:
                    raise TimeoutError("HA nie załadował oczekiwanej integracji")
                self.advance(job, "dashboard")
            if job["stage"] == "dashboard":
                with self.coordinator.operation(self.project.id, job["id"]):
                    self.dashboard.apply(job, self.save)
                self.advance(job, "verify")
            if job["stage"] == "verify":
                self.ha("verify", job)
                active = self.backend.request("/status")
                if (
                    not active.get("healthy")
                    or active.get("sha") != job["backend"]["sha"]
                    or active.get("version") != job["backend"]["version"]
                ):
                    raise ValueError("Nieprawidłowy końcowy stan backendu")
                self.dashboard.finish(job, self.save)
                self.installer.finish()
                job["status"] = "success"
                self.save(job)
                self.coordinator.release(self.project.id, job["id"])
        except Exception:  # noqa: BLE001 - preserve durable recovery without leaking errors.
            if job is None:
                return
            job["status"] = "failed"
            job["message"] = (
                "Import przerwany. Zachowano plan i poprzedni kod; sprawdź wskazany etap przed wznowieniem."
            )
            self.save(job)
        finally:
            self.running = False

    def resume(self, job_id):
        with self.lock:
            job = self.read()
            if self.running and job and job["id"] == job_id:
                return job
            if (
                not job
                or job["id"] != job_id
                or job["status"] not in ("failed", "running")
            ):
                raise ValueError("Nie można wznowić tego zadania")
            self.coordinator.claim(self.project.id, job["id"])
            job["status"] = "running"
            self.save(job)
            self.launch()

    def cancel(self, job_id):
        with self.lock:
            job = self.read()
            if not job or job["id"] != job_id or job["status"] != "review":
                raise ValueError("Można anulować wyłącznie review")
            job["status"] = "cancelled"
            self.save(job)
            if self.coordinator.owner() == {
                "project": self.project.id,
                "id": job["id"],
            }:
                self.coordinator.release(self.project.id, job["id"])
