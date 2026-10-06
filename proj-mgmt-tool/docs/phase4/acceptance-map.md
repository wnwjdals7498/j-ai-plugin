# 4단계 수용 기준과 실제 시험 연결

2026-10-06. 아래는 [21개 예정 시험](verification.md)을 실제 시험 가족에 연결한 표다. 한 행은 모든 입력 조합의 통과 선언이 아니다. 최종 실행·source hash·실패/skip·허용 범위는 [구현 상태](implementation-status.md)와 그 증거 manifest를 따른다. `tests/`의 경로는 이 프로젝트 기준이다.

| 수용 ID | 실제 확인 경계·시험 가족 | 계층·해석 제한 |
|---|---|---|
| P4-R0-01 | `test_phase4_foundation`, `test_backup`, `test_phase3_migration`, `test_phase4_registry` | 실제 SQLite migration/rollback·IDs·backup·structured 오류/exit. Host는 별도 continuity/decision 시험 |
| P4-R0-02 | foundation·registry·Host continuity/decision·Hosted context | 명시 project/현재 owner/scope/private/revoked 또는 다른 세션 거부. caller 승인/refs는 실제 증거 아님 |
| P4-R0-03 | foundation의 두 process CAS·event 구별·replay, change·alignment의 exact replay, retention | 실제 DB rollback/current auth·partial ref 보호. 전체 가능한 crash 위치의 완전 열거 아님 |
| P4-R1-01 | current context의 actual-facts receipt integration | 계획·basis에서 관찰한 구현·실제 verification-at-basis·후속 fail·가짜 ref 구별. 현재 코드/증거 적용성은 FILE 재확인 |
| P4-R1-02 | current context checkpoint 사건/replay·planning checkpoint, hosted CLI checkpoint | 실제 `decision_saved` 등 확정 사건 필요. idle/Stop은 checkpoint/Done 근거 아님 |
| P4-R1-03 | current context 실제 capture·basis 비교·stale detail, changes mid-read mutation, Host publish-work-basis | 선택 source/DB coherence·현재 run/revision. 환경의 모든 조건을 안다는 의미 아님 |
| P4-R2-01 | R2/R3 actual Git 수집/replay·R4 actual change 연결 | 실제 commit/dirty hash·관찰 pointer·미확인 이유. 분석/반영 기준 무단 전진 없음 |
| P4-R2-02 | R2/R3 actual rename/delete·branch/non-Git/owner 경계, current·Hosted source 변화 | 실제 원본 보존·unknown 또는 거부. 상세 branch/merge 조합의 전수 시험 아님 |
| P4-R2-03 | changes core parser·actual mapping decision/event·mid-read mutation | Python 최상위 선언·명시 graph refs만 지원. 동적/미지원 언어는 unknown |
| P4-R3-01 | local/Hosted actual decision→assessment→resolution→F1/F3→typed receipt | 현재 user delegation/결정의 실제 event·범위 재검증. 의미 판단은 후보/검토 경계이며 모델 문구는 승인 아님 |
| P4-R3-02 | actual applicability→기존 F6/P2 verification/evidence, 기존 verification/reuse 회귀 | 정의·대상·조건·증거 현재성 검사. 원 pass/state를 새 pass로 재작성하지 않음 |
| P4-R3-03 | actual alignment replay/stale/active-run 관문, Host quiescence, 기존 publication/documents/HostedFiles 복구 | 실제 효과/readback·CAS·미완료 보호. 새 native 모델의 늦은 결과 품질 판정은 별도 계층 |
| P4-R4-01 | current context/registry budget·mandatory overflow·bounded envelope | UTF-8 bytes/물리 JSON lines. token 실제 usage·품질 향상은 이 시험으로 입증 안 됨 |
| P4-R4-02 | local/Hosted detail source·owner·cursor 재확인, retention/current chain | 현재 refs 보존·미보존 unavailable·다른 세션 private 거부. 이전 개요는 권한 아님 |
| P4-R4-03 | current context action 규칙·actual R2/R3/R4 연결·기존 execution/control/pending 회귀 | 조건부 다음 조회/검토 제안, `executable=false`. 자동 실행/소유권 이전/Done 없음 |
| P4-R5-01 | hooks/storage config·Hosted CLI SessionStart·portable package Hook | adapter/CLI/TLS fixture. 실제 제품 설치·native 이벤트 발생을 확인한 시험 아님 |
| P4-R5-02 | hooks pending/replay/timeout·역순 Stop, checkpoint boundary·기존 lifecycle/pending | 원 사건/request 보존·오류 신호, Stop/idle 완료/해제 없음. 실제 제품 crash 전체 조합은 미실행 |
| P4-R5-03 | hooks·Host continuity private/sentinel·기존 diagnostics transaction/fallback | 합성 prompt/transcript/path/credential sentinel, rollback/fallback. 실제 사용자 대화/설정 미열람 |
| P4-R6-01 | Hosted context 두 device/session, Host auth/CLI/network/transfer/migration·branch mapping | 실제 loopback HTTPS·독립 Host process·별도 config, offline 신규 권한 없음. 외부/Linux Host 미실행 |
| P4-R6-02 | local/Hosted R4·portable/installed wheel update/reinstall·Hook, 독립 모델 입력 생성 | 시스템/패키지 계층과 모델/실제품 계층 분리. 새 독립 모델 평가는 자동 승인 검토 차단, 실제 제품 설치 미실행 |
| P4-R6-03 | `scripts/measure_phase4_r6_resume.py`의 실제 동일 조건 비교 | 전체 호출/읽기/전송/실패·시간·상태 보존. 1회 sample·provider tokens/모델 품질 unknown; 비교 불가 이유 보존 |

기존 1~3단계 시험은 현재 source에서 회귀를 실행한 결과만 연결한다. 역사적 evidence·fixture·agent 보고를 새 실제 통과 횟수로 합산하지 않는다. 독립 모델 시험의 [실제 차단 기록](evidence/model-actual/README.md)과 [제품 capability 확인](evidence/r5-product-capabilities.md)을 별도로 확인한다.
