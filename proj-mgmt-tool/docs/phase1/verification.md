# 시험·실동작 확인 계획

아래는 수행할 시험의 정의다. 작성 시점에는 PMT 구현·시험을 실행하지 않았다. 문서 검토, fixture 시험, 실제 본체·제품 설치 시험의 결과를 서로 대체하지 않는다.

## 결과와 증거

- `pass`: 지정 대상·환경에서 실행돼 기대값을 충족하고 증거가 보존됨.
- `fail`: 실제 실행 결과가 기대값을 충족하지 않음.
- `blocked`: runtime·제품·권한·활성화·선행 작업이 없어 실행/확인 불가.
- `not_run`: 아직 수행하지 않은 예정 시험.
- `stale`: 과거 pass가 현재 대상·조건·근거에 적용되지 않음.
- `aborted`: 실행을 시작했으나 중단돼 완료하지 못함.
- `reused`: 현재 조건에서 원 성공 execution ID를 참조하는 판정. 새 실행·새 pass 횟수로 계산하지 않음.

시험마다 정의 버전, 전제 데이터, 입력/fixture, 명령, 대상 commit·dirty 지문, OS·Python·SQLite·제품·연결부 버전, 시작/종료 시각, 종료 코드, 기대값/실측값, 증거 ID·hash를 저장한다. 자세한 필드는 [로그 규약](logging.md#증거-manifest)을 따른다.

pytest의 임시 경로·fixture·실패 주입을 사용하고 각 시험은 자신의 data root를 갖는다. 프로세스 경합·CLI·재시작에는 실제 자식 프로세스를 사용한다. [격리 fixture](https://docs.pytest.org/en/stable/how-to/tmp_path.html), [실패 주입](https://docs.pytest.org/en/stable/how-to/monkeypatch.html), [subprocess](https://docs.python.org/3/library/subprocess.html).

## DB·상태·문맥 시험

| ID | 전제·입력 | 기대값·합격 기준 | 필수 증거 |
|---|---|---|---|
| DATA-01 | 빈 data root setup 두 번, 다른 프로세스로 재조회 | 스키마 정상, DB·환경 ID 유지, 기록 재조회 일치 | runtime/schema·ID·행 수 |
| DATA-02 | 지원 구버전 DB 이관, 이관 중 실패, 지원 밖 새 버전 입력 | 이관 성공 또는 명시 거부. 실패 시 기존 DB·version 보존 | 전후 schema·hash·오류 단계 |
| DATA-03 | 등록 범위·부모 관계·repository URL 표현·fork 후보 입력 | 유효 관계만 저장. 표현 정규화와 별도 repo 정체성 구분. 자동 병합 없음 | 범위/관계·저장/거부 결과 |
| TX-01 | 상태 변경 뒤 업무 이벤트/요청 결과 저장 지점에 오류 주입 | 상태·revision·이벤트·요청 성공 기록 전부 rollback | 전후 DB 조회 + rollback 로그 |
| IDEM-01 | 같은 request ID·의미 필드, JSON key 순서만 변경하여 재호출·동시 최초 호출 | 원 응답·원 revision·생성 ID 유지, 변경·이벤트 1회 | 두 응답·revision·행 수 |
| IDEM-02 | 같은 request ID에 다른 유효 operation/actor/session/대상/revision/payload/context_refs 사용 | 의미 지문 충돌, 기존 상태·원응답 불변 | 필드별 입력·응답·DB 비교 |
| EVENT-01 | 다른 request ID로 같은 실제 event ID를 재전송 | 이벤트 한 건·원 반영 결과 유지 | normalized event·행 수 |
| EVENT-02 | 동일 prompt 내용에 서로 다른 실제 event ID, 지연·역순 종료 | 두 행동은 두 이벤트. 수신 순서로 상태 역행/자동 Done 없음 | ID·수신 순서·Item 상태 |
| REV-01 | 같은 expected_revision으로 두 독립 프로세스 동시 변경 | 한 변경만 성공, 다른 요청은 revision conflict | 시작 동기화·양쪽 응답·최종 revision |
| CLAIM-01 | 같은 Planned Item에 독립 두 프로세스를 barrier로 동시에 착수 | 유효 점유 성공자 정확히 하나, 실패자는 충돌 | 두 PID/session·응답·owner/token 존재 확인 |
| CLAIM-02 | 다른 session 또는 이전 token으로 release/finish | 소유권 충돌, 현재 상태·소유자 보존 | 원문 token 없는 비교 로그·DB 상태 |
| CLAIM-03 | 오래된 heartbeat, 작업자 종료·격리 확인 후 명시 회복 | 시간만으로 재점유 없음. 회복 이유·새 token 존재, 이전 token 거부 | 과정·owner 상태·이벤트 |
| DONE-01 | 결과 누락·기준 unknown/fail·증거 누락·미완료 자식으로 finish | 완료 기준 위반, Done/완료 이벤트 없음 | 입력 분류·오류·DB 조회 |
| DONE-02 | 현재 점유·revision·결과·모든 기준의 유효 근거로 finish, 같은 요청 재호출 | Done과 완료 이벤트 1회, 점유 해제, 재호출 원결과 | 응답·기준 포함 관계·DB 조회 |
| DECISION-01 | 사용자/위임 메인 선택·직접 작성·AI 위임·대체 대상 명시 저장, 재전송·stale revision | 명시된 선택 주체·근거 보존, 현재/파기 관계 일치, 중복 없음·충돌 거부 | 입력·actor·대체 관계·event·revision |
| READ-01 | 여러 범위와 큰 이력, 선택 Item·query·limit/cursor | 범위 밖 자료 제외, 안정 순서·pagination, 현재 결정·다음 작업 조회 | 예상 fixture·반환 ID·revision |
| READ-02 | 4,500자 예산을 넘는 관련 문맥, 오래된 결정·주의 존재 | 예산 준수와 축소/상세 조회 표시, 현재 기준·핵심 경고를 숨기지 않음 | 생성 결과·길이·필수 정보 확인 |

## 검증 재사용·리소스·백업 시험

| ID | 전제·입력 | 기대값·합격 기준 | 필수 증거 |
|---|---|---|---|
| VERIFY-01 | 동일 정의·실제 코드·dirty 상태·환경·의존성·명령·입력, 유효 성공 증거 | 같은 검증 ID를 후보로 반환. 다른 Item에서도 기준 포함 관계로 참조 | canonical 지문·증거 hash·판정 이유 |
| VERIFY-02 | 위 조건 각각 하나씩 변경, 범위/조건 미확인, 실행 중 파일 변경 | 재사용 불가이며 달라진 조건·unknown 이유 표시 | 조건별 fixture·mismatch/stale 결과 |
| VERIFY-03 | 과거 성공 뒤 관련 실패·철회·기준 변경 또는 증거 삭제/손상 | 과거 성공을 재사용하지 않음 | 이벤트 순서·증거 상태·불가 이유 |
| RESOURCE-01 | 허용된 파일 staging·hash 확인·ready 등록·근거 승격 | ready 파일과 DB 참조 일치, 범위·보존 분류 유지 | ID·크기·hash·DB 참조 |
| RESOURCE-02 | 파일 배치 뒤 DB 실패·프로세스 종료, 참조 파일 누락/손상 | 고아/누락/손상 진단. 완료 증거로 숨겨 사용하지 않음 | 파일 목록·DB·진단 결과 |
| RESOURCE-03 | data root 밖 경로·링크·허용 밖 파일 참조·삭제 요청 | 거부, 외부 파일·참조 중 증거 불변 | 대상 경계 검사·오류·전후 hash |
| BACKUP-01 | 기록·ready 증거 존재, maintenance 동안 경쟁 쓰기/GC 시도, backup 후 격리 restore | 경쟁 쓰기 차단, 복원 ID·관계·파일 hash 일치 | backup ID·manifest·유효성·비교 결과 |
| BACKUP-02 | 잘못된 schema·manifest·hash·누락 리소스로 restore, 백업 실패 | 명시 실패, 기존 활성 데이터 보존 | 실패 단계·기존 data hash·불일치 목록 |

검증 후보 조회 성공은 원 시험을 새로 수행한 성공이 아니다. 원 증거·조건을 연결해 `reused`로 보고한다. 지문을 만드는 함수만 모의해 조건 비교 시험을 통과시키지 않는다. 작은 실제 저장소·dirty 파일·fixture를 사용한다.

## CLI·재시작·훅·로그 시험

| ID | 전제·입력 | 기대값·합격 기준 | 필수 증거 |
|---|---|---|---|
| CLI-01 | 실제 CLI subprocess에 정상 UTF-8 JSON | 한 줄 JSON·request ID·결과, stdout에 로그 없음, exit code 일치 | stdin fixture·stdout/stderr·실제 종료 코드 |
| CLI-02 | malformed JSON·누락 필드·지원 밖 버전·상한 초과·중복 JSON key·위반 상태 | 정의된 오류 코드·필드, parse 불가이면 request ID null. 업무 상태/이력 불변, 결정적 요청 오류 기록은 계약대로 | 요청·오류·DB 전후 비교 |
| CLI-03 | 다른 cwd·공백/한글 경로·설치 위치에서 같은 data root 사용 | checkout/cwd에 의존하지 않고 동일 DB·기록 조회 | 실행 인자·경로 별칭·DB ID·결과 |
| RECOVER-01 | 실제 프로세스를 commit 전/후 응답 전 지점에서 종료 후 재시작 | commit 전 미반영, commit 후 같은 요청 원결과. DB 무결성·현재 점유 유지 | 종료 지점·프로세스·DB 검사·재호출 |
| STOP-01 | 각 제품의 Stop/idle/session-end/subagent-stop fixture | 대응 normalized event만, Item Done·결정 생성 없음 | 제품·매핑 버전·이벤트·Item 상태 |
| HOOK-01 | 제품별 정상·중복·역순·미지원 fixture, native ID 유무별 입력 | namespace/pending ID 유지, 중복 보장 범위 명시, native 반환 계약 준수 | 원 fixture 출처·정규화·request/event ID·반환 채널 |
| HOOK-02 | CLI의 DB busy·I/O 오류·timeout 주입 후 같은 이벤트 재전송 | 짧은 native 반환·pending 표식·안정 ID 보존, 회복 후 한 번만 저장 | 경과·재전송함·반영 수·native stdout/stderr |
| HOOK-03 | PMT와 재전송함 모두 쓰기 실패, 지원 밖 native payload | 기록 성공 위장 없음, event_not_persisted 또는 입력 거부가 관찰됨, 자동 완료 없음 | 오류 결과·native 알림/stderr·DB 상태 |
| LOG-01 | 비밀 토큰·transcript·사용자 경로를 포함한 테스트 payload | 허용 ID/길이/hash로 진단, 비밀 원문·전체 payload 로그 없음 | 금지값 탐색·로그 필드·정제 fixture |
| LOG-02 | 진단 로그 파일만 쓰기 실패, 업무 저장은 정상 | 업무 성공 유지·warnings/fallback 관찰, 중복 재실행 없음 | DB commit·응답·stderr |
| LOG-03 | 업무 이벤트 저장 자체에 실패 | 업무 상태도 rollback, 성공 기록 없음 | DB 상태·실패·rollback 로그 |

RECOVER-01은 정의한 프로세스 종료 시나리오를 확인한다. 실제 전원 손실·모든 파일 시스템의 내구성을 입증한 것으로 표현하지 않는다. 훅 시간 예산과 원문 매핑은 [연결 규약](adapters-packaging.md#훅-처리와-실패-반환)에 맞춘다.

## 패키지·실제품 설치 시험

아래 시험은 지정한 제품·버전별로 각각 수행한다. 데이터·프로필·작업 공간을 시험별로 격리한다. native 신뢰 설정이 필요한 경우 실제 절차와 결과를 기록한다.

| ID | 전제·입력 | 기대값·합격 기준 | 필수 증거 |
|---|---|---|---|
| PKG-01 | 배포 패키지·고정 버전·manifest, 깨끗한 checkout 외 위치 | package 경로·manifest·runtime·진입점·포함 자산 검사. 임의 로컬 절대 경로 없음 | artifact hash·검증 출력·버전 |
| INSTALL-01 | 제품의 공식 경로로 실제 설치·활성화·local setup | 공통 본체 호출·쓰기/읽기 성공, 기능별 ready/trust 상태 확인 | 제품/runtime 버전·설치/설정 결과·DB ID |
| INSTALL-02 | 첫 세션에서 작업·결정·검증 기록, 새 실제 세션 시작 | 관련 상태·다음 작업·근거 복원, 이전 기록 유지 | 두 세션 ID·문맥·리소스 hash·native 이벤트 |
| INSTALL-03 | 기록이 있는 구버전에서 제품 package 갱신·새 버전 호환 검사·schema 이관, 중간 실패 | 정상 시 데이터 유지·재활성화. 미지원 시 쓰기 차단·데이터 보호. 검증 가능한 package 원복과 DB 복원을 별도 확인 | backup·전후 버전/DB ID·실패/복구·지원 밖 상태 |
| INSTALL-04 | 패키지 제거 후 재설치·동일 data root 지정 | 원 등록 제거 확인, DB·resources 보존, 재설치 후 기록 재조회 | 등록 상태·전후 DB/근거 hash·조회 결과 |
| INSTALL-05 | 실제 지시·세션 시작·응답 종료, 명시적 선택·claim·finish | 문서화된 native 이벤트 수집. Stop/idle 뒤 미완료, 명시 finish로만 Done | 제품 입력 출처·정규화·업무 이벤트·native 출력 |

fixture로 INSTALL 시험을 대체하지 않는다. 설치 명령만 exit 0인 것도 충분하지 않다. 실제 에이전트의 문맥 조회와 훅 이벤트가 PMT에 도달해야 한다. 목표 제품 중 미실행이 있으면 전체 1단계 완료로 표시하지 않는다.

## 검증 단계

| 단계 | 필수 시험 |
|---|---|
| G0 | 계약·작업·시험·로그 간 정합성 검토. 실행 성공으로 계산하지 않음 |
| G1 | DATA, TX, IDEM, EVENT, REV, CLAIM, DONE, DECISION, READ, VERIFY, RESOURCE, BACKUP, CLI, RECOVER, STOP, HOOK, LOG 전체 |
| G2 | G1 통과 + PKG-01, 제품별 setup·의존·신뢰 절차·버전 확인 |
| G3 | G2 통과 + 목표 세 제품의 INSTALL-01~05 |

P7은 G1, P8은 G2, P9는 G3의 결과를 제공한다. 필수 시험 목록은 작업자 임의로 축소하지 않는다. 환경이 부족한 제품·버전은 blocked와 필요한 조건을 명시한다.

작업 묶음의 시험 ID는 해당 구현이 만족해야 할 계약이다. 최종 G1 실행 소유자는 P7의 메인, G2는 P8 통합 소유자, G3는 P9의 메인이다. 여러 묶음이 같은 ID를 참조해도 동일 대상의 시험을 각자 다시 실행해야 한다는 뜻은 아니다. 실행 위임 시 한 소유자와 evidence ID를 지정한다.

## 시험 결과 재사용

1. 정의·대상 commit와 관련 dirty 파일·환경·의존성·설정·입력 fixture·명령이 일치하는지 확인한다.
2. 원 시험의 범위가 현재 완료 기준을 포함하고 증거가 존재·검증되며 이후 관련 실패·철회가 없어야 한다.
3. 재사용은 원 실행 ID와 이유를 기록한다. 재사용 때문에 새 실행 횟수·새 pass를 만들어내지 않는다.
4. 차이가 확인되거나 영향 범위를 알 수 없으면 해당 시험을 재실행한다. 공유 계약 변경은 그 소비자 시험까지 포함한다.
5. 변동 사항이 없다면 이미 통과한 전체 시험을 작업자마다 반복하지 않는다. 메인은 통합 상태가 달라진 범위와 최종 설치 시나리오를 판단한다.

## 실패 조치

실패 증거·원인 가설을 먼저 보존한다. 관측·공식 근거로 원인을 좁히고 수정 원리를 기록한 뒤, 실패 재현 시험 → 관련 회귀 시험을 실행한다. 증거가 없으면 원인 확정으로 기록하지 않는다. 처리 결과와 영향받는 작업 묶음을 메인에게 인계한다.
