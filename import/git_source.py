"""Immutable Git source selection for Import reviews.

Branch/ref input is never interpolated into a shell string. Git is invoked with
an argv list. Branch names must match a conservative pattern and appear in
``git ls-remote --heads``. Explicit commit SHAs must be 40-character hex.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SOURCE_REF = "main"
SOURCE_KIND_BRANCH = "branch"
SOURCE_KIND_COMMIT = "commit"

FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Conservative Git branch names: no option injection, no refspec tricks.
BRANCH_NAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?$"
)
FORBIDDEN_REF_MARKERS = (
    "..",
    "@{",
    "\\",
    " ",
    "\t",
    "\n",
    "\r",
    ":",
    "?",
    "*",
    "[",
    "~",
    "^",
    ";",
    "|",
    "&",
    "`",
    "$",
    "(",
    ")",
    "'",
    '"',
    "<",
    ">",
    "\x00",
)
RESERVED_NAMES = frozenset({
    "HEAD",
    "FETCH_HEAD",
    "ORIG_HEAD",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "COMMIT_EDITMSG",
})


class InvalidSourceRef(ValueError):
    """User-supplied Git ref is not an allowed branch name or commit SHA."""


@dataclass(frozen=True)
class SourceRevision:
    source_ref: str
    source_kind: str
    commit_sha: str
    short_sha: str
    available_branches: tuple[str, ...]
    branch_tip_sha: str | None = None
    stale: bool = False
    reviewed_sha: str | None = None

    @classmethod
    def fallback(
        cls,
        source_ref: str | None = None,
        available_branches: tuple[str, ...] = (),
    ) -> "SourceRevision":
        ref = source_ref or DEFAULT_SOURCE_REF
        branches = available_branches or (DEFAULT_SOURCE_REF,)
        return cls(
            source_ref=ref,
            source_kind=SOURCE_KIND_COMMIT if FULL_SHA_RE.fullmatch(ref) else SOURCE_KIND_BRANCH,
            commit_sha="",
            short_sha="-",
            available_branches=branches,
            branch_tip_sha=None,
            stale=False,
        )


def short_sha(commit_sha: str) -> str:
    if FULL_SHA_RE.fullmatch(commit_sha):
        return commit_sha[:7]
    return "-"


def validate_branch_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise InvalidSourceRef("Empty branch name.")
    if name.startswith("-") or name.startswith("/") or name.startswith("."):
        raise InvalidSourceRef("Invalid branch name.")
    if name.endswith("/") or name.endswith(".") or name.endswith(".lock"):
        raise InvalidSourceRef("Invalid branch name.")
    if name.startswith("refs/") or "//" in name:
        raise InvalidSourceRef("Invalid branch name.")
    if any(marker in name for marker in FORBIDDEN_REF_MARKERS):
        raise InvalidSourceRef("Invalid branch name.")
    if name in RESERVED_NAMES:
        raise InvalidSourceRef("Invalid branch name.")
    if not BRANCH_NAME_RE.fullmatch(name):
        raise InvalidSourceRef("Invalid branch name.")
    return name


def validate_commit_sha(value: str) -> str:
    if not isinstance(value, str) or not FULL_SHA_RE.fullmatch(value):
        raise InvalidSourceRef("Commit SHA must be a 40-character lowercase hex digest.")
    return value


def parse_requested_source(source: str | None, source_sha: str | None) -> tuple[str, str]:
    """Resolve UI fields to (ref, kind). Empty input defaults to main."""
    sha = (source_sha or "").strip()
    branch = (source or "").strip()
    if sha:
        return validate_commit_sha(sha), SOURCE_KIND_COMMIT
    if not branch:
        return DEFAULT_SOURCE_REF, SOURCE_KIND_BRANCH
    return validate_branch_name(branch), SOURCE_KIND_BRANCH


def _parse_ls_remote_heads(output: str) -> dict[str, str]:
    heads: dict[str, str] = {}
    for line in output.splitlines():
        raw = line.strip()
        if not raw:
            continue
        sha, separator, ref = raw.partition("\t")
        if not separator or not FULL_SHA_RE.fullmatch(sha):
            continue
        if not ref.startswith("refs/heads/"):
            continue
        name = ref[len("refs/heads/"):]
        try:
            validate_branch_name(name)
        except InvalidSourceRef:
            continue
        heads[name] = sha
    return heads


def list_remote_heads(repo: str, runner) -> dict[str, str]:
    output = runner(["git", "ls-remote", "--heads", repo])
    return _parse_ls_remote_heads(output)


def ordered_branch_names(heads: dict[str, str]) -> tuple[str, ...]:
    names = list(heads)
    names.sort(key=lambda name: (name != DEFAULT_SOURCE_REF, name))
    return tuple(names)


def _fetch_arg_for_branch(name: str) -> str:
    validated = validate_branch_name(name)
    return f"refs/heads/{validated}"


def checkout_source(
    *,
    repo: str,
    workdir: Path,
    runner,
    source_ref: str | None = None,
    pin_sha: str | None = None,
) -> SourceRevision:
    """Clone one remote ref into workdir and return the resolved immutable SHA.

    When ``pin_sha`` is set for a branch and the remote tip has moved, the new
    tip is checked out and ``stale=True`` is returned so Apply can refuse.
    Explicit commit SHAs are immutable and never become stale.
    """
    requested = source_ref or DEFAULT_SOURCE_REF
    pin = validate_commit_sha(pin_sha) if pin_sha else None
    heads = list_remote_heads(repo, runner)
    branches = ordered_branch_names(heads)

    if FULL_SHA_RE.fullmatch(requested):
        source_kind = SOURCE_KIND_COMMIT
        display_ref = validate_commit_sha(requested)
        fetch_arg = display_ref
        tip = None
    else:
        name = validate_branch_name(requested)
        if name not in heads:
            raise InvalidSourceRef(f"Unknown branch: {name}")
        source_kind = SOURCE_KIND_BRANCH
        display_ref = name
        fetch_arg = _fetch_arg_for_branch(name)
        tip = heads[name]

    workdir = Path(workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    runner(["git", "init"], cwd=workdir)
    runner(["git", "remote", "add", "origin", repo], cwd=workdir)
    runner(["git", "fetch", "--depth", "1", "--no-tags", "origin", fetch_arg], cwd=workdir)
    runner(
        ["git", "-c", "advice.detachedHead=false", "checkout", "--detach", "FETCH_HEAD"],
        cwd=workdir,
    )
    sha = runner(["git", "rev-parse", "HEAD"], cwd=workdir)
    if not FULL_SHA_RE.fullmatch(sha):
        raise RuntimeError("git rev-parse returned an unexpected revision.")
    if source_kind == SOURCE_KIND_COMMIT and sha != display_ref:
        raise RuntimeError("Checked out SHA does not match the requested commit.")

    stale = bool(pin and pin != sha)
    return SourceRevision(
        source_ref=display_ref,
        source_kind=source_kind,
        commit_sha=sha,
        short_sha=short_sha(sha),
        available_branches=branches,
        branch_tip_sha=tip if source_kind == SOURCE_KIND_BRANCH else None,
        stale=stale,
        reviewed_sha=pin,
    )
