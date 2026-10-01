# 구현 연결 규격 v1

2026-10-01 메인이 P0의 코드 연결 계약을 확정한다. 도메인 의미는 contracts.md를 따른다. 아래 이름은 병렬 소유 경계를 연결하기 위한 것으로 파일별 수정 순서는 아니다.

## 공통 런타임

- Python package는 `pmt`이며 런타임 의존성은 표준 라이브러리다. 테스트만 pytest를 사용한다.
- Database는 data root·config root를 명시해서 만들 수 있다. 모든 시험은 두 경로를 격리한다.
- `Database.connect()`는 Row 객체·foreign keys·명시 transaction/busy 설정을 제공한다.
- `Database.write(maintenance_owner=None)`는 connection context를 반환하고 짧은 원자적 쓰기를 담당한다.
- `Database.run_request(request, handler, maintenance_owner=None)`는 같은 transaction에서 handler(connection, request)의 result를 반영하고 `(response, exit_code)`를 반환한다. replay·결정적 오류·로그 warning을 공통 처리한다.
- 조회는 새 connection에서 수행한다. 파일 I/O operation은 staging/운영 소유권을 먼저 확정하고 마지막 DB 반영에 run_request를 사용한다.
- `PmtError(code, message, exit_code=2, retryable=False, details=None)`는 공통 오류다.
- 공통 util은 UTC 시각, UUID, canonical JSON/SHA-256, 안전한 JSON 해석·입력 검사를 제공한다.

## 모듈 책임

| 소유 묶음 | 책임·호출 경계 |
|---|---|
| P1 | DB·schema·paths·errors·diagnostic log·canonical util. 스키마와 아래 호출 이름 변경은 메인 확인 |
| P2 | lifecycle handler(db, connection, request): create_scope, save_change, save_decision, record_event, claim_task, release_claim, recover_claim, finish_task |
| P3 | queries/verification handler(db, connection, request): read_context, record_verification, lookup_verification. P2에서 사용할 완료 근거 검증 함수 제공 |
| P4 | resources/backup: register_resource, diagnose, backup, restore. 파일 작업/maintenance gate와 검증된 리소스 조회 함수 제공 |
| 메인 | service dispatcher, CLI, package 진입점·문서·통합·실제 설치 시험 |

handlers는 result dict를 반환하거나 PmtError를 발생시킨다. 각자 응답 envelope와 요청 중복 처리를 구현하지 않는다. 기능 간 함수를 호출할 때 DB connection을 공유할 수 있어야 하며 중첩 transaction을 열지 않는다.

## 초기 요청 payload

- create_scope: `kind, slug, parent_id?, body?`; scope 종류는 environment/repository/project/classification.
- save_change: 새 기록은 scope_id + `kind, title, parent_id?, body?, reason`; 기존 기록은 record_id·expected_revision + `title?/body?/reason`. kind는 work/item/fact/principle/backlog. `status=Planned`는 이유를 동반한 Blocked 해소, `status=Canceled`는 명시 취소다. 진행 중 취소에는 현재 token·session과 `stopped=true`가 필수이며 진행 자식이 있으면 거부한다. Done/Canceled 기록은 변경하지 않는다.
- Item body에는 `criteria`(ID 문자열 또는 id·description 등 기준 객체), `workspace`(검증할 작업 공간), `next`, `watch`, `blocked_by`를 사용할 수 있다. state는 lifecycle이 관리한다. Work 조회는 자식 진행인 aggregate_state를 함께 반환한다.
- claim/release/recover/finish: record_id·expected_revision·session_id + 계약의 소유권/이유/결과. release는 `status=Paused|Blocked`, recover는 종료/격리 근거, finish는 `result, verification_ids, claim_token`.
- decision/event/verification/resource의 상세 payload는 해당 소유자가 승인된 의미를 유지하며 명세·시험으로 제공하고 메인이 통합한다.
- setup와 get_request_result는 공통 기반·dispatcher에서 처리한다. resource 입력은 JSON 내부 bytes 대신 허용된 source 파일 경로를 사용한다.
- record_verification의 pass에는 실행 전 lookup_verification의 input_fingerprint를 `before_fingerprint`로 전달한다. 현재 snapshot과 다르거나 증거가 유효하지 않으면 pass를 거부한다. 같은 scope/환경/작업 공간/입력/기준의 다른 Item도 기존 ID를 참조할 수 있다. lookup은 새 pass를 기록하지 않는다.

## 공통 저장 필드

P1은 최소 meta, scopes, records, events, requests, claims, artifacts, artifact_refs, verifications, file_jobs 영역을 제공한다. 논리 ID는 text UUID, 상세 구조는 JSON, revision은 integer, 시간은 UTC ISO 문자열이다. 후속 소유자는 P1이 인계한 실제 schema와 호출 계약을 소비한다. 공유 schema를 직접 확장하지 않는다.

## 검증 순서

P1 인계 후 P2/P3/P4를 병렬 구현한다. 자체 시험 뒤 메인이 실제 의존 함수를 연결하여 G1을 판단한다. 제품 fixture·포장 조사만 선행 준비할 수 있으며 본체 기능을 임시 성공으로 위장하지 않는다.
