# 폴더·기술·모듈

## 폴더 구성

아래는 2~4단계 코드 배치다. 공개 계약과 책임 경계를 유지하며 [2단계 실측 범위](../phase2/implementation-status.md), [3단계 실측 상태](../phase3/implementation-status.md), [4단계 실측 상태](../phase4/implementation-status.md)를 구분해 확인한다.

```text
proj-mgmt-tool/
├─ AGENTS.md                   공통 규칙·참조 진입점
├─ pyproject.toml              본체 런타임·배포·시험 설정
├─ src/pmt/
│  ├─ cli.py / service.py      요청 경계·검증·유스케이스 연결
│  ├─ db.py / paths.py         트랜잭션·스키마·사용자 경로
│  ├─ lifecycle.py / queries.py 상태 변경·문맥 조회
│  ├─ verification.py         검증 등록·재사용 판정
│  ├─ resources.py            리소스·백업·복원
│  ├─ hooks.py / diagnostics.py 이벤트 정규화·진단
│  ├─ errors.py / util.py      공통 오류·최소 공통 함수
│  ├─ planning/               두 트리·기능 명세·문서/graph 생성
│  ├─ routing/                가용 자원·모델/실행 경로·client settings
│  ├─ execution/              Queue·시도·결과·취소·범위 점유
│  ├─ runners/                subagent·Claude/Codex CLI 연결
│  ├─ reconciliation/         Git 변화·문서/계획 영향 분석
│  ├─ steps.py / operations.py 지시·검토·운영·보존
│  ├─ phase2*.py              Phase 2 operation 연결·공통 경계·이관 DDL
│  ├─ phase3.py / phase3_schema.py Phase 3 로컬 registry·schema 4
│  ├─ phase4.py / phase4_schema.py Phase 4 로컬 registry·schema 5
│  ├─ continuity/             현재 사실·basis·checkpoint·변경·정렬·재개·보존
│  ├─ hosted_continuity.py / hosted_context.py / hosted_changes.py
│  │                          Host 현재 권한과 클라이언트 관찰 연결
│  ├─ store.py / http_store.py LocalStore·인증 HTTPS operation port
│  ├─ storage_config.py       단일 저장소 선택·환경/checkout 매핑
│  ├─ workspace.py / hosted_runtime.py 현재 Host 권한으로 로컬 파일·실행 접근
│  ├─ hosted_files.py         로컬 graph/문서 효과·Host metadata/복구 연결
│  ├─ migration.py / pending.py 이관·백업·복원·생성 결과 재조정
│  ├─ host/                   auth·저장 allowlist·source/resource/plan·HTTP·Host CLI
│  └─ efficiency/             SourcePin·graph·문서·context·reuse·result·control·batch
├─ integrations/{codex,claude,opencode}/ 제품별 훅·설치 연결
├─ skills/proj-mgmt-tool/      에이전트의 사용 절차·참조
├─ scripts/                   패키징·검증 도구
├─ tests/                     계약·통합·장애·설치 시험
└─ docs/
   ├─ 01~04 단계 문서         요구·범위·완료 조건
   ├─ phase1/phase2/phase3/phase4/ 단계별 상세 계약·검증 기록
   └─ pmt-docs/               공통 구조·규칙·프로젝트 graph
```

플러그인 배포물과 사용자 데이터는 분리한다. data root에는 SQLite·리소스·임시 자료를, config root에는 환경/설치 설정을 둔다. 경로 선택은 명시 인수 → PMT 환경 변수 → OS 기본값 순이다. Windows 기본값은 각각 `%LOCALAPPDATA%/pmt-v3`, `%APPDATA%/pmt-v3`다. slug는 표시·정리용이고 식별은 고정 ID로 한다.

## 기술 선정

| 기술 | 역할·선정 이유 | 제한 |
|---|---|---|
| Python ≥3.13 + 표준 라이브러리 | 기존 본체·CLI·프로세스·JSON·파일 처리 재사용, 설치 의존성 최소화 | 장시간 외부 실행 중 DB 트랜잭션을 유지하지 않음 |
| SQLite | 로컬 상태·이력·점유·Queue의 원자적 변경 | 다중 작성자는 짧은 트랜잭션으로 조정; 네트워크 공유 파일로 운영하지 않음 |
| UTF-8 JSON | CLI·runner 계약과 Git graph 교환 | 버전·크기·필드 검증 필수; 대용량 본문은 참조 |
| Markdown + Git | 사람이 읽는 원칙·구조·결정, 변경 비교·기준 커밋 | 실행 상태 저장·작업 lock의 대체 수단으로 사용하지 않음 |
| JavaScript 제품 연결부 | 현재 OpenCode 플러그인 진입점 | 업무 규칙은 Python 본체로 위임 |
| pytest / setuptools | 기존 시험·Python 패키징 방식 유지 | 시험 의존성과 운영 의존성 분리 |
| 선택 설치 FastAPI/Pydantic·Uvicorn | Host 저장 API의 형식 검증·OpenAPI·ASGI 실행 | 클라이언트 기본 의존성이 아님; Git/runner 원격 호출 금지 |

클라이언트 기본 런타임은 Python 표준 라이브러리·SQLite다. Host는 `host` extra로 FastAPI/Pydantic·Uvicorn을 설치한다. 현재 Core는 0.4.0, SQLite는 schema 5이며 schema 4 원본을 백업한 뒤 continuity 테이블을 추가한다. graph schema 1과 protocol 1은 유지한다. 로컬 실행은 승인된 native handoff와 확인된 Claude/Codex CLI 경로에 한정한다. SDK·직접 모델 API·원격 runner는 제외한다. [Host 연결 계약](../phase3/host-api-contract.md)과 [4단계 연결 계약](../phase4/runtime-contract.md)은 실제 endpoint와 인증·파일/프로세스 경계를 설명한다. 현재 검증은 Windows 로컬 HTTPS이며 외부/Linux/proxy 수용을 뜻하지 않는다.

## 기능별 책임과 입출력

| 모듈 | 입력 → 출력 | 책임·연관 부분 |
|---|---|---|
| 진입점·서비스 | 검증된 요청 → 공통 응답 | CLI/훅 호출을 유스케이스에 연결; 제품 출력 규칙은 adapter가 변환 |
| 저장·상태·조회 | ID/기대 revision/변경 의도 → 상태·이력·문맥 | SQLite의 유일한 상태 변경 경계; planning/execution도 이 경계 사용 |
| 검증·리소스 | 정의·대상 지문·실측 결과 → 증거 참조·재사용 판정 | 완료 판단 근거 제공; 모델의 성공 주장만으로 pass 생성 금지 |
| planning | 요구·가용 자원·사용자 결정 → 두 트리·명세·계획·문서/graph | 목표·대전제와 방법의 추적 관계 보존; 실행은 하지 않음 |
| routing | 역할·가용 능력·정책·필요 권한 → 모델·runner·선정 이유 | 실제 연결·지원 범위로 선택; 실행/재시도는 execution 책임 |
| execution | 승인 Step·의존성·실행 정책 → job/run·진행·검토 대기 결과 | Queue·범위 lock·취소·재시도·결과 저장·메인 통지 |
| runners | 실행 요청 → handle·상태·결과·취소 확인 | 제품별 기술 차이만 변환; 목표 변경·Done 판정·독자 재배정 금지 |
| reconciliation | 기준 커밋·현재 Git·관련 문서/계획 → 영향·갱신·검토 기준 | 점유 후 실행 직전 최신화; planning/verification에 무효화 범위 전달 |
| diagnostics | 허용된 관찰 필드 → 구조화 로그·쓰기 실패 신호 | 비밀/본문 제외; 업무 이력과 분리 |
| Phase 3 efficiency | SourcePin·delta·manifest/ref → 로컬 graph/document/context/reuse/result/control 상태 | `phase3.py`는 별도 로컬 CLI registry이며 Host API allowlist가 아님; SQLite 업무 상태와 Git 원본을 분리 |
| Phase 4 continuity | 현재 DB·실제 client source·확정 사건·변경/근거 refs → immutable basis/checkpoint·재개/정렬 결과 | 개요는 실행 권한이 아니며 source·private 상세는 현재 run/claim으로 확인; 원본 pass나 작업 완료 상태를 추정해 변경하지 않음 |
| Host·HttpStore | 인증된 저장 intent·source/resource refs → 현재 권한으로 판정한 envelope/receipt | Host가 공유 Queue·lock·결과·metadata의 권위 원본; 클라이언트 DB fallback 금지 |
| 연결·로컬 실행 | 기기별 profile/mapping·Host run/context → 로컬 실제 관찰·native 요청·opaque handle | 파일 접근 전 현재 권한/SourcePin 확인; argv/PID/환경변수는 로컬 |
| client 모델 설정·plan metadata | 로컬 정책/가용 능력·F4 완료 refs → route·current published plan ref | ConfigRoot 전용 모델 설정과 Host review 게시를 분리; 구현 Step은 현재 plan을 요구 |
| 이관·pending | 고정 version/hash 세트 또는 이미 생성된 결과 → 복원/재처리/충돌 근거 | 원본 보존·빈 target·exact request replay; 신규 offline 점유 금지 |

의존 방향은 `adapter → service → 업무 모듈 → 저장/리소스`다. 업무 모듈은 제품별 훅 형식을 알지 않는다. 2단계에서는 execution이 routing·runner·reconciliation을 조합하며, runner에서 execution을 재귀 호출하지 않는다. 같은 프로세스의 모듈 간에는 명시적인 Python 함수/타입 계약을 사용하고 내부 HTTP 호출을 만들지 않는다.

## 확장 방식

- 새 제품: capability 조회·훅 변환·runner 구현·적합성 시험을 추가한다. 저장 규칙을 제품마다 복제하지 않는다.
- 새 모델: provider/model/인증/지원 기능을 등록한다. 역할과 모델명을 코드에 고정하지 않는다.
- 새 작업 종류: 종류별 검증·완료 조건을 확장한다. Work/Item/Step 의미와 실행 시도 식별자는 유지한다.
- 저장 서버: `LocalStore`와 `HttpStore`가 같은 operation 결과·조회·호환 포트를 제공한다. 저장 위치는 profile의 단일 mode로 선택한다. Host는 별도 allowlist와 현재 인증·scope·revision·요청 지문을 검증한다. 연결 실패는 오류이며 다른 DB로 자동 전환하지 않는다.
- 계약/스키마 변경: 버전·이관·구버전 입력 처리·백업/복원·소비자 시험을 함께 변경한다. 만능 플러그인 시스템을 먼저 만들지 않고 실제 두 번째 구현이 필요한 경계부터 분리한다.
