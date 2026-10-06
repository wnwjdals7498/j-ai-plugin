# P4 R6-A 실제 비용 비교

2026-10-06 UTC. 격리 Local SQLite/Git fixture에서 같은 초기 DB 상태를 복제해 legacy 재개와 R1–R4 재개를 비교했다. 모델·외부 Host·제품 호출은 하지 않았다. 원 측정 자료와 source hash는 [JSON evidence](p4-r6-a-actual.json)에 있다.

| Scenario | Path | Calls | Protocol bytes | Wall ms | Git calls | Instrumented file-read bytes | F5 detail pages |
|---|---:|---:|---:|---:|---:|---:|---:|
| No change | Legacy | 8 | 24,238 | 4,878.193 | 31 | 3,923 | 2 |
| No change | R1–R4 | 7 | 27,285 | 10,922.389 | 80 | 15,904 | 2 |
| Related source change | Legacy | 9 | 26,110 | 6,701.422 | 44 | 5,471 | 2 |
| Related source change | R1–R4 | 14 | 47,484 | 27,664.538 | 186 | 27,938 | 2 |

두 pair 모두 같은 DB snapshot, project, run, acceptance hash, role, environment와 물리 workspace를 사용했다. 변경 pair의 source commit은 양쪽 재개 경로 전에 공통 fixture setup으로 만들었으므로 표의 시간·읽기 비용에는 포함하지 않았다. Legacy 경로는 bounded context, 현재 run/Step/directive, 선택 파일 hash, SourcePin, F5 context/detail을 조회했다. R1–R4 경로는 현재 facts/checkpoint/basis를 확인하고, 변경 pair에서 실제 change/index/assessment/applicability refs와 overview/bundle/detail을 수집했다.

두 경로 모두 F5 projection에 600 UTF-8 byte budget을 사용했고 required 항목이 생략되어 bundle/context가 `complete=false`였다. 양쪽 모두 detail 2쪽을 실제 조회했다. No-change R4는 `no_change_confirmed`를 반환했다. Related-change R4는 실제 change와 assessment를 묶고 applicability는 `unknown`, next action은 검토 필요·실행 불가로 남겼다. 이 측정 fixture에는 적용된 alignment receipt가 없다.

측정은 각 경로에서 실제 request/response UTF-8 bytes, 호출 수와 elapsed time을 합산했다. 선택 파일 byte 수는 Python `Path.read_bytes` 호출만 세었다. Git subprocess 호출 수도 기록했지만 Git 내부 파일 I/O, `open`/`read_text` 및 OS 수준의 다른 디스크 읽기 byte는 관측하지 않아 `unknown`이다. SQLite 물리 I/O, CPU/memory, model token·품질도 미측정이다. 측정상 R1–R4 경로는 이 fixture에서 더 많은 Git·선택 파일 읽기와 더 긴 시간을 썼다. 비용 절감률이나 품질 우위는 주장하지 않는다.

실행 입력·규칙·환경 hash와 두 경로의 state before/after는 JSON evidence에 보존했다. 모델 사용량은 호출하지 않아 `unknown_not_invoked`다.
