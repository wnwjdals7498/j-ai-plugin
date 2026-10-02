# F8 로컬 실행 제어 검증

기준 commit `4f632da`, 작업 트리 수정 상태. 작업자 `gpt-6-luna`; Python은 프로젝트 `.venv`. 모델의 성공 주장과 실제 테스트 결과를 구분한다.

최종 진단 수정 후 인계한 SHA-256(최종 통합 source와는 별도):

| 상대 경로 | SHA-256 |
|---|---|
| `src/pmt/efficiency/control.py` | `cde6726f709d036e61db71cae50400b43bc4be37d968c502d58d7c58d326bad0` |
| `src/pmt/efficiency/local_runtime.py` | `e9ab545b159ce9ea1761549e93a2b4f10be8ed36ae4ceb70ebc807d93be9c503` |
| `tests/test_phase3_control.py` | `870f45532ad664d00b5b24a3e83e655e6f629db97f5a152d62955e32696b21b7` |

| 실행 | 결과 |
|---|---|
| `.venv/Scripts/python.exe -m pytest tests/test_phase3_control.py -q --basetemp=.pmt-test/f8-final-control10` | exit 0, 10 passed, 124.73초 |
| `.venv/Scripts/python.exe -m pytest tests/test_phase2_runners.py -q --basetemp=.pmt-test/f8-runner-final2` | exit 0, 9 passed |
| 마지막 진단 로깅 수정 후 컴파일·source/ACK 부정 경계 2개 | exit 0 |

실제 Git·SQLite·리소스·로컬 supervisor fixture로 source/권한 재검증, 예산 문맥 전달, native nonce/실제 handle ACK, 반복 관찰의 이력 중복 회피, 실제 결과 hash, 취소 후 잠금 유지, unknown 조정, 검증된 transient/종료 조건과 재시도 2회 상한을 확인했다.

중단 복구 시험은 Phase2 attach 성공 뒤 F8 ACK 저장을 실패시킨다. 재전송 시 동일 nonce·handle의 기존 attach 요청을 회수하고 ACK를 저장한다. 두 번째 native 호출을 만들지 않는다. 다른 handle은 충돌이다. 재시도는 새 run에 이전 context ref를 재사용하지 않으며 source 변경 시 실행 전 차단한다.

이전 전체 실행은 F5 fixture 리소스 저장/DB 준비 중 Windows PermissionError와 readonly 오류가 있었다. 실패한 각 사례의 격리 재실행은 통과했고, 이후 전체 10개가 통과했다. 권한 확대 시험도 이전 오류를 해소하지 못했으므로 원인을 sandbox로 단정하지 않는다.

이 증거는 설치된 Codex/Claude의 실제 외부 모델 호출, 실제 제품 native tool, UI 알림 수용, Host 실행을 검증하지 않는다. CLI는 로컬 fixture다. 현재 runner receipt는 transient 분류를 제공하지 않아 그 근거가 없는 실제 실행의 자동 재시도는 허용되지 않는다. `not_started` caller 주장만으로 종료를 확인하지 않는다. 후속 F9 runner 변경은 최종 통합 회귀를 별도로 확인한다.
