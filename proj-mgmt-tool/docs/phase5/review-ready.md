# Phase5 개발·검증 결과와 E3 승인 관문

제안 D1~D8 모두 적용. 클라이언트 PMT와 Host 관리 PMT Server를 분리했다. Plugin0.5.0 / Core0.4.1 / DB5 / graph1 / protocol1 / Host APIv1 유지. 새 Host 암호화 의존성이나 CA 생성 기능 없음.

| 묶음 | 상태·근거 |
|---|---|
| A0 | 기준선·읽기 전용 Host 조사 완료. 최초 whole650pass/23fail/2error/4skip 보존 |
| A1 | 공통 계약·단일 manifest·handoff 검증·Host runner·device 목록 완료 |
| B1 | OS 경로·보호 credential·local 기본 준비 완료 |
| B2 | local 작업/검증·link·mode/check·cached Hook 완료 |
| B3 | 공용 Claude/Codex Hook과 Windows/Bash 진입점 완료 |
| C1 | config CAS·init/adopt·키 보관/ACL 완료. 운영 adopt는 미실행 |
| C2 | TLS register/check·doctor·serve 중복 방지·status/logs 완료 |
| D1 | 실제 격리 TLS project/repository/device/handoff 완료 |
| D2 | 실제 인계 connect/link/check·CA·credential·동시 writer 보존 완료 |
| E1 | service/firewall/backup timer plan/apply 명령 생성·모의 적용 완료 |
| E2 | backup/restore-check/import/upgrade 및 실제 격리 TLS 양방향 switch 완료 |
| E3 | 두 논리 플러그인/네 target·문서·재현 릴리스·신규 실패0 검증 완료 |
| F1 | 미착수. push된 SHA 설치와 운영 단계별 승인 필요 |
| F2/F3 | 다른 컴퓨터에서 실행 금지. F1 실제 namespace/device/commit 확정 뒤 인계 작성 |

최종 전체 pytest:955cases,926pass/21fail/0error/8skip, exit1,1242.27s. A0 test-ID 비교: 신규 실패0, 기존 시험 누락0, 비교 exit0. 남은21실패는 A0 목록의 subset이다. 과거 증거·package snapshot을 추가/삭제하지 않았다. [원시 결과·비교](evidence/2026-10-08/E3/final-summary.json).

Python3.13.13과3.14.5rc1의 통합306pass/4skip 근거에 더해 마지막 클라이언트 보강은3.13 대상39pass 및148pass/2skip broad 결과로 확인했다. 실제 Windows PowerShell/Git Bash는 최종 번들6/6 성공. 실제 Linux, 다른 Windows 계정 DPAPI, 제품의 새 대화·신뢰 화면·운영 재부팅은 미실행.

릴리스는 source commit aa39931835204ee4508e53df01a4cc53ef01434c의 git archive만으로 빌드했다. 네 ZIP/모든 파일·source hashes, Unix creator3/sh0755/cmd0644 검증. Claude5검사0/no warning, 로컬 Markdown 깨진 링크0. 실제 격리 Codex app-server는 PMT Server skill을 발견하고 Hook0개를 반환했다. [최종 릴리스 검증](evidence/2026-10-08/E3-release-final/README.md).

운영 Host 변경 없음. A0 조사 snapshot은 Core0.4.0/Python3.13.13, 기존 PMT-Host 작업과 D:/PMTHostState 경로다. 새 운영 백업·서비스·방화벽·키·기기 발급은 수행하지 않았다. 시험 기기는 C:/PMT/work의 별도 Host에만 발급했고 listener는 종료했다. 운영 namespace/device를 아직 읽어 인계에 채우지 않았으며 테스트 UUID를 운영값으로 사용하지 않는다.

기존 supervisor는 보호 파일의 claim key를 Host child 환경에만 주입한다. F1은 일반 관리자 세션에 키가 있다고 가정하지 말고 승인된 원천·계정·ACL 이전 계획을 보여 줘야 한다. 운영8765 doctor/status guard 제거는 자동 승인 검토가 거부해 유지했으며 F1 별도 승인 항목이다. 실제 Native installer 호출 검증도 거부되어 실행 없는 명령 검증으로 대체했고 설치 성공으로 기록하지 않았다.

2026-10-08 사용자 승인 후 feat/phase5-plugin-split push와 master 대상 [PR #1](https://github.com/wnwjdals7498/j-ai-plugin/pull/1) 생성·첨부 완료. GitHub head 06235fd25fcf28208a633f0fb5412d51939a9c15와 master 8054c83a85b73356f0e2e52322855b9e03b419ce 확인. PR open/병합 충돌 없음, 병합 미실행. 기록 전용 후속 커밋은 구현·시험 소스를 바꾸지 않는다. F1 설치 고정 SHA는 06235fd다. 다음 승인 대상은 [F1-1 새 venv 설치 계획](f1-install-plan.md)이다. [인계 §6](../handoff/codex-phase5-host.md)은 “각 단계는 실행 전 무엇을 바꾸는지 보여 주고 사용자 승인을 받는다”고 요구한다. E3 승인은 F1 설치·정지·서비스 교체·재부팅·키 이전·운영 기기 발급 승인을 대신하지 않는다.
