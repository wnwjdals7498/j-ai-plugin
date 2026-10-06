# 4단계 구현 상태

2026-10-06. `gpt-6-luna` 3개 작업자가 병렬 구현했고 메인이 계약·통합·검증을 담당했다. **코드 구현 및 로컬 기능 검증 완료, 실제 제품/독립 모델/외부 배포는 미수용**이다. 작업 폴더의 간헐적 파일 접근 실패는 근본 원인 미확정으로 남긴다. 전체 단일 실행이 모두 통과했다고 표시하지 않는다.

Core **0.4.0**, SQLite **5**, graph **1**, protocol **1**, exit **0~5**. 기존 자료는 백업 후 additive migration한다. 실제 사용자 DB/profile·외부 Host·직접 모델 API로 시험하지 않았다.

## 구현한 기능

| 범위 | 실제 동작 |
|---|---|
| R0 저장·보존 | immutable 참조/hash·shared/private·현재 권한 후 replay·pointer CAS·journal, 90일 미참조 정리. 현재 근거·미완료 효과·활성 실행은 보존 |
| R1 현재 사실 | 계획/기준에서 관찰한 구현/검증을 실제 refs로 구별. DB+실제 source basis, 확정 사건 checkpoint·session link. summary/ref는 실행 권한 아님 |
| R2 변경·연관 | 실제 Git/dirty/rename/delete·분기·non-Git·수집 중 변화, 설정 graph 및 monorepo 하위 workspace. Python 선언/명시 graph mapping 외에는 unknown |
| R3 영향·적용성 | 실제 결정/event·위임·F6/P2 증거, F1 graph/F3 문서 publication·readback 후 alignment receipt. active Step/run·미확정 효과·stale source는 조정 대기 |
| R4 재개 | bounded metadata 개요→현재 점유/source/F5 bundle/detail, actual change/assessment/applicability/applied pointer 연결. stale/unknown/누락은 incomplete, 다음 행동은 executable=false |
| R5 Hook·스킬 | 명시 scope SessionStart 개요·current checkpoint 조회, pending/replay·안전 오류. Stop/idle는 완료/unlock 아님. 패키지 안 continuity 참조 포함 |
| R6 Host·이관 | client Git/private spool + 인증 Host metadata, actual decision receipt·typed apply·현재 SQL/owner/CAS, shared refs 이관·private projection 제외·미해결 journal 보호 |

실제 19개 로컬 operation·별도 Host 저장/서비스 포트는 [런타임 계약](runtime-contract.md), 21개 예정 기준과 실제 시험 가족은 [수용 연결표](acceptance-map.md)를 따른다. 계획의 기대와 실제 결과를 구별한다.

## 실제 검증 결과

| 실행 | 결과·해석 | 증거 |
|---|---|---|
| 전체 source-stable 회귀 670사례 | 662 passed/4 skipped/2 failed/2 errors, exit 1. skip은 Windows symlink 권한, 네 이상은 SQLite/Git 파일 접근 | [manifest](evidence/p4-full-final1.json), [JUnit](evidence/p4-full-final1.xml) |
| 동일 코드·새 경로 네 이상 재검증 | 4 passed, exit 0. 재현 안 됨을 확인하며 원인 해결 증거는 아님 | [JUnit](evidence/p4-failure-recheck1.xml) |
| 실제 Host P2 적용성 추가 | 실제 로컬 graph 검사·evidence→snapshot→record_verification→read_applicability applicable. 원 ID/fingerprint/pass/valid 보존, local DB 없음. 1 passed | [JUnit](evidence/p4-host-applicability-tracked.xml) |
| 오류 진단/자료 보존 보완 후 회귀 | 106 passed/2 failed/1 error. 실제 SQLITE_READONLY(8) 및 실패 DB를 보존 | [로그](evidence/p4-final-observability.log) |
| 일반 로컬 비교 | 해당 세 사례 3 passed; 이어 관련 110사례 중 109 passed/1 setup error로 readonly 재현. sandbox만의 문제로 확정하지 않음 | [비교](evidence/p4-isolation-check.xml), [회귀](evidence/p4-final-local-regression.xml) |
| 작업 폴더 밖 전용 임시 폴더의 같은 회귀 | **110 passed**, exit 0. 코드 변경 없이 관련 실제 사례 검증. 저장 위치 영향 가능성의 관찰이며 정확한 OS 원인은 미확정 | [로그](evidence/p4-temp-location-regression.log), [JUnit](evidence/p4-temp-location-regression.xml) |
| 패키지 | 3종 최종 ZIP/폴더/현재 source SHA 대조, 독립 CLI setup/재개. Core0.3/schema4→0.4/schema5·폴더 재설치·Hook/TLS portable 시험은 4 passed | [최종 package manifest](evidence/p4-final-package.json) |

SQLite 오류 번호/이름만 안전하게 기록하며 raw exception·SQL·비밀을 출력하지 않는다. actual readonly 주입 시험은 rollback·원 DB ID·미커밋 request를 확인했다. 실패한 context fixture를 보존하도록 변경했다. 근거·사후 ACL·재현 범위·미확정 원인은 [파일 접근 조사](evidence/p4-file-access-investigation.md)를 따른다. 성공 횟수를 합산해 실패한 전체 실행을 성공으로 바꾸지 않는다.

## 패키지와 사용

최종 배포물은 [Codex ZIP](../../dist/final/0.4.0/codex.zip), [Claude ZIP](../../dist/final/0.4.0/claude.zip), [OpenCode ZIP](../../dist/final/0.4.0/opencode.zip)이다. `dist/0.4.0`은 진단 보완 전 중간 snapshot이며 현재 배포는 `dist/final/0.4.0`과 그 manifest를 사용한다. 설치/활성화는 실제 사용자 profile에서 수행하지 않았다. 실제 OpenCode API 버전 호환과 제품 지원표는 [공식 capability 조사](evidence/r5-product-capabilities.md)를 따른다.

재개 순서는 [사용 안내](../usage.md), 설치된 AI의 구체적 절차는 [packaged continuity 참조](../../skills/proj-mgmt-tool/references/continuity-workflow.md)를 따른다. 새 source pin이면 기존 F5 graph index도 갱신한다. private Step 지시 원문은 기존 리소스에 유지하고 개요/관리툴로 복제하지 않는다.

## 효율·미수용

동일 시작 DB/source/goal/role/예산의 소규모 실제 비교에서 현재 경로는 더 많은 bytes/Git/파일 확인과 시간을 사용했다. 무변화 Legacy 4.88초→현재 10.92초, 관련 변경 6.70초→27.66초였다. F5는 양쪽 600B budget으로 incomplete였고 모델은 호출하지 않았다. **속도·토큰 절감 또는 모델 품질 향상을 입증하지 않았다.** [실측·제한](evidence/p4-r6-a-actual.md), [JSON](evidence/p4-r6-a-actual.json)을 확인한다.

실제 제품 설치·새 native 이벤트, 새 독립 AI의 재개 판단, provider tokens/비용, 외부/Linux Host 운영은 미검증이다. 독립 Codex CLI 전송 시험은 자동 승인 검토가 local-only 조건·민감성 미확인을 이유로 거절했다. 프로세스를 중단하고 우회하지 않았으며 [차단 기록](evidence/model-actual/README.md)을 보존했다. fixture가 실제 모델/제품 검증을 대체하지 않는다.
