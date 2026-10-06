"""Conservative, source-bound change and implementation-link primitives.

These helpers intentionally describe only inspected Git names and a small Python AST
surface. They never interpret code changes as approval or complete semantic impact.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import PurePosixPath
from typing import Any

from ..errors import PmtError
from ..util import fingerprint

RULE_VERSION = "p4-link-index-1"
_SUPPORTED_SUFFIXES = {".py"}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_name_status_z(raw: bytes) -> list[dict[str, Any]]:
    """Parse `git diff --name-status -z --find-renames` without losing odd paths."""
    try:
        tokens = raw.decode("utf-8", errors="strict").split("\0")
    except UnicodeDecodeError as exc:
        raise PmtError("git_output_invalid", "Git path metadata is not UTF-8") from exc
    if tokens and tokens[-1] == "":
        tokens.pop()
    result, index = [], 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            raise PmtError("git_output_invalid", "Git returned an empty change status")
        kind = status[0]
        if kind in {"R", "C"}:
            if index + 1 >= len(tokens):
                raise PmtError("git_output_invalid", "Git returned an incomplete rename/copy record")
            before, after = tokens[index:index + 2]
            index += 2
            if kind == "C":
                # Copy detection does not prove the old path was removed.
                result.append({"kind": "added", "path": after, "similarity": int(status[1:] or "0")})
            else:
                result.append({"kind": "renamed", "before_path": before, "path": after,
                               "similarity": int(status[1:] or "0")})
            continue
        if kind not in {"A", "M", "D", "T", "U", "X", "B"} or index >= len(tokens):
            raise PmtError("git_output_invalid", "Git returned an unsupported or incomplete change record")
        path = tokens[index]
        index += 1
        normalized = {"A": "added", "M": "modified", "D": "deleted", "T": "type_changed",
                      "U": "unmerged", "X": "unknown", "B": "broken"}[kind]
        result.append({"kind": normalized, "path": path})
    return result


def normalize_repo_path(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or value.startswith("/")
            or (len(value) >= 2 and value[1] == ":")):
        raise PmtError("source_path_invalid", "Source paths must be nonempty repository-relative POSIX paths")
    value = value.rstrip("/")
    if not value:
        raise PmtError("source_path_invalid", "Source path must identify a repository entry")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in value.split("/")) or path.is_absolute():
        raise PmtError("source_path_invalid", "Source path escapes or aliases the selected repository scope")
    return path.as_posix()


def _covers(path: str, candidate: str) -> bool:
    return candidate == path or candidate.startswith(path.rstrip("/") + "/")


def extract_python_symbols(path: str, content: bytes) -> dict[str, Any]:
    """Extract only statically declared Python symbols; dynamic behavior stays unknown."""
    path = normalize_repo_path(path)
    digest = sha256_bytes(content)
    if PurePosixPath(path).suffix.casefold() not in _SUPPORTED_SUFFIXES:
        return {"path": path, "content_hash": digest, "status": "unknown",
                "reason_code": "unsupported_language", "symbols": []}
    try:
        text = content.decode("utf-8", errors="strict")
        tree = ast.parse(text, filename=path, type_comments=True)
    except (UnicodeDecodeError, SyntaxError, ValueError) as exc:
        return {"path": path, "content_hash": digest, "status": "unknown",
                "reason_code": "python_parse_failed", "symbols": []}
    symbols = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            symbols.append({"name": node.name, "kind": kind, "line": node.lineno,
                            "qualified_name": f"{path}::{node.name}"})
    dynamic = any(isinstance(node, (ast.Call, ast.Import, ast.ImportFrom)) and
                  ((isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
                    node.func.id in {"eval", "exec", "getattr", "__import__"}) or
                   (isinstance(node, (ast.Import, ast.ImportFrom))))
                  for node in ast.walk(tree))
    return {"path": path, "content_hash": digest, "status": "partial" if dynamic else "parsed",
            "reason_code": "dynamic_or_external_binding" if dynamic else None,
            "symbols": sorted(symbols, key=lambda item: (item["line"], item["qualified_name"]))}


def build_link_index(*, source_entries: list[dict[str, Any]], graph: dict[str, Any] | None,
                     scope_hash: str, explicit_mappings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build version-bound path candidates while keeping human and extracted links distinct."""
    if not isinstance(scope_hash, str) or len(scope_hash) != 64:
        raise PmtError("source_scope_invalid", "scope_hash must be a SHA-256 digest")
    entries = []
    seen = set()
    for entry in source_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("content"), bytes):
            raise PmtError("source_inventory_invalid", "Local source entries require path and inspected bytes")
        path = normalize_repo_path(entry["path"])
        if path in seen:
            raise PmtError("source_inventory_invalid", "Source inventory contains a duplicate path")
        seen.add(path)
        entries.append(extract_python_symbols(path, entry["content"]))
    nodes = (graph or {}).get("nodes", []) if isinstance(graph, dict) else []
    graph_paths: dict[str, set[str]] = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            continue
        refs = node.get("file_refs", [])
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, str):
                continue
            try:
                normalized = normalize_repo_path(ref.replace("\\", "/"))
            except PmtError:
                continue
            graph_paths.setdefault(normalized, set()).add(node["id"])
    links = []
    mapped_paths = set()
    for entry in entries:
        for ref_path, node_ids in graph_paths.items():
            if _covers(ref_path, entry["path"]) or _covers(entry["path"], ref_path):
                links.append({"path": entry["path"], "node_ids": sorted(node_ids),
                              "link_state": "extracted_candidate", "evidence": "graph_file_ref",
                              "source_hash": entry["content_hash"]})
                mapped_paths.add(entry["path"])
    for mapping in explicit_mappings or []:
        if not isinstance(mapping, dict) or not isinstance(mapping.get("path"), str) or not isinstance(mapping.get("node_id"), str):
            raise PmtError("mapping_invalid", "Explicit mappings require path and node_id")
        path = normalize_repo_path(mapping["path"])
        if path not in seen or not any(node.get("id") == mapping["node_id"] for node in nodes):
            continue
        if mapping.get("verified_mapping_ref") and mapping.get("decision_ref") == mapping.get("verified_mapping_ref"):
            state = "verified_mapping"
        else:
            state = "review_required"
        links.append({"path": path, "node_ids": [mapping["node_id"]], "link_state": state,
                      "evidence": "explicit_mapping", "decision_ref": mapping.get("decision_ref"),
                      "source_hash": next(item["content_hash"] for item in entries if item["path"] == path)})
        if state == "verified_mapping":
            mapped_paths.add(path)
    unmapped = sorted(seen - mapped_paths)
    body = {"rule_version": RULE_VERSION, "scope_hash": scope_hash,
            "source_inventory_hash": fingerprint([{k: entry[k] for k in ("path", "content_hash", "status", "reason_code")}
                                                    for entry in entries]),
            "entries": entries, "links": sorted(links, key=lambda item: (item["path"], item["link_state"], item["node_ids"])),
            "coverage": {"selected_count": len(entries), "mapped_count": len(seen & mapped_paths),
                         "unmapped_count": len(unmapped), "unknown_count": sum(item["status"] == "unknown" for item in entries),
                         "unmapped_paths": unmapped, "complete": not unmapped and all(item["status"] == "parsed" for item in entries)}}
    body["index_hash"] = fingerprint(body)
    return body

