# 5단계 결정

2026-10-08. 기준: `05-plugin-split.md` §8, `codex-phase5-host.md` §3.3.

**상태: 사용자 확인 대기. 아래는 제안값이며 확정값이 아니다. A1 이후 구현에 아직 적용하지 않는다.**

| ID | 제안값 | 상태 |
|---|---|---|
| D1 | `pmt-lifecycle` 유지, 표시 이름 PMT | 확인 대기 |
| D2 | `handoff_file` 또는 `host_url`이 있으면 hosted, 없으면 local | 확인 대기 |
| D3 | claim key 보관: env + Linux 0600 file + Windows DPAPI | 확인 대기 |
| D4 | Claude plugin secret을 OS 저장소로 복사, `CLAUDE_ENV_FILE`에는 credential 제외 | 확인 대기 |
| D5 | 기존 인증서 등록 기본. `tls create-ca`는 cryptography를 host extra에 추가 승인할 때만 제공 | 확인 대기 |
| D6 | local 짧은 명령에 done 포함 | 확인 대기 |
| D7 | Codex connect + 공용 Hook을 B·D 단계에 포함 | 확인 대기 |
| D8 | Windows APPDATA/pmt, LOCALAPPDATA/pmt/data. 기존 ~/.config/pmt 보존 | 확인 대기 |

## 구현 선택

- A0: 기존 pytest 설정이 `.pmt-test/pytest-temp`를 basetemp로 사용하므로 `.pmt-test` 상위 폴더를 미리 준비한다. 최초 실패 결과를 보존한다. 제품 코드 변경 없음.
- Host 조사: 라이브 프로세스의 CIM 경로/명령행/계정이 현재 실행 권한에서 공개되지 않아 실행 중 작업의 supervisor 설정에서 비밀 없는 인수를 확인한다. DPAPI·claim key·TLS 개인키·기기 credential 파일은 열지 않는다.
- Host 조사: `/health`는 공개 CA로 TLS와 loopback SAN을 검증한다. proxy는 해당 health 요청에만 사용하지 않는다. 전역 설정 변경 없음.
- Graphify: 사용자 규칙에 따라 root AGENTS.md와 .codex/hooks.json을 설치한다. API 인증이 필요한 문서 의미 분석은 실패로 기록하고, 외부 API가 필요 없는 AST 구조 추출로 대체한다. 문서 관계까지 분석했다고 보고하지 않는다.
- A0: 기본 개발 Python은 3.14.5rc1이다. 안정 3.13.13의 별도 진단 venv `C:/PMT/src/venv-dev-313`를 준비해 동일 redirect fixture를 비교했다. 양쪽에서 기대 오류 code와 단일 요청을 관찰했으나 최초 간헐 실패 원인 해결로 판정하지 않는다. 운영 venv/설정 미변경.
- A0: 격리 wheel fixture는 --no-build-isolation을 사용하지만 새 venv에 setuptools가 없었다. pyproject build-system에 이미 선언된 setuptools>=68을 개발 venv에 설치한 뒤 영향받은 설치 시험 5개만 다시 검증한다. 런타임/프로젝트 의존성 변경 없음. 원래 제품 실패는 수정하지 않는다.
