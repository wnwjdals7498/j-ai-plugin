## 구현 9 배포

- **배포 대상**: 로컬 Gitea 마켓플레이스 플러그인과 개발 서버 단일 Docker Compose 허브 인스턴스.
- **배포 절차**: 게이트 2(`plan release`) 통과 → 태그 → 이미지 빌드 → DB 백업 → `docker compose up -d` → `/api/v1/health` 확인 → 전체 대조.
- **되돌리기**: 이전 이미지 태그로 재기동하고, 스키마 변경이 있으면 백업 DB를 복원한다.
- **버전 규칙**: SemVer(주.부.수)와 허브 API `/api/v1` 경로 고정.
