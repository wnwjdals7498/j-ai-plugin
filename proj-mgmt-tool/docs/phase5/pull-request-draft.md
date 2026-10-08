# PR 제안

Title: feat: split PMT client and Host management plugins

기존 PMT에서 개발 기기 연결과 Host 운영 절차를 함께 관리하던 부분을 pmt-lifecycle과 pmt-server로 나눕니다. 클라이언트는 Python 설정만으로 local을 시작하고, 별도 인계 파일·보호 credential로 hosted를 연결하거나 명시 전환합니다. Host 관리 플러그인은 별도 venv의 관리 CLI를 호출합니다.

클라이언트는 OS 기본 경로, DPAPI/owner-only credential, local 작업 완료 근거, 공용 Hook, Windows/Bash 진입점을 제공합니다. Host에는 config CAS, 기존 TLS 등록, 진단/로그/중복 실행 방지, project/device/handoff, 검증된 backup/import와 guarded upgrade를 추가합니다. Service/firewall/timer는 plan을 검토한 뒤 명시 적용하며 이번 작업에서 운영 시스템에 적용하지 않았습니다.

Plugin0.5.0 / Core0.4.1이며 Host APIv1, DB5, graph1, protocol1과 claim/auth 계약을 유지합니다. Server 번들은 Core 소스를 복사하지 않습니다. Claude 서버 Hook은 별도 파일에 두어 실제 Codex0.160.1의 기본 Hook 발견에서 제외했습니다.

Validation: 최종 whole pytest926passed/21failed/0errors/8skipped, exit1. 실패21개 모두 기존 A0 실패이며 test-ID 비교 신규 실패0/기존 누락0. Python3.13/3.14 통합, 실제 격리 TLS 연결·양방향 모드 전환·DBhash 보존, 최종 PS/GitBash6회, Claude5검사, Codex 실제 skill 발견/Hook0개를 확인했습니다. 릴리스는 committed git archive로 생성해 file/source/ZIPhash와 Unixmode를 검증했습니다.

운영 Host/F1, 실제 Linux/F2 및 제품 새 세션/F3는 승인·해당 환경 후속 단계입니다. 과거 Phase3/4 증거를 수정하거나 누락 fixture를 만들어 통과시키지 않았습니다. 전체 결과는 docs/phase5/review-ready.md와 progress/evidence에 기록했습니다.

Tools/models: Codex 메인 세션 기본 설정, native implementation agents gpt-6-luna, independent reviewer gpt-5.5, repository explorer gpt-5.3-codex-spark; Git, pytest, Graphify AST-only, Claude/Codex CLI, local Codex app-server RPC.
