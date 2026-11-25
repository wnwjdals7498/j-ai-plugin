# forge-ledger 저장소 전용 Gitea 러너 운영 문서

저장소 전용 PR 자동 검사 러너(`forge-ledger-runner`)의 등록, 운영, 이미지 재빌드, 장애 대응 절차를 정리한다.
이 문서와 설정 파일에는 Gitea 주소·IP·토큰 값을 적지 않으며, 실행 시점의 환경 변수로만 주입한다.

## 1. 필요 환경 변수

| 변수명 | 용도 | 비고 |
|---|---|---|
| `GITEA_INSTANCE_URL` | Gitea 인스턴스 주소 | 필수 |
| `GITEA_RUNNER_REGISTRATION_TOKEN` | 저장소 러너 등록 토큰 | 최초 등록 시에만 사용 후 즉시 폐기 |
| `FORGE_RUNNER_DATA` | 호스트 측 러너 상태·캐시 저장 경로 | 필수, 저장소 디렉터리 밖 경로 사용 |

## 2. 최초 등록 절차 (메인 세션 전용)

등록 토큰과 Gitea 주소는 메인 세션(운영자)에서만 다루며, 하위 에이전트나 저장소 파일에 남기지 않는다.

1. 저장소 밖 호스트 경로에 데이터 디렉터리를 만들고 `FORGE_RUNNER_DATA`로 지정한다.
2. Gitea 저장소 설정(Actions > Runners)에서 등록 토큰을 발급받아 현재 셸의 `GITEA_RUNNER_REGISTRATION_TOKEN`에 설정한다.
3. 필요한 환경 변수를 주입해 러너를 기동한다.

```bash
export FORGE_RUNNER_DATA="/path/outside/repo/forge-runner-data"
export GITEA_INSTANCE_URL="<Gitea 인스턴스 주소>"
export GITEA_RUNNER_REGISTRATION_TOKEN="<발급받은 등록 토큰>"

docker compose -f ci/runner/compose.yml up -d
```

4. 로그에서 등록 완료(`Runner registered successfully`)와 `${FORGE_RUNNER_DATA}/.runner` 파일 생성을 확인한다.
5. 등록 직후 현재 셸에서 등록 토큰을 즉시 버린다.

```bash
unset GITEA_RUNNER_REGISTRATION_TOKEN
```

6. 이후 재기동이나 컨테이너 재생성 시에는 `GITEA_RUNNER_REGISTRATION_TOKEN` 없이 `GITEA_INSTANCE_URL`과 `FORGE_RUNNER_DATA`만 설정해 실행한다.

## 3. 상태 확인과 재시작

`docker compose -f ci/runner/compose.yml ps|logs|restart`는 `compose.yml`이 필수 환경 변수를 검사하므로 `GITEA_INSTANCE_URL`과 `FORGE_RUNNER_DATA`가 설정된 셸에서만 동작한다.

환경 변수 설정이 있는 셸:

컨테이너 상태 확인:

```bash
docker compose -f ci/runner/compose.yml ps
```

최근 로그 확인:

```bash
docker compose -f ci/runner/compose.yml logs --tail 50
```

러너 재시작:

```bash
docker compose -f ci/runner/compose.yml restart
```

환경 변수 없이 컨테이너 이름으로 바로 실행:

```bash
docker ps --filter name=forge-ledger-runner
docker logs --tail 50 forge-ledger-runner
docker restart forge-ledger-runner
```

## 4. 검사 이미지 재빌드 절차

`ci/Dockerfile` 또는 `ci/image/*`(`TAG`, `semgrep-packs.txt`) 등 이미지 입력이 변경되면 로컬 검사 이미지(`forge-ledger-ci:<TAG>`)를 다시 빌드한다. (`ci/requirements-*.txt`는 실행 시점 가상환경 설치 입력이므로 이미지 재빌드 대상이 아니다.)

이미지 입력을 바꾸는 PR에서는 기존 태그를 덮어쓰지 않도록 `ci/image/TAG` 정수를 먼저 올린 뒤 아래 순서로 빌드·반영한다.

1. `ci/image/TAG`의 정수를 1 올리고, 저장소 루트에서 빌드 스크립트를 실행한다.

```bash
bash scripts/ci-image-build.sh
```

2. 다음 두 곳의 태그도 새 `ci/image/TAG` 값으로 갱신한 뒤 러너 컨테이너를 재생성한다.
   - `ci/runner/compose.yml`의 `GITEA_RUNNER_LABELS` (`forge-ci:docker://forge-ledger-ci:<TAG>`)
   - `.gitea/workflows/pr-check.yml`의 각 작업 `container` (`forge-ledger-ci:<TAG>`)

```bash
docker compose -f ci/runner/compose.yml up -d --force-recreate
```

`ci/runner/config.yaml`에 `force_pull: false`가 설정되어 있으므로 러너는 외부 레지스트리에서 받지 않고 호스트에 빌드된 로컬 이미지를 사용한다.

## 5. 외부 네트워크 장애 시 동작

`pr-check` 워크플로의 일부 단계(예: 취약점 데이터베이스 조회 등 외부 통신이 필요한 검사)는 외부 네트워크 연결이 끊기면 실패로 끝난다.
검사가 외부 장애로 실패하더라도 자동으로 통과 처리되지 않으므로, 네트워크가 복구된 뒤 PR에서 검사를 다시 실행한다.

## 6. 비상 우회 (브랜치 보호 규칙 일시 해제)

러너 장애나 긴급 장애 복구로 PR 자동 검사를 완료할 수 없는 경우에만 저장소 관리자가 비상 우회를 사용한다.

1. 우회 사유와 대상 PR·커밋을 먼저 확정한다.
2. Gitea 저장소 설정에서 `main` 브랜치 보호 규칙을 일시 해제하고 PR을 병합한다.
3. 병합 직후 `main` 브랜치 보호 규칙을 원래 설정대로 즉시 복원한다.
4. 우회 사유, 대상 PR·커밋, 해제 시각, 복원 시각, 사후 검증 결과를 PR 코멘트 또는 운영 이슈에 기록한다.
