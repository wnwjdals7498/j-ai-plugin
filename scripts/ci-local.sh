#!/usr/bin/env bash
# 커밋된 HEAD를 임시 폴더에 복제해 검사 전용 이미지(forge-ledger-ci)로 검사를 실행한다.
# 커밋하지 않은 변경은 검사 대상이 아니다.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
job="${1:-all}"
tag=$(cat "$repo_root/ci/image/TAG")
tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/forge-ci-local.XXXXXX")

cleanup() {
    rm -rf "$tmp_dir"
}
trap cleanup EXIT

head_sha=$(git -C "$repo_root" rev-parse HEAD)
git clone --no-local --quiet "$repo_root" "$tmp_dir"
if [ "$(git -C "$tmp_dir" rev-parse HEAD)" != "$head_sha" ]; then
    git -C "$tmp_dir" checkout --quiet "$head_sha"
fi

docker run --rm \
    -v "$tmp_dir:/src" \
    -w /src \
    "forge-ledger-ci:$tag" \
    ci/run-checks.sh "$job"
