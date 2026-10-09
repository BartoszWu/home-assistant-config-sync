"""Pinned backend deployment and recovery evidence behind one internal interface."""

import re
import time


class Executor:
    def __init__(self, transport, sleep=time.sleep):
        self.transport = transport
        self.sleep = sleep

    def review(self, release, require_data):
        if not require_data:
            return 0
        capabilities = self.transport.request("/capabilities")
        minimum = capabilities.get("minimum_version")
        if (
            type(capabilities.get("recovery_receipt")) is not int
            or capabilities["recovery_receipt"] != 1
            or not isinstance(minimum, str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", minimum)
            or tuple(map(int, release["version"].split(".")))
            < tuple(map(int, minimum.split(".")))
        ):
            raise ValueError(
                "Executor or selected release requires a recovery-contract update"
            )
        return 1

    @staticmethod
    def validate(job_id, release, receipt):
        if not isinstance(receipt, dict) or set(receipt) != {
            "schema_version",
            "job_id",
            "sha",
            "version",
            "kind",
            "verified",
            "sha256",
        }:
            raise ValueError("Missing recovery receipt")
        if (
            type(receipt["schema_version"]) is not int
            or receipt["schema_version"] != 1
            or receipt["verified"] is not True
            or receipt["job_id"] != job_id
            or receipt["sha"] != release["sha"]
            or receipt["version"] != release["version"]
            or receipt["kind"] not in {"sqlite", "state_archive"}
            or not isinstance(receipt["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", receipt["sha256"])
        ):
            raise ValueError("Recovery receipt does not match the approved deployment")
        return dict(receipt)

    def deploy(self, job_id, release, contract):
        self.transport.request(
            "/jobs",
            {"id": job_id, "sha": release["sha"], "version": release["version"]},
        )
        for _ in range(360):
            result = self.transport.request("/jobs/" + job_id)
            if result["status"] == "success":
                if contract == 1:
                    if any(
                        result.get(k) != v
                        for k, v in (
                            ("id", job_id),
                            ("sha", release["sha"]),
                            ("version", release["version"]),
                        )
                    ):
                        raise ValueError(
                            "Executor result belongs to another deployment"
                        )
                    return self.validate(job_id, release, result.get("recovery"))
                return None
            if result["status"] != "running":
                raise ValueError("Aktualizacja backendu nie powiodła się")
            self.sleep(5)
        raise TimeoutError("Aktualizacja backendu nie zakończyła się")
