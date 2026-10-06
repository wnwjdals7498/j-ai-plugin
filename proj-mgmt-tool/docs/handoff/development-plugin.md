# 개발 서버 플러그인 설치용 handoff 프롬프트

아래 본문을 개발 서버에서 작업할 AI에게 전달한다. Windows Host의 비밀 없는 인계 정보와 별도 보호 경로의 해당 개발 기기 credential이 선행 입력이다.

---

이 개발 서버에서 PMT 플러그인을 설치하고 Windows PMT Host에 연결해 실제 작업 이력과 새 세션 재개를 사용할 수 있게 하라. 사용 중인 에이전트/OS를 조사하고 해당 제품의 실제 설치·Hook과 PMT 기능을 검증해 결과를 남겨라.

## 목적·범위

- 목적: Git·코드·모델 실행은 이 개발 환경에서 수행하고 공유 상태·이력은 Windows Host 한 곳에 저장한다.
- 기술: Python3.13 이상·PMT client/제품별 native plugin·skill/Hook·표준 라이브러리 HTTPS client·현재 run/범위 점유·private spool.
- 범위: 기존 설정 보존, 호환 본체·번들/skill/Hook 설치, 기기별 protected credential·hosted profile/mapping, 실제 제품 세션·코드/검증·이력/재개 시험.
- 제외: Host DB 직접 연결/복사/SMB, Host에서 모델 실행, local business DB fallback, unconfirmed 기존 작업 takeover/Done/unlock, 직접 모델 API 추가 또는 설치 시험을 핑계로 무승인 모델 서비스 호출.

## 기준·입력

저장소: https://github.com/wnwjdals7498/j-ai-plugin
컴포넌트: proj-mgmt-tool
구현 기준 commit: 9f555185db9e772d77b71060144d3c39ca42fed2
Host/client 기대: Core0.4.0 / SQLite5 / graph1 / protocol1

읽을 것: AGENTS.md, docs/handoff/deployment-order.md(있으면), docs/phase4/implementation-status.md와 runtime-contract.md, skills/proj-mgmt-tool/references/host-workflow.md·continuity-workflow.md. 현재 code/CLI/공식 제품 문서를 우선하며 기존 0.3 예제를 그대로 사용하지 마라.

Windows Host로부터 받을 값: actual HTTPS endpoint, 공개 CA/신뢰 방법, namespace_id, 이 기기의 device_id·actor·scope/permission, repository_id·project_id, credential 환경변수 이름과 보호 전달 완료. Host claim key/TLS private key/admin token은 입력이 아니다.

현지 조사: 개발 OS·Python/Git, 실제 코딩 에이전트(Codex/Claude Code/OpenCode) 및 버전·CLI/Desktop/remote 형태, Hook/Python이 실제 실행되는 계정/컴퓨터, 각 repo checkout·branch·상대 graph 경로, 기존 PMT data/config/설치·미처리 실행·이력. UI가 다른 PC에 있어도 실제 Hook 실행 환경에 본체/번들이 있어야 한다. 필요한 주소/설치 범위/실사용자 전환 값만 짧게 확인하라.

## 진행 순서

1. 기존 제품 설정·PMT profile/credential 참조·로컬 데이터와 dirty Git 변경을 보존한다. 사용하려는 Host/client code 기준을 맞추고 Python3.13 이상을 확인한다. Git root/하위 workspace, branch/detached/non-Git 상황을 추정하지 않는다.
2. 같은 release로 `scripts/build_plugins.py`를 실행해 사용 제품의 0.4.0 bundle을 만든다. dist ZIP은 Git에 포함되지 않으므로 다른 컴퓨터의 local dist가 있다고 가정하지 않는다. manifest와 actual files/버전을 대조한다. 기존 버전 폴더를 덮지 말고 version 경로를 별도로 보존한다.
3. 제품별 공식 방식으로 플러그인·skill을 등록한다. Codex는 local marketplace 등록/설치/활성화와 `/hooks`의 신뢰 검토를 별도 확인한다. 실제 CLI 버전의 help가 지원하지 않는 명령을 무작정 실행하거나 Desktop 전용 흐름을 headless 성공으로 표시하지 마라. Claude는 local marketplace 또는 먼저 `--plugin-dir`로 로딩을 확인하고 실제 설치 범위와 새 세션을 검증한다. 기존 marketplace/다른 플러그인 설정을 통째로 덮지 않는다.
4. OpenCode는 버전별 공식 API와 현재 V1 어댑터를 대조한다. V2는 V1 구현이 그대로 실행되지 않으므로 등록만으로 완료를 주장하지 마라. V2 필요 시 원 source의 호환 포팅·별도 테스트·release/양쪽 버전 반영을 독립 변경으로 남긴다. cache만 고쳐 재현 불가능하게 만들지 마라. 먼저 실제 호환되는 제품 하나로 Host 연결을 완료한다.
5. 개발 기기 전용 ConfigRoot와 DataRoot를 제품/checkout 코드 밖에 정한다. 같은 기기의 여러 제품은 같은 설정/이력을 사용하고, 다른 기기의 profile/environment UUID·device token은 복사하지 않는다. credential은 protected storage에서 지정 환경변수로 주입한다. PMT_CONFIG_ROOT, PMT_DATA_ROOT, PMT_PYTHON(절대 실행 경로), 명시 PMT_SCOPE_ID(project UUID)와 선택 PMT_RECORD_ID를 실제 에이전트/Hook 프로세스에 전달한다. Hook의 외부 첫 Python도 `python`/Windows `py -3`가 지원 버전을 가리키는지 확인한다. PMT_PYTHON만 설정했다고 처음 Hook 실행기까지 바뀌었다고 가정하지 마라.
6. `pmt storage status`의 config hash를 읽고 `pmt storage configure`로 hosted를 설정한다. 최초 hash는 null, 기존 설정은 현재 hash로 CAS한다. 입력은 mode/endpoint/credential_env/device_id/namespace_id/expected_actor/CA·workspace_mappings다. mapping은 repository_id/project_id/branch/branch_key_sha256/local_root/relative_graph_path이며 branch hash는 실제 branch key의 UTF-8 SHA256이다. local_root는 이 기기의 절대 checkout 경로다. profile 환경 UUID는 configure가 생성/보존하게 한다. TLS·인증 compatibility·setup session의 실제 probe가 성공한 뒤 설정을 게시하고 `storage probe/status`로 확인한다. hosted profile에 일반 local setup을 실행해 별도 업무 DB를 만들지 마라.
7. canary scope에서 현재 facts/저장/조회/동일 request replay·stale revision을 확인한다. native SessionStart가 해당 project의 bounded overview/checkpoint를 읽는지 실제 제품에서 검증한다. 같은 기기의 별도 세션과 다른 기기가 가능하면 실제 공통 work/path 점유 충돌을 확인한다. 정적 Hook 파일·직접 bridge fixture만으로 제품 설치 성공을 주장하지 마라.
8. 실제 canary Work/Item/Step의 승인된 실행 경로를 사용한다. 현재 scope/run claim→실제 source/지시 버전→F5/재개 문맥→작업/독립 코드 검사·증거→상위 검토→명시 완료 순서다. 같은 코딩 에이전트가 대상 모델/권한을 지원하면 native subagent를 우선한다. 직접 API는 추가하지 않는다. 실제 모델 세션은 사용자의 승인된 작업에서 시험하며 허용이 없으면 제품 로딩·Hook/저장 시험과 모델 작업을 구별해 미실행을 기록한다.
9. 변경·재개는 capture_work_basis→collect_changes/link index→assessment/applicability→실제 F1/F3/readback→typed apply→current F5 index/bundle/detail로 확인한다. 다른 session의 private content·old cursor는 권한이 아니다. Stop/idle로 완료/점유 해제하지 마라. Host 불통에서는 신규 shared 작업을 중단하고 이미 생성된 own terminal result만 pending에 보존해 원 요청으로 조정한다.
10. 기존 local 이력이 있으면 별도 백업·quiescent source·빈 Host import target을 확인하고 사용자 전환 결정 후 이관한다. 기존 local DB를 삭제하거나 두 shared primary를 계속 사용하지 않는다. 복원 시 credential/profile/claim/process 소유권을 데이터와 함께 복사하지 않는다.

## 시험·로그·완료 조건

실제 제품 설치/활성화·새 세션/Hook, trusted TLS+0.4.0/5/1/1 호환, actual actor/scope 인증, shared 기록 재조회, 같은 점유의 한 실행만 성공, exact replay·미완료 보존·stale source 거부, 실제 승인 작업의 code test/evidence와 독립 검토를 확인하라. Hosted DataRoot에는 pending/private spool이 존재할 수 있고 ConfigRoot에는 routing-client.sqlite3가 있을 수 있으나 local business primary(pmt.sqlite3)를 만들어 fallback하면 안 된다.

로그에는 UTC·code/제품/OS/Python·scope/session/run·지시/source/basis/checkpoint·revision·result/error/SQLite code/name·시간·허용된 evidence/hash를 남긴다. 전체 대화/지시·credential·환경변수 전체·raw argv/PID/절대경로를 shared metadata/log로 보내지 마라. 작업 폴더의 간헐적 파일 접근 실패는 실제 실행 계정/저장 위치에서 조사하고 실패 자료를 보존한다. 이유를 추측하거나 DB reset/무조건 재실행으로 성공처럼 만들지 마라.

출력: 제품별 실제 설치/Hook/모델 작업 tier 지원표, current config의 비밀 없는 endpoint/device/namespace/environment/scope/mapping 요약, actual 시험·exit·전후 refs, 재시작/업데이트/재설치/credential 회전 및 pending 복구 방법, Windows Host와 함께 확인한 이력/점유·재개 결과와 미해결. 외부 실서버·제품이 미실행이면 계획/fixture를 성공으로 대신하지 마라.
