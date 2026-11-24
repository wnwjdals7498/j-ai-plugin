#!/usr/bin/env bash
# forge-ledger PR 자동 검사 실행 스크립트.
# 사용법: ci/run-checks.sh <lint|test|secrets|sast|deps|matrix|install|all>
set -euo pipefail

CI_VENV_DIR="${CI_VENV_DIR:-/tmp/forge-ci-venv}"
export CI_VENV_DIR

if [ -n "${UV_CACHE_DIR:-}" ]; then
    unset UV_NO_CACHE
fi

export PYTHONDONTWRITEBYTECODE=1
export RUFF_CACHE_DIR="$CI_VENV_DIR/.ruff_cache"
export MYPY_CACHE_DIR="$CI_VENV_DIR/.mypy_cache"
export COVERAGE_FILE="$CI_VENV_DIR/.coverage"

ensure_git_safe_dir() {
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        git config --global --add safe.directory "$(pwd)"
    fi
}

ensure_py39_venv() {
    local venv_dir="$CI_VENV_DIR/py39"
    if [ ! -f "$venv_dir/.ready" ] || [ ! -x "$venv_dir/bin/python" ]; then
        mkdir -p "$CI_VENV_DIR"
        uv venv --clear "$venv_dir" --python 3.9
        uv pip install --python "$venv_dir/bin/python" --require-hashes -r ci/requirements-py39.txt
        touch "$venv_dir/.ready"
    fi
}

ensure_py3x_venv() {
    local venv_dir="$CI_VENV_DIR/py3x"
    if [ ! -f "$venv_dir/.ready" ] || [ ! -x "$venv_dir/bin/python" ]; then
        mkdir -p "$CI_VENV_DIR"
        uv venv --clear "$venv_dir" --python 3.12
        uv pip install --python "$venv_dir/bin/python" --require-hashes -r ci/requirements-py3x.txt
        touch "$venv_dir/.ready"
    fi
}

job_lint() {
    ensure_git_safe_dir
    python3.9 ci/inputs_hash.py --check /etc/forge-ci/inputs.sha256
    ensure_py39_venv
    "$CI_VENV_DIR/py39/bin/ruff" check .
    "$CI_VENV_DIR/py39/bin/ruff" format --check .
    "$CI_VENV_DIR/py39/bin/mypy"
    git ls-files -z '*.sh' | xargs -0 shellcheck
    shopt -s nullglob
    local workflows=(.gitea/workflows/*.yml)
    shopt -u nullglob
    if [ "${#workflows[@]}" -gt 0 ]; then
        actionlint -config-file ci/actionlint.yaml "${workflows[@]}"
    fi
}

cleanup_pycache() {
    find . -maxdepth 5 -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
}

job_test() {
    ensure_git_safe_dir
    trap cleanup_pycache EXIT
    ensure_py39_venv
    "$CI_VENV_DIR/py39/bin/coverage" run -m unittest discover -s tests -t .
    "$CI_VENV_DIR/py39/bin/coverage" report
    claude plugin validate .
}

job_secrets() {
    ensure_git_safe_dir
    gitleaks git --config .gitleaks.toml --redact --no-banner --exit-code 1 .
    local base head
    if [ -n "${BASE_SHA:-}" ]; then
        base="$BASE_SHA"
    else
        if ! base="$(git merge-base origin/main HEAD)"; then
            echo "BASE_SHA가 비어 있고 origin/main과의 merge-base를 구할 수 없음" >&2
            return 1
        fi
    fi
    if [ -z "$base" ]; then
        echo "BASE_SHA 또는 origin/main merge-base가 비어 있음" >&2
        return 1
    fi
    head="${HEAD_SHA:-HEAD}"
    python3.9 ci/scan_changed_secrets.py --base "$base" --head "$head"
}

job_sast() {
    ensure_git_safe_dir
    semgrep scan --config /opt/semgrep-rules --error --metrics=off --disable-version-check .
    ensure_py3x_venv
    "$CI_VENV_DIR/py3x/bin/bandit" -c pyproject.toml -r plugins ci scripts
}

job_deps() {
    ensure_py3x_venv
    local req
    for req in ci/requirements-*.txt; do
        "$CI_VENV_DIR/py3x/bin/pip-audit" --disable-pip --no-deps --require-hashes -r "$req"
    done
    trivy fs --scanners misconfig,secret --exit-code 1 --skip-check-update --skip-db-update --cache-dir /opt/trivy-cache --skip-dirs tests --ignorefile .trivyignore.yaml .
}

job_matrix() {
    ensure_git_safe_dir
    trap cleanup_pycache EXIT
    export UV_PYTHON_DOWNLOADS=never
    mkdir -p "$CI_VENV_DIR"
    local versions=(3.10 3.11 3.12 3.13)
    local -A results=()
    local failed=0
    local ver venv_dir
    for ver in "${versions[@]}"; do
        venv_dir="$CI_VENV_DIR/m$ver"
        echo "=== [matrix] Python $ver ==="
        if uv venv --clear "$venv_dir" --python "$ver" &&
            "$venv_dir/bin/python" -m unittest discover -s tests -t .; then
            results["$ver"]="통과"
        else
            results["$ver"]="실패"
            failed=1
        fi
    done
    echo "=== matrix 결과 ==="
    for ver in "${versions[@]}"; do
        echo "Python $ver: ${results[$ver]}"
    done
    return "$failed"
}

job_install() (
    local tmp_home
    tmp_home="$(mktemp -d)"
    trap 'rm -rf "$tmp_home"' EXIT
    export HOME="$tmp_home"
    local plugin_root="$PWD/plugins/forge-ledger"
    claude plugin marketplace add "$PWD"
    claude plugin install forge-ledger@forge
    local listing read_from
    listing="$(claude plugin list)"
    read_from="$(printf '%s\n' "$listing" | sed -n 's/^[[:space:]]*Read from:[[:space:]]*//p' | sed -n '1p')"
    if [ "$read_from" != "$plugin_root" ]; then
        echo "설치된 플러그인 경로가 저장소와 다름: '$read_from' (기대: $plugin_root)" >&2
        return 1
    fi
    local py
    for py in python3.9 python3.13; do
        echo "=== [install] $py ==="
        "$py" ci/install_smoke.py --root "$plugin_root" --python "$py"
    done
)

job_all() {
    local jobs=(lint test secrets sast deps matrix install)
    local -A results=()
    local failed=0
    local job
    for job in "${jobs[@]}"; do
        echo "=== [$job] 시작 ==="
        if bash "${BASH_SOURCE[0]}" "$job"; then
            results["$job"]="통과"
        else
            results["$job"]="실패"
            failed=1
        fi
    done
    echo "=== 검사 결과 요약 ==="
    for job in "${jobs[@]}"; do
        echo "$job: ${results[$job]}"
    done
    return "$failed"
}

main() {
    if [ "$#" -ne 1 ]; then
        echo "사용법: $0 <lint|test|secrets|sast|deps|matrix|install|all>" >&2
        return 2
    fi
    case "$1" in
        lint) job_lint ;;
        test) job_test ;;
        secrets) job_secrets ;;
        sast) job_sast ;;
        deps) job_deps ;;
        matrix) job_matrix ;;
        install) job_install ;;
        all) job_all ;;
        *)
            echo "알 수 없는 작업: $1 (허용: lint|test|secrets|sast|deps|matrix|install|all)" >&2
            return 2
            ;;
    esac
}

main "$@"
