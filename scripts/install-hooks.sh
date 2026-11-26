#!/usr/bin/env bash
# 클론한 뒤 한 번 실행한다: 이 저장소의 git 훅 폴더를 .githooks로 지정한다.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
git config core.hooksPath .githooks
