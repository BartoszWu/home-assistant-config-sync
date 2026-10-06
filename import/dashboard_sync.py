"""Pinned dashboard-set preview, Apply and verification behind one interface."""


class DashboardSync:
    def __init__(self, access):
        self.access = access

    @staticmethod
    def key(item):
        return item["kind"] + ":" + item["relative"]

    def preview(self, project, sha=None):
        revision, releases, items = self.access.review(project, sha)
        expected = {}
        for item in items:
            changed = item["preview_ha_hash"] != item["preview_desired_hash"]
            if item.get("warnings") or (changed and not item["selectable"]):
                raise ValueError("Conflict or warning; review in ordinary Import")
            key = self.key(item)
            if key in expected:
                raise ValueError("Duplicate dashboard-set artifact")
            expected[key] = {
                "kind": item["kind"],
                "relative": item["relative"],
                "before": item["preview_ha_hash"],
                "after": item["preview_desired_hash"],
            }
        return revision.commit_sha, releases, {"expected": expected}

    def verify(self, job, after=True):
        if not after:
            _, _, current = self.access.review(job["project"], job["source_sha"])
            items = {self.key(i): i for i in current}
            if set(items) != set(job["dashboard"]["expected"]):
                raise ValueError("Dashboard selection changed")
            for key, expected in job["dashboard"]["expected"].items():
                item = items[key]
                if (
                    item["preview_desired_hash"] != expected["after"]
                    or item["preview_ha_hash"] != expected["before"]
                ):
                    raise ValueError("Dashboard changed after review")
            return
        for expected in job["dashboard"]["expected"].values():
            kind, relative = expected["kind"], expected["relative"]
            if self.access.read(kind, relative) != expected["after"]:
                raise ValueError("Dashboard read-back failed")
            if (
                kind in ("dashboard", "managed")
                and expected["before"] != expected["after"]
            ):
                entry = self.access.provenance(kind, relative)
                if (
                    not entry
                    or not entry.canonical
                    or entry.commit_sha != job["source_sha"]
                    or entry.content_hash != expected["after"]
                ):
                    raise ValueError("Dashboard provenance verification failed")

    def apply(self, job, save):
        _, _, current = self.access.review(job["project"], job["source_sha"])
        items = {self.key(i): i for i in current}
        expected = job["dashboard"]["expected"]
        if set(items) != set(expected):
            raise ValueError("Dashboard selection changed")
        form = {"source": ["main"], "reviewed_sha": [job["source_sha"]]}
        for key, item in items.items():
            target = expected[key]
            if item["preview_desired_hash"] != target["after"]:
                raise ValueError("Desired dashboard changed")
            if item["preview_ha_hash"] == target["after"]:
                continue
            if (
                item["preview_ha_hash"] != target["before"]
                or item.get("warnings")
                or not item["selectable"]
            ):
                raise ValueError("Dashboard changed or became unsafe")
            prefix = "" if item["kind"] == "dashboard" else item["kind"] + "_"
            for field, value in (
                ("selected", item["relative"]),
                ("preview_hash", item["relative"] + ":" + target["before"]),
                ("desired_hash", item["relative"] + ":" + target["after"]),
            ):
                form.setdefault(prefix + field, []).append(value)
        if any(k.endswith("selected") for k in form):
            results = self.access.apply(form, owner=(job["project"], job["id"]))
            if not isinstance(results, list) or any(not r.get("ok") for r in results):
                raise ValueError("Dashboard Apply failed")
        self.verify(job)

    def finish(self, job, save):
        self.verify(job)
        expected = job["dashboard"]["expected"]
        changed = [
            e["relative"]
            for e in expected.values()
            if e["kind"] == "dashboard" and e["before"] != e["after"]
        ]
        if changed and not job.get("export_requested"):
            self.access.export(changed)
            job["export_requested"] = True
            save(job)
