## 구현 1 데이터 보관 설계

- **저장 대상과 위치**: 연결 설정과 결정 목록·누적 문서는 저장소 `.ledger/`, 단위 계획서와 작업 항목은 Plane, 수집 사본과 알림은 허브 SQLite에 둔다.
- **다이어그램**: [data-model.drawio](./data-model.drawio)

| 테이블 | 필드 | 보관 기간 | 비고 |
| --- | --- | --- | --- |
| event | id, source, payload_sha256, correlation_id, received_at | 본문 30일, 메타 180일 | 알림 참조 시 1년 |
| entity | id, source, kind, external_id, state, updated_at | 3년 | D10 기준 |
| alert | id, rule_code, dedupe_key, state, cause_event_id | 1년 | 조건 해소 시 자동 닫힘 |
