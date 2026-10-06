# 로컬 파일 접근 실패와 재검증

2026-10-06. 원인 상태: **미확정**. 실패를 삭제하거나 재시도 통과를 전체 단일 실행 성공으로 바꾸지 않는다. 실제 사용자 DB/profile과 외부 Host는 접근하지 않았다.

## 실제 관찰

- [전체 실행](p4-full-final1.json)은 source 변경 없이 670개 사례를 실행했다. 662 passed/4 skipped/2 failed/2 errors, exit 1이다. skip 4개는 현재 Windows 계정의 symlink 생성 권한 제한이다.
- claim02의 `save_change`는 `database_error`, exit 4, rollback으로 끝났다. 직전 같은 DB의 `create_scope`는 성공했고 실패 요청은 ledger에 저장되지 않았다.
- F5/F8 fixture의 artifact INSERT는 `attempt to write a readonly database`였다. 당시 fixture 자동 정리로 두 DB가 사라졌고 exception 상세가 출력에서 잘려 확장 오류 번호를 복구하지 못했다.
- nested Git fixture의 `.git/config`와 최종 R6 첫 시도의 `.git/objects` 쓰기는 `Permission denied`였다. PMT 업무 동작 전에 발생한 fixture Git 실패다.
- [동일 코드·새 경로 재검증](p4-failure-recheck1.xml)은 네 실패 사례 모두 4 passed/exit 0이다. 재현되지 않았다는 증거이며 근본 원인 증명은 아니다.

사후 보존된 claim DB는 schema 5, `quick_check=ok`, WAL, `query_only=0`이었다. DB/WAL/SHM 및 Git config/objects 디렉터리에는 ReadOnly 속성이 없었고 실행 계정의 상속 FullControl ACL이었다. 사후 속성은 실패 순간의 쓰기 가능성을 보장하지 않는다. 원 파일을 초기화·삭제·reset하지 않았다.

## 진단 보완과 후속 실행

SQLite write/오류 경계에 `sqlite_errorcode`와 `sqlite_errorname`을 추가했다. 전체 exception 문자열·SQL·본문·비밀값은 기록하지 않는다. 실제 `PRAGMA query_only=ON`으로 readonly 쓰기를 재현한 시험은 오류 번호 8/`SQLITE_READONLY`, rollback, 기존 DB ID 및 미커밋 request를 확인한다. setup/test 실패 시 actual context fixture를 보존하고 위치를 local test artifact에 기록한다.

[보완 후 회귀](p4-final-observability.log)는 106 passed/2 failed/1 error, exit 1이다. 세 실패에서 실제 `SQLITE_READONLY(8)`을 기록했고 fixture DB를 보존했다. [일반 로컬 실행 비교](p4-isolation-check.xml)는 동일 세 사례 3 passed/exit 0이다. 기존 sandbox 계정이 소유한 pytest cache는 일반 계정에서 접근 경고가 발생해 후속 실행은 cache provider만 끈다. 업무 DB/권한/동시성 규칙을 시험을 위해 완화하지 않았다.

SQLite는 운영체제의 쓰기 권한 때문에 연결이 readonly로 열릴 수 있으나, 이번 실패가 그 원리로 발생했다고 확정하지 않는다. [공식 연결 문서](https://www.sqlite.org/c3ref/open.html)의 `sqlite3_db_readonly`와 open flag 설명은 가능한 메커니즘이며 이번 순간의 관찰을 대체하지 않는다. `query_only=0`·정상 ACL만으로 connection이 실제 writable이라고 단정하지 않는다.

최종 관련 회귀는 [일반 로컬 회귀 로그](p4-final-local-regression.log)와 [JUnit](p4-final-local-regression.xml)을 따른다. 향후 같은 장애가 나오면 보존 DB·exact source/hash·오류 code/name·ACL/attributes·실행 계정·원 request를 함께 조사한다. 동작 완료·강제 unlock·DB 자동 초기화·무조건 재실행으로 처리하지 않는다.

일반 로컬 관련 회귀는 109 passed/1 setup error였고 그 한 사례에도 `SQLITE_READONLY(8)`이 재현됐다. 따라서 sandbox 단독 원인 가설은 입증되지 않았다. 코드·cache 정책을 그대로 유지하고 전용 OS 임시 폴더로 입력 경로를 바꾼 [동일 회귀](p4-temp-location-regression.xml)는 **110 passed/exit 0**였다. 저장 위치의 영향 가능성은 이 비교로 관찰했지만 한 번의 비교로 정확한 파일 감시/OS/ACL 메커니즘을 확정하지 않는다. 실제 사용자의 저장 설정을 변경하지 않았다.
