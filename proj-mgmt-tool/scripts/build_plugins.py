#!/usr/bin/env python3
"""Build versioned, standalone PMT plugin folders and ZIP archives.

The builder is preparation only: it never installs or registers a product
plugin. Existing version output is immutable and will not be overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTS = ("codex", "claude", "opencode")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class BuildError(RuntimeError):
    """An expected, user-actionable build failure without local paths."""

    def __init__(self, message: str, *, cause: OSError | None = None):
        super().__init__(message)
        self.cause_type = type(cause).__name__ if cause is not None else None
        self.errno = getattr(cause, "errno", None) if cause is not None else None
        self.winerror = getattr(cause, "winerror", None) if cause is not None else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_list(root: Path, relative: str) -> list[Path]:
    base = root / relative
    if not base.exists():
        raise BuildError("A required package source is missing")
    if base.is_file():
        if base.is_symlink():
            raise BuildError("Package sources cannot contain symbolic links")
        return [base]
    result = []
    for path in sorted(base.rglob("*")):
        if any(part.casefold() == "__pycache__" for part in path.relative_to(base).parts):
            continue
        if path.is_symlink():
            raise BuildError("Package sources cannot contain symbolic links")
        if path.is_file():
            result.append(path)
    if not result:
        raise BuildError("A required package source is empty")
    return result


def _source_map(root: Path, product: str) -> dict[Path, PurePosixPath]:
    source_map: dict[Path, PurePosixPath] = {}

    def add_tree(source_relative: str, output_relative: str) -> None:
        source_base = root / source_relative
        for source in _file_list(root, source_relative):
            if source_base.is_dir():
                relative = source.relative_to(source_base)
                source_map[source] = PurePosixPath(output_relative) / PurePosixPath(relative.as_posix())
            else:
                source_map[source] = PurePosixPath(output_relative)

    add_tree("src/pmt", "src/pmt")
    add_tree("skills/proj-mgmt-tool", "skills/proj-mgmt-tool")
    add_tree(f"integrations/{product}/hook.py" if product != "opencode" else "integrations/opencode/pmt.js",
             f"integrations/{product}/hook.py" if product != "opencode" else "integrations/opencode/pmt.js")
    add_tree("docs/usage.md", "USAGE.md")
    if product in {"codex", "claude"}:
        add_tree(f"integrations/{product}/hooks/hooks.json", "hooks/hooks.json")
        add_tree(f"integrations/{product}/{'.codex-plugin' if product == 'codex' else '.claude-plugin'}/plugin.json",
                 f"{'.codex-plugin' if product == 'codex' else '.claude-plugin'}/plugin.json")
    elif product == "opencode":
        add_tree("integrations/opencode/bridge.py", "integrations/opencode/bridge.py")
        add_tree("integrations/opencode/README.md", "integrations/opencode/README.md")
    return source_map


def _copy_one(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _version_manifest(source: dict[str, Any], product: str, version: str, file_hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "product": product,
        "plugin_name": "pmt-lifecycle",
        "plugin_version": version,
        "core_version": source["core_version"],
        "protocol_version": 1,
        "schema_version": source["schema_version"],
        "files": file_hashes,
        "hash_excludes": ["pmt-package.json (self-reference)"],
    }


def _static_schema_version(root: Path, db_source: str) -> int:
    """Read the supported schema literal without importing/executing project code."""
    direct = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)\s*$", db_source, re.MULTILINE)
    if direct:
        return int(direct.group(1))
    alias = re.search(r"^SCHEMA_VERSION\s*=\s*([A-Za-z_]\w*)\s*$", db_source, re.MULTILINE)
    if alias:
        symbol = alias.group(1)
        imported = re.search(
            rf"^from\s+\.([A-Za-z_]\w*)\s+import\s+[^\n]*\bSCHEMA_VERSION\s+as\s+{re.escape(symbol)}\b",
            db_source, re.MULTILINE,
        )
        if imported:
            schema_source = (root / "src" / "pmt" / f"{imported.group(1)}.py").read_text(encoding="utf-8")
            literal = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)\s*$", schema_source, re.MULTILINE)
            if literal:
                return int(literal.group(1))
    # Compatibility with the prior phase2 alias retained by older source trees.
    if "SCHEMA_VERSION = PHASE2_SCHEMA_VERSION" in db_source:
        phase2_source = (root / "src" / "pmt" / "phase2_schema.py").read_text(encoding="utf-8")
        literal = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)\s*$", phase2_source, re.MULTILINE)
        if literal:
            return int(literal.group(1))
    raise ValueError("Database schema version must resolve to a static integer literal")


def _standalone_launcher() -> str:
    return '''"""Portable PMT CLI launcher for an unpacked plugin folder."""\nfrom pathlib import Path\nimport sys\n\nPACKAGE_ROOT = Path(__file__).resolve().parents[1]\nsys.path.insert(0, str(PACKAGE_ROOT / "src"))\nfrom pmt.cli import main\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'''


def _write_generated_files(package: Path, product: str, version: str,
                           source_info: dict[str, Any]) -> None:
    scripts = package / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "pmt.py").write_text(_standalone_launcher(), encoding="utf-8", newline="\n")

    if product == "codex":
        portable = {
            "name": "pmt-lifecycle",
            "version": version,
            "description": "Record minimal lifecycle events and use the local PMT workflow.",
        }
        (package / "plugin.json").write_text(json.dumps(portable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        compat_path = package / ".codex-plugin" / "plugin.json"
        compat = json.loads(compat_path.read_text(encoding="utf-8"))
        compat["version"] = version
        compat.setdefault("author", {"name": "PMT"})
        compat_path.write_text(json.dumps(compat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        marketplace_root = package / ".agents" / "plugins"
        marketplace_root.mkdir(parents=True, exist_ok=True)
        marketplace = {
            "name": "pmt-local",
            "interface": {"displayName": "PMT Local Plugins"},
            "plugins": [{
                "name": "pmt-lifecycle",
                "source": {"source": "local", "path": "./"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }],
        }
        (marketplace_root / "marketplace.json").write_text(json.dumps(marketplace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif product == "claude":
        plugin_path = package / ".claude-plugin" / "plugin.json"
        plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
        plugin["version"] = version
        plugin.setdefault("author", {"name": "PMT"})
        plugin_path.write_text(json.dumps(plugin, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        marketplace = {
            "name": "pmt-local",
            "owner": {"name": "PMT"},
            "description": "Local PMT lifecycle plugin.",
            "version": version,
            "plugins": [{
                "name": "pmt-lifecycle",
                "source": "./",
                "description": "Record minimal lifecycle events in PMT.",
                "version": version,
                "category": "Productivity",
                "strict": True,
            }],
        }
        path = package / ".claude-plugin" / "marketplace.json"
        path.write_text(json.dumps(marketplace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        package_json = {
            "name": "pmt-lifecycle-opencode",
            "version": version,
            "description": "Local PMT lifecycle event bridge for OpenCode.",
            "type": "module",
            "main": "./integrations/opencode/pmt.js",
            "exports": "./integrations/opencode/pmt.js",
        }
        (package / "package.json").write_text(json.dumps(package_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_file_manifest(package: Path, product: str, version: str, source_info: dict[str, Any]) -> None:
    hashes: dict[str, str] = {}
    for path in sorted(package.rglob("*")):
        if path.is_file() and path.name != "pmt-package.json":
            relative = path.relative_to(package).as_posix()
            if Path(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
                raise BuildError("Package entry escaped its root")
            hashes[relative] = _sha256(path)
    manifest = _version_manifest(source_info, product, version, hashes)
    (package / "pmt-package.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _make_zip(package: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(package.rglob("*")):
            if not path.is_file():
                continue
            name = path.relative_to(package).as_posix()
            if name.startswith("/") or ".." in PurePosixPath(name).parts:
                raise BuildError("Package ZIP contains an unsafe path")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def _verify_source_snapshot(root: Path, source_maps: dict[str, dict[Path, PurePosixPath]],
                            source_sets: dict[str, set[Path]], source_hashes: dict[Path, str]) -> None:
    for product, original_set in source_sets.items():
        if set(_source_map(root, product)) != original_set:
            raise BuildError("Package source file set changed during build; no output was published")
    if any(_sha256(source) != original for source, original in source_hashes.items()):
        raise BuildError("Package source changed during build; no output was published")


def _publish_stage(stage: Path, final: Path, output_root: Path, *, root: Path,
                   source_maps: dict[str, dict[Path, PurePosixPath]],
                   source_sets: dict[str, set[Path]], source_hashes: dict[Path, str]) -> None:
    # Windows scanners/indexers may briefly hold an output directory. Retry only
    # the documented sharing/permission errors and keep the whole wait below 1 s.
    retry_delays = (0.05, 0.1, 0.2, 0.4)
    for attempt in range(len(retry_delays) + 1):
        if final.exists():
            raise BuildError("That version output already exists; choose a new version")
        _verify_source_snapshot(root, source_maps, source_sets, source_hashes)
        try:
            os.replace(stage, final)
            return
        except OSError as exc:
            transient_winerrors = {5, 32, 33}
            retryable = os.name == "nt" and getattr(exc, "winerror", None) in transient_winerrors
            if not retryable or attempt >= len(retry_delays):
                raise BuildError("Could not publish package output after safe bounded retry",
                                 cause=exc) from exc
            # Recheck immutability and the final path before every retry. A version
            # that appeared meanwhile is preserved and never replaced.
            time.sleep(retry_delays[attempt])


def _remove_stage_safely(stage: Path, output_root: Path) -> None:
    resolved_root = output_root.resolve(strict=True)
    resolved_stage = stage.resolve(strict=True)
    if resolved_stage.parent != resolved_root or not stage.name.startswith(".pmt-build-"):
        raise BuildError("Refused unsafe staging cleanup path")
    shutil.rmtree(resolved_stage)


def build_plugins(output_dir: str | Path, version: str | None = None,
                  source_root: str | Path = PROJECT_ROOT) -> dict[str, Any]:
    root = Path(source_root).resolve(strict=True)
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        core_version = pyproject["project"]["version"]
        from_src = (root / "src" / "pmt" / "__init__.py").read_text(encoding="utf-8")
        core_version_match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", from_src)
        schema_text = (root / "src" / "pmt" / "db.py").read_text(encoding="utf-8")
        schema_version = _static_schema_version(root, schema_text)
        if not core_version_match or core_version_match.group(1) != core_version:
            raise ValueError
        source_info = {"core_version": core_version, "schema_version": schema_version}
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise BuildError("Project version or schema metadata is invalid") from exc
    version = version or source_info["core_version"]
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise BuildError("Version must use numeric major.minor.patch form")

    out = Path(output_dir).expanduser().resolve()
    protected_sources = [(root / "src" / "pmt").resolve(), (root / "skills" / "proj-mgmt-tool").resolve(),
                         (root / "integrations").resolve()]
    if any(out == source or source in out.parents for source in protected_sources):
        raise BuildError("Output directory cannot be inside a package source directory")
    out.mkdir(parents=True, exist_ok=True)
    final = out / version
    if final.exists():
        raise BuildError("That version output already exists; choose a new version")
    # Snapshot the complete source set and each unique source digest before any
    # product is copied. Shared core/skill sources must keep the same baseline
    # across all three products.
    source_maps = {product: _source_map(root, product) for product in PRODUCTS}
    source_sets = {product: set(source_map) for product, source_map in source_maps.items()}
    all_inputs = {source: _sha256(source)
                  for source in sorted({source for source_map in source_maps.values() for source in source_map})}
    stage = Path(tempfile.mkdtemp(prefix=f".pmt-build-{version}-", dir=out))
    try:
        for product in PRODUCTS:
            package = stage / product
            source_map = source_maps[product]
            for source, relative in source_map.items():
                if _sha256(source) != all_inputs[source]:
                    raise BuildError("Package source changed before copy; no output was published")
                destination = package.joinpath(*relative.parts)
                _copy_one(source, destination)
                if _sha256(destination) != all_inputs[source] or _sha256(source) != all_inputs[source]:
                    raise BuildError("Package source changed during copy; no output was published")
            # Copy exact source paths first, then apply product version metadata and generated manifests.
            _write_generated_files(package, product, version, source_info)
            _write_file_manifest(package, product, version, source_info)
            _make_zip(package, stage / f"{product}.zip")

        # Re-hash the full source snapshot immediately before publication and on
        # every bounded retry. A late source edit can never be accepted as new baseline.
        _publish_stage(stage, final, out, root=root, source_maps=source_maps,
                       source_sets=source_sets, source_hashes=all_inputs)
    except Exception as build_error:
        try:
            if stage.exists():
                _remove_stage_safely(stage, out)
        except Exception as cleanup_error:
            detail = str(build_error) if isinstance(build_error, BuildError) else "package build failed"
            raise BuildError(f"{detail}; safe staging cleanup also failed") from cleanup_error
        raise
    results = {}
    for product in PRODUCTS:
        results[product] = {
            "directory": str(final / product),
            "zip": str(final / f"{product}.zip"),
            "zip_sha256": _sha256(final / f"{product}.zip"),
            "manifest_sha256": _sha256(final / product / "pmt-package.json"),
        }
    return {"version": version, "core_version": source_info["core_version"],
            "schema_version": source_info["schema_version"], "output": str(final), "products": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--version", help="Plugin package version; defaults to the core version")
    args = parser.parse_args(argv)
    try:
        print(json.dumps({"ok": True, **build_plugins(args.output_dir, args.version)}, ensure_ascii=False))
        return 0
    except BuildError as exc:
        detail = {key: getattr(exc, key) for key in ("cause_type", "errno", "winerror")
                  if getattr(exc, key, None) is not None}
        print(json.dumps({"ok": False, "error": {"code": "package_build_failed",
                                                    "message": str(exc), **detail}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
