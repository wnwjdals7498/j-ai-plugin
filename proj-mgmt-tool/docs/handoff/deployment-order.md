# Windows Host → 개발 서버 설치 순서

2026-10-06. 실행 지시는 [Windows 서버 프롬프트](windows-host.md), [개발 서버 프롬프트](development-plugin.md)를 복사한다. 이번 문서는 실행 순서이며 실제 서버를 배포하거나 사용자 플러그인을 설치한 결과가 아니다.

## 역할·기준

Windows Host는 SQLite·공유 이력·Queue·점유·리소스·인증을 맡는다. 개발 서버는 Git checkout·문서·코드·모델/에이전트 실행·private spool을 맡는다. 개발 서버가 Host의 SQLite 파일을 직접 열거나 SMB로 공유하지 않는다. 통신은 인증 HTTPS의 protocol-v1 JSON operation/resource API다.

코드 기준은 `9f555185db9e772d77b71060144d3c39ca42fed2`, Core0.4.0/DB5/graph1이다. 두 서버에 같은 구현 기준을 설치한다. `docs/usage.md`의 앞부분과 일부 기존 문서에는 0.3.0/schema4 예제가 남아 있으므로 실제 `pyproject.toml`, `pmt --version`, compatibility 응답과 [4단계 상태](../phase4/implementation-status.md)를 우선한다. 최신 변경을 채택할 때는 기준 차이·호환·검증을 함께 확인한다.

권장 초기 구성은 Python3.13 이상 + Host 전용 venv + `proj-mgmt-tool[host]`의 FastAPI/Pydantic/Uvicorn이다. 본체와 동일한 Python/SQLite를 쓰며 서버만 HTTP 의존성을 설치한다. 처음에는 직접 TLS 단일 프로세스로 경계를 검증한다. Windows 자동 구동은 작업 스케줄러의 시작 트리거를 기본 제안한다. 기존 Windows 서비스 관리 체계가 있으면 같은 단일 프로세스·키/계정·종료/재시작 조건을 충족하는 방식으로 맞춘다. 이는 운영 선택이며 아직 실서버 수용 결과가 아니다.

## 작업 순서

| 순서 | 실행 환경·작업 | 완료 후 다음 단계에 전달할 것 |
|---|---|---|
| 1 | 두 환경 조사·동일 코드 고정 | Host Windows/Python·계정·LAN/VPN 경로·인증서, 개발 OS/에이전트 버전·실제 실행 위치·checkout. 주소/접속 범위와 설치 범위 확정 |
| 2 | Windows Host 수동 구동 | 전용 data/config·보호된 영속 claim key·TLS·단일 worker. 실제 health와 인증 compatibility 확인 |
| 3 | Host 프로젝트·기기 등록 | bootstrap 관리 연결로 필요한 repository/project UUID 등록, 개발 기기 전용 actor/device·scope/권한 발급. 임시 bootstrap 권한 회수 |
| 4 | Host 자동 구동·보존 | 재시작·로그아웃/시작 트리거 후 동일 namespace/key/DB, 중복 프로세스 방지, 로그·별도 빈 경로 복원 검증 |
| 5 | 개발 서버 본체·플러그인 설치 | 같은 release에서 제품별 번들 생성/등록, 실제 실행 계정·Python·skill/Hook 발견. 기존 설정/DB 백업 보존 |
| 6 | 개발 서버 hosted 연결 | secret 저장→`storage configure`의 실제 TLS/권한/호환 probe→config CAS. repository/project/branch/checkout/graph mapping·명시 PMT_SCOPE_ID |
| 7 | 실제 통합·새 세션 시험 | 단일 canary 작업→두 세션의 충돌/재전송/재개→실제 모델 사용 승인 후 네이티브 작업. 훅 관찰과 실제 작업 완료를 구별 |
| 8 | 운영 전환 | 필요한 기존 이력만 quiescent backup/transfer로 이관, 별도 복원 확인·사용자 전환 선택. 이후 변경 버전·계정·로그·복구 지시 기록 |

1단계 환경 조사는 동시에 가능하다. 개발 서버의 패키지 준비도 Host 작업과 병렬 가능하지만 **hosted 연결·공유 쓰기는 2~4단계의 인계와 검증 뒤** 진행한다. 기존 로컬 이력의 이관은 빈 namespace를 요구하므로 해당 namespace에 canary/새 프로젝트를 먼저 생성하지 않는다. 신규 파일럿 namespace와 실제 이관 target을 분리하거나 canary를 별도 서버 인스턴스로 시험한다. 원본 데이터를 삭제해 target을 비우지 않는다.

## 서버 간 인계 데이터

비밀 없는 인계: endpoint/port·인증서 발급 주체/공개 CA 참조, code/core/db/graph/protocol, namespace_id·device_id·actor·scope/권한, repository_id·project_id, 서비스/작업 이름·운영 계정·보존 위치·health/compatibility 결과.

기기 credential은 안전한 별도 전달 경로로 개발 서버의 보호 저장소에 넣는다. 인계 문서에는 credential 환경변수 **이름**만 기록한다. Host의 claim key·TLS private key·관리 기기 token은 개발 서버에 보내지 않는다. 서버와 개발 기기의 environment UUID는 별개로 생성하며 같은 값을 복사하지 않는다.

## 연결·운영에서 확인할 점

- 직접 TLS면 접속 URL의 hostname/IP가 인증서 SAN과 맞아야 한다. 내부 CA라면 개발 클라이언트가 공개 CA를 명시적으로 신뢰한다. Host bind는 현재 CLI가 literal IP를 요구한다. loopback HTTP 시험 옵션을 원격 공개 용도로 쓰지 않는다.
- Host data/config는 Git 작업 폴더·동기화 폴더·네트워크 공유 밖의 로컬 디스크를 우선한다. 예: `C:\ProgramData\PMT\host-data`, `C:\ProgramData\PMT\host-config`. 실제 실행 계정으로 DB·WAL·resources의 쓰기와 재시작을 확인한다.
- claim key는 실제 구동 계정이 재시작 후 동일 값으로 읽어야 한다. 개발 기기 credential도 에이전트/훅 프로세스가 읽는 보호 저장소에서 환경변수로 주입한다. 현재 터미널의 변수 설정만으로 자동 구동·다른 앱에 적용됐다고 간주하지 않는다.
- 작업 스케줄러는 중복 인스턴스 금지·실패 재시작·장시간 실행 제한·실제 child 종료를 확인한다. 코드의 Uvicorn은 `workers=1`이며 서비스 관리자에서도 중복 Host를 만들지 않는다. [Microsoft 작업 등록](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask), [실행/중복/재시작 설정](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset)
- Codex는 등록/설치와 Hook trust가 별개다. CLI/Desktop/remote 실행 환경에서 실제 capability를 확인한다. [OpenAI 설치·패키징](https://developers.openai.com/plugins/build/plugins), [Hook trust](https://learn.chatgpt.com/docs/hooks)
- Claude Code는 local marketplace 설치와 새 세션 로딩을 검증한다. [공식 설치 개요](https://code.claude.com/docs/en/plugins)
- 현재 PMT OpenCode 어댑터는 V1 형식이다. V2에 파일만 등록해 완료로 표시하지 않는다. V2 필요 시 호환 포팅과 별도 검증을 작업으로 구별한다. [공식 V1→V2 안내](https://opencode.ai/v2/docs/build/plugins/migrate-v1/)

현재 증거는 로컬/TLS 및 fixture 중심이며 실제 Windows Server 상시 운영·실제 제품 설치가 미수용이다. 간헐적 SQLite/Git 쓰기 오류 원인도 미확정이므로 설치 서버의 실제 계정·저장 위치에서 재검증한다. 처음에는 필요한 개발 기기 한 곳과 제한된 시험 프로젝트로 운영 확인한다.
