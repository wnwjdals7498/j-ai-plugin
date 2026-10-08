# A0 관찰 명령

각 명령은 읽기 전용이거나 격리된 개발 checkout/venv/시험 데이터만 변경한다. 운영 설정·프로세스·방화벽·기기·비밀은 변경하지 않는다.

## 저장소·환경

- `git rev-parse --show-toplevel`, `git status --short`, `git rev-parse HEAD`.
- `git clone https://github.com/wnwjdals7498/j-ai-plugin.git C:\PMT\src\j-ai-plugin`, exit 0.
- `git switch -c feat/phase5-plugin-split origin/master`, exit 0, 기준 `8054c83a85b73356f0e2e52322855b9e03b419ce`.
- `python --version`: 3.14.5rc1.
- `python -m venv C:\PMT\src\venv-dev`; editable install `[test,host,host-test]`, exit 0. 새 프로젝트 의존성 추가 없음.
- `py -0p`: Python3.13 등록 경로는 오래됨. 해당 경로로 venv 생성 시 실패; 운영 base의 실제 Python 3.13.13으로 별도 `venv-dev-313` 생성·기존 extra 설치, exit 0. 운영 venv는 읽기 버전 확인만 수행.
- `claude --version`: 2.1.280; `codex --version`: 0.160.1. 각각 exit 0.
- `claude plugin validate C:\PMT\src\j-ai-plugin`: exit 0.
- `claude plugin validate C:\PMT\src\j-ai-plugin\proj-mgmt-tool`: exit 0.

## 운영 Host 조사

- `Get-ScheduledTask`, 필터 PMT: 실행 중 `PMT-Host`, jjm, supervisor executable/경로만 보존.
- `Get-ScheduledTaskInfo -TaskName PMT-Host`: 마지막 실행 시각.
- `Get-CimInstance Win32_Process`: live PID의 상세 command line/executable/owner는 권한 제한으로 미확인.
- `Get-CimInstance Win32_Service`: PMT 서비스 발견 없음.
- `Get-NetTCPConnection -State Listen`: `0.0.0.0:8765`, PID 27756; 시험용 18765 listener 없음.
- `Get-NetFirewallRule`, `Get-NetFirewallPortFilter`, `Get-NetFirewallAddressFilter`: 기존 다섯 규칙 읽기. `firewall-survey.json`.
- supervisor runtime.json에서는 data/config root, host/port, 환경변수 이름/key ID, 인증서/개인키 경로, Python 경로 등 비밀 없는 참조만 보존. 실제 key/credential/private-key 파일은 열지 않음.
- 운영 Python의 `importlib.metadata.version('proj-mgmt-tool')`: 0.4.0, Python 3.13.13, exit 0.
- 공개 leaf를 cryptography로 읽어 SAN/만료/fingerprint 확인. TLS 개인키는 읽지 않음.
- `urllib` GET `https://127.0.0.1:8765/health`: `ssl.create_default_context(cafile=<공개 CA>)`, `ProxyHandler({})`, timeout5. HTTP200/status ok/API1, exit 0.

## Graphify

- `graphify codex install`, exit 0. root AGENTS.md와 .codex/hooks.json 생성.
- `graphify extract .`, exit 1. headless semantic chunks 7/7 HTTP401; 인증 상세는 로그에서 제거. 문서 의미 graph 미생성.
- `graphify update . --no-cluster`, exit 0. AST-only graph 9396 nodes/19415 edges. 외부 API 없이 구조 graph 생성.
- `graphify query '<Phase5 관련 심볼>' --budget <900~1500>`: 생성 전 query exit1(graph 없음), 생성 후 exit0. 구조 graph 기준 결과만 사용.

## 기준선·진단

- 첫 전체 pytest 명령·시간·Python·OS·exit는 `initial-baseline-meta.json`; 출력/XML은 `initial-pytest.txt/xml`. 119 passed/1 failed/559 errors. 559 errors의 원인은 `.pmt-test` 부모 디렉터리 누락. 나머지 redirect 실패 원인 미확정.
- `.pmt-test` 부모만 생성 후 같은 제품 코드로 전체 pytest 재실행. `baseline-meta.json`, `pytest.txt`, `pytest.xml`.
- pytest 환경변수는 격리 ConfigRoot/DataRoot만 지정. 비밀 환경변수·전체 argv를 출력/기록하지 않음.
- 원래 redirect fixture를 단독 호출해 오류 code, 내부 예외 type/errno, 요청 개수, Python만 보존. 3.14.5rc1·3.13.13 모두 remote_http_error/request_count1; 최초 실패 재현 안 됨. 해당 결과는 전체 suite 성공·원인 해결의 증거가 아니다.

## 관문

- D1~D8 확인을 한 표/한 질문으로 요청. 답변 전 A1 이후 작업 금지.
- E3 종료 후 push/PR, F1 운영 단계는 인계 문서의 별도 승인 관문 유지.
- F2/F3는 이 컴퓨터에서 실행하지 않으며 F1 실측값이 확보되면 인계 문서를 작성한다.
