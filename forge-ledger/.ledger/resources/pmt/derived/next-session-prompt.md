# 인계: forge-ledger 플러그인 (PMT 프로젝트 forge-wiki-plugin)

## 시작 순서
1. `python3 ~/.claude/skills/proj-mgmt-tool/scripts/pmt.py resume forge-wiki-plugin` 출력만 읽고 시작한다.
2. 계획 원본은 `~/docs/projects/forge-wiki-plugin/resources/derived/forge-ledger-plan.html`이다(게시본: https://claude.ai/artifact/GoiCM7p1v12oJfdRfJxj5K). 통째로 읽지 말고 필요한 절만 grep으로 찾는다. 절 id: `summary`, `principles`, `borrowed`, `architecture`, `storage`, `method`, `forges`, `reading`, `automation`, `workflows`, `commands`, `roles`, `phases`, `decisions`, `risks`, `sources`.
3. 사용자에게 아래 결정 D1–D6과 플러그인 저장소 위치를 확인받는다. 답을 받기 전에는 구현하지 않는다.

## 목표
GitHub·Gitea·GitLab에 연결하는 Claude Code 플러그인 forge-ledger를 만든다. 프로젝트 현황은 위키에 생성물로 두고, 결정 이력과 작업 구조는 저장소 `.ledger/`를 원천으로 둔다. 상세 내용은 필요할 때만 단계적으로 읽게 한다. PMT 스킬과 ai-memory 플러그인(`~/.claude/plugins/cache/iteasy/ai-memory/0.8.2/`)의 방식을 참고했다.

## 현재 상태
- 계획 v2까지 끝났다(Work1-1, Work1-2 Done). 구현은 시작하지 않았다.
- 결정 대기는 백로그 B1(D1–D5)과 B2(D6)이다. 추천은 모두 C안이다.
  - D1 엔진 형태: Python 표준 라이브러리 CLI를 원천으로 두고, P5에서 얇은 MCP 래퍼를 추가한다.
  - D2 위키 갱신 주체: CI가 기본이고, CI가 없는 저장소만 로컬에서 동기화한다.
  - D3 결정 ID: `D-YYMMDD-xxxx`(날짜+짧은 해시)를 원천으로 쓰고, 위키에서는 순번 별칭을 보여 준다.
  - D4 PMT 관계: PMT는 개인 실행, forge-ledger는 팀 공유 지식을 맡고 ID로 잇는다.
  - D5 GitLab 시험 환경: GitHub·Gitea로 먼저 진행하고, GitLab은 P2 끝에 정한다.
  - D6 작업 계획의 원천: 구조(범위·완료 기준·verify·선행)는 `.ledger/plan/`, 상태·담당·재개 메모는 포지 이슈에 둔다.
- 플러그인 저장소 위치는 미정이다(`/data/workspace/github/forge-ledger` 없음).

## 설계 요약
- **값 위치**: 저장소 `.ledger/`에 `ledger.yml`, `project.md`, `status.yml`, `plan/`, `decisions/`, `facts/`, `journal/`을 둔다.
  - 위키 페이지 `Home`·`Plan`·`Decisions`·`Milestones`·`Timeline`·`Weekly`·사이드바는 생성물이다. 생성 구역과 수동 구역을 표시로 나눈다.
  - 토큰은 userConfig `sensitive`, CLI 로그인, CI 비밀값에만 둔다.
  - 캐시와 세션 표시는 `${CLAUDE_PLUGIN_DATA}`에 둔다.
- **업무 5단계**: 전체 작성 → 세부 정리 → 단위 분리 → 의존성·우선순위 → 착수(`next`·`start`·`note`·`verify`·`end`). 단계를 넘어가는 조건은 `ledger plan check`가 검사한다.
  - 착수 가능 목록은 우선순위 → 핵심 경로 → 풀리는 뒤 작업 수 → ID 순으로 정렬한다.
- **구성**:
  - 스킬 3개: `ledger-record`, `ledger-read`, `ledger-wiki`
  - 명령 10개: init, plan, next, work, status, decide, find, sync, report, doctor
  - 훅 6종: SessionStart, PostToolUse, PreToolUse, Stop, PreCompact, SessionEnd. 실패해도 작업을 막지 않고(fail-open), Stop은 `stop_hook_active`를 보고 한 번만 막는다.
  - 엔진: `scripts/ledger.py`와 `scripts/forges/{github,gitea,gitlab}.py`
  - CI 템플릿 3종
- **단계**:
  - P1: 저장 형식, 엔진, 읽기, 로컬 plan·next·start
  - P2: 위키 생성기, 포지 어댑터, `plan apply`
  - P3: CI, PR 필수 검사, 나머지 훅
  - P4: 업무 자동화
  - P5: MCP 래퍼, PMT·ai-memory 연동, eval

## 검증된 사실
아래는 다시 조사하지 않는다. 출처는 계획 HTML의 근거 S1–S21과 PMT 사실 F1–F4에 있다.
- **위키 저장 방식**: 세 포지 모두 위키가 git 저장소다.
  - GitHub는 첫 페이지를 웹에서 만든 뒤에 clone할 수 있다.
  - GitHub 비공개 저장소의 위키는 Pro·Team·Enterprise 요금제에서만 쓸 수 있다.
- **CI에서 위키 쓰기**:
  - GitHub: `GITHUB_TOKEN` 권한 표에 wiki 항목이 없어 별도 토큰이 필요하다.
  - GitLab: CI job token이 쓸 수 있는 기능 목록에 wiki가 없어 프로젝트 액세스 토큰이 필요하다.
  - Gitea: 작업 토큰에 `wiki` 범위가 있다. 실제 push는 시험하지 않았다.
- **사이드바 파일**: GitHub·Gitea는 `_Sidebar`·`_Footer`, GitLab은 `_sidebar`다.
- **위키 API**: Gitea는 `/repos/{owner}/{repo}/wiki/...`, GitLab은 `/projects/:id/wikis`다. GitHub 문서는 웹 편집과 git clone만 안내한다.
- **의존성**:
  - GitHub: blocked by, sub-issue(부모당 100개, 8단계)
  - Gitea: 의존성 기능이 기본으로 켜져 있다.
  - GitLab: blocks·is blocked by는 Premium·Ultimate 전용이고, Free에는 relates to만 있다.
- **Claude Code 플러그인 규격**:
  - 매니페스트는 `.claude-plugin/plugin.json`에만 둔다.
  - userConfig의 `sensitive` 값은 보안 저장소에 들어가고, 훅 프로세스에는 `CLAUDE_PLUGIN_OPTION_<KEY>`로 전달된다.
  - `${CLAUDE_PLUGIN_DATA}`는 플러그인을 업데이트해도 유지된다.
  - 훅 출력은 10,000자에서 잘린다.
- **PMT 엔진**: 착수 가능 = `Planned`이고 차단 없음, ID 순 정렬만 한다(`pmt.py` 1404–1410행). 위임 게이트(926–935행)와 30분 게이트(936–948행)가 있다.
- **환경**: `gh` 2.97.0이 로그인되어 있다. `tea`·`glab`은 없다. 시험용 Gitea 1.27.2가 이 서버에서 systemd `gitea.service`로 돌고 있다.

## 미확인 (P2에서 시험)
- Gitea 작업 토큰으로 위키 git push가 되는지
- Gitea 1.27에 상하위 이슈 기능이 있는지
- 계획 HTML의 mermaid 도표 6개가 제대로 렌더링되는지

## 주의
- 작업은 PMT로 관리한다. 새 Item을 추가한 뒤 `start` → `note` → `verify` → `end` 순서로 진행하고, 결정은 사용자가 합의한 뒤 `pmt decide`로 남긴다.
- 하위 작업 위임은 전역 규칙의 herdr 순서를 따른다.
- IP, 접속 주소, 토큰, 계정 같은 민감값은 계획, 코드, 기록에 넣지 않는다.
- 이전 세션에서 로컬 headless Chrome 실행이 권한 분류기에 두 번 막혔다. 다시 시도하지 말고, 필요하면 사용자 허락을 받는다.
- 관련 프로젝트: PMT `gitea-dev-workflow`(Gitea 개발 운영 지도, https://claude.ai/artifact/QTQ8dxdcvKKRL3FThRAsWd)
