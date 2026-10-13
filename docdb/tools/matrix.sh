#!/usr/bin/env bash
# Python 3.10~3.14 매트릭스: 버전별 venv 생성 -> 의존성 설치 -> pytest. (C-27, D9)
# 종료 코드: FAIL이 하나라도 있으면 1, 아니면 0.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)" || exit 1
cd "$ROOT" || exit 1

if ! command -v uv >/dev/null 2>&1; then
    echo "uv 명령을 찾을 수 없다" >&2
    exit 1
fi

TMP="$(mktemp -d)" || exit 1
trap 'rm -rf "$TMP"' EXIT
trap 'exit 130' INT TERM

# 기존 인터프리터만 쓴다(네트워크 다운로드 금지).
export UV_PYTHON_DOWNLOADS=never

pass=0
fail=0
skip=0

for ver in 3.10 3.11 3.12 3.13 3.14; do
    name="py$ver"
    if ! uv python find "$ver" >/dev/null 2>&1; then
        echo "$name SKIP(인터프리터 없음)"
        skip=$((skip + 1))
        continue
    fi
    venv="$TMP/.venv-$ver"
    py="$venv/bin/python"
    log="$TMP/log-$ver.txt"
    if uv venv "$venv" --python "$ver" >"$log" 2>&1 \
        && uv pip install --python "$py" -c constraints-dev.txt pytest pysqlite3 mcp >>"$log" 2>&1 \
        && PYTHONNOUSERSITE=1 "$py" -m pytest -q >>"$log" 2>&1; then
        echo "$name PASS"
        pass=$((pass + 1))
    else
        echo "$name FAIL"
        fail=$((fail + 1))
        tail -n 20 "$log" | sed 's/^/    /' >&2
    fi
done

echo "PASS=$pass FAIL=$fail SKIP=$skip"
[ "$fail" -eq 0 ]
