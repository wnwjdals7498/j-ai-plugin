# syntax=docker/dockerfile:1
# forge-ledger PR 검사 전용 이미지. 빌드: bash scripts/ci-image-build.sh
# 버전은 모두 고정한다(자동 갱신 금지). 빌드 컨텍스트는 ci/image/ 이다.

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM python:3.12-slim-bookworm

ARG PYTHON39_VERSION=3.9.25
ARG NODE_VERSION=26.13.1
ARG GITLEAKS_VERSION=8.30.1
ARG TRIVY_VERSION=0.76.2
ARG SHELLCHECK_VERSION=0.11.0
# shellcheck 릴리스에는 체크섬 파일이 없어 sha256을 직접 고정한다(shellcheck-v0.11.0.linux.x86_64.tar.xz).
ARG SHELLCHECK_SHA256=8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198
ARG ACTIONLINT_VERSION=1.7.12
ARG SEMGREP_VERSION=1.186.0
ARG CLAUDE_CODE_VERSION=2.1.331

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_NO_CACHE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git libatomic1 xz-utils \
    && rm -rf /var/lib/apt/lists/*

# uv
COPY --from=uv /uv /uvx /usr/local/bin/

# Python 3.9 (uv 관리 배포본) -> python3.9
RUN uv python install --no-bin "${PYTHON39_VERSION}" \
    && ln -sf "$(uv python find --no-project "${PYTHON39_VERSION}")" /usr/local/bin/python3.9 \
    && python3.9 --version

WORKDIR /tmp/dl

# Node (배포처 SHASUMS256.txt로 확인)
RUN curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" \
    && grep " node-v${NODE_VERSION}-linux-x64.tar.xz\$" SHASUMS256.txt | sha256sum -c - \
    && tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /usr/local --strip-components=1 --no-same-owner \
    && rm -rf /tmp/dl/* \
    && node --version

# gitleaks (배포처 checksums.txt로 확인)
RUN f="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
    && curl -fsSLO "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${f}" \
    && curl -fsSLO "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_checksums.txt" \
    && grep "  ${f}\$" "gitleaks_${GITLEAKS_VERSION}_checksums.txt" | sha256sum -c - \
    && tar -xzf "${f}" -C /usr/local/bin gitleaks \
    && rm -rf /tmp/dl/*

# trivy (배포처 checksums.txt로 확인)
RUN f="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
    && curl -fsSLO "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/${f}" \
    && curl -fsSLO "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_checksums.txt" \
    && grep "  ${f}\$" "trivy_${TRIVY_VERSION}_checksums.txt" | sha256sum -c - \
    && tar -xzf "${f}" -C /usr/local/bin trivy \
    && rm -rf /tmp/dl/*

# shellcheck (ARG로 고정한 sha256으로 확인)
RUN f="shellcheck-v${SHELLCHECK_VERSION}.linux.x86_64.tar.xz" \
    && curl -fsSLO "https://github.com/koalaman/shellcheck/releases/download/v${SHELLCHECK_VERSION}/${f}" \
    && echo "${SHELLCHECK_SHA256}  ${f}" | sha256sum -c - \
    && tar -xJf "${f}" \
    && install -m 0755 "shellcheck-v${SHELLCHECK_VERSION}/shellcheck" /usr/local/bin/shellcheck \
    && rm -rf /tmp/dl/*

# actionlint (배포처 checksums.txt로 확인)
RUN f="actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" \
    && curl -fsSLO "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/${f}" \
    && curl -fsSLO "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_checksums.txt" \
    && grep "  ${f}\$" "actionlint_${ACTIONLINT_VERSION}_checksums.txt" | sha256sum -c - \
    && tar -xzf "${f}" -C /usr/local/bin actionlint \
    && rm -rf /tmp/dl/*

# semgrep (python3.12 venv) + 레지스트리 규칙 묶음
COPY semgrep-packs.txt /tmp/semgrep-packs.txt
RUN python3.12 -m venv /opt/semgrep \
    && /opt/semgrep/bin/pip install "semgrep==${SEMGREP_VERSION}" \
    && ln -sf /opt/semgrep/bin/semgrep /usr/local/bin/semgrep \
    && mkdir -p /opt/semgrep-rules \
    && while IFS= read -r pack; do \
         [ -z "${pack}" ] && continue; \
         curl -fsSL "https://semgrep.dev/c/${pack}" -o "/opt/semgrep-rules/${pack//\//-}.yaml" || exit 1; \
       done < /tmp/semgrep-packs.txt \
    && test "$(find /opt/semgrep-rules -type f | wc -l)" -eq "$(grep -c . /tmp/semgrep-packs.txt)" \
    && test -z "$(find /opt/semgrep-rules -type f -size 0)" \
    && rm -f /tmp/semgrep-packs.txt \
    && ls -l /opt/semgrep-rules

# Claude Code CLI
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && claude --version

# trivy 점검 규칙을 미리 받아 둔다(오프라인 실행용)
RUN mkdir -p /tmp/trivy-empty /opt/trivy-cache \
    && trivy fs --scanners misconfig,secret --cache-dir /opt/trivy-cache /tmp/trivy-empty \
    && rm -rf /tmp/trivy-empty

WORKDIR /

# 이미지 입력 해시 기록(scripts/ci-image-build.sh가 넘긴다)
ARG INPUTS_SHA256=""
RUN if [ -z "${INPUTS_SHA256}" ]; then echo "INPUTS_SHA256 build-arg 필요: bash scripts/ci-image-build.sh" >&2; exit 1; fi \
    && mkdir -p /etc/forge-ci \
    && printf '%s\n' "${INPUTS_SHA256}" > /etc/forge-ci/inputs.sha256
