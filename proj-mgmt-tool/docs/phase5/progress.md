# 5단계 진행 기록

- 기준: `origin/master` `8054c83a85b73356f0e2e52322855b9e03b419ce`.
- 작업 checkout: `C:\PMT\src\j-ai-plugin`, 브랜치 `feat/phase5-plugin-split`.
- 메인: Codex. 구현 작업자는 A1 완료와 사용자 결정 확정 뒤 `gpt-6-luna`로 소유 영역을 분리한다.
- 현재 관문: D1~D8 사용자 응답 대기. `codex-phase5-host.md` §3.3에 따라 A1 이후 미착수.
- 운영 금지 영역: `D:\PMTHostState\host-data`, `D:\PMTHostState\host-config`, `D:\PMTHostState\secrets`, 기존 release/venv, 작업 `PMT-Host`, TCP 8765.
- 개발 시험: checkout의 `.pmt-test`, loopback 18765. 운영 경로는 시험에 사용하지 않는다.

| ID | 상태 | commit(시험 대상) | 시험(명령·결과) | 증거 경로 | 미해결·다음 할 일 |
|---|---|---|---|---|---|
| A0 | 진행 | 기준 `8054c83` | 필수 파일 존재; marketplace/plugin validate exit 0; 전체 pytest 650 passed/23 failed/2 errors/4 skipped, exit 1; 설치 재검증 2 passed/3 failed/0 errors, exit 1 | `evidence/2026-10-08/A0/` | 첫 실행 119 passed, 1 failed, 559 errors: 559 errors만 `.pmt-test` 부모 디렉터리 누락. 별도 redirect 실패는 원인 미확정이며 3.13/3.14 독립 진단에서 재현 안 됨. 최초 증거 보존, 폴더 준비 후 전체 재시험 종료. 결과는 summary.json. 전체 suite 2 errors는 선언된 setuptools 빌드 backend 누락; 제품 코드 수정 없이 설치 재검증 2 passed/3 failed/0 errors. 남은 3 설치 실패는 과거 package-snapshot fixture 부족; 원본 과거 증거 미수정. D1~D8 확정 대기 |
| A1 | 미착수 | — | 미실행 | — | A0·사용자 결정 확정 필요 |
| B1 | 미착수 | — | 미실행 | — | A1 선행, 클라이언트 |
| B2 | 미착수 | — | 미실행 | — | B1 선행, 클라이언트 |
| B3 | 미착수 | — | 미실행 | — | B1 선행, 클라이언트 |
| C1 | 미착수 | — | 미실행 | — | A1 선행, 서버 |
| C2 | 미착수 | — | 미실행 | — | C1 선행, 서버 |
| D1 | 미착수 | — | 미실행 | — | C2 선행, 서버 |
| D2 | 미착수 | — | 미실행 | — | B2·D1 인계 샘플 선행, 클라이언트 |
| E1 | 미착수 | — | 미실행 | — | C2 선행, 서버 명령 생성만 시험 |
| E2 | 미착수 | — | 미실행 | — | D1·D2 선행, backup/switch |
| E3 | 미착수 | — | 미실행 | — | E1·E2 선행, 전체 회귀·번들·문서; 종료 후 push/PR 승인 관문 |
| F1 | 미착수 | — | 미실행 | — | E3·push된 commit 선행. 운영 변경은 인계 문서 §6 각 승인 관문 필요 |
| F2 | 미착수 | — | 이 컴퓨터에서 실행 금지 | — | F1의 비밀 없는 실값으로 Linux 개발 서버 인계 작성 |
| F3 | 미착수 | — | 이 컴퓨터에서 실행 금지 | — | F1의 비밀 없는 실값으로 Windows 개발 PC 인계 작성 |

## 계획 검토

- 의존성: A0 → A1 → 독립 클라이언트 B/서버 C → D1 → D2 → E2 → E3 → F1.
- 공유 `pyproject.toml`, host CLI/auth, handoff, marketplace/build, 문서는 메인만 수정.
- 클라이언트·서버 작업자는 다른 소유자의 변경을 되돌리지 않는다. 동시 작업 기본 3개 이내.
- 변경 금지 계약: Core 0.4.x, SQLite 5, graph 1, protocol 1, Host API v1.
- 기존 실패는 A0에 기록만 하고 수정하지 않는다. 이후 비교에서는 새 실패를 별도로 식별한다.
- Graphify 통합 준비됨. headless semantic extraction은 인증 오류로 실패, AST-only 복구 완료(9396 nodes, 19415 edges). 문서 의미 관계는 미추출. 외부 인증 오류 상세는 로그에서 제거했다.



## 현재 재개 지점

A0 기준선·Host 조사·환경 복구 검증 기록 완료. D1~D8 사용자 확인 대기이므로 A0 전체는 진행 상태, A1 이후 미착수다. 다음 세션은 decisions.md와 이 상태를 읽고, 확인 응답이 있으면 결정표를 확정한 뒤 A1부터 진행한다. 전체 suite의 nonzero baseline을 숨기거나 통과로 재분류하지 않는다.
- A0 체크포인트 문서·증거의 독립 검증 pass. 실제 전체 시험은 nonpassing이며 관문 대기는 그대로 유지한다. Graphify 생성 graphify-out/은 로컬 분석 자료로 커밋하지 않는다.
