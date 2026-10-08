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

## 마켓플레이스 구조

| 제품 | 목록 | 플러그인 경로 |
|---|---|---|
| Codex | `.agents/plugins/marketplace.json` | `./proj-mgmt-tool` |
| Claude Code | `.claude-plugin/marketplace.json` | `./proj-mgmt-tool` |

저장소 원천을 직접 설치하므로 별도 `dist` 생성이 필요하지 않습니다. ZIP 배포가 필요하면 기존 `proj-mgmt-tool/scripts/build_plugins.py`를 사용합니다. 새 배포에서는 본체·플러그인 manifest·Claude 마켓플레이스 항목의 버전을 함께 갱신합니다.

공식 규격: [Codex 플러그인·마켓플레이스](https://developers.openai.com/plugins/build/plugins), [Claude Code 마켓플레이스](https://code.claude.com/docs/en/plugin-marketplaces), [Claude Code manifest](https://code.claude.com/docs/en/plugins-reference).
