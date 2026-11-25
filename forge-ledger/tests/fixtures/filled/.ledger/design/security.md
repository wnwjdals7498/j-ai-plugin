## 구현 6 인증 · 보안 처리

| 접근 경로 | 인증 주체 | 인증 수단 | 토큰 보관 위치 |
| --- | --- | --- | --- |
| 대시보드 | 사람 | Gitea OAuth2 PKCE | 허브 HttpOnly 세션 쿠키 |
| Plane API | 플러그인 CLI | 개인 API 키(X-API-Key) | userConfig sensitive |
| 허브 웹훅 | Gitea·Plane | HMAC-SHA256 서명 | 허브 비밀값 파일(권한 600) |

- **외부 노출**: Gitea 웹·git만 외부 https 노출. 대시보드·조회 API·웹훅·Penpot은 사내망 전용.

| 위협 | 대응 |
| --- | --- |
| 위조·재전송 웹훅 | 서명 확인 및 payload_sha256 중복 판정 |
| 토큰 유출 | PreToolUse 쓰기 차단 훅, 형식 검사기 비밀값 규칙, 화면 끝 4자 가림 |
| 외부 Gitea 로그인 대입 공격 | 공개 가입 끄기, 2단계 인증, 월 1회 실패 기록 점검 |

- **보안 화면 프레임**: S10 로그인 및 권한 없음 표 정의(2차에 Penpot 보드 상세화)
