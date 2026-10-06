# 4단계 작업 안내

2026-10-06. 상태: **코드 구현·로컬 기능 검증, 미수용 별도**. 계획의 조사 기준은 `ac7aded`/Core 0.3.0/SQLite 4/graph 1이었다. 현재 코드는 Core 0.4.0/SQLite 5/graph 1이다. 논리 포트와 실제 operation의 연결은 [실제 연결 계약](runtime-contract.md), 확인한 범위·파일 접근 오류·제품/모델 미수용은 [구현 상태](implementation-status.md)를 따른다.

전체 목적과 사용자 범위는 [4단계 방향 문서](../04-session-continuity.md), 공통 개발 규칙은 [AGENTS.md](../../AGENTS.md)를 따른다. 특정 파일의 줄 단위 추가/수정/삭제 목록을 Step 지시로 만들지 않는다.

## 읽기 경로

| 목적 | 문서 |
|---|---|
| 목표·범위·전체 흐름 | [4단계 방향](../04-session-continuity.md) |
| 원본·정보 객체·권한·저장·연결 의미 | [공통 계약](contracts.md) |
| 전체 배정·31개 Step·구현 순서·선행/인수 관문 | [상세 작업 계획](implementation-plan.md) |
| 공통 입력/출력·서비스 연결·현재 API 공백 | [병렬 구현 연결 규격](implementation-interfaces.md) |
| A: 현재 사실·basis·checkpoint·개요/작업 문맥·조건부 행동 | [R1/R4 상세계획](plan-current-context.md) |
| B: actual change·mapping·근거 적용성·방향/문서 반영 | [R2/R3 상세계획](plan-change-alignment.md) |
| C: Hook·제품·Host 저장·복구·독립 세션·비용·패키지 | [R5/R6 상세계획](plan-session-host-integration.md) |
| 수용 시나리오·실제 관찰·로그·효율 | [검증 명세](verification.md) |
| 실제 CLI/Host·저장·현재 지원 연결 | [런타임 계약](runtime-contract.md) |
| 실행한 시험·현재 구현·미수용 범위 | [구현 상태](implementation-status.md) |
| 21개 예정 시험과 실제 사례 연결 | [수용 연결표](acceptance-map.md) |

## 기능과 배정

| ID | 기능·산출물 | 소유·소비 |
|---|---|---|
| R0 | 데이터/포트·호환·권한·오류·로그·시험 규격 | 메인 + 저장 담당; 전체 소비 |
| R1 | 현재 방향·구현 상태·checkpoint·session 연결 참조 | 사실/문맥 담당; R3/R4 |
| R2 | 실제 변경 receipt·코드/기능/요구 연결 index | 변경 담당; R3 |
| R3 | 영향·근거 적용성·자율 범위·반영 receipt | 변경 담당 + 메인 의미 판단; R4/R5 |
| R4 | 재개 개요·작업 문맥·행동 제안·상세 읽기 | 사실/문맥 담당; 제품·메인·runner |
| R5 | Hook·스킬·의미 있는 checkpoint 갱신 | 연결 담당; native 주입 또는 명시 조회 |
| R6 | 인증 Host 저장·복구·통합·비교·패키징 | 연결/시험 담당 + 메인; 최종 수용 |

R번호와 R번호-S번호는 논리 작업 ID다. 실제 Project/Work/Item/Step UUID는 착수 시 별도로 발급해 연결한다. 계획 문서가 임의의 PMT 작업 상태·실행을 생성했다고 주장하지 않는다.

상세계획은 `gpt-6-luna` 세 작업자가 병렬 작성하고 메인이 연결했다. 같은 모델의 A/B/C가 공유 계약을 인계받아 구현했고 메인이 통합·검증한다. 자기 영역·공통 계약·직접 선행 인계만 읽으며, 전체 문서를 매번 반복 통독하지 않는다. 상세계획의 기대와 실제 코드·시험 결과를 구별한다.

## 작업자가 반환할 내용

- 목표/완료 기준별 구현 결과와 실제 수정 영역.
- 공통 계약 변경, 호환·이관 영향과 소비자 전달 사항.
- 수행한 시험 ID·실제 명령·종료 코드·기준 commit/dirty·환경·source/증거 hash.
- 실패·blocked·not_run·미확인·이후 무효화된 성공.
- 관련 서비스가 사용할 실제 입출력, 복구 지점, 다음 착수 조건.

검토되지 않은 임의 원본, caller Boolean, 모델의 성공 문구를 실제 구현·검증·종료의 증거로 삼지 않는다. 원본·현재 사실·해석 제안·반영된 결정·검증을 구별한다. 구현 상태와 시험 증거는 실제 착수 후 별도 기록하며 이전 3단계 기록을 새 성공으로 복제하지 않는다.
