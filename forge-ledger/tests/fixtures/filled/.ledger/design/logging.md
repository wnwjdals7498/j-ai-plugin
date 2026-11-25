## 구현 4 로깅 처리 레퍼런스

- **로그 흐름**: 웹훅 수신 후 서명 확인 → 민감 필드 제거 후 원본 저장 → 200 응답 → 엔터티 반영·링크 해석·규칙 평가.
- **형식**: 표준 출력 한 줄 JSON(ts, level, component, source, event_id, correlation_id, action, result, duration_ms).
- **보관 기간**: 이벤트 본문 30일, 메타데이터·변경 이력 180일.
- **추적 순서**: S7 수집 상태 확인 → S6 원인 이벤트 확인 → `hub replay` 재처리 → `hub reconcile` 즉시 대조 → Gitea 이슈 등록.
- **다이어그램**: [logging-flow.drawio](./logging-flow.drawio)
