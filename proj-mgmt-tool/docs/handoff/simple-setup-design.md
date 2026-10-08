# PMT 간편 설정·자동 운영 설계 (Claude Code 우선)

2026-10-08. 상태: 설계 초안. 구현·검증 결과는 별도로 기록한다.

## 문제

0.4.1 hosted 설치를 실제 개발 서버(yss)에 적용한 결과, 사용자가 직접 해야 하는 작업이 너무 많았다.

| 단계 | 현재 방식 |
|---|---|
| Host 연결 | JSON 작성 후 `pmt storage configure`, CA 파일 별도 설치 |
| credential | `~/.bashrc` 수정으로 셸 전체에 주입 |
| 프로젝트 연결 | 저장소마다 `.claude/settings.local.json`에 `PMT_*` 작성 |
| 작업 기록·점유 | `request_id`·`expected_revision`·`claim_ref`·actor를 넣은 JSON 직접 작성 |
| 완료 | Step·execution run·verification·evidence를 거치는 여러 operation. 실제 모델 세션도 완료하지 못함 |

## 요구

1. 초기 설정은 Claude Code `/plugin` 설정 화면에서 값을 입력하는 것으로 끝난다.
2. 이후 연결 구성·프로젝트 연결·요청 식별자·revision·점유 참조는 플러그인이 처리한다. 사람과 AI는 짧은 명령만 쓴다.
3. 명령 하나(`pmt check`)로 연결·인증·기록·조회·재전송을 끝까지 확인한다.
4. 기존 Core 0.4.0 / SQLite 5 / graph 1 / protocol 1 계약과 Host는 바꾸지 않는다. 클라이언트 연결부만 바꾼다.

## 실측 근거 (Claude Code 2.1.292, Rocky Linux 8.9)

- `userConfig`의 `sensitive` 값은 `~/.claude/.credentials.json`(600)의 `pluginSecrets`에 저장되고 `settings.json`에는 남지 않는다. Hook 프로세스에는 `CLAUDE_PLUGIN_OPTION_<KEY>`로 정확한 값이 전달된다.
- exec-form Hook `command`의 `${user_config.KEY}`는 설정 후 치환된다. `default`는 적용되지 않으므로 필수 값은 `required`로 둔다.
- `CLAUDE_ENV_FILE`(SessionStart)에 쓴 `export`는 같은 세션의 이후 Bash 명령에 적용된다(공식 문서).
- 플러그인 `bin/`의 파일은 플러그인이 켜진 동안 Bash 도구 PATH에 들어간다(공식 문서).

## 설계

### 1. 설정 화면 (userConfig)

| 키 | 종류 | 설명 |
|---|---|---|
| `python_path` | file, required | Python 3.13 이상 절대 경로 (0.4.1과 동일) |
| `host_url` | string, required | 예: `https://222.234.220.199:8765` |
| `host_ca_file` | file | Host 공개 Root CA PEM 절대 경로. 공개 CA면 비움 |
| `device_id` | string, required | Host가 발급한 기기 UUID |
| `namespace_id` | string, required | Host namespace UUID |
| `actor` | string, required | Host가 발급한 actor |
| `device_credential` | string, sensitive, required | Host 기기 credential |

ConfigRoot/DataRoot는 사용자에게 묻지 않는다. 기본값 `${XDG_CONFIG_HOME:-~/.config}/pmt`, `${XDG_DATA_HOME:-~/.local/share}/pmt/data`를 쓴다.

### 2. SessionStart 자동 처리

1. 설정값이 비어 있으면 아무것도 바꾸지 않고 "`/plugin`에서 설정하라"는 한 줄만 표시한다.
2. ConfigRoot profile이 없거나 설정값과 다르면 현재 hash로 `storage configure`(CAS)를 실행한다. credential은 Hook 환경의 `CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL`을 `PMT_HOST_CREDENTIAL`로 넘겨 probe한다. 실패하면 설정을 게시하지 않고 오류 code만 표시한다.
3. 현재 작업 폴더의 Git root·branch를 확인한다. 이미 mapping이 있으면 그 project를 쓴다. 없으면 Host가 이 기기에 허가한 project가 하나일 때만 자동 연결을 제안하고, 여러 개면 `pmt link <project>`를 안내한다. 추정으로 연결하지 않는다.
4. `CLAUDE_ENV_FILE`에 `PMT_CONFIG_ROOT`, `PMT_DATA_ROOT`, `PMT_PYTHON`, `PMT_SCOPE_ID`(연결된 경우), `PMT_HOST_CREDENTIAL`을 쓴다. 이후 Bash 도구의 `pmt` 명령이 이 값을 쓴다.
5. 연결된 project면 기존처럼 bounded overview를 additionalContext로 넣는다.

이로써 `~/.bashrc` 수정과 저장소별 `settings.local.json`은 필요 없어진다.

### 3. `pmt` 명령 (플러그인 `bin/`)

| 명령 | 동작 |
|---|---|
| `pmt check` | 연결·인증 호환 확인, 전용 진단 record 1건 쓰기→읽기→같은 요청 재전송 확인. 결과를 표로 출력 |
| `pmt link [project]` | 현재 checkout을 project에 연결(mapping 추가, CAS) |
| `pmt status` | 현재 project의 Work/Item 목록·상태·점유자 |
| `pmt add work "<제목>"` / `pmt add item <work> "<제목>" --criteria "<기준>"` | 기록 생성 |
| `pmt start <item>` | 최신 revision을 읽고 점유. claim 참조는 DataRoot의 세션 상태에 저장 |
| `pmt done <item> --test "<명령>"` | 테스트 명령 실행 → 결과를 evidence로 등록 → verification 기록 → 완료. 실패하면 완료하지 않고 출력 요약을 보여준다 |
| `pmt pause <item> --next "<다음 할 일>"` | 점유 해제(Paused) |

공통: actor·scope·session은 설정과 세션에서 채운다. `request_id`는 명령마다 새로 만들고, 재시도는 DataRoot에 저장한 같은 요청을 다시 보낸다. revision 충돌이면 최신 상태를 다시 읽어 보여주고 멈춘다(자동 덮어쓰기 없음). 출력은 사람이 읽는 짧은 표, `--json`이면 원 응답.

### 4. 유지하는 원칙

- Stop/idle로 완료하거나 점유를 풀지 않는다. 다른 세션 점유를 회수하지 않는다.
- hosted 실패를 local DB로 대체하지 않는다.
- credential을 출력·로그·shared metadata에 넣지 않는다.

## 미확정

- `pmt done`의 hosted 내부 절차(Step·execution run·verification). 실제 코드 경로 조사 후 확정한다.
- `CLAUDE_ENV_FILE` 값이 서브에이전트 Bash에도 적용되는지. 실측 필요.
- Codex는 같은 `pmt` 명령을 쓰되 설정 화면·Hook 주입 방식이 다르다. Claude 완료 후 별도 설계.
