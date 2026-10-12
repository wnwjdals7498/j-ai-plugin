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

## model-handoff (Claude Code)

계획한 태스크를 Codex·Antigravity·개인 Claude 계정에 우선순위대로 병렬로 넘깁니다. 실행자가 사용량 한도에 걸리면 해제 시각까지 대기로 표시하고 다음 순위로 전환합니다. Python 3.9 이상, 표준 라이브러리만 사용합니다.

```bash
claude plugin install model-handoff@j-ai-plugins
```

- 사용: 세션에서 "하위 모델로 병렬 작업하자" → 태스크 분할·실행자 제안 → 승인 후 실행
- 설정: `/model-handoff:setting` 또는 `handoff config order codex,agy,claude`, `handoff status`
- 설정·대기 상태: `~/.config/model-handoff/` (`MODEL_HANDOFF_HOME`으로 변경)
- 승인 방식: 기본은 각 CLI의 자동 승인 모드입니다(Codex `--approve-for-me` + 샌드박스 네트워크 허용, Antigravity `--sandbox` + `toolPermission: proceed-in-sandbox`(`handoff setup agy`로 설정), Claude `--permission-mode auto`). 모든 검사를 끄는 `bypass`는 `handoff config set <실행자>.permission bypass`로 직접 지정해야 하며 격리된 환경에서만 사용합니다.
- 개인 계정 분리: `handoff config set claude.config_dir <경로>`, `handoff config set codex.codex_home <경로>`
- 주의: Antigravity 공식 FAQ는 소비자 요금제에서 agy를 다른 코딩 에이전트 안에서 감싸 쓰는 것을 지원하지 않으며 계정 정지 사유가 될 수 있다고 안내합니다. `handoff config disable agy`로 끌 수 있습니다.

## 마켓플레이스 구조

| 제품 | 목록 | 플러그인 경로 |
|---|---|---|
| Codex | `.agents/plugins/marketplace.json` | `./proj-mgmt-tool`, `./pmt-server` |
| Claude Code | `.claude-plugin/marketplace.json` | `./proj-mgmt-tool`, `./pmt-server`, `./model-handoff` |

저장소 원천을 직접 설치하므로 별도 `dist` 생성이 필요하지 않습니다. ZIP 배포는 `proj-mgmt-tool/scripts/build_plugins.py`로 codex/claude/opencode 클라이언트와 server를 만듭니다. 새 배포에서는 본체·플러그인 manifest·마켓플레이스 항목의 버전을 함께 갱신합니다.

공식 규격: [Codex 플러그인·마켓플레이스](https://developers.openai.com/plugins/build/plugins), [Claude Code 마켓플레이스](https://code.claude.com/docs/en/plugin-marketplaces), [Claude Code manifest](https://code.claude.com/docs/en/plugins-reference).
