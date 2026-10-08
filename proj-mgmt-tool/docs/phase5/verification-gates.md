# 5단계 검증 관문

이 표는 수용 시험의 담당·검증 계층을 연결한다. pytest 함수/parameter 개수를 T-ID 완료 개수로 바꾸지 않는다. 실제 결과는 각 evidence와 progress를 따른다. Source 변경 뒤에는 해당 범위의 이전 성공을 최신 결과로 표현하지 않는다.

| 범위 | 담당 묶음 | 실제 관찰할 것 | 현재 근거/후속 |
|---|---|---|---|
| X-02 인계 형식 | A1 | strict JSON/unknown version/CA PEM hash/secret URL·값 거부 | A1 latest52 pass 중 schema tests, 별도 review |
| 기존 Host serve | A1 | 실제 TLS health 본문·인증 compatibility, 런처 kwargs/확장 연결 | A1 actual TLS fixture/52 pass |
| T-C01-1~4 | B1 | empty local/hosted missing/local 유지/CAS 실패 보존 | B1 evidence; B3 전체 Hook fixture 연결 후 최신 회귀 필요 |
| T-C04-1 | B1/F2 | owner/0700/0600 거부 규칙과 실제 POSIX 파일 | Windows stat-double pass, 실제 POSIX skip/F2 |
| T-C04-2 | B1/F3 | Windows CurrentUser DPAPI와 다른 계정 거부 | same-user/corrupt-data native pass; 다른 계정 미실행 |
| T-C04-3 | B1/B3 | credential이 hook 반환 env/envfile/stdout에 없음 | B1 저장·Rollback·실제 Core parser fixture; 후속 진입점 회귀 |
| T-C05-1~2 | B1 | OS defaults/명시 roots/legacy config·data 보존 | B1 fixture 근거 |
| T-C06-1·3 / T-C07-1~4 / T-C08-1·3 | B2 | 실제 Git/SQLite/명령 exit/evidence/Done 및 실패·dirty 보존 | B2 60pass/2skip; parent194pass/3skip, 실제 local Git/SQLite/command |
| T-C10-3 | B3 | 원래 hook acceptance와 Core native normalization/출력 유지 | B3 parent42pass; BC2 parent194pass/3skip; 의도된 uninitialized warning 변경 |
| T-C11-1~3 | B3/F2/F3 | 공용 wrapper argv, Windows PowerShell/GitBash, 실제 Linux/Claude Bash | 이 Host의 shell 시험과 실제 제품/다른 PC는 분리 |
| T-S01-1~2 / T-S02-1~3 | C1 | 버전/누락 deps/strict config/독립 process CAS/비밀값 거부 | C1 25pass/1skip, BC1 parent169pass/3skip |
| T-S03-1~4 | C1 | dry-run/새 상태 적용/반복·부분 상태 거부/동시 init 및 실패 정리/adopt 보존 | synthetic DB 사본만 사용, 운영 경로 금지 |
| T-S04-4 | C1 | DPAPI LocalMachine + 실제 run-account SID ACL, 넓은 권한 거부 | native unit 시험; 실제 서비스 계정은 F1 |
| T-S05-1~3 / T-S06-* / T-S07-1~2 | C2 | TLS pair/SAN/chain, doctor 실패 주입, 실제 설정 기반 serve·중복 거부 | C2 35pass/1skip; BC2 parent194pass/3skip; actual TLS health/compat |
| T-S11-1~2 / T-S12-1~4 / T-S13-1 | D1 | 실제 TLS 정식 scope 생성/기기 수명주기/권한/비밀 없는 인계 파일 | preflight 2 actualTLS pass: 실제 repository parent 필요 |
| T-C03-1~4 / T-C06-2 / T-S13-2 | D2 | D1 실제 생성 파일로 connect/link/check, 잘못된 credential·CA 보존 | D2 실제 isolatedTLS connect/status/link/check 완료, broad129pass/2POSIXskip |
| T-S08-1~2 | E1 | 계획 명령/반복 적용 모델 결과 동일, 실제 등록하지 않음 | E1 worker21pass +parent85pass/2skip; task/firewall/systemd는 generated/fake-adapter만; F1과 구별 |
| T-S15-1~3 / T-S17-2 / T-C09-1~3 | E2 | 일관 backup/빈 target restore-check/import/upgrade 실패 보존/switch busy 및 DB hash | D1/D2 뒤, 기존 migration 경계 사용 |
| T-S18-1·3 / T-C02-1 / X-01~04 | E3 | 두 논리 plugin·manifest·파일 목록·번들 검증·새 실패 없는 전체 회귀 | source 고정 뒤 whole pytest 및 A0 test-ID 차이 비교 |
| F1 실제 운영 | F1 | 같은 namespace/devices/keys, 백업·롤백·서비스/FW/재부팅·계정 | E3 push/PR 승인 뒤, 운영 단계별 별도 승인 필요 |
| F2/F3 실제 개발 기기 | 인계 작성 | 실제 설치/신뢰/new session/remote 사용자 결과 | 이 컴퓨터에서 실행 금지; F1 비밀 없는 실값으로 인계 |

## 자료 보존과 판정

- raw stdout/JUnit과 실제 종료 코드를 보존한다. 환경 복구 뒤 특정 재시험은 최초 whole-suite 결과를 덮어쓰지 않는다.
- A0 whole baseline은 650 pass/23 fail/2 error/4 skip, exit1. 빌드 의존성 보완의 특정 설치 재시험은 2 pass/3 fail/0 error. 과거 package-snapshot 누락은 추가/삭제하지 않는다.
- 후속 whole 회귀는 A0와 test ID 단위로 비교한다. 의도된 C01/C04/C10/X03 계약 변경의 기존 기대값만 새 명세에 맞추고, 관계없는 실패를 몰래 고치거나 지우지 않는다.
- 실제 서비스·방화벽·다른 계정·재부팅·다른 컴퓨터의 검증이 차단되면 blocked/미실행을 유지한다. 계획/fixture 성공을 실측 성공으로 쓰지 않는다.
