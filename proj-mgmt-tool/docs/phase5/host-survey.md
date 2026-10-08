# 운영 Host 조사 — 2026-10-08

읽기 전용 조사. 운영 프로세스·데이터·설정·비밀·기기·방화벽·자동 시작은 변경하지 않았다.

| 항목 | 관찰 |
|---|---|
| 실행 형태 | 작업 스케줄러 `\PMT-Host`, Running. Python supervisor가 자식 Host를 감시 |
| 작업 계정 | 작업 정의의 `jjm`. 라이브 프로세스 owner는 현재 CIM 권한으로 확인 불가 |
| 작업 마지막 실행 | 2026-10-06 17:37:48 Asia/Seoul |
| supervisor | `D:/Application/PMTHost/deployment/supervise.py` |
| supervisor 설정 | `D:/PMTHostState/host-config/runtime.json` — 비밀의 위치/이름만 조사 |
| Python/venv | `D:/Application/PMTHost/releases/9f555185db9e772d77b71060144d3c39ca42fed2/venv/Scripts/python.exe`, Python 3.13.13 |
| 설치 Core | `proj-mgmt-tool` 0.4.0 (설치 metadata 직접 확인) |
| 운영 deployment commit | `9f555185db9e772d77b71060144d3c39ca42fed2` (supervisor 설정) |
| data root | `D:/PMTHostState/host-data` |
| config root | `D:/PMTHostState/host-config` |
| bind / port | `0.0.0.0:8765`, 실제 LISTEN PID 27756 |
| claim key 환경변수 이름 | `PMT_HOST_CLAIM_KEY` — 값 확인/출력 없음 |
| claim key ID / retained keys | `primary` / 빈 목록 (설정의 식별자만 조사) |
| 공개 서버 인증서 | `D:/PMTHostState/secrets/server-chain.pem` |
| TLS 개인키 위치 | `D:/PMTHostState/secrets/server-key.pem` — 파일 열지 않음 |
| 인증서 SAN | IP `222.234.220.199`, `10.8.0.1`, `127.0.0.1`; DNS SAN 없음 (공개 leaf 직접 확인) |
| leaf 만료 | 2027-10-06T07:16:44Z (직접 확인) |
| CA | `D:/Application/PMTHost/public/pmt-root-ca.pem`; 공개 manifest상 만료 2036-10-03T07:16:44Z |
| CA DER SHA-256 | `4ef67f8b68430d79474b0ee80c990fb578a89a2088a5933a3077f8072aed54a7` (공개 certificate manifest) |
| `/health` | `https://127.0.0.1:8765/health` → HTTP 200, `status=ok`, `api_version=1`; 공개 CA·hostname 검증 유지, 이 요청에 proxy 미사용 |
| 방화벽 | 기존 규칙 5개 Enabled/Inbound: 승인 Public·WireGuard Allow 2개, Public 기타·WireGuard 기타·기타 인터페이스 Block 3개 |
| 별도 Windows 서비스 | 이름/실행 경로에 PMT가 있는 서비스 발견 없음 |
| namespace / 기기 목록 / 활성 claim·run | 이 조사에서 미조회. F1 승인 전 상태 점검 필요 |

## 조사 한계

- 라이브 프로세스의 ExecutablePath·CommandLine·owner는 현재 실행 권한에서 공개되지 않는다. 실행 중인 스케줄러 작업과 supervisor 설정의 인수를 근거로 삼았다. 실제 child argv를 직접 관찰했다고 보고하지 않는다.
- 조사용 read-only `/health`를 제외하고 운영 endpoint를 개발 시험에 사용하지 않는다.
- 비밀값, DPAPI blob, TLS 개인키, 기기 credential 파일은 열거나 출력하지 않았다.
- `codex-phase5-host.md`는 기존 0.4.x Host를 가정한다. 실제 설치 Core는 0.4.0이며, 0.4.1은 문서의 클라이언트 설치 관찰과 구별한다.

## 개발·시험 격리

- checkout `C:/PMT/src/j-ai-plugin`, branch `feat/phase5-plugin-split`, 기준 `8054c83`.
- 개발 venv `C:/PMT/src/venv-dev` (Python 3.14.5rc1 ≥3.13).
- 시험 ConfigRoot/DataRoot는 checkout의 `proj-mgmt-tool/.pmt-test/` 아래.
- D/F 단계 통합 시험 Host는 loopback `127.0.0.1:18765` 예정이며 아직 기동하지 않았다. 기존 pytest/진단 fixture는 loopback의 OS 할당 임시 포트를 사용한다. 운영 8765는 시험에 사용하지 않는다.
- 위 운영 data/config/secrets·release/venv·작업·TCP 8765는 시험 금지 영역.

## 근거

`evidence/2026-10-08/A0/host-survey.json`, `firewall-survey.json`, `tls-leaf.json`.
관찰 명령은 baseline 요약에 기록한다. 공개 인증서 외 운영 파일의 본문·전체 환경변수·전체 argv를 증거에 복제하지 않는다.
