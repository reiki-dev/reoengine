"""Pure review validation and diff parsing helpers."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from ._data import Json
from .policy import _diff_path

def _review_approved(review: Mapping[str, Json]) -> bool:
    findings = review.get("findings")
    return (review.get("approved") is True and isinstance(findings, list)
            and all(isinstance(finding, dict) and finding.get("severity") in ("info", "low", "medium")
                    for finding in findings))

def _incomplete_review(digest: Mapping[str, Json]) -> str | None:
    coverage, changed = digest.get("coverage"), digest.get("changed_files")
    if not isinstance(coverage, list) or not isinstance(changed, list):
        return "missing coverage"
    paths: list[str] = []
    for item in coverage:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return "invalid coverage"
        path = item["path"]
        if not isinstance(path, str):
            return "invalid coverage path"
        paths.append(path)
    if (any(not isinstance(path, str) for path in changed) or len(paths) != len(set(paths))
            or len(paths) != len(changed) or set(paths) != {path for path in changed if isinstance(path, str)}):
        return "coverage does not match changed files"
    if digest.get("risk_tier") == "high":
        if digest.get("unknowns") != []:
            return "unknowns remain"
        if any(not isinstance(item, dict) or item.get("reviewed") is not True for item in coverage):
            return "files remain unreviewed"
    return None

def _effective_review_approved(review: Mapping[str, Json], digest: Mapping[str, Json]) -> bool:
    return _review_approved(review) and _incomplete_review(digest) is None

def review_input_digest(diff_sha256: str, gates_sha256: str, changed_files: tuple[str, ...],
                        task: Mapping[str, Json]) -> str:
    value = {"diff_sha256": diff_sha256, "gates_sha256": gates_sha256,
             "changed_files": sorted(changed_files), "task": dict(task)}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()

def _diff_sections(patch: bytes) -> dict[str, list[str]]:
    """Index complete diff sections by both sides, including pure renames."""
    sections: dict[str, list[str]] = {}
    for section in re.split(rb"(?m)^(?=diff --git )", patch):
        lines = section.splitlines()
        if not lines or not lines[0].startswith(b"diff --git "):
            continue
        header = re.fullmatch(rb'("(?:[^"\\]|\\.)*"|a/.*?) ("(?:[^"\\]|\\.)*"|b/.*)', lines[0][11:])
        paths: set[str] = set()
        if header is not None:
            paths.update(_diff_path(header.group(index))[2:] for index in (1, 2))
        # Prefer explicit metadata: an unquoted name may itself contain " b/".
        explicit_paths: set[str] = set()
        for line in lines[1:]:
            if line.startswith(b"@@") or line == b"GIT binary patch":
                break
            for prefix in (b"rename from ", b"rename to "):
                if line.startswith(prefix):
                    explicit_paths.add(_diff_path(line[len(prefix):]))
            if line.startswith((b"--- ", b"+++ ")):
                path = _diff_path(line[4:].removesuffix(b"\t"))
                if path.startswith(("a/", "b/")):
                    explicit_paths.add(path[2:])
        if explicit_paths:
            paths = explicit_paths
        text = section.decode("utf-8", errors="replace")
        for path in paths:
            sections.setdefault(path, []).append(text)
    return sections

def _excerpt_in_section(excerpt: str, section: str) -> bool:
    """A literal fragment, nonblank after removing optional diff markers."""
    def without_markers(text: str) -> str:
        return "\n".join(line[1:] if line.startswith(("+", "-", " ")) else line
                         for line in text.splitlines())

    unmarked_excerpt = without_markers(excerpt)
    if not unmarked_excerpt.strip():
        return False
    normalized = without_markers(section)
    return excerpt in section or excerpt in normalized or unmarked_excerpt in normalized

def _line_in_section(line: int, excerpt: str, section: str) -> bool:
    """Match the first quoted line using new coordinates, or old for deletions."""
    quoted = excerpt.splitlines()
    if line < 1 or not quoted:
        return False
    first = quoted[0]
    marker = first[:1] if first.startswith(("+", "-", " ")) else ""
    text = (first[1:] if marker else first).rstrip()
    old = new = old_left = new_left = 0
    for row in section.splitlines():
        hunk = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", row)
        if hunk:
            old, old_left = int(hunk.group(1)), int(hunk.group(2) or "1")
            new, new_left = int(hunk.group(3)), int(hunk.group(4) or "1")
            continue
        if row.startswith("\\ No newline at end of file"):
            continue
        prefix = row[:1]
        if prefix == "+" and new_left > 0:
            number = new
            new, new_left = new + 1, new_left - 1
        elif prefix == "-" and old_left > 0:
            number = old
            old, old_left = old + 1, old_left - 1
        elif prefix == " " and old_left > 0 and new_left > 0:
            number = new
            old, old_left = old + 1, old_left - 1
            new, new_left = new + 1, new_left - 1
        else:
            old_left = new_left = 0
            continue
        if (number == line and row[1:].rstrip() == text
                and (marker not in ("+", "-") or marker == prefix)):
            return True
    return False

