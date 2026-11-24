#!/usr/bin/env bash
# 검사 전용 이미지 forge-ledger-ci:<TAG>를 로컬에 빌드한다. 레지스트리에 올리지 않는다.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

tag=$(cat ci/image/TAG)
digest=$(python3 ci/inputs_hash.py)

docker build -f ci/Dockerfile --build-arg INPUTS_SHA256="$digest" -t "forge-ledger-ci:$tag" ci/image
