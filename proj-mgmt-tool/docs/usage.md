# PMT 사용

클라이언트 플러그인 PMT 0.5.0은 Core 0.4.1, SQLite5, graph1, protocol1을 사용한다. Python 3.13 이상 필요. Host API v1과 기존 업무·인증 계약은 유지한다. 현재 확인 범위는 [5단계 진행 기록](phase5/progress.md), 과거 실측은 [4단계 결과](phase4/implementation-status.md)를 따른다.

## local 시작

[저장소 설치 안내](../../README.md)로 pmt-lifecycle을 설치한다. Claude는 /plugin에서 python_path만 지정하면 새 세션 SessionStart에 local 준비가 진행된다. Codex는 PMT_PYTHON을 지정하고 /hooks에서 실행 명령을 검토해 신뢰한 뒤 새 세션을 연다.

명시 PMT_CONFIG_ROOT와 PMT_DATA_ROOT가 우선한다. 기본 경로는 Linux ~/.config/pmt와 ~/.local/share/pmt, Windows %APPDATA%/pmt와 %LOCALAPPDATA%/pmt/data. 기존 Windows의 ~/.config/pmt 및 기존 XDG data가 모두 있으면 그대로 유지한다. 코드 캐시와 데이터는 분리하고 설치·제거·업데이트가 DB를 이동하거나 합치지 않는다.

빈 기기에서 첫 link/check 같은 준비 명령도 local 설정을 만든다. mode는 읽기 전용이며 빈 기기의 저장소를 만들지 않는다. Git checkout에서 실행:

```text
pmt mode
pmt link --new "내 프로젝트"
pmt projects
pmt check
pmt add work "기능 구현"
pmt add item "첫 작업" --parent <work-id> --criteria "검증 기준"
pmt start <item-id>
pmt done <item-id> --test "<실제 시험 명령>" --result "확인한 결과"
```

done 전 변경을 커밋한다. 미커밋 변경은 실행을 막고, 시험 실패는 Item In Progress와 점유를 보존한다. 완료 근거는 실제 명령의 exit code와 stdout, Core verification/evidence다. pause는 명시적 해제이며 Stop이나 세션 종료는 Done으로 바꾸지 않는다. 같은 저장소의 다른 branch는 기존 project를 재사용한다. link <project-name>은 알려진 프로젝트를 선택하고 unlink는 현재 branch의 연결만 해제한다.

## hosted 연결

Host 관리자가 만든 비밀 없는 pmt-handoff/v1 JSON과 별도 credential 파일을 받는다. Host claim key와 TLS 개인키를 받지 않는다.

```text
pmt connect --handoff <handoff.json> --credential-file <credential-file>
pmt storage status
pmt projects
pmt link <project-name>
pmt check
```

connect --dry-run은 파일 형식, 호환 버전, 공개 CA hash 및 credential 입력을 확인하며 파일을 쓰거나 Host에 접속하지 않는다. 실제 connect는 TLS 인증·compatibility probe 뒤 CAS로 hosted 프로필을 게시한다. CA hash를 관리자와 별도 경로로 대조한다. 잘못된 CA/credential이나 게시 충돌은 기존 설정과 보호 저장소를 보존한다. 명시 PMT_HOST_CREDENTIAL과 별도 입력 credential이 다르면 먼저 환경변수를 정리하라는 오류를 낸다.

Claude는 /plugin의 handoff_file과 sensitive device_credential로 같은 인계를 사용한다. 기존 개별 host_url/device/namespace/actor 설정도 유지한다. 같은 ConfigRoot를 쓰는 Claude/Codex는 한 기기 identity를 공유한다. 다른 actor/device를 쓰려면 제품 시작 전에 서로 다른 PMT_CONFIG_ROOT를 지정한다.

Credential은 Linux owner-only 파일, Windows CurrentUser DPAPI에 보관한다. Hook 환경 파일에는 비밀이 없다. disconnect는 저장된 credential만 제거하며 hosted 프로필·CA·project 목록은 유지한다. Host가 중단돼도 local DB로 넘어가지 않는다.

자세한 클라이언트 절차와 오류·pending은 [Host workflow](../skills/proj-mgmt-tool/references/host-workflow.md), [Windows 개발 기기](phase5/setup-windows.md), [Linux 개발 기기](phase5/setup-linux.md)를 따른다. Host 구축·기기 발급은 별도 [pmt-server 스킬](../../pmt-server/skills/pmt-server/SKILL.md)에서 수행한다.

## 모드 전환과 기존 데이터

기존 local 프로필에 hosted 옵션을 입력하는 것만으로 전환하지 않는다. 명시 전환:

```text
pmt storage switch --to hosted --handoff <handoff.json> --credential-file <credential-file> --export <bundle.zip>
pmt storage switch --to local
```

미처리 pending이나 활성 claim/run이 있으면 switch_busy로 거부한다. local DB를 삭제하지 않고 기존 파일을 보존한다. export는 기존 quiescent migration 번들을 만든다. Host import는 관리자가 빈 namespace에 별도로 실행하며 클라이언트 switch가 가져오지 않는다. 원본 데이터와 기기 인증은 별개다.

## 진입점과 고급 작업

Bash는 bin/pmt, Windows PowerShell은 bin/pmt.cmd를 사용한다. PMT_PYTHON이 우선하며 저장된 client.json의 Python 경로가 fallback이다. Python이 없거나 지원 버전보다 낮으면 명확한 오류를 표시한다. 사용자 설정을 시험용으로 바꾸지 않는다.

기존 JSON Core CLI는 scripts/pmt.py로 유지한다. 요청 envelope와 operation은 [CLI workflow](../skills/proj-mgmt-tool/references/cli-workflow.md), 두 트리·모델·Queue는 [model workflow](../skills/proj-mgmt-tool/references/model-workflow.md), 세션 재개는 [continuity workflow](../skills/proj-mgmt-tool/references/continuity-workflow.md)를 따른다. 수집된 lifecycle 이벤트는 사용자 승인이나 업무 완료를 뜻하지 않는다.

## 번들 배포

저장소 원천을 마켓플레이스에서 직접 설치할 때 dist는 필요 없다. ZIP이 필요하면 이 컴포넌트에서 실행:

```text
python scripts/build_plugins.py --output-dir dist/plugins
```

기본 plugin 버전은 manifest의 0.5.0이며 Core 버전 0.4.1과 구분한다. 같은 버전 출력은 덮어쓰지 않는다. 논리 플러그인은 pmt-lifecycle과 pmt-server 두 개. 제품 배포 대상은 기존 codex/claude/opencode 클라이언트와 server이다. server에는 Core src를 넣지 않고 Host venv를 사용한다. 사용자 DB·설정·credential·개인키는 어느 번들에도 넣지 않는다.
