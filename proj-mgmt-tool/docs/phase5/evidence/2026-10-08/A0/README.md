# A0 기준선 결과

기준 source `8054c83a85b73356f0e2e52322855b9e03b419ce`, Windows 10.0.26100, 개발 Python 3.14.5rc1. Core/src·tests·pyproject는 변경하지 않았다.

| 확인 | 결과 | 증거 |
|---|---|---|
| push된 재구성·Phase5 필수 파일 | 모두 존재 | baseline-meta.json |
| Claude CLI | 2.1.280 | commands.md |
| Codex CLI | 0.160.1 | commands.md |
| Claude marketplace validate | pass, exit 0 | claude-validate-marketplace.txt |
| Claude plugin validate | pass, exit 0 | claude-validate-plugin.txt |
| 첫 pytest | 119 passed / 1 failed / 559 errors, exit 1 | initial-pytest.xml/txt, initial-baseline-meta.json |
| 시험 root 준비 후 전체 pytest | **650 passed / 23 failed / 2 errors / 4 skipped**, exit **1**, 약 18분32초 | pytest.xml/txt, baseline-meta.json, summary.json |
| Graphify headless full extraction | failed, semantic chunks 7/7 HTTP401; 비밀 상세 제거 | graphify-extract.txt |
| Graphify AST-only recovery | pass, exit 0, 9396 nodes / 19415 edges | graphify-ast-only.txt |
| Host 공개 health | HTTP200, status ok, API1, TLS 검증 유지 | host-survey.json |
| 독립 redirect 진단 | 3.14·3.13 모두 expected code remote_http_error, request_count1. 최초 실패 재현 안 됨 | redirect-diagnostic-314.json / -313.json |

## 실패 구분

- 첫 실행의 559 setup errors는 `.pmt-test` 부모 디렉터리 누락이었다. 부모 폴더만 준비하고 첫 증거를 보존했다.
- 첫 실행의 별도 redirect 실패는 위 폴더 오류와 다른 문제다. 이후 전체 suite와 독립 진단에서 재현되지 않았으며, 원인 해결 증거는 없다.
- 전체 suite의 2 setup errors는 `pip wheel --no-build-isolation`에서 `setuptools.build_meta`를 가져오지 못한 설치 fixture 준비 실패다. 기존 build-system 요구사항 setuptools>=68을 개발 venv에 설치한 뒤 영향받은 설치 시험 5개를 재실행했다: **2 passed / 3 failed / 0 errors**, exit 1. 2개의 설치 fixture 오류는 재현되지 않았고, 남은 3 실패는 Git에 없는 과거 0.3.0 package-snapshot fixture 부족이다. 과거 증거는 추가/삭제하지 않았다. 상세는 build-recheck-summary.json/XML. 제품 코드는 수정하지 않았다.
- 23 실패는 unchanged source에서 관측된 기준선이다. 설정 2, 기본 패키징 1, batch 1, local integration 12, pending 1, reuse 1, Phase4 current context 1, foundation 1, 실제 패키징 3. 정확한 test ID와 message는 summary.json/XML에 있다. 전부 하나의 원인으로 단정하지 않는다.
- 4 skip은 현재 Windows 계정의 symlink 권한 부족이다. 해당 경로를 통과로 표시하지 않는다.

## 해석과 다음 단계

- 전체 suite **통과 아님**. baseline 검증에서 발견된 기존 제품 실패는 handoff 지시대로 기록만 했다.
- D1~D8 아직 미확정. A1~F3 미착수. A0 전체 완료로 판정하지 않는다.
- Python 3.13 전체 회귀는 실행하지 않았다. 3.13 단일 진단 결과를 전체 suite 결과로 바꾸지 않는다.
- 기준선 실행 중 문서/증거와 Graphify 생성물이 추가됐다. 제품 코드·tests·pyproject는 unchanged다. 이후 source-stable 전체 회귀는 로그를 격리 시험 디렉터리에 먼저 저장하고 종료 뒤 Git evidence로 모은다.
- 운영 Host는 읽기 전용으로 조사했으며 서비스·방화벽·DB·비밀·기기를 변경하지 않았다. 실제 F1 운영 전환 미실행.
