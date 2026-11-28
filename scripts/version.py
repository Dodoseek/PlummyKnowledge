#!/usr/bin/env python3
"""PlummyKnowledge version management (docs/adr/0009-automatic-versioning.md).

The single source of truth is the ``VERSION`` file in the repository root.

Format (SemVer subset):
    X.Y.Z          release
    X.Y.Z-dev.N    development build N on the way to release X.Y.Z

Commands:
    show                 print the current version
    bump                 next development version; called by the pre-commit hook
    sync-index           re-stage VERSION after `git commit <paths>`; called by the post-commit hook
    release              X.Y.Z-dev.N -> X.Y.Z, move CHANGELOG `Unreleased` into the release section,
                         commit and annotated tag vX.Y.Z (docs/adr/0025-semver-and-contract-stability.md)
    notes X.Y.Z          print the CHANGELOG section of release X.Y.Z (GitHub Release text)
    set VERSION          set a new target manually (must be greater than the current one)
    check [--against R]  validate VERSION and ensure it is greater than in revision R
    merge O A B          git merge driver: result = next(max(A, B))

Uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional

VERSION_FILE = "VERSION"
CHANGELOG_FILE = "CHANGELOG.md"
UNRELEASED = "Unreleased"
_SECTION = re.compile(r"^## \[(?P<name>[^\]]+)\].*$", re.MULTILINE)
_LINK = re.compile(r"^\[[^\]]+\]: \S+$", re.MULTILINE)
_UNRELEASED_LINK = re.compile(r"^\[Unreleased\]: (?P<base>\S+)/compare/(?P<previous>\S+)\.\.\.HEAD$", re.MULTILINE)
SKIP_ENV = "PLUMMY_SKIP_VERSION_BUMP"
_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-dev\.(0|[1-9]\d*))?$"
)


class Version(NamedTuple):
    major: int
    minor: int
    patch: int
    dev: Optional[int] = None

    @classmethod
    def parse(cls, text: str) -> "Version":
        match = _PATTERN.match(text.strip())
        if not match:
            raise ValueError(
                f"invalid version {text.strip()!r}: expected X.Y.Z or X.Y.Z-dev.N"
            )
        major, minor, patch, dev = match.groups()
        return cls(int(major), int(minor), int(patch), None if dev is None else int(dev))

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.dev is None else f"{base}-dev.{self.dev}"

    @property
    def is_release(self) -> bool:
        return self.dev is None

    def precedence(self) -> tuple[int, int, int, int, int]:
        # SemVer: a pre-release (X.Y.Z-dev.N) has lower precedence than the release X.Y.Z.
        return (self.major, self.minor, self.patch, 1 if self.is_release else 0, self.dev or 0)

    def next_commit(self) -> "Version":
        """Version for the next commit: dev.N -> dev.N+1, release X.Y.Z -> X.(Y+1).0-dev.1."""
        if self.is_release:
            return Version(self.major, self.minor + 1, 0, 1)
        return self._replace(dev=self.dev + 1)

    def to_release(self) -> "Version":
        if self.is_release:
            raise ValueError(f"{self} is already a release")
        return self._replace(dev=None)


class CommandError(Exception):
    pass


def _section_bounds(text: str, name: str) -> Optional[tuple[int, int, int]]:
    """(heading start, body start, body end) of ``## [name]``; the body ends at the next section or links."""
    sections = list(_SECTION.finditer(text))
    for index, match in enumerate(sections):
        if match.group("name") != name:
            continue
        end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
        link = _LINK.search(text, match.end(), end)
        return match.start(), match.end(), link.start() if link else end
    return None


def _without_empty_groups(body: str) -> str:
    """Drop ``### Group`` headings that have no entries under them."""
    kept: list[str] = []
    for block in re.split(r"(?m)^(?=### )", body):
        lines = [line for line in block.strip("\n").splitlines()]
        if lines and lines[0].startswith("### ") and not any(line.strip() for line in lines[1:]):
            continue
        if any(line.strip() for line in lines):
            kept.append("\n".join(lines))
    return "\n\n".join(kept)


def changelog_release(text: str, version: Version, date: datetime.date) -> str:
    """CHANGELOG text with the entries of ``Unreleased`` moved into the section of ``version``."""
    if _section_bounds(text, str(version)) is not None:
        raise CommandError(f"{CHANGELOG_FILE} already has a section for {version}")
    bounds = _section_bounds(text, UNRELEASED)
    if bounds is None:
        raise CommandError(f"{CHANGELOG_FILE} has no '## [{UNRELEASED}]' section")
    start, body_start, body_end = bounds
    body = _without_empty_groups(text[body_start:body_end])
    if not re.search(r"(?m)^\s*[-*] \S", body):
        raise CommandError(
            f"'{UNRELEASED}' in {CHANGELOG_FILE} has no entries: describe the release before tagging it"
        )
    rest = text[body_end:].lstrip("\n")
    released = f"## [{UNRELEASED}]\n\n## [{version}] - {date.isoformat()}\n\n{body}\n"
    result = text[:start] + released + ("\n" + rest if rest else "")
    link = _UNRELEASED_LINK.search(result)
    if link:
        base, previous = link.group("base"), link.group("previous")
        result = (
            result[: link.start()]
            + f"[{UNRELEASED}]: {base}/compare/v{version}...HEAD\n"
            + f"[{version}]: {base}/compare/{previous}...v{version}"
            + result[link.end() :]
        )
    return result


def changelog_notes(text: str, version: str) -> str:
    """Body of the CHANGELOG section of release ``version``."""
    Version.parse(version)
    bounds = _section_bounds(text, version)
    if bounds is None:
        raise CommandError(f"{CHANGELOG_FILE} has no section for {version}")
    _, body_start, body_end = bounds
    body = text[body_start:body_end].strip("\n")
    if not body:
        raise CommandError(f"the {CHANGELOG_FILE} section for {version} is empty")
    return body + "\n"


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise CommandError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _repo_root() -> Path:
    return Path(_git("rev-parse", "--show-toplevel").stdout.strip())


def _read_blob(spec: str) -> Optional[str]:
    """Content of ``spec`` (e.g. ``HEAD:VERSION`` or ``:VERSION``) or None if it does not exist."""
    result = _git("show", spec, check=False)
    return result.stdout if result.returncode == 0 else None


def _version_path() -> Path:
    return _repo_root() / VERSION_FILE


def _read_file_version() -> Version:
    path = _version_path()
    if not path.exists():
        raise CommandError(f"{VERSION_FILE} not found in {path.parent}")
    return Version.parse(path.read_text(encoding="utf-8"))


def _write_version(version: Version, *, stage: bool) -> None:
    _version_path().write_text(f"{version}\n", encoding="utf-8")
    if stage:
        # Respects GIT_INDEX_FILE, so it also works for `git commit -a` / `git commit <paths>`.
        _git("add", "--", VERSION_FILE)


def cmd_show(_: argparse.Namespace) -> int:
    print(_read_file_version())
    return 0


def cmd_bump(_: argparse.Namespace) -> int:
    if os.environ.get(SKIP_ENV) == "1":
        print(f"version: bump skipped ({SKIP_ENV}=1)", file=sys.stderr)
        return 0

    staged_text = _read_blob(f":{VERSION_FILE}")
    if staged_text is None:
        staged = _read_file_version()  # VERSION exists but is not tracked yet
    else:
        staged = Version.parse(staged_text)

    head_text = _read_blob(f"HEAD:{VERSION_FILE}")
    if head_text is not None:
        if _git("diff", "--cached", "--quiet", "HEAD", check=False).returncode == 0:
            # Nothing staged: git will refuse the commit ("nothing to commit") or it is a
            # message-only amend. Do not touch VERSION, otherwise it stays staged.
            return 0
        head = Version.parse(head_text)
        if staged.precedence() > head.precedence():
            # VERSION was raised deliberately (release, `set`, merge driver) — keep it.
            print(f"version: {staged} (set explicitly, automatic bump skipped)", file=sys.stderr)
            return 0
        if staged != head:
            # Stale index entry (e.g. after `git commit <paths>`); never go backwards.
            print(f"version: staged {staged} is behind HEAD {head}; bumping from HEAD", file=sys.stderr)
        current = head
    else:
        current = staged  # first commit that introduces VERSION

    new = current.next_commit()
    _write_version(new, stage=True)
    print(f"version: {current} -> {new}", file=sys.stderr)
    return 0


def cmd_sync_index(_: argparse.Namespace) -> int:
    """post-commit: after `git commit <paths>` the real index may keep the old VERSION.

    Re-stage VERSION only when the working tree already equals HEAD (written by the hook),
    so user edits are never staged implicitly.
    """
    head_text = _read_blob(f"HEAD:{VERSION_FILE}")
    path = _version_path()
    if head_text is None or not path.exists():
        return 0
    if path.read_text(encoding="utf-8") == head_text and _read_blob(f":{VERSION_FILE}") != head_text:
        _git("add", "--", VERSION_FILE, check=False)
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    dirty = _git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise CommandError(
            "working tree has uncommitted changes to tracked files; commit or stash them first"
        )
    current = _read_file_version()
    release = current.to_release()
    tag = f"v{release}"
    if _git("rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0:
        raise CommandError(f"tag {tag} already exists")
    changelog_path = _repo_root() / CHANGELOG_FILE
    if not changelog_path.exists():
        raise CommandError(f"{CHANGELOG_FILE} not found: a release needs release notes (ADR-025 §5)")
    changelog = changelog_release(
        changelog_path.read_text(encoding="utf-8"), release, datetime.datetime.now(datetime.timezone.utc).date()
    )
    if args.dry_run:
        print(f"release: {current} -> {release} (tag {tag}) [dry run]")
        return 0
    changelog_path.write_text(changelog, encoding="utf-8")
    _git("add", "--", CHANGELOG_FILE)
    _write_version(release, stage=True)
    _git("commit", "-m", f"chore(release): {tag}")
    _git("tag", "-a", tag, "-m", f"PlummyKnowledge {tag}")
    print(f"release: {current} -> {release}, tag {tag} created")
    print(f"publish with: git push origin HEAD {tag}")
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    path = _repo_root() / CHANGELOG_FILE
    if not path.exists():
        raise CommandError(f"{CHANGELOG_FILE} not found")
    sys.stdout.write(changelog_notes(path.read_text(encoding="utf-8"), args.version))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    current = _read_file_version()
    target = Version.parse(args.version)
    if target.precedence() <= current.precedence():
        raise CommandError(f"{target} is not greater than current {current}")
    _write_version(target, stage=True)
    print(f"version: {current} -> {target} (staged; it will be kept by the next commit)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    current = _read_file_version()
    previous_text = _read_blob(f"{args.against}:{VERSION_FILE}")
    if previous_text is not None:
        previous = Version.parse(previous_text)
        if current.precedence() <= previous.precedence():
            raise CommandError(
                f"{VERSION_FILE} {current} is not greater than {previous} in {args.against}"
            )
        print(f"version: {current} > {previous} ({args.against}) — ok")
    else:
        print(f"version: {current} — ok (no {VERSION_FILE} in {args.against})")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    """Git merge driver (%O %A %B). Writes the result to %A; non-zero exit = conflict."""
    try:
        ours = Version.parse(Path(args.ours).read_text(encoding="utf-8"))
        theirs = Version.parse(Path(args.theirs).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"version merge driver: {exc}", file=sys.stderr)
        return 1
    newest = max(ours, theirs, key=Version.precedence)
    result = newest.next_commit()
    Path(args.ours).write_text(f"{result}\n", encoding="utf-8")
    print(f"version: merged {ours} + {theirs} -> {result}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PlummyKnowledge version management (ADR-009)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print the current version").set_defaults(func=cmd_show)
    sub.add_parser("bump", help="bump the development version (pre-commit hook)").set_defaults(
        func=cmd_bump
    )
    sub.add_parser("sync-index", help="re-stage VERSION after a commit (post-commit hook)").set_defaults(
        func=cmd_sync_index
    )
    release = sub.add_parser("release", help="X.Y.Z-dev.N -> X.Y.Z, commit and tag")
    release.add_argument("--dry-run", action="store_true")
    release.set_defaults(func=cmd_release)
    notes = sub.add_parser("notes", help="print the CHANGELOG section of a release")
    notes.add_argument("version")
    notes.set_defaults(func=cmd_notes)
    set_cmd = sub.add_parser("set", help="set a new version manually")
    set_cmd.add_argument("version")
    set_cmd.set_defaults(func=cmd_set)
    check = sub.add_parser("check", help="validate VERSION and monotonic growth")
    check.add_argument("--against", default="HEAD~1")
    check.set_defaults(func=cmd_check)
    merge = sub.add_parser("merge", help="git merge driver for VERSION")
    merge.add_argument("base")
    merge.add_argument("ours")
    merge.add_argument("theirs")
    merge.set_defaults(func=cmd_merge)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CommandError, ValueError) as exc:
        print(f"version: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
