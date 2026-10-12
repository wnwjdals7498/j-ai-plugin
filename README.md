# J AI Plugins

Codex와 Claude Code에서 같은 GitHub 저장소로 PMT를 설치합니다. PMT Core 0.4.0, SQLite schema 5이며 Python 3.13 이상이 필요합니다.

## Codex

이 변경이 GitHub에 반영된 뒤 터미널에서 실행합니다.

```powershell
codex plugin marketplace add wnwjdals7498/j-ai-plugin
codex plugin add pmt-lifecycle@j-ai-plugins
```

앱을 재시작하고 새 세션의 `/hooks`에서 PMT 실행 명령을 검토해 신뢰합니다.

## Claude Code

이 변경이 GitHub에 반영된 뒤 터미널에서 실행합니다.

```powershell
claude plugin marketplace add wnwjdals7498/j-ai-plugin
claude plugin install pmt-lifecycle@j-ai-plugins
```

설치 후 `/plugin`에서 PMT의 `python_path`를 Python 3.13 이상 실행 파일의 절대 경로로 설정합니다. 값이 없으면 훅은 실행되지 않습니다. 새 세션에서 사용합니다.

## PMT 데이터와 서버 연결

두 제품은 같은 PMT 본체와 스킬을 사용하며 제품별 Hook은 각각의 manifest에서 선택합니다. 설치·업데이트는 PMT 서버 연결이나 기존 데이터 이관을 자동으로 수행하지 않습니다.

데이터 경로와 HTTPS 서버 연결은 기기별로 설정합니다. [PMT 사용 안내](proj-mgmt-tool/docs/usage.md)와 [개발 환경 연결 절차](proj-mgmt-tool/docs/handoff/development-plugin.md)를 따릅니다.

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
| Codex | `.agents/plugins/marketplace.json` | `./proj-mgmt-tool` |
| Claude Code | `.claude-plugin/marketplace.json` | `./proj-mgmt-tool`, `./model-handoff` |

저장소 원천을 직접 설치하므로 별도 `dist` 생성이 필요하지 않습니다. ZIP 배포가 필요하면 기존 `proj-mgmt-tool/scripts/build_plugins.py`를 사용합니다. 새 배포에서는 본체·플러그인 manifest·Claude 마켓플레이스 항목의 버전을 함께 갱신합니다.

공식 규격: [Codex 플러그인·마켓플레이스](https://developers.openai.com/plugins/build/plugins), [Claude Code 마켓플레이스](https://code.claude.com/docs/en/plugin-marketplaces), [Claude Code manifest](https://code.claude.com/docs/en/plugins-reference).
