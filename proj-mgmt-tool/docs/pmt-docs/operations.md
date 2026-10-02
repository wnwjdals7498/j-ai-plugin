# 운영·로그·보존

## 저장과 실패 처리

| 종류 | 저장·목적 | 실패 처리 |
|---|---|---|
| 업무 이력 | SQLite, 결정·상태·소유권·실행 결과 추적 | 업무 변경과 함께 rollback; 완료 응답 금지 |
| 진단 로그 | 현재 JSONL stderr; 2단계 선택적 data root 내 logs | 경고/fallback, 이미 커밋한 업무를 재실행하지 않음 |
| 검증 증거 | 리소스 + manifest, 실제 결과·재사용 근거 | 필수 증거 누락은 검증 성공/완료 확정 금지 |
| 진행 스냅샷 | 2단계 SQLite run의 최근 관찰 | 관찰 실패를 표시, 마지막 실제 진행을 새 진행으로 꾸미지 않음 |

현재 구현은 `diagnostics.py`의 허용 필드 기반 stderr JSONL이다. 파일 로그 회전·2단계 run 필드·자동 정리는 아래의 **구현할 운영 계약**이며 이미 동작하는 기능으로 간주하지 않는다.

## 로그 필드·관찰 지점

공통: UTC 시각, level, component, event_name, correlation/request/session ID, operation, outcome, error_code, retryable, duration_ms. 대상: scope/record/event ID, 이전/새 revision, transaction_outcome, ownership_result. 2단계 추가: Work/Item/Step/job/run ID, 실제 agent/model/route, 지시 버전, 상태 전이, 대기 이유, native handle의 안전한 참조.

| 관찰 지점 | 필수 확인 |
|---|---|
| 요청 수신·반환 | 요청 ID·동작·결과·시간; payload 본문 제외 |
| 트리/문서 갱신 | 버전·변경 이유·영향 노드 참조 |
| 경로 선택·Queue·점유 | 선택 이유·지원 여부·대기 대상·점유 결과 |
| 실행 시작·종료·취소·조정 | run·실제 경로·상태·종료 확인 근거 |
| Git 최신화·결과 검토 | 이전/새 기준·영향·검토 결과·증거 참조 |
| 정리·백업·복원 | 대상 종류·개수·manifest·성공/실패·남은 조치 |

INFO 기본, WARNING은 지연·차단·비필수 저장 실패, ERROR는 동작 실패, DEBUG는 기간·크기를 제한한다. 전체 대화·지시 본문·환경 변수 덤프·인증정보·claim token은 기록하지 않는다. 예외 문자열/명령 인수도 정제하며 필드 허용목록만으로 비밀 안전성을 단정하지 않는다. 절대 경로는 필요시 안전한 별칭으로 변환한다.

진행 화면에는 작업/시도, 현재 단계·모델·경로, 경과 시간, 마지막 확인 시각과 실제 변화 시각, 마지막 산출물, 대기 대상/이유, 다음 행동·사용자 결정 필요 여부를 표시한다. 시작·변경·오류·완료에 갱신하고 장기 실행은 60초 이내 관찰을 남긴다. 매 관찰을 업무 이벤트로 누적하지 않으며 근거 없는 진행률을 표시하지 않는다.

## 보존·회전 — 2단계 운영 기본값

| 자료 | 기준 |
|---|---|
| 종료된 일반 실행·진행 이력 | 종료 후 3개월, 참조/복구 필요 여부 확인 후 정리 |
| 미참조 임시 handoff·테스트 이미지 | 생성 후 1주일 |
| 파일 진단 로그 | 1주일 + 파일당 10MiB·최대 10개, 먼저 도달한 제한 적용; 설정 가능 |
| 현재 결정·유효 지식·활성 계획·참조 증거 | 유효/참조 중에는 보존 |
| 진행 작업·미처리 결과·미확인 실행 | 해결·정리 전까지 보존 |

정리는 참조 확인 → 후보 기록 → 정리/실패 기록 순서다. 임시 자료가 증거로 채택되면 정식 리소스로 전환한다. 증거가 삭제/손상되면 관련 검증의 재사용 자격도 만료한다. 회전 로그는 보장된 시험 증거 저장소가 아니므로 필요한 로그를 리소스로 승격한다. Git 이력을 3개월 규칙으로 자동 재작성하지 않는다. 백업 사본의 보존·삭제는 별도 사용자 정책으로 관리한다.

## 장애·복구와 운영 시험

- DB busy는 제한된 대기 후 재시도 가능 오류로 반환한다. 모델 실행 내내 DB 쓰기 트랜잭션을 잡지 않는다.
- 파일 게시와 DB 참조 사이의 장애는 고아/누락 상태를 진단한다. DB와 파일을 하나의 원자 작업이라고 가정하지 않는다.
- 불명확한 실행은 원 실행기 조회로 복구한다. 실행 종료 확인 없이 점유 강제 회수·다른 runner 재배정은 하지 않는다.
- 백업은 DB와 리소스 manifest의 일관성을 확인하고 복원은 별도 빈 root에서 검증한다. 이관 전 백업·실패 복구 경로를 시험한다.
- 운영 시험은 디스크/로그 쓰기 실패, DB 경합, 강제 종료, callback 중복, 보존 예외, 비밀 필터, 회전 상한을 재현한다. 기준은 [P2-LOCK/RUN/CANCEL/DATA/OBS](development.md#필수-시험과-증거)다.

검증 manifest에는 시험 ID·정의 버전, 실제 명령·종료 코드, 실행 시각, commit/dirty 지문, 환경/의존성/설정/입력 지문, 실행자·correlation ID, 증거 참조를 남긴다. 상세 기존 규약은 [1단계 로깅](../phase1/logging.md)을 재사용한다.

## 3단계 Host·연결·복구

Host만 `host` extra를 설치하고 `pmt-host`로 별도 data/config root를 선택한다. 서버는 한 프로세스로 SQLite·resource를 소유한다. 관리자 명령에서 기기를 발급·회전·폐기하고 필요한 scope/read/write/runtime/review/admin 권한을 선택한다. credential과 versioned claim key 값은 안전한 로컬 환경 설정에 둔다. HTTP JSON은 1MiB, resource는 8MiB, transfer ZIP은 64MiB 상한이다. 기본 인증서를 검증하며 redirect를 따라가지 않는다.

클라이언트 `pmt storage configure`는 mode·endpoint·credential 환경변수 이름·device/namespace·workspace mapping을 받고 TLS/호환·principal·session 검증 뒤 config hash CAS로 게시한다. `probe`는 실제 연결 확인, `status`는 비밀 없는 로컬 설정 상태다. hosted 실패 시 local DB를 열지 않는다. 현재 실행/설치 수용과 외부 배포 미실행은 [실측 상태](../phase3/implementation-status.md)를 확인한다.

| 관찰·복구 | 현재 기준 |
|---|---|
| 반복 실행 관찰 | 최근 상태와 next poll만 반환; 변화/조치가 있을 때 제어 event/notice 기록 |
| control·native ACK | 원 요청·source/context·nonce·control/run revision·실제 opaque handle 대조; private prompt는 일반 metadata/log 제외 |
| 응답 유실 | 원 request ID+semantic fingerprint로 현재 권한 아래 조회; 이미 반영됐으면 추가 write 없음 |
| Host 중단 중 종료 | 이미 attached 된 own local spool의 actual receipt/output hash만 수집; 새 파일 작업/dispatch/claim 금지 |
| pending 재연결 | 원 owner/device/env/session·source·scope·revision 확인, stale/conflict/unknown 보존; body/ID 바꾸어 우회 금지 |
| backup·restore | quiescent 원본→sanitized manifest/SQLite/resources→빈 target import→ID/FK/hash 확인; 원본 삭제·자동 primary 전환 없음 |

진단 필드는 request/run/scope/source/context/batch/hash/count·단계·코드·시각·시간을 사용한다. credential·전체 대화·native prompt·argv/env·PID·로컬 절대경로는 공유 metadata/일반 로그에 기록하지 않는다. 파일 effect와 DB commit의 중단 지점은 journal 및 실제 파일 hash로 판단한다. Windows 공유 잠금은 제한된 재시도 후 unknown으로 유지하며 복구본 삭제·다른 실행 재시작·점유 해제로 처리하지 않는다.
