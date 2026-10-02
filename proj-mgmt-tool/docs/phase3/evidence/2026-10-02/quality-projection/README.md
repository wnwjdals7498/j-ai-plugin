# 동일 source 문맥 비교

격리된 실제 Git/SQLite/F5에서 작업 두 노드와 무관한 160개 노드를 생성했다. 같은 SourcePin·Step·지시·기준의 전체 전달값은 124,900 bytes, bounded 응답 전체는 7,760 bytes였다. 이 fixture의 전달값은 93.79% 줄었고 필수 목적·금지 범위·방법·입출력·시험·로그·기준 ID 보존 시험이 통과했다. F5 생성 시간은 1,901.58ms였다.

명령: `.venv/Scripts/python.exe -m pytest tests/test_phase3_quality_projection.py -q --basetemp=.pmt-test/quality-projection2`. 종료 코드 0, 1 passed/6.22s. 초기 실행의 시험 helper 이름 오류는 수정 후 이 실행에서 해결됐다.

독립된 fresh `gpt-6-luna` 네이티브 서브에이전트 두 개에 각각 full/bounded fixture 하나만 제공했다. 아홉 인계 항목의 의미 추출과 기준 ID·금지 범위·방법 보존을 확인했다. 다른 변형/답안·코드 접근은 금지했다. 결과와 입력 SHA는 [실측 manifest](comparison.json)에 보관한다. 이는 한 synthetic 작업의 인계 이해 시험이며 구현 품질 전체를 증명하지 않는다.

byte 비교는 **전달값만** 측정했다. Git/인덱스 준비·추가 상세 조회·재작업·review·전체 wall time·native tool 읽기 비용은 통합 비용 비교에 포함하지 않았으므로 총비용/속도 향상은 not_run이다. 제공자 token 사용량과 모델 실행 시간은 unknown이며 byte 감소를 token 절감률로 표현하지 않는다. 실서비스 Claude/API 전송은 하지 않았다.
