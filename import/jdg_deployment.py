"""One durable, reviewed JDG deployment. Ordinary managed-file policy stays unchanged."""

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
from pathlib import Path
from urllib.request import Request, urlopen

DOMAIN = "jdg_ksiegowy"
VERSION = re.compile(r"\d+\.\d+\.\d+\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
FILES = frozenset(
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
MAX_ARCHIVE = 2 * 1024 * 1024


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


def release_set(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "backend_version", "integration_version"}
        or value["schema_version"] != 1
    ):
        raise ValueError("Nieprawidłowy zestaw wydań JDG")
    if any(
        not isinstance(value[k], str) or not VERSION.fullmatch(value[k])
        for k in ("backend_version", "integration_version")
    ):
        raise ValueError("Nieprawidłowa wersja JDG")
    if tuple(map(int, value["backend_version"].split("."))) < (0, 2, 0) or tuple(
        map(int, value["integration_version"].split("."))
    ) < (0, 7, 0):
        raise ValueError("Wydania nie obsługują wspólnego importu JDG")
    return value


def unpack(data, expected_hash, version):
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
                item.filename not in FILES
                or item.filename in result
                or stat.S_ISLNK(mode)
                or total > MAX_ARCHIVE
            ):
                raise ValueError("Nieprawidłowy plik w paczce integracji")
            result[item.filename] = archive.read(item)
    if set(result) != FILES:
        raise ValueError("Niekompletna paczka integracji")
    manifest = json.loads(result["manifest.json"])
    if (
        manifest.get("domain") != DOMAIN
        or manifest.get("version") != version
        or manifest.get("requirements") != []
    ):
        raise ValueError("Nieprawidłowy manifest integracji")
    return result


class Installer:
    def __init__(self, ha_root):
        self.parent = Path(ha_root) / "custom_components"
        self.target = self.parent / DOMAIN
        self.stage = self.parent / ".jdg_ksiegowy-stage"
        self.previous = self.parent / ".jdg_ksiegowy-previous"

    def snapshot(self, target=None):
        target = self.target if target is None else target
        if (
            self.parent.parent.is_symlink()
            or self.parent.is_symlink()
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
            if relative not in FILES:
                raise ValueError("Niezarządzany plik w integracji")
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return digest(files)

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
    def __init__(self, config_path=Path("/review/jdg.json")):
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

    def request(self, path, value=None):
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

        with build_opener(NoRedirect).open(request, timeout=30) as response:
            data = response.read(65537)
            if len(data) > 65536:
                raise ValueError("Nieprawidłowa odpowiedź wykonawcy")
            return json.loads(data)


def download_integration(version):
    if not VERSION.fullmatch(version):
        raise ValueError("Nieprawidłowa wersja integracji")
    base = f"https://github.com/BartoszWu/ha-jdg-ksiegowy/releases/download/v{version}/"
    with urlopen(base + "jdg_ksiegowy.sha256", timeout=30) as response:
        checksum = response.read(66).decode().strip()
    with urlopen(base + "jdg_ksiegowy.zip", timeout=30) as response:
        data = response.read(MAX_ARCHIVE + 1)
    unpack(data, checksum, version)
    return data, checksum


class Deployments:
    """One serialized job; stored plans survive browser, Core and Import restarts."""

    def __init__(
        self,
        directory,
        installer,
        backend,
        ha,
        dashboard,
        downloader=download_integration,
        sleep=time.sleep,
    ):
        self.directory = Path(directory)
        self.installer = installer
        self.backend = backend
        self.ha = ha
        self.dashboard = dashboard
        self.downloader = downloader
        self.sleep = sleep
        self.lock = threading.RLock()
        self.running = False

    def read(self):
        path = self.directory / "job.json"
        return json.loads(path.read_text()) if path.exists() else None

    def save(self, job):
        atomic_json(self.directory / "job.json", job)

    def review(self, releases, source_sha, dashboard_plan):
        with self.lock:
            old = self.read()
            if self.running or (
                old and old["status"] in ("running", "review", "failed")
            ):
                raise ValueError(
                    "Zadanie JDG już istnieje; dokończ je lub anuluj review"
                )
            release_set(releases)
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
            data, checksum = self.downloader(releases["integration_version"])
            job = {
                "id": uuid.uuid4().hex,
                "status": "review",
                "stage": "backup",
                "releases": releases,
                "source_sha": source_sha,
                "dashboard": dashboard_plan,
                "backend": backend,
                "backend_before": active,
                "integration_before": self.installer.snapshot(),
                "archive_hash": checksum,
                "restart": False,
            }
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            archive = self.directory / "integration.zip"
            archive.write_bytes(data)
            archive.chmod(0o600)
            files = unpack(data, checksum, releases["integration_version"])
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
                job["status"] = "running"
                self.save(job)
            elif job["status"] != "running":
                return job
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
        job = self.read()
        try:
            if job["status"] != "running":
                return
            if job["stage"] == "backup":
                self.ha("check", job)
                self.ha("backup", job)
                self.advance(job, "backend")
            if job["stage"] == "backend":
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
                    data, job["archive_hash"], job["releases"]["integration_version"]
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
                    except Exception:
                        pass  # Core may close the socket; loaded-version verification decides success.
            if job["stage"] == "wait_ha":
                for _ in range(120):
                    try:
                        self.ha("verify", job)
                        break
                    except Exception:
                        self.sleep(5)
                else:
                    raise TimeoutError("HA nie załadował oczekiwanej integracji")
                self.advance(job, "dashboard")
            if job["stage"] == "dashboard":
                self.dashboard(job)
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
                self.installer.finish()
                job["status"] = "success"
                self.save(job)
        except Exception:
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
