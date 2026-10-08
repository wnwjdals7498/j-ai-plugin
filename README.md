# J AI Plugins

PMT 0.5.0은 두 플러그인으로 나뉜다. **pmt-lifecycle (PMT)**는 개발 기기의 프로젝트·세션·작업 기록, **pmt-server (PMT Server)**는 Host 관리용. Core0.4.1 / SQLite5 / graph1 / protocol1 / Host APIv1 유지. Python3.13 이상 필요.

변경이 GitHub에 반영된 뒤 필요한 플러그인을 설치한다.

```text
codex plugin marketplace add wnwjdals7498/j-ai-plugin
codex plugin add pmt-lifecycle@j-ai-plugins
claude plugin marketplace add wnwjdals7498/j-ai-plugin
claude plugin install pmt-lifecycle@j-ai-plugins
```

Claude /plugin에서 PMT python_path만 지정하면 새 세션에서 local 준비. Codex는 PMT_PYTHON을 지정하고 새 세션 /hooks에서 실행 명령을 검토·신뢰한다. Git checkout에서 pmt link --new "프로젝트 이름", pmt check로 시작한다. PowerShell은 pmt.cmd를 사용한다.

Host 연결은 비밀 없는 인계 JSON과 별도 credential로 pmt connect를 사용한다. local 프로필은 입력 옵션만으로 hosted로 바뀌지 않으며 pmt storage switch로 명시 전환한다. 같은 ConfigRoot를 쓰는 Claude/Codex는 한 기기 identity를 공유한다. 별도 기기를 쓰면 별도 ConfigRoot를 지정한다. 설치·업데이트·제거는 사용자 DB·설정을 삭제하거나 이관하지 않는다.

[PMT 사용 안내](proj-mgmt-tool/docs/usage.md), [Windows 설정](proj-mgmt-tool/docs/phase5/setup-windows.md), [Linux 설정](proj-mgmt-tool/docs/phase5/setup-linux.md), [확인 결과](proj-mgmt-tool/docs/phase5/progress.md)를 따른다.

Host 관리자 컴퓨터에서는 pmt-server@j-ai-plugins를 설치하고 별도 Host venv에 같은 release의 proj-mgmt-tool[host]를 설치한다. 서버 플러그인은 Core를 복사하지 않고 그 venv를 사용한다. [pmt-server 스킬](pmt-server/skills/pmt-server/SKILL.md)과 [운영 Host 인수 관문](proj-mgmt-tool/docs/handoff/codex-phase5-host.md)을 따른다.

마켓플레이스는 Claude .claude-plugin/marketplace.json, Codex .agents/plugins/marketplace.json. 두 목록은 각각 ./proj-mgmt-tool 및 ./pmt-server를 참조한다. 직접 설치에는 dist가 필요 없다. ZIP 배포는 proj-mgmt-tool/scripts/build_plugins.py로 codex/claude/opencode 클라이언트와 server를 만든다. 논리 플러그인은 두 개이고 제품 배포 대상은 네 개다.

공식 규격: [Codex plugin package](https://developers.openai.com/plugins/build/plugins), [Claude marketplace](https://code.claude.com/docs/en/plugin-marketplaces), [Claude manifest](https://code.claude.com/docs/en/plugins-reference).
