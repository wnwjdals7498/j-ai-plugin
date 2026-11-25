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

top_root=$(git -C "$repo_root" rev-parse --show-toplevel)
sub_dir=$(git -C "$repo_root" rev-parse --show-prefix)
sub_dir=${sub_dir%/}

head_sha=$(git -C "$repo_root" rev-parse HEAD)
base_sha="${BASE_SHA:-$(git -C "$repo_root" merge-base "origin/${BASE_BRANCH:-master}" HEAD)}"
target_head="${HEAD_SHA:-$head_sha}"

git clone --no-local --quiet "$top_root" "$tmp_dir"
if [ "$(git -C "$tmp_dir" rev-parse HEAD)" != "$head_sha" ]; then
    git -C "$tmp_dir" checkout --quiet "$head_sha"
fi

docker run --rm \
    -e BASE_SHA="$base_sha" \
    -e HEAD_SHA="$target_head" \
    -v "$tmp_dir:/src" \
    -w "/src/$sub_dir" \
    "forge-ledger-ci:$tag" \
    ci/run-checks.sh "$job"
