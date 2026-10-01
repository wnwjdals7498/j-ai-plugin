# 로그·이력·검증 증거 규약

로그의 목적은 입력이 어디에서 실패했고 무엇이 실제로 반영됐는지 설명하는 것이다. 이 문서는 구현할 규약이며 현재 실행 로그가 생성됐다는 뜻은 아니다.

## 세 종류를 구분

| 종류 | 내용·저장 | 기록 이유 |
|---|---|---|
| 업무 이력 | SQLite의 결정·상태 변경·이유·이전/이후 revision·근거 ID | 다음 세션과 결정/파기 추적에 필요한 원본 |
| 진단 로그 | 구조화된 JSONL, machine CLI에서는 stderr 또는 별도 사용자 로그 경로 | 오류·지연·충돌·설치 문제 분석 |
| 시험 증거 | 실행 결과 manifest + 필요한 출력·fixture·DB 검사·설치 확인 | 시험 합격·실패와 재사용 자격 입증 |

업무 이력에 모든 debug·heartbeat를 넣지 않는다. 진단 로그를 프로젝트의 사실·결정 문서로 자동 승격하지 않는다. 긴 시험 출력은 리소스로 저장하고 업무 기록에는 ID와 요약을 연결한다.

## 공통 진단 필드

| 필수 | 값 |
|---|---|
| 기본 | at_utc, level, component, event_name, correlation_id |
| 호출 | request_id, operation, session_id, source product/version, adapter/core version |
| 대상 | scope_id, record/item ID, event_id, 검증/리소스/백업 ID 중 관련값 |
| 결과 | outcome, error_code, retryable, duration_ms, exit_code |
| 변경 | old/new revision, transaction outcome, 소유권 비교 결과 |

없는 필드는 null 또는 미제공으로 명시한다. 토큰·claim token의 원문은 로그에 쓰지 않는다. OS·Python·SQLite·schema·journal 설정은 초기화/시험 manifest에서 기록하고 매 이벤트에 중복 출력하지 않는다.

## 기록할 시점

| 구성 | 필수 이벤트 | 연결되는 시험 |
|---|---|---|
| 저장 기반 | init/migration 시작·성공·실패, 지원 밖 schema, maintenance 진입·해제 | DATA-01~03, BACKUP-01 |
| 변경·점유 | request 수신·idempotent replay·conflict, commit/rollback, claim/release/finish 결과 | TX-01, IDEM-01~02, REV-01, CLAIM-01~03, DONE-01~02 |
| 이벤트 수집 | 원 이벤트 ID·매핑 버전·정규화 type·저장/중복/미지원/대기 결과 | EVENT-01~02, STOP-01, HOOK-01~03 |
| 조회·검증 | 범위·결과 수·축소, 조회 revision, 검증 match/mismatch/stale의 이유 | READ-01~02, VERIFY-01~03 |
| 리소스 | staging/ready/참조 상태, hash·크기, orphan/missing/corrupt 진단 | RESOURCE-01~03 |
| 백업·복원 | maintenance·백업 ID·manifest·파일 수·hash 검사·실패 단계 | BACKUP-01~02 |
| CLI | parse·dispatch·종료 결과. stdout은 JSON 전용 | CLI-01~03, RECOVER-01 |
| 설치 | 대상 버전·패키지 hash·runtime·data root 별칭·연결·trust 상태·업데이트/제거 결과 | PKG-01, INSTALL-01~05 |

기본 운영은 INFO, 실패는 WARNING/ERROR를 사용한다. DEBUG는 제한된 진단에만 켜고 기간·출력량을 관리한다. 테스트는 로그의 최소 필드·호출 연결·오류 결과를 검사하며 메시지 문장 전체를 구현과 동일하게 복제하는 시험을 만들지 않는다.

Python 표준 logging을 구조화된 출력으로 사용한다. 라이브러리 공통 로깅과 제품별 출력 변환을 구분한다. [공식 logging 문서](https://docs.python.org/3/library/logging.html).

## 실패 처리

| 장애 | 업무 결과 | 관찰 가능한 출력 |
|---|---|---|
| 업무 이벤트 또는 요청 결과 기록 실패 | 상태·revision도 rollback. 성공 아님 | 오류 코드 + rollback 결과 + 비밀 없는 stderr |
| 진단 로그 파일 저장 실패 | 업무 commit이 성공했다면 성공 유지 | warnings에 `diagnostic_log_unavailable`, 최소 stderr fallback |
| 훅에서 PMT 호출 실패 | 업무 완료/결정을 만들지 않음 | native 규약에 맞춘 실패 표식 + 재전송함에 필요한 이벤트·안정 ID |
| PMT 저장·재전송함 모두 실패 | 저장됐다고 보고하지 않음 | `event_not_persisted`와 최소 native/stderr 경고. 손실 가능을 숨기지 않음 |
| commit 후 응답 전달 실패 | 반영 여부를 추정해 재실행하지 않음 | 같은 request ID로 원결과 조회·재처리 |
| 실패 증거 파일 저장 실패 | 시험 pass로 위장하지 않음 | test result의 evidence 상태 missing/blocked |

진단 장애를 성공 업무의 재실행 이유로 삼지 않는다. DB 업무 이력이 실패했는데 진단 로그만 남았다고 성공으로 처리하지 않는다. 훅의 실패 표식·timeout·native exit mapping은 [제품 규약](adapters-packaging.md)을 따른다.

## 로그와 재전송함의 최소 수집

- 비밀키·인증 토큰·claim token·환경 변수 전체·transcript 전문은 수집하지 않는다.
- adapter payload는 허용 필드만 사용한다. 사용자 선택·목표·결정에 필요한 본문과 출처 발췌는 업무 입력으로 명시 저장한다.
- 진단 로그에는 본문보다 ID·길이·hash·오류 종류를 남긴다. 실제 개인 경로는 data root 별칭 등으로 표현한다.
- 재전송함은 제한된 이벤트 envelope·필요 payload·안정 ID만 보존한다. 오류 처리 중에 원문 전체를 fallback으로 복제하지 않는다.
- native 훅 원문 fixture는 테스트용 값으로 치환하고 제품·버전·공식 source 형식과 변환 범위를 기록한다.
- 로그·시험 데이터·DB·리소스는 기본적으로 Git 배포물에 넣지 않는다. 시험 완료 보고는 증거 ID·작은 요약·대상 상태를 참조한다.

## 증거 manifest

검증 실행에는 `test_id, definition_version, result, target_commit, dirty_fingerprint, environment, dependency/settings fingerprint, input/fixture version, command, at_start/end, exit_code, executor, correlation_id, evidence_refs`를 저장한다.

manifest와 증거는 현재 대상에 묶는다. 같은 이름의 이전 파일을 새 실행의 성공 증거로 쓰지 않는다. 파일 hash·존재 여부를 확인한다. 사용자에게 보이는 결과는 pass/fail/blocked/not_run과 후속 조치이며 내부 긴 로그는 필요할 때 조회한다.

`environment`에는 OS/architecture·Python/SQLite·제품/연결부 버전과 profile/data 별칭, `command`에는 실행 경로 별칭·인자·cwd scope·허용 설정을 넣는다. dependency/settings/fixture·package hash와 대상 scope도 재사용 비교에 포함한다. reused는 source_execution_id·현재 판정 시각·사유만 연결하며 원 실행의 종료 코드·시각을 새 실행 값으로 바꾸지 않는다. aborted/stale/reused 상태도 사용자 결과에서 구분한다.

## 보존 정책

일반 실행·의미 있는 진행 이력은 종료 후 3개월, 미참조 임시는 생성 후 1주일이다. 진단 로그는 기본 1주일·용량 제한으로 회전하고, 조사에 필요한 발췌는 증거로 승격한다. 현재 결정·유효 지식·진행 작업·미처리 결과·참조 증거는 계속 보존한다.

자동 GC가 구현되기 전에는 보존 기한·참조와 정리 대상을 조회·진단한다. 백업도 명시된 보존 정책을 적용한다. 원본만 삭제하고 백업에 무기한 보존된 것을 삭제 완료로 보고하지 않는다.
