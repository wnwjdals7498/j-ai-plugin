# 폴더·기술·모듈

## 폴더 구성

아래는 현재 2단계 코드 배치다. 공개 계약과 책임 경계를 유지하며 [실측 범위](../phase2/implementation-status.md)를 확인한다.

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
│  ├─ routing/                가용 자원·모델/실행 경로 선택
│  ├─ execution/              Queue·시도·결과·취소·범위 점유
│  ├─ runners/                subagent·Claude/Codex CLI 연결
│  ├─ reconciliation/         Git 변화·문서/계획 영향 분석
│  ├─ steps.py / operations.py 지시·검토·운영·보존
│  └─ phase2*.py              operation 연결·공통 경계·이관 DDL
├─ integrations/{codex,claude,opencode}/ 제품별 훅·설치 연결
├─ skills/proj-mgmt-tool/      에이전트의 사용 절차·참조
├─ scripts/                   패키징·검증 도구
├─ tests/                     계약·통합·장애·설치 시험
└─ docs/
   ├─ 01~03 단계 문서         요구·범위·완료 조건
   ├─ phase1/phase2/         단계별 상세 계약·검증 기록
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

2단계는 Python 표준 라이브러리·SQLite를 유지하며 런타임 의존성을 추가하지 않았다. 최신 사용자 결정에 따라 실행은 네이티브 서브에이전트·Claude/Codex CLI에 한정한다. SDK·직접 모델 API는 후속 제안이다. [3단계](../03-hosted-storage.md)는 Python·FastAPI/Pydantic·Uvicorn과 HTTPS JSON 저장 API를 계획한다.

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

의존 방향은 `adapter → service → 업무 모듈 → 저장/리소스`다. 업무 모듈은 제품별 훅 형식을 알지 않는다. 2단계에서는 execution이 routing·runner·reconciliation을 조합하며, runner에서 execution을 재귀 호출하지 않는다. 같은 프로세스의 모듈 간에는 명시적인 Python 함수/타입 계약을 사용하고 내부 HTTP 호출을 만들지 않는다.

## 확장 방식

- 새 제품: capability 조회·훅 변환·runner 구현·적합성 시험을 추가한다. 저장 규칙을 제품마다 복제하지 않는다.
- 새 모델: provider/model/인증/지원 기능을 등록한다. 역할과 모델명을 코드에 고정하지 않는다.
- 새 작업 종류: 종류별 검증·완료 조건을 확장한다. Work/Item/Step 의미와 실행 시도 식별자는 유지한다.
- 저장 서버: 서비스 유스케이스를 저장 연결 경계로 감싼다. 아직 없는 `LocalStore/HttpStore` 추상화를 구현 완료로 표현하지 않는다. 원격에서도 revision·멱등성·점유 판정은 서버가 권위 있게 처리해야 한다.
- 계약/스키마 변경: 버전·이관·구버전 입력 처리·백업/복원·소비자 시험을 함께 변경한다. 만능 플러그인 시스템을 먼저 만들지 않고 실제 두 번째 구현이 필요한 경계부터 분리한다.
