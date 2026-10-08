# 5단계 결정

2026-10-08. 기준: `05-plugin-split.md` §8, `codex-phase5-host.md` §3.3.

**상태: 확정. 2026-10-08 사용자 응답 “전부 제안으로 설정”에 따라 D1~D8을 채택한다. D5는 기존 인증서 등록·검사만 이번 범위에 포함하며 CA 자동 발급/새 host 의존성 추가는 제외한다.**

| ID | 확정값 | 상태 |
|---|---|---|
| D1 | `pmt-lifecycle` 유지, 표시 이름 PMT | 확정 |
| D2 | `handoff_file` 또는 `host_url`이 있으면 hosted, 없으면 local | 확정 |
| D3 | claim key 보관: env + Linux 0600 file + Windows DPAPI | 확정 |
| D4 | Claude plugin secret을 OS 저장소로 복사, `CLAUDE_ENV_FILE`에는 credential 제외 | 확정 |
| D5 | 기존 인증서 등록·검사만 구현. CA 자동 발급은 이번 범위 제외 | 확정 |
| D6 | local 짧은 명령에 done 포함 | 확정 |
| D7 | Codex connect + 공용 Hook을 B·D 단계에 포함 | 확정 |
| D8 | Windows APPDATA/pmt, LOCALAPPDATA/pmt/data. 기존 ~/.config/pmt 보존 | 확정 |

## 구현 선택

- A0: 기존 pytest 설정이 `.pmt-test/pytest-temp`를 basetemp로 사용하므로 `.pmt-test` 상위 폴더를 미리 준비한다. 최초 실패 결과를 보존한다. 제품 코드 변경 없음.
- Host 조사: 라이브 프로세스의 CIM 경로/명령행/계정이 현재 실행 권한에서 공개되지 않아 실행 중 작업의 supervisor 설정에서 비밀 없는 인수를 확인한다. DPAPI·claim key·TLS 개인키·기기 credential 파일은 열지 않는다.
- Host 조사: `/health`는 공개 CA로 TLS와 loopback SAN을 검증한다. proxy는 해당 health 요청에만 사용하지 않는다. 전역 설정 변경 없음.
- Graphify: 사용자 규칙에 따라 root AGENTS.md와 .codex/hooks.json을 설치한다. API 인증이 필요한 문서 의미 분석은 실패로 기록하고, 외부 API가 필요 없는 AST 구조 추출로 대체한다. 문서 관계까지 분석했다고 보고하지 않는다.
- A0: 기본 개발 Python은 3.14.5rc1이다. 안정 3.13.13의 별도 진단 venv `C:/PMT/src/venv-dev-313`를 준비해 동일 redirect fixture를 비교했다. 양쪽에서 기대 오류 code와 단일 요청을 관찰했으나 최초 간헐 실패 원인 해결로 판정하지 않는다. 운영 venv/설정 미변경.
- A0: 격리 wheel fixture는 --no-build-isolation을 사용하지만 새 venv에 setuptools가 없었다. pyproject build-system에 이미 선언된 setuptools>=68을 개발 venv에 설치한 뒤 영향받은 설치 시험 5개만 다시 검증한다. 런타임/프로젝트 의존성 변경 없음. 원래 제품 실패는 수정하지 않는다.

- 확정 의미: D2는 신규 프로필의 초기 모드 판정이다. 기존 local→hosted 전환과 hosted→local 전환은 명시 명령으로만 수행하며 실패 fallback은 없다. D3는 세 보관 방식의 지원 범위 결정이며 운영 키 이전 자체의 승인은 아니다.
- 버전 계약 선택: 플러그인 release 0.5.0, Python Core 0.4.1, SQLite 5, graph 1, protocol 1, Host API v1. communication §5.1의 동시 버전 갱신은 동일 숫자로 강제한다는 뜻으로 해석하지 않는다. Core major.minor 불변 규칙을 우선하며 server version은 release와 Core를 별도로 출력한다.
- A1 공개 내부 인터페이스: handoff.validate_handoff/load_handoff/build_handoff (검증된 JSON dict), host.cli.serve_host(db,args,claim_keys=None,log_config=None), AuthRegistry.list_devices() (credential/hash 제외). 상세 인계 계약을 A1 종료에 기록한다.

- D1 구현: Claude root manifest의 displayName=PMT, Codex 호환 manifest의 interface.displayName=PMT. 설치 ID는 pmt-lifecycle. 공식 문서와 현재 설치된 OpenAI 번들의 호환 manifest를 확인했다.

- A1: pmt-server script 연결만 추가하면 없는 모듈로 설치되는 문제가 있어 server_admin의 실행 가능한 version 진입점을 함께 마련한다. 해당 초기 세 파일의 소유권은 A1 완료 후 C1 작업자에게 인계한다. 나머지 서버 명령은 C1 이후 구현하며 미완료 기능을 성공으로 출력하지 않는다.
- B1 구현 선택: legacy ConfigRoot와 기존 XDG DataRoot가 함께 존재하고 PMT_DATA_ROOT가 없으면 둘 다 유지한다. 새 설치만 Windows APPDATA/LOCALAPPDATA 기본값을 적용한다. 기존 DB/pending이 새 빈 경로 뒤로 사라지는 것을 막으며 이동/병합하지 않는다.
- 동일 ConfigRoot는 하나의 Host device/actor/environment 신원을 가진다. Claude/Codex가 같은 root를 쓰면 같은 신원을 공유한다. 서로 다른 기기 신원을 발급하면 기존 PMT_CONFIG_ROOT override로 root를 분리해야 한다. setup-windows의 같은 root+별도 device 권고는 후속 문서에서 수정한다. 프로필/인증 schema 변경 없음.
- B1 검증은 실제 Core hooks.main까지 제품 인자를 전달하는 시험을 포함한다. 상위 bridge를 통째로 mock한 결과만으로 Hook 연결 완료를 판정하지 않는다.
- C1 실제 서비스 계정 ACL/키 읽기는 F1에서 확인한다. 현재 운영 supervisor는 보호 파일에서 읽은 claim key를 자식 프로세스 환경에 주입한다. 관리자 프로세스에서 같은 env 이름의 값이 있다고 추정하지 않는다. 기존 키 파일·구동 계정 변경은 F1 승인 관문에서 별도 계획한다.

- D1 등록 모델 정정: S-11의 “repository는 논리 UUID뿐” 가정이 현재 Core와 다르다. 기존 workspace 경계는 실제 repository scope와 project parent/binding을 검사한다. actual HTTPS로 write-only bootstrap의 environment→repository→project 생성과 논리-only repository UUID 거부를 검증했다(2 tests pass). 계약을 바꾸지 않고 정식 create_scope로 필요한 부모들을 만든다. 한 project scope는 하나의 실제 repository에 속하므로 다른 repository는 별도 project로 등록한다. 임의의 business SQL/Host allowlist 변경은 하지 않는다.

- C10 fixture 수정: 관리된 캐시 없는 non-SessionStart 이벤트는 이제 not_configured 경고로 Core 전에 중단한다. 기존 DB/pending 동시 장애 시험의 Native 메시지 기대값만 이에 맞추고, 직접 Core 장애·원본 보존 검증은 유지한다. 실제 구버전 profile.json+DB direct-root 경로는 별도로 호환해야 하며 새 storage.json으로 억지 초기화하지 않는다.
