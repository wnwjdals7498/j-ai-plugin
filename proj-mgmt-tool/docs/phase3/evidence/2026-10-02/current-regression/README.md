# 현재 전체 회귀와 보완 시험

기준 commit `4f632da`, dirty 구현 코드. 실제 전체 실행은 신규 hosted-planning 가족을 제외한 551개였다. 종료 코드 1, **543 passed / 3 failed / 1 error / 4 skipped**, 1,339.61s. [명령·환경·소스 hash](result.json), [실제 출력](pytest-output.txt), [JUnit](junit.xml)을 보존한다. 실행 도중 Host data/plan/client-routing 세 파일이 변경돼 해당 가족은 이후 현재 source의 개별·F15 시험으로 확인한다.

- 두 실패는 `persist_json_resource`의 Windows 공유 잠금 `WinError 32`였다. 임시 파일 정리도 실패해 원 오류를 덮는 경계를 발견했다. 이 공유 게시부는 별도 보완·관련 회귀가 필요하며 이 전체 실행을 통과로 바꾸지 않는다.
- 두 SQLite readonly 오류는 fixture 준비에서 발생했다. `test_f10_actual_missing_acceptance_stays_unknown_and_blocks_f8`와 `test_active_target_claim_blocks_import_without_overwriting_target`의 격리 실행은 종료 0, **2 passed/55.39s**였다. 명령의 basetemp는 `.pmt-test/current-readonly-isolation`이다. 환경 원인을 확정하지 않으며 원 실패를 유지한다.
- 네 skip은 Windows 계정의 symlink 생성 권한 제한이다. 해당 플랫폼 동작을 성공으로 주장하지 않는다.

수정 후 [리소스 게시부](../resource-publication/README.md)의 현재 18개 회귀와 실제 F9 native/CLI 각 1개가 통과했다. common resource와 schema/Step/백업/Host resource 호환을 추가 확인한 `tests/test_resources.py tests/test_backup.py tests/test_phase2_steps.py tests/test_phase2_storage.py tests/test_phase3_protocol.py tests/test_phase3_host_resources.py`는 종료 0, **42 passed / 2 symlink skip / 30.67s**였다. basetemp는 `.pmt-test/final-resource-compatibility`다. 새 변경과 실패 경로만 다시 시험했으며 전체 551개 실행을 성공한 것처럼 합산하지 않는다.

신규 hosted-planning 가족은 별도 4개와 관련 23개 회귀가 통과했다. 실제 localhost·설치·업데이트·복원·현재 SourcePin·파일/결과 복구의 최종 수용과 repaired package는 [F15 자료](../../local-acceptance/README.md)를 따른다. 이전 package는 source 변경 후 현재 코드 수용 근거로 사용하지 않는다.
