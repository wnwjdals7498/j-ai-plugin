# A1 공식 manifest 근거

조회일: 2026-10-08.

- OpenAI, Package your plugin: https://developers.openai.com/plugins/build/plugins — “Add OpenAI-specific metadata” 절, interface.displayName 예제(조회본 L1261–1297), “Plugin structure” 절의 .codex-plugin/plugin.json 호환 fallback(L1218–1240). 현재 설치된 공식 spreadsheets 호환 manifest의 interface.displayName도 확인했다. 이 작업은 기존 호환 형식을 유지하며 표시 이름만 PMT로 채운다.
- Claude Code, Plugin manifest reference: https://code.claude.com/docs/en/plugins-reference — displayName 절(조회본 L274–276): 설치 ID와 별도로 UI 이름 지정. “Validate the manifest” 절(L198–208): 실제 CLI 검증으로 수용 확인. PMT manifest는 현재 Claude 2.1.280의 validate에서 exit0으로 확인했다.

공식 문서 확인은 manifest 필드 근거다. 실제 새 제품 세션 설치·Hook 신뢰/enablement 수용은 이후 E/F 시험과 구별한다.
