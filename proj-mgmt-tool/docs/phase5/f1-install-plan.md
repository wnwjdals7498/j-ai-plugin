# F1-1 새 venv 설치 계획 — 실행 전 승인 대기

근거: [Host 인계 §6](../handoff/codex-phase5-host.md), [Windows 설정 §1.1](setup-windows.md). 각 단계는 실행 전 변경 내용을 보여 주고 사용자 승인을 받는다. E3 push/PR 승인만 받았으며 아래 설치 명령은 아직 실행하지 않았다.

## 설치 대상

- 코드: GitHub에 push한 전체 SHA `06235fd25fcf28208a633f0fb5412d51939a9c15`. [PR #1](https://github.com/wnwjdals7498/j-ai-plugin/pull/1), head `feat/phase5-plugin-split`, base `master`.
- 새 venv: `C:\PMT\app\0.5.0\venv`. 2026-10-08 읽기 전용 확인에서 `C:\PMT\app\0.5.0` 없음. 경로의 0.5.0은 플러그인 버전이며 Core 패키지는 0.4.1이다.
- 기반 Python: `D:\Application\PMTHost\python\cpython-3.13.13-windows-x86_64-none\python.exe`, 3.13.13. 기존 독립 Python을 읽어 새 venv만 생성하며 기존 Host venv와 Python 파일은 수정하지 않는다.
- 설치 범위: 패키지의 선언된 `[host]` extra(FastAPI/Pydantic/Uvicorn), 해당 전이 의존성과 선언된 빌드 도구. 시험 extra와 `cryptography`는 추가하지 않는다. GitHub/패키지 저장소 네트워크 다운로드가 필요하다.

설치 단계는 새 app 폴더/venv와 다운로드 캐시·증거만 만든다. 운영 데이터·설정·비밀·TLS·작업 스케줄러·방화벽·8765 listener를 변경하거나 Host를 재시작하지 않는다. 서비스 계정의 실제 접근 가능 여부는 후속 adopt/기동 계획에서 별도 검증한다. 현재 Python 폴더 ACL을 읽었지만 서비스 계정 기동 성공으로 간주하지 않는다.

## 승인 후 실행할 순서

대상 경로가 생겼으면 덮어쓰지 않고 내용을 확인해 계획을 다시 정한다. 각 명령의 종료 코드를 검사하고 실패하면 다음 단계로 진행하지 않는다.

```powershell
$phase5AppRoot = 'C:\PMT\app\0.5.0'
$phase5Python = 'D:\Application\PMTHost\python\cpython-3.13.13-windows-x86_64-none\python.exe'
if (Test-Path -LiteralPath $phase5AppRoot) { throw 'Install target already exists; inspect before continuing.' }
& $phase5Python -m venv "$phase5AppRoot\venv"
if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
$phase5NewPython = "$phase5AppRoot\venv\Scripts\python.exe"
& $phase5NewPython -m pip install 'proj-mgmt-tool[host] @ git+https://github.com/wnwjdals7498/j-ai-plugin@06235fd25fcf28208a633f0fb5412d51939a9c15#subdirectory=proj-mgmt-tool'
if ($LASTEXITCODE -ne 0) { throw 'Host dependency installation failed' }
& $phase5NewPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Installed dependency check failed' }
& "$phase5AppRoot\venv\Scripts\pmt-server.exe" --json version
if ($LASTEXITCODE -ne 0) { throw 'PMT Server version check failed' }
```

`pip install`의 대상은 새 venv뿐이다. 설치 실패 시 기존 Host에 영향 없이 로그와 부분 설치 경로를 보존하고 복구안을 보고한다. 기존 경로나 새 폴더를 자동 삭제하지 않는다.

## 완료 판정과 증거

- `pip check` exit 0.
- `pmt-server --json version` exit 0; Plugin0.5.0/Core0.4.1/DB5/graph1/protocol1, Host metadata schema는 기존 계약; FastAPI/Pydantic/Uvicorn 버전 존재.
- 설치된 배포 metadata의 `direct_url.json`에 요청 SHA와 실제 `vcs_info.commit_id`가 모두 위 전체 SHA인지 확인한다.
- 명령·종료 코드·OS/Python/설치된 의존성 버전·비밀 없는 출력은 `evidence/2026-10-08/F1-install/`에 기록한다.
- 설치 성공 뒤 별도 F1-2 adopt 계획을 준비한다. init/adopt/doctor/serve, 운영 정지·백업, 서비스·방화벽 교체, 재부팅, claim key 이전, 운영 기기 발급은 이번 승인에 포함하지 않는다.

운영 포트 doctor/status 보호 장치는 유지한다. 제거 시도는 자동 승인 검토가 운영 안전장치 약화로 거절했으며, 이후 실제 운영 진단 경로는 별도 명시 승인과 검토가 필요하다.
