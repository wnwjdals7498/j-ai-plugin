# 5단계 진행 기록

- 기준: `origin/master` `8054c83a85b73356f0e2e52322855b9e03b419ce`.
- 작업 checkout: `C:\PMT\src\j-ai-plugin`, 브랜치 `feat/phase5-plugin-split`.
- 메인: Codex. 구현 작업자는 A1 완료와 사용자 결정 확정 뒤 `gpt-6-luna`로 소유 영역을 분리한다.
- 현재 단계: D1~D8 확정, A0/A1/B1 기록 완료. B2 구현·B3 독립 진입점 구현·C1 검증 완료·C2 착수 준비.
- 운영 금지 영역: `D:\PMTHostState\host-data`, `D:\PMTHostState\host-config`, `D:\PMTHostState\secrets`, 기존 release/venv, 작업 `PMT-Host`, TCP 8765.
- 개발 시험: checkout의 `.pmt-test`, loopback 18765. 운영 경로는 시험에 사용하지 않는다.

| ID | 상태 | commit(시험 대상) | 시험(명령·결과) | 증거 경로 | 미해결·다음 할 일 |
|---|---|---|---|---|---|
| A0 | 완료 | 기준 `8054c83` | 필수 파일 존재; marketplace/plugin validate exit 0; 전체 pytest 650 passed/23 failed/2 errors/4 skipped, exit 1; 설치 재검증 2 passed/3 failed/0 errors, exit 1 | `evidence/2026-10-08/A0/` | 첫 실행 119 passed, 1 failed, 559 errors: 559 errors만 `.pmt-test` 부모 디렉터리 누락. 별도 redirect 실패는 원인 미확정이며 3.13/3.14 독립 진단에서 재현 안 됨. 최초 증거 보존, 폴더 준비 후 전체 재시험 종료. 결과는 summary.json. 전체 suite 2 errors는 선언된 setuptools 빌드 backend 누락; 제품 코드 수정 없이 설치 재검증 2 passed/3 failed/0 errors. 남은 3 설치 실패는 과거 package-snapshot fixture 부족; 원본 과거 증거 미수정. D1~D8 사용자 확정 완료; 기존 nonpassing baseline 보존 |
| A1 | 완료 | 기준 6161335; 기록 commit은 git log | 최신52 tests pass, exit0; 실제 HTTPS health/compat; console script smoke; Claude validate; independent review approve | evidence/2026-10-08/A1/ | 공통 API 인계: implementation-interfaces.md. B1/C1 독립 착수 가능 |
| B1 | 완료 | 기준 938cc4e; 기록 commit은 git log | worker31 pass/2 POSIX skip; parent68 pass/2 skip; compile0; independent review approve | evidence/2026-10-08/B1/ | 구현/Windows 범위 검증 완료. 실제 Linux/다른 Windows 계정/제품 세션은 F2/F3, 실제 hosted 인계 D2 |
| B2 | 진행 | B1 인계 | 준비 | evidence/2026-10-08/B2/ | gpt-6-luna: local commands/link/check/mode |
| B3 | 진행 | B1 06e18ec | 별도 작업자 준비 | evidence/2026-10-08/B3/ | gpt-6-luna: integrations/bin/pmt_easy 진입점; B2와 disjoint |
| C1 | 완료 | 기준 938cc4e; 기록 commit은 git log | worker25 pass/1 POSIX skip; parent BC1 169 pass/3 skip; independent review approve | evidence/2026-10-08/C1/, BC1-integration/ | init/CAS/key lifecycle/ACL 검증. 실제 서비스계정 F1·Linux F2 미실행 |
| C2 | 진행 | C1 인계 | 준비 | evidence/2026-10-08/C2/ | gpt-6-luna: tls/doctor/serve/logs/status |
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

2026-10-08 D1~D8 확정 완료. A0 기준선·Host 조사·환경 복구 기록은 동일 source/환경의 유효한 근거로 재사용한다. A1/B1 구현·현재 환경 검증 완료(938cc4e/06e18ec). B2 클라이언트 명령 구현과 C1 서버 초기화/보안 리뷰 수정 진행. C1 수정 후 부모 통합·독립 재검증을 거쳐 C2로 넘긴다. A0 전체 시험 nonpassing을 숨기지 않으며 handoff 지시대로 기존 실패를 고치지 않는다. 이후 전체 회귀는 동일 Python3.14 환경에서 baseline과 비교하고 새 실패를 식별한다.
Graphify 출력은 로컬 분석 자료이며 커밋하지 않는다. E3의 push/PR 승인과 F1의 운영 단계별 승인은 아직 받지 않았다.

검토 상태: B1은 반복 발견된 실제 Hook/credential 보존 문제를 수정하고 독립 리뷰 승인 및 parent68pass/2skip으로 닫았다. C1은 실제 서비스 계정 ACL, init 실패 정리, strict config/CAS snapshot, 키 변경 실패 보존을 보강 중이다. Graphify 갱신은 병렬 source가 안정된 통합 지점에서 메인이 수행한다. D1의 scope 관계 가정은 actualTLS 2tests로 검증해 API 변경 없이 상세 명세에 정정했다.
