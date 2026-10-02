# 3단계 구현·검증 상태

상태: **승인된 로컬 범위 구현·수용 완료**. 기준 commit `4f632da`, 검증 2026-10-02~03 KST. 실행 범위는 [사용자 결정](scope-decisions.md)을 따른다. F0~F15 구현과 [48개 시험 ID](traceability.md)를 연결했으며 전체 실행의 실패·환경 제한·미실행도 그대로 보존한다. 현재 최종 판정은 [F15 재조정 수용](evidence/local-acceptance/repaired-source/f15-reconciled-local-acceptance.json)이다.

| 기능 | 현재 확인 |
|---|---|
| F0 공통 저장·호환 | schema 4 이관·SourcePin·CAS 저장·journal/outbox·LocalStore 구현. 기반/측정/기존 이관·백업 묶음 25개 통과, 측정 보완 5개 및 public CLI 연결 2개 통과 |
| F1~F3 graph | 변경·복구, 현재 원본/인덱스, 검증된 delta의 영향 계산·coverage 연결 구현. 최신 경계 묶음 9개 통과, 이전 F2 자체 묶음 4개 통과 |
| F4 부분 문서 | 기준선·부분 생성·수기/무관 구간 보존·현재 source/CAS·중단 복구 구현. 단계별 7개 시나리오와 마지막 변경 후 영향 핵심 시험 통과 |
| F5 문맥·재개 | 실제 원본/Step·권한·예산·alias/detail·새 세션·F8용 ref 읽기 16개 통과 |
| F6 재사용 | 실제 선택 조건·후보 자동 조회·근거/current source·1 claim/2 events·body ref·F3 무효화 20개 통과 |
| F7 결과 축약 | 리소스·정제·상세 조회 및 공개 CLI 14개 시험 통과 |
| 공통 파일 게시 | 원본 분리·후보 no-replace 게시·중단 복구 24개 통과, Windows symlink 권한 1개 skip. Linux syscall은 adapter stub 확인이며 실환경 시험 아님 |
| 기존 기능 호환·패키징 | 233건 첫 회귀의 schema 기대값·builder metadata 수정 후 격리 snapshot의 관련 18개 통과. 격리 Git 권한 실패는 선별 재실행 1개 통과로 미재현 |
| F8 실행 제어 | 실제 StatePort·bounded context·native action/ACK·중단 ACK 복구·local runtime·관찰/통지 구현. 최종 10개 통과, 관련 runner 9개 통과. 진단 로깅 변경 뒤 부정 경계 2개 통과 |
| F9 묶음 배정 | queued 두 run의 원자 scope union·1 physical slot·실제 F5 context·handle/결과·child별 검토 연결 구현. 전체 11개 통과와 2개 Windows fixture 오류 뒤 해당 경로 격리 통과. 추가 독립 프로세스 경합·대표 Step 선검토·누락 결과/보고서 변경 거절·scope별 Queue 조회 통과 |
| F10 로컬 통합 | F8 묶음 native 요청 누락 수정 후 6개 대표 행동을 연결한 최종 21개 통과. 실제 per-scenario 자료는 local-integration/actual-runs에 보관. fixture 품질이며 모델 품질·토큰 절감 통과 주장이 아님 |
| F11 Host | 실제 Windows loopback HTTPS·Uvicorn·격리 SQLite·3 device/env/session을 연결한 4개 통과, Host 데이터/리소스/전송 회귀 48개 통과. CRUD·독립 client claim 경합·재처리·응답 폐기 후 복구·권한 폐기·Source/F2/F5/F6·8MiB/hash/CA/범위 거부 확인. reverse proxy/Linux/외부 Host 수용은 제외 |
| F12 연결 | 저장소 설정/선택, 실제 CLI·현재 checkout/runtime·private control CAS·native 그룹 ACK/report collect·로컬 graph/문서 adapter 구현. hosted-files 9개, hosted planning/client routing 4개 통과. 새 프로젝트 plan 게시→구현 Step→client 모델 선택→Host Queue/F5/native action 및 완료 cache의 lock/checkout 없는 이력 조회 확인. 최종 통합 회귀는 진행 중 |
| F13 이관·백업·복원 | 원본 보존·quiesce·sanitized bundle·ID/FK/hash·빈 target 원자 SQL import·중단 재처리 구현. 실제 두 local HTTPS Host의 import/replay→backup/download→별도 restore를 포함한 18개 통과. 실제 사용자 데이터 전환/자동 primary switch는 하지 않음 |
| F14 복구 | durable pending·safe route template·owner/source/현재 권한·exact request 조회/재전송·CLI/runtime producer 구현. 실제 Host 중단→로컬 프로세스 종료→offline capture→재연결·한 번 submit 및 두 환경/branch 충돌 포함 24개 통과. 명시적 pending CLI status/capture/reconcile도 실제 HTTPS·같은 원 요청 한 번 반영 확인 |
| F15 전체 통합 | 설치/업데이트·두 HTTPS client·복원/reindex·파일/outbox·CLI·plan/routing·source scope 여섯 그룹 총 21개가 각각 exit 0. 실행 중 두 test 파일의 공백 변경을 AST/hash로 확인해 원 aggregate는 inconclusive 보존, 현재 관련 F9/Host 21개 추가 통과로 local-tier 재조정 수용. repaired immutable 0.3.0 제품 3개 hash/portable CLI smoke 확인. 실제 native 제품 등록·외부/Linux 배포는 미실행 |

Host 준비에서 점유 토큰이 요청 재처리 응답에 저장되는 기존 경계를 확인했다. 신뢰된 Python adapter가 내부 토큰을 만들고 캐시 전에 locator로 바꿀 수 있는 seam을 추가했으며, 재처리·해시 저장·잘못된 factory rollback 시험 2개가 통과했다. 로컬 기본 동작과 Host 실제 구현/검증은 구분한다.

F8 중간 실행에서는 Windows 임시 리소스/DB 준비 권한 오류가 있었다. 실패 사례를 보존하고 마지막 전체 10개 실행의 통과 근거와 구분한다. `require_escalated` 시험도 이전 권한 오류를 해결하지 못했으므로 원인을 sandbox로 확정하지 않는다.

통합에서 발견한 두 오류를 근거로 수정했다. F2 깊이 경계가 마지막 허용 노드를 빠뜨리던 문제는 실제 미방문 이웃만 unknown으로 계산하도록 바꿨다. F8 native 묶음 요청에는 모든 child/문맥/기준과 예산 안의 그룹 prompt를 전달하며, 단일 Step 요청으로 축소되면 실행을 차단한다. 관련 경계 시험과 F10 전체 통합은 수정 후 통과했다.

최적화 전 [재현 자료](evidence/2026-10-02/baseline/README.md)는 기존 validator/renderer의 실제 byte·시간 관측이다. [동일 source 문맥 비교](evidence/2026-10-02/quality-projection/README.md)는 한 synthetic 목표에서 전체 124,900→bounded 7,760 bytes 및 독립 fresh `gpt-6-luna` 두 세션의 필수 인계 의미 보존을 확인했다. 총비용·속도·provider token 절감과 전체 구현 모델 품질을 증명하는 결과는 아니다.

현재 [전체 회귀](evidence/2026-10-02/current-regression/README.md)는 543 passed, 3 failed, 1 error, 4 symlink skip을 기록했다. 두 파일 실패의 원 오류를 덮던 정리·교체 경계는 no-replace/제한된 retry·stage/journal 보존으로 수정했고 관련 18개, 실제 F9 각 1개, 호환 42개가 통과했다. 두 SQLite 준비 오류는 격리 2개 통과로 미재현이다. 안전한 extended code/name을 추가했으나 환경 원인을 확정하지 않으며 원 실행 실패는 보존한다. 전체 실행과 이후 보완 시험·F15 수용을 구분한다.

현재 숫자는 해당 실행 시점의 결과이며 이후 변경된 코드의 최종 통과 수가 아니다. 최종 수용에는 기능별 시험 ID·명령/종료 코드·현재 source hash·환경·증거·실패/미실행 범위를 연결한다. 이전 실패 evidence는 보존한다.

최종 배포물은 [repaired source package manifest](evidence/local-acceptance/repaired-source/f15-package-snapshot.json)를 따른다. 수정 전 snapshot과 실패 집계는 과거 증거이며 현재 설치 대상이 아니다. 실제 사용자 DB/설정·primary는 변경하지 않았고 API/Claude 실서비스를 호출하지 않았다. 전체 provider token·속도/총비용 개선과 production/native 제품 수용은 완료로 주장하지 않는다.
