# 기존 기능 회귀 관찰

4단계 전체 회귀가 기존 F10 시나리오를 실행해 생성한 실제 관찰 JSON이다. 이전 3단계 evidence는 원래 내용으로 보존하고 이번 관찰은 이 폴더로 옮겼다. 이전 SourcePin/정의·baseline과 같은 실행으로 재사용하거나 비용 개선으로 해석하지 않는다. 전체 실행 결과·source 지문은 상위 [manifest](../p4-full-final1.json)를 따른다.

기존 시나리오 test writer는 이제 pytest의 별도 임시 출력 root를 사용한다. 반복 시험이 역사적 증거를 덮어쓰지 않는다. 이 경로 보정은 업무 API/기능/실제 SourcePin을 변경하지 않는다.
