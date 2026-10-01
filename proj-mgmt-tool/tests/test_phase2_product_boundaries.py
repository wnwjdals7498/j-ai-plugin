"""Local Phase 2 product harness boundary checks; no product session is started."""
from __future__ import annotations

from pathlib import Path
import os
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_phase2_products as verifier


@pytest.fixture(scope="module")
def package_distribution(tmp_path_factory):
    from build_plugins import build_plugins
    root = tmp_path_factory.mktemp("native-phase2-product-boundaries")
    build_plugins(root / "distribution", verifier.EXPECTED_CORE, ROOT)
    return root


@pytest.fixture(autouse=True)
def isolated_distribution_root(package_distribution, monkeypatch):
    monkeypatch.setattr(verifier, "TEST_ROOT", package_distribution)


@pytest.mark.parametrize("product", ["claude", "codex"])
def test_built_package_has_current_core_schema_and_integrity(product: str, package_distribution):
    package = package_distribution / "distribution" / verifier.EXPECTED_CORE / product
    checked = verifier._load_package(package, product)
    assert checked["root"] == package.resolve()
    assert len(checked["manifest_sha256"]) == 64
    assert checked["launcher"].is_file()


def test_product_environment_does_not_inherit_credentials_or_proxies(tmp_path: Path, package_distribution):
    package = package_distribution / "distribution" / verifier.EXPECTED_CORE / "claude"
    env = verifier._base_env(tmp_path, package, "claude", Path(sys.executable), Path(sys.executable))
    forbidden = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"}
    assert not (forbidden & set(env))
    assert env["HOME"] == str(tmp_path / "home")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "home" / "claude-config")
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["NO_PROXY"] == "127.0.0.1,localhost"
    assert os.environ.get("PATH", "") not in env["PATH"]


def test_run_root_must_be_fresh_and_inside_native_phase2_area(tmp_path: Path, package_distribution):
    package = package_distribution / "distribution" / verifier.EXPECTED_CORE / "claude"
    outside = package_distribution / "not-a-native-phase2-run"
    with pytest.raises(verifier.CheckError, match="run_root_name_invalid"):
        verifier.run_check("claude", package, outside, None, Path(sys.executable))


def test_run_root_refuses_existing_content(tmp_path: Path, package_distribution):
    package = package_distribution / "distribution" / verifier.EXPECTED_CORE / "claude"
    run_root = package_distribution / "native-phase2-existing"
    run_root.mkdir(exist_ok=True)
    marker = run_root / "keep.json"
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(verifier.CheckError, match="run_root_not_empty"):
        verifier.run_check("claude", package, run_root, None, Path(sys.executable))
    assert marker.read_text(encoding="utf-8") == "{}"
