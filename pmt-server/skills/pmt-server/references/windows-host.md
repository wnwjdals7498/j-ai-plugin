# Windows PMT Host 구축용 handoff 프롬프트

아래 본문을 Windows 서버에서 작업할 AI에게 전달한다. `<확인할 값>`은 해당 서버에서 조사하거나 사용자에게 확인하며 추정값으로 접속·이관하지 않는다.

---

이 Windows 서버에 PMT 저장 Host를 설치하고 개발 서버가 HTTPS로 접속할 수 있게 구성하라. 실제 구현 코드와 공식 문서를 기준으로 작업을 끝내고 증거·개발 서버 인계서를 남겨라.

## 목적·범위

- 목적: 여러 개발 환경이 같은 프로젝트 이력·Queue·점유를 사용하도록 단일 저장 원본을 운영한다.
- 기술: Python3.13 이상, 전용 venv, `proj-mgmt-tool[host]`의 FastAPI/Pydantic/Uvicorn, 로컬 SQLite/resources, HTTPS. 초기 자동 실행은 Windows 작업 스케줄러 시작 트리거를 제안하고 기존 관리 체계가 있으면 조사 후 맞춘다.
- 범위: Host 설치·인증·scope/기기 등록·TLS·필요 접속 방화벽·자동 시작·로그·backup/복원 시험. Git·모델·코드 실행은 개발 서버 책임이다.
- 제외: SMB SQLite 공유, Host의 모델/원격 runner 실행, 기존 데이터 초기화, 무승인 이력 primary 전환·불필요한 인터넷 전체 공개.

## 기준·입력

저장소: https://github.com/wnwjdals7498/j-ai-plugin
컴포넌트: proj-mgmt-tool
구현 기준 commit: 9f555185db9e772d77b71060144d3c39ca42fed2
예상 호환: Core0.4.0 / SQLite5 / graph1 / protocol1

먼저 AGENTS.md, docs/handoff/deployment-order.md(해당 checkout에 있으면), docs/pmt-docs/operations.md, docs/phase3/host-api-contract.md, docs/phase4/runtime-contract.md 및 implementation-status.md를 선택해 읽어라. 0.3 예제·계획·fixture를 현재 실제 지원으로 간주하지 마라. 실제 CLI `--help`와 코드를 대조하라.

확인할 값: Windows/Python 버전·관리 권한, 설치/로그/data/config/backup 경로, 실제 자동 구동 계정, 서버 bind IP·접속 DNS/IP·port(8765는 초기 제안), 허용할 개발 서버/LAN/VPN 범위, TLS PEM 인증서/chain·private key 또는 발급 방법, 신규 namespace인지 기존 이력 이관인지, 대상 repository/project와 개발 기기 actor.

일반 선택은 기존 환경과 이 지시로 결정하되 주소·계정·접속 범위·실사용자 데이터 전환처럼 필요한 값만 짧게 확인하라. 등록 전 설정/경로/기존 서비스를 조사하고 사용자 변경을 보존하라.

## 진행 순서

1. 승인된 코드 기준을 확보하고 기존 checkout의 dirty 변경을 보존한다. 신규 version 경로의 venv에 Host extra를 설치하고 actual core/db/graph/protocol을 확인한다. 코드와 mutable 데이터는 분리하며 Git/동기화/공유 폴더 밖의 로컬 data/config를 우선한다.
2. claim key를 보호 저장소에서 생성·보존하고 구동 계정이 환경변수 PMT_HOST_CLAIM_KEY로 읽도록 구성한다. 코드가 요구하는 base64 32bytes 이상이며 재시작마다 새 키를 만들지 않는다. TLS private key도 구동 계정만 필요한 권한으로 읽게 한다. secret 값을 도구 출력·채팅·문서·Git·일반 로그에 노출하지 말고 issue-device/generate-claim-key stdout을 안전하게 취급한다.
3. `pmt-host --data-root ... --config-root ... serve`의 실제 `--host/--port/--claim-key-env/--ssl-certfile/--ssl-keyfile`로 직접 TLS를 수동 구동한다. bind는 literal IP이고 worker는 1이다. health만으로 인증 성공을 판단하지 않는다. reverse proxy가 기존 운영 요구면 loopback backend·명시 trusted proxy·실제 HTTPS 인식/클라이언트 검증을 별도 확인한다.
4. 신규 업무 namespace라면 최초 필요한 scope/프로젝트를 공식 PMT operation으로 등록한다. 기존 이력 import target이면 업무 scope/canary를 먼저 만들지 않고 빈 target에 검증된 bundle을 import한 뒤 원 UUID를 유지한다. 필요한 경우 로컬 관리 CLI의 임시 bootstrap device/관리 연결로 시작하고 실제 sessions/operations를 사용한다. direct SQL로 업무/검증 성공을 만들지 않는다. repository/project UUID와 namespace를 기록한다. 개발 기기마다 actor/device/credential을 별도 발급하고 사용할 project의 read/write/runtime/review만 부여한다. bootstrap의 광범위 권한은 역할 종료 후 회수한다.
5. 현재 허용 범위의 방화벽을 구성하고 실제 개발 네트워크에서 인증서·도달성·compatibility를 확인한다. credential은 개발 서버의 보호 저장소로 별도 전달하며 인계서에는 환경변수 이름·전달 완료 여부만 남긴다. Host claim key/private TLS key/admin credential은 보내지 않는다.
6. 수동 확인 뒤 자동 시작을 등록한다. 실제 구동 계정의 secret 재로딩·절대 Python 경로·작업 디렉터리·단일 인스턴스·실패 재시작·장시간 실행 제한을 설정한다. wrapper는 Host 프로세스를 실제로 감독하고 종료 코드/로그를 전달해야 한다. native service가 아닌 Python console 실행기를 `sc create`에 넣은 것만으로 서비스 완료를 주장하지 마라.
7. 재시작/로그아웃 이후 같은 DB·namespace·claim key와 인증이 유지되는지 실제 검증한다. 장기 실행 로그와 SQLite code/name을 남긴다. backup은 quiescent manifest/resources 경계를 사용하고 별도 빈 target에서 복원/hash/FK를 확인한다. 현재 live token·인증 환경은 데이터 backup과 분리해 보호한다. 기존 이력 import 대상은 업무 데이터가 비어 있어야 하므로 canary와 실이관 namespace를 섞지 않는다.

## 시험·로그·완료 조건

실제 HTTPS health + 인증 compatibility(0.4.0/5/1/1), 잘못된 credential/scope 거부, 작은 canary project 생성/읽기/동일 요청 replay·stale revision, 동시 동일 점유는 한 실행만 성공, 재시작 후 같은 이력 조회, backup→빈 target 복원을 확인하라. 필요한 기기 간 충돌/Hook/모델 시험은 개발 서버 인계 뒤 함께 검증하며 미실행은 구별한다.

기록: UTC·commit/core/schema·OS/Python/구동 계정·request/event/run refs·전후 revision·actual exit·오류 code/name·허용된 hash·증거 참조. 불명확한 쓰기는 원 요청/현재 상태를 조회하고 완료·unlock·DB reset으로 바꾸지 마라. 문제는 실제 원인 근거와 최소 수정·재시험으로 해결하고 fixture 성공을 실제 서버 성공으로 확대하지 마라.

출력: 서버 운영 인계서(endpoint/공개 CA 참조/버전/namespace/repository/project UUID/개발 device·actor·scope/권한/credential 환경변수 이름), 서비스/작업 이름·시작/중지/상태·로그·backup·키 회전/복구 방법, actual 시험 결과와 미해결. 비밀값은 포함하지 않는다. 개발 서버와 실제 연결 및 데이터가 확인되기 전 전체 운영 수용으로 보고하지 마라.
