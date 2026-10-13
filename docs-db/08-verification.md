# 08. 검증 — 테스트, 매트릭스, 실측, 완료 기준, 위험

## 1. 테스트 목록

| 테스트 | 내용 | 상태 |
|---|---|---|
| `test_scenarios.py` | 원 33건(적용 판정, 문서·필드·경로·증거·이력·승인·보존 규칙). 카탈로그 픽스처를 JSON으로 바꾸고 "194종 집계" 단정을 "적재한 종류 수와 일치"로 일반화(P2) | 원본 있음, 수정 필요 |
| `test_token.py` | 원 28건(ctx·get·put·since·find·trace·call_log) | 원본 있음 |
| `test_mcp.py` | 공식 SDK 클라이언트로 도구 6개·결과 text 1블록 확인 | 원본 있음 |
| `audit.py` | 표준 항목↔컬럼 매핑 94/94 | 원본 있음 |
| `test_fixes.py` | P4: 항목 삭제 후 `find`에 나오지 않음. P5: `local_only` 문서의 `get` L2·`ctx` 요약·`find` 발췌·`since` diff에 본문 없음 | 신규 |
| `test_relations.py` | D10: AI가 넣은 관계는 `proposed`만, 사람만 확정·반려, `confirmed` 삭제는 사람만, 반려 재제안 `REJECTED_BEFORE`, 조회 뷰는 `confirmed`만, 추출 규칙 R1~R5, `v_missing_required_links`, `change_log` 기록 | 신규 |
| `test_import.py` | 합성 md 3개: 규칙 매핑, `next` 예산, `label` 적용, digest 변경 시 `stale`, 적용 후 R1·R2 추출, `change_log` 체인 유효, `report` 수치 | 신규 |
| `test_env.py` | venv 없음, venv 버전 불일치, Python 3.9에서 안내 메시지와 종료 코드 2 | 신규 |

## 2. 검증 매트릭스 (D9)

| 위치 | 대상 |
|---|---|
| 로컬 Linux(개발 서버) | Python 3.10, 3.11, 3.12, 3.13, 3.14(`uv`로 설치) × 전체 테스트 |
| GitHub Actions | `ubuntu-latest`, `macos-latest`(arm64), `windows-latest` × Python 3.10, 3.13. **push는 사용자 승인 후** |
| 도구 실측 | `claude plugin validate`, Claude Code `/mcp`에서 도구 6개 확인, Codex에서 도구 노출 확인 |

## 3. 실측 항목 (구현 계획의 첫 단계, 미결 O7)

| # | 확인 | 결과에 따라 바뀌는 것 |
|---|---|---|
| S1 | Claude Code·Codex가 SDK 서버에 실제로 연결되는지(MCP 세대 포함) | 실패 시 SDK 버전 범위 |
| S2 | Codex `codex.mcp.json`의 상대 경로·작업 폴더·환경 변수·Windows 명령 해석 | [06](06-runtime-plugin.md) 6.3, 3절 Codex 행 |
| S3 | Claude MCP `env`에서 `${CLAUDE_PROJECT_DIR}` 치환 여부, MCP 서버 작업 폴더 | [06](06-runtime-plugin.md) 5절 탐색 시작점 |
| S4 | Windows: `py` 실행, venv site-packages 붙이기, 한글 stdio | [06](06-runtime-plugin.md) 3절 |
| S5 | 휠 없는 플랫폼(Linux aarch64, Intel Mac)에서 `pysqlite3` 소스 빌드 | [06](06-runtime-plugin.md) 1.1절 지원 범위 표기 |

## 4. 완료 기준

1. 로컬 매트릭스(Python 3.10~3.14) 전체 통과.
2. GitHub Actions 3개 OS 통과. push 승인 전에는 "미검증"으로 표기한다.
3. `claude plugin validate` 통과, Claude Code와 Codex에서 도구 6개 확인.
4. `test_relations.py`, `test_import.py` 통과(합성 데이터 기준).
5. venv가 없거나 Python이 3.10 미만이면 안내 메시지와 함께 종료 코드 2로 끝난다.
6. 원문 설계 문서 0장 수정(P3), 마켓플레이스 2곳 등록, 저장소 README에 설치 절차 추가.

## 5. 위험

| 위험 | 대응 |
|---|---|
| `pysqlite3` 휠이 없는 플랫폼 | S5 결과로 지원 범위를 README에 명시 |
| `mcp` SDK 변경 | `constraints.txt` 고정, 매트릭스로 갱신 확인 |
| 데이터 폴더 venv가 플러그인 제거 후 남음 | `setup --remove` 제공, README에 명시 |
| 시작 카탈로그는 원 194종의 일부 | 변경 절차([07](07-catalogue.md) 5절)로 확장. 194종 관련 수치는 "재현 불가"로 표기 |
| 확정 대기 관계가 쌓임 | `ctx`에 대기 수 표시, `link review`로 일괄 처리 |
| PostgreSQL 이전(원 결정 DEC-07) | 범위 밖. SQLite 유지 |
