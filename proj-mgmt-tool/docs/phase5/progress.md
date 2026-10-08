# 5단계 진행 기록

- 기준: `origin/master` `8054c83a85b73356f0e2e52322855b9e03b419ce`.
- 작업 checkout: `C:\PMT\src\j-ai-plugin`, 브랜치 `feat/phase5-plugin-split`.
- 메인: Codex. 구현 작업자는 A1 완료와 사용자 결정 확정 뒤 `gpt-6-luna`로 소유 영역을 분리한다.
- 현재 단계: A0/A1/B1/B2/B3/C1 완료. C2 완료. D1/E1 완료, D2 리뷰 보강. E2는 D2 확정 후 착수.
- 운영 금지 영역: `D:\PMTHostState\host-data`, `D:\PMTHostState\host-config`, `D:\PMTHostState\secrets`, 기존 release/venv, 작업 `PMT-Host`, TCP 8765.
- 개발 시험: checkout의 `.pmt-test`, loopback 18765. 운영 경로는 시험에 사용하지 않는다.

| ID | 상태 | commit(시험 대상) | 시험(명령·결과) | 증거 경로 | 미해결·다음 할 일 |
|---|---|---|---|---|---|
| A0 | 완료 | 기준 `8054c83` | 필수 파일 존재; marketplace/plugin validate exit 0; 전체 pytest 650 passed/23 failed/2 errors/4 skipped, exit 1; 설치 재검증 2 passed/3 failed/0 errors, exit 1 | `evidence/2026-10-08/A0/` | 첫 실행 119 passed, 1 failed, 559 errors: 559 errors만 `.pmt-test` 부모 디렉터리 누락. 별도 redirect 실패는 원인 미확정이며 3.13/3.14 독립 진단에서 재현 안 됨. 최초 증거 보존, 폴더 준비 후 전체 재시험 종료. 결과는 summary.json. 전체 suite 2 errors는 선언된 setuptools 빌드 backend 누락; 제품 코드 수정 없이 설치 재검증 2 passed/3 failed/0 errors. 남은 3 설치 실패는 과거 package-snapshot fixture 부족; 원본 과거 증거 미수정. D1~D8 사용자 확정 완료; 기존 nonpassing baseline 보존 |
| A1 | 완료 | 기준 6161335; 기록 commit은 git log | 최신52 tests pass, exit0; 실제 HTTPS health/compat; console script smoke; Claude validate; independent review approve | evidence/2026-10-08/A1/ | 공통 API 인계: implementation-interfaces.md. B1/C1 독립 착수 가능 |
| B1 | 완료 | 기준 938cc4e; 기록 commit은 git log | worker31 pass/2 POSIX skip; parent68 pass/2 skip; compile0; independent review approve | evidence/2026-10-08/B1/ | 구현/Windows 범위 검증 완료. 실제 Linux/다른 Windows 계정/제품 세션은 F2/F3, 실제 hosted 인계 D2 |
| B2 | 완료 | 기준 c88ab73; 기록 commit은 git log | worker60pass/2POSIXskip; parent194pass/3skip; independent closure approve | evidence/2026-10-08/B2/, BC2-integration/ | 실제 local Git/SQLite/test-command/Done, cached Hook, read-only mode 검증. 실제 hosted connect는 D2 |
| B3 | 완료 | 기준 06e18ec; 기록 commit은 git log | worker8entry +23focusedhook; parent42pass/0fail; actualbuiltWindowsPS/GitBash exit0+missingPythonexit3 | evidence/2026-10-08/B3/ | CLI frontdoors/공용 CodexHook 검증; 실제 제품 trust/새세션·Linux는 F2/F3 미실행 |
| C1 | 완료 | 기준 938cc4e; 기록 commit은 git log | worker25 pass/1 POSIX skip; parent BC1 169 pass/3 skip; independent review approve | evidence/2026-10-08/C1/, BC1-integration/ | init/CAS/key lifecycle/ACL 검증. 실제 서비스계정 F1·Linux F2 미실행 |
| C2 | 완료 | 기준 c88ab73 | worker35pass/1POSIXskip; parent194pass/3skip; closure approve | evidence/2026-10-08/C2/, BC2-integration/ | 실제 TLS/doctor/status/logs/중복 실행 검증. F1 운영 서비스 계정은 미실행 |
| D1 | 완료 | 기준 a2fd13d; 기록 commit은 git log | worker41pass/2skip + explicit sample1pass; parent16pass/1skip; review approve | evidence/2026-10-08/D1/, D1-preflight/ | 실제 TLS scope/device/권한/secret-free handoff 검증. D2 sample은 외부 격리 경로 |
| D2 | 진행 | D1 실제 인계 | 구현 시작 | evidence/2026-10-08/D2/ | 클라이언트 connect/link/check, badCA/token/CAS 보존 |
| E1 | 완료 | 기준 81d410a; 기록 commit은 git log | worker21pass; parent85pass/2skip; closure approve | evidence/2026-10-08/E1/ | plan/apply/service/firewall/timer 명령 모형, 실제 등록·도달성·재부팅은 F1/F2 미실행. 운영8765진단 guard 승인 의존 |
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
2026-10-08 B2/C2 source 최신 통합194pass/3POSIXskip. B2/C2 기록 완료. D1 완료·D2 진행, E1 final21pass 및 parent85pass/2skip 확정. D2 HTTP-default와 동시 writer 보존 리뷰 수정 중. E3 자산은 준비 중이며 전체 완료는 E2 뒤 판정. A0 nonpassing baseline은 보존하며 E3 whole-suite에서 test ID 단위로 비교한다.
Graphify 출력은 로컬 분석 자료이며 커밋하지 않는다. source 안정 지점에서 메인만 AST-only 갱신한다. E3 push/PR 승인과 F1 운영 단계별 승인은 아직 받지 않았다.
D1 scope 관계 가정은 actual TLS2tests로 검증하고 API 변경 없이 정정했다. D1 실제 인계 파일 이후 D2 시작; E1은 별도 파일에서 명령 생성/모형 시험만 수행한다.
