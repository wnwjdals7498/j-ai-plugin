# 제품 연결과 플러그인화 계획

공통 본체·CLI는 한 번 구현하고 제품별 연결부만 다르게 만든다. 스킬은 작업 절차, 연결부는 native 입력/출력 변환, 본체는 데이터·상태·오류를 담당한다. 설치 검증은 1단계의 마지막 필수 작업이다.

## 제품별 연결 계약

| 제품 | 공식 규약에 따른 초기 경로 | 공통 의미로 변환 | 실측할 부분 |
|---|---|---|---|
| Codex | command hook, skills, portable plugin과 필요한 제품 설정 | SessionStart→session_started, UserPromptSubmit→prompt_submitted, Stop→turn_stopped. 지원될 때 SubagentStop 수집 | Windows Python 경로·실제 event ID·hook 신뢰/enablement·지원 표면 |
| Claude Code | command hook, skills, 제품 manifest·설치 규약 | SessionStart·UserPromptSubmit·Stop·SessionEnd의 의미별 이벤트 | 실제 선택 입력·timeout·plugin root·업데이트 후 연결과 데이터 보존 |
| OpenCode | 선택 stable 버전의 JS plugin·공식 skill discovery | 문서화된 session.created·session.idle·session.error·tool.execute.after 등 | 실제 payload·Python 호출·로그 반환·설치/업데이트. 지시는 지원 입력 경로 또는 명시 PMT 저장 |

근거: [Codex 훅](https://learn.chatgpt.com/docs/hooks), [Codex 패키징](https://developers.openai.com/plugins/build/plugins), [Claude 훅](https://code.claude.com/docs/en/hooks), [Claude manifest](https://code.claude.com/docs/en/plugins-reference), [OpenCode stable plugin](https://opencode.ai/docs/plugins/), [OpenCode skills](https://opencode.ai/docs/skills/).

공통 이벤트 후보는 `session_started, prompt_submitted, turn_stopped, session_idle, session_ended, subagent_stopped, tool_completed, hook_error`다. fixture로 의미와 ID를 확인한 항목만 해당 제품의 지원 표에 넣는다. 모든 제품이 같은 이름·payload·선택 hook을 제공한다고 가정하지 않는다.

Stop/idle/session end는 업무 Done이 아니다. tool 완료도 그 도구의 종료 정보이며 전체 작업 성공이 아니다. 사용자 선택·결정은 명시 PMT 입력으로 저장하고 이벤트 본문에서 임의 추론하지 않는다.

OpenCode는 초기 구현에서 검증한 stable 규약을 고정한다. 별도 v2 규약·beta 표면의 동작을 stable 지원 사실과 섞지 않는다. 모델 분배·외부 제공자 API 연결은 P6의 범위가 아니다.

## 본체에 전달하는 입력과 반환

| 방향 | 계약 |
|---|---|
| native → adapter | 공식 이벤트 원본의 허용 필드, 제품/연결부 버전, session/turn/tool/event 식별 |
| adapter → PMT | [공통 JSON](contracts.md#요청응답)의 record_event/read_context. 최초 생성 ID를 재전송함에 보존 |
| PMT → adapter | 성공·충돌·일시 실패·warnings·관련 결과 ID |
| adapter → native | 해당 제품의 승인된 반환 형식. 기록용 hook은 기본적으로 에이전트 진행을 바꾸지 않음 |

전체 transcript를 읽어 지시/선택을 맞추지 않는다. `transcript_path`나 제품 내부 저장 파일을 안정적 API로 간주하지 않는다. 원문 식별자가 부족하면 별도 재전송함에서 실제 발생마다 ID를 발급·보존하고 한계와 판정 근거를 기록한다.

native occurrence ID가 있으면 제품·설치 instance·session·종류와 함께 namespace로 사용한다. 없으면 연결부가 발생 UUID와 최소 envelope를 pending에 먼저 보존한 후 CLI로 보낸다. replay는 그 ID를 유지한다. pending 저장 전 실패나 안정 ID 없는 native 재호출 자체는 동일 발생인지 입증할 수 없으므로 `dedup_scope=adapter_replay_only`와 미보존 상태를 표시한다. payload 내용 hash로 두 번의 정당한 같은 지시를 합치지 않는다.

## 훅 처리와 실패 반환

- 기록용 hook은 짧은 JSON CLI 호출 또는 최소 pending 기록으로 끝낸다. 장시간 모델 작업·테스트·문서 생성을 hook 안에서 기다리지 않는다.
- 일반 기록 hook의 목표는 2초 이내다. native의 더 짧은 상한이 있으면 그 이하의 예산을 적용한다. 이 값은 성능 목표이며 fixture와 실제 runtime에서 측정한다.
- DB busy 재시도 예산은 hook deadline보다 작게 제한한다. 초과하면 같은 ID의 pending 이벤트를 남기고 native에 실패/대기를 표시한다.
- 기록 성공의 PMT stdout은 연결부 내부에서 소비한다. native에 PMT JSON을 그대로 출력하지 않는다. 문맥 제공 hook은 공식 native 문맥 필드로만 반환한다.
- PMT 실패를 native block/continuation 명령으로 오인시키지 않는다. 기록 실패로 작업을 계속시키는 경우에도 데이터 저장 성공을 주장하지 않는다.
- pending은 데이터 root의 별도 재전송 영역에 필요한 정규화 envelope만 저장한다. replay는 같은 event/request ID로 수행하고 완료된 pending은 정리한다.
- 원 DB·pending 모두 실패하면 최소 native/stderr 경고로 미보존을 표시한다. 완전한 무손실 수집을 보장한 것으로 보고하지 않는다.

실제 timeout·hook 제어·로그 매핑은 제품 버전별 근거와 HOOK-01~03·INSTALL-05로 확인한다. 특히 같은 이름의 native 필드가 같은 의미라고 가정하지 않는다.

| 초기 native 반환 정책 | 정상 기록 | PMT 실패·pending/미보존 | 시험 관찰 위치 |
|---|---|---|---|
| Codex command hook | exit 0, 기록용 stdout 비움 | 에이전트를 막지 않는 exit 0 + 지원되는 native warning 필드. 미지원이면 정제 stderr | native 출력·stderr·hook 실행 기록·pending 상태 |
| Claude command hook | exit 0, 기록용 stdout 비움 | exit 0 + 지원되는 systemMessage 등 경고, 미지원이면 정제 stderr. block/continuation 결정은 내보내지 않음 | native JSON·stderr·제품 hook 결과·pending 상태 |
| OpenCode 이벤트 callback | 정상 resolve, PMT CLI stdout 내부 소비 | 예외를 제품 작업 흐름으로 던지지 않고 정상 반환 + 제품의 공식 plugin 로그 warning | callback 결과·제품 로그·pending 상태 |

warning의 실제 허용 field·출력 형태는 고정한 제품/event 버전의 fixture로 확인한다. 그 버전이 지원하지 않으면 fallback 채널을 지정하고 HOOK 시험의 기대값으로 등록한다. 훅 기록 실패를 선택 승인·작업 완료로 대체하지 않는다.

## 스킬의 역할

시작 시 관련 문맥 조회 → 요구/선택/결정 명시 저장 → claim 성공 후 작업 → 결과·검증 근거 확인 → finish 또는 pause/blocked 기록을 안내한다. 실제 CLI 도구가 없는 환경은 지원 불가 상태를 명시한다.

규칙은 공통 스킬 내용으로 유지하고, 제품별 discovery·namespace·명령 실행 방식만 연결한다. 미래 2단계의 같은 에이전트 서브 우선 원칙은 포함할 수 있으나 이 단계에서 모델 분배기를 구현하지 않는다.

## local setup의 입출력

| 입력 | 출력 |
|---|---|
| 제품·scope, Python 실행 경로, data root, core/schema/adapter 버전 | DB·환경·설치 ID, 실제 사용 데이터 경로, 기능별 ready/blocked, 호환성·쓰기/읽기 점검 |

data root는 명시한 `--data-root` → `PMT_DATA_ROOT` → OS별 PMT 사용자 데이터 기본값 순으로 정한다. setup은 선택 경로를 연결부 설정에 보존한다. 예시 기본값은 Windows `%LOCALAPPDATA%/pmt-v3`, Linux의 사용자 data 디렉터리, macOS 사용자 Application Support다. 실제 검증된 OS만 지원으로 표시한다.

설치 경로·cwd·제품 임시 plugin data 폴더를 DB 정체성으로 쓰지 않는다. 여러 제품이 같은 PMT를 사용할 때 동일한 명시 data root를 사용한다. 패키지와 데이터 경로·버전은 각각 기록한다.

환경 프로필 UUID는 별도 사용자 PMT 설정에 보존하고 제품별 installation ID와 구분한다. setup 재호출에서 같은 프로필·data root는 기존 DB ID를 유지한다. root를 바꾸면 새 DB 선택인지 기존 DB 이동인지 명시적으로 구분하며 자동 이동·자동 병합하지 않는다. profile/environment ID·data root resolver는 공통 본체가 제공하고 연결부가 각자 재정의하지 않는다.

Python 실행 가능성·sqlite3 로드·스키마 호환·경로 쓰기 권한·실제 CLI 왕복을 점검한다. 없는 runtime을 있는 것으로 표시하거나 기존 사용자 설정을 묵시적으로 덮어쓰지 않는다. runtime 설치가 필요하면 확인된 공식 경로와 조치를 안내한다.

## 패키지 경계와 배포물

- 공통 core·CLI·스킬 내용은 한 원천으로 버전 관리한다. 제품별 manifest·hook/plugin 등록과 포함 자산은 그 제품 규격으로 제공한다.
- 패키지에는 실행 코드·등록 정보·스킬·공식 의존 안내·버전·배포물 hash를 포함한다. 사용자 DB·리소스·진단 로그·실행 비밀값은 제외한다.
- 개발 checkout 절대 경로·개인 Python 설치 경로를 배포물에 고정하지 않는다. package-root와 setup에서 확인한 runtime을 사용한다.
- 설치물·제품 어댑터·DB schema·CLI protocol 버전을 구분한다. 기존 사용자 데이터의 이관·호환성을 명시한다.
- Codex plugin은 설치/enable만으로 native hook의 현재 정의가 자동 신뢰되지 않는다. 공식 신뢰 절차와 실제 기능 상태를 setup·시험에서 확인한다. [공식 안내](https://developers.openai.com/plugins/build/plugins).
- Claude의 공식 package 검사와 각 제품 설치 경로를 사용한다. OpenCode의 공식 stable local/npm plugin 경로를 대상 버전에서 검증한다.

## 설치 생명주기

제품은 자신의 package 업데이트 시점을 관리할 수 있다. PMT가 교체 직전 callback을 받는다고 가정하지 않는다. 사용자가 통제하는 업데이트에서는 사전 백업을 권장하고, 새 PMT의 setup/첫 호출은 쓰기 전에 schema 호환을 검사한다. 이관이 필요하면 DB+리소스 백업을 확보한 후 수행한다. 지원 밖 schema에는 쓰지 않고 복구 조치를 반환한다.

| 동작 | 과정·성공 조건 | 시험 |
|---|---|---|
| 최초 설치 | 공식 등록 → runtime·data 설정 → native 활성화 → 실제 문맥·기록 왕복 | PKG-01, INSTALL-01 |
| 새 세션 | 같은 data root에서 현재 상태·근거 조회, 실제 이벤트 확인 | INSTALL-02, INSTALL-05 |
| 업데이트 | 제품의 공식 package 갱신 → 새 버전 setup/첫 호출 호환 검사 → 이관 전 백업 → 필요한 이관 → 활성화·기록/조회 | INSTALL-03 |
| 재설치 | 설치 코드·연결 등록만 복구하고 동일 DB·리소스 재사용 | INSTALL-04 |
| 제거 | 대상 제품 등록·package 제거 확인, 사용자 data는 보존 | INSTALL-04 |

native trust가 다시 필요하면 그 상태를 ready로 숨기지 않는다. 이관·업데이트 실패 시 호환되는 코드와 백업의 복구점을 보존한다. 기존 사용자 DB를 설치 시험 데이터로 사용하지 않는다. 사용자 데이터 삭제 명령은 초기 설치 lifecycle 기본 동작에 포함하지 않는다.

package 원복과 DB 복원은 별도 조치다. 대상 제품의 공식 이전 버전 설치·등록 기능을 확인한 경우에만 코드 원복 가능으로 보고한다. DB는 복원 검증을 통과한 백업을 사용한다. 어느 경로가 미지원/미확인이면 그 상태와 데이터 보호 결과를 명시하고 자동 복구 성공을 주장하지 않는다.

| 제품의 설치·제거 범위 | 사용할 공식 경로 | 측정·보존 경계 |
|---|---|---|
| Codex | 개인 local/Git marketplace 또는 대상 버전의 공식 package 설치/등록 | 격리한 등록 scope의 enable/trust·참조 제거 확인. 관리되는 cache 삭제를 자동 보장하지 않음 |
| Claude Code | 공식 marketplace/plugin 설치·검증·update/uninstall | user/project 중 시험 scope를 고정. manifest 검사·등록 제거·재설치 확인. 공통 PMT data는 별도 |
| OpenCode stable | 문서화된 local JS plugin 또는 npm/config plugin 등록 | 시험 디렉터리/config entry의 로딩·해제 확인. npm/Bun cache와 PMT 사용자 데이터는 구분 |

각 경로의 실제 명령·설정·제품 버전·프로필 scope·남는 cache/설정은 P8/P9 입력/출력과 설치 증거에 기록한다. 공식 API가 확인되지 않은 부분은 수동 검증 경로 또는 blocked로 두고 임의 내부 cache를 조작하지 않는다.

## 제품별 증거와 완료

`product/version, adapter/core/schema/protocol version, package hash, profile/data alias, installation_id, native trust/enablement, command/exit, event/session ID, before/after DB ID·artifact hash, result/blocked reason`을 남긴다.

fixture 성공은 native 설치 성공이 아니다. 지정한 세 제품에서 실제 기록·새 세션 조회·업데이트·제거 후 재설치가 확인돼야 G3가 통과한다. 특정 제품이 없으면 필요한 설치·권한·지원 조건을 기록하고 해당 시험을 blocked로 둔다.
