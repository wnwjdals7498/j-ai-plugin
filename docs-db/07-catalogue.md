# 07. 카탈로그 — 문서 종류 규칙 (결정 D2)

## 1. 역할

카탈로그는 R 계층(규칙)이다. 문서 종류, 종류별 필수·선택 필드, 종류 간 연결 규칙, 별칭, 적용 조건을 정의한다. 다른 모든 데이터가 FK로 카탈로그를 참조하므로, 카탈로그 없이는 문서를 저장할 수 없다.

원 카탈로그(`project-documentation-catalogue.yaml`, 194종, 필드 1,284개, 연결 769개, 별칭 768개)는 없다. 그래서 시작 카탈로그를 만든다.

## 2. 형식

- JSON만 읽는다(D2, PyYAML 제거). 로더는 표준 `json` 모듈만 쓴다.
- 구조는 원 YAML 구조를 그대로 따른다. 최상위 `standard_version`, `schema_version`, `groups`, `facts`, `document_types`. 종류별 `kind_id`, `title_ko`, `title_en`, `category`, `lifecycle`, `mode`, `owner_role`, `review_days`, `required_when`, `applies_when`, `content_files`, `required_fields`, `optional_fields`, `required_link_kinds`, `optional_link_kinds`, `covered_names`. 이 구조는 원 `test_token.py`의 `mini_catalogue()`가 만드는 것과 같다.
- 확장 속성(원 카탈로그에 없음, 설계 판단): 필드별 `extract_rel`(관계 추출 규칙 R3, [03](03-relations.md) 3.1).

## 3. 시작 카탈로그 18종

포함 기준: 원문 설계 문서·토큰 명세·시드에 이름이나 필드가 나온 종류. 근거 없이 채운 필드와 설명에는 `description`에 `[설계 판단]`을 붙인다. 출처가 `test_token`인 필드는 원 작성자가 미니 카탈로그에 쓴 값이다.

| 종류 | 이름 | 저장 방식 | 문서에 나온 필드 | 출처 |
|---|---|---|---|---|
| A01 | 문서 마스터 목록 | view | — | design DEC-02 |
| A04 | 용어 | register | term_id | design DEC-02, seed |
| B03 | 역할·책임(RACI) | narrative | roles, accountable | design 6.4, seed |
| B04 | RAID(위험·이슈·가정·의존성) | register | items | design DEC-02, seed |
| D01 | 요구사항 | narrative | requirement_ids, behavior, acceptance, scope, owners | design 6.4, test_token |
| D08 | RTM(요구사항 추적표) | view | edges | design DEC-02 |
| F06 | 의존성·결정 색인 | view | — | design 7.1 D-F06 |
| F07 | 기술 부채 | register | debt_id | seed |
| G01 | 작업 환경 | narrative | — | design F-1 |
| K01 | 환경 | narrative | environment_ids | seed |
| K02 | 서버 목록 | narrative | resources, topology, owners | design 6.4, test_token |
| K07 | 드리프트(기대값과 실제 상태의 차이) 점검 | run | — | design DEC-02 |
| L02 | 테스트 케이스 | narrative | test_ids | design 6.4, seed |
| L03 | 테스트 결과 | run | execution | design 6.4 |
| L07 | 결함 | register | defects | seed |
| P06 | Runbook(운영 절차서) | narrative | steps, rollback, owners | design 6.4, test_token |
| R04 | 백업 증거 | run | verification | design 6.4 |
| S03 | 자산 | register | assets | seed |

- 사실(`cap.*`·`evt.*`)은 위 종류의 적용 조건에 쓰이는 것만 넣는다. 범위는 원 설계 미결 Q5(미결 O5).
- 종류별 `item.attrs` 스키마(예: K02 자원의 기대 속성)는 원 설계 미결 Q6(미결 O6). K02·S03·L02부터 정의한다.
- 종류 간 연결 규칙은 [03-relations.md](03-relations.md) 6절(확정 D12).

## 4. 항목 종류 시드 필터

원 시드(`docdb_seed_item_types.sql`)의 39행은 문서 종류 36개를 FK로 참조한다. 시작 카탈로그 18종에 없는 종류를 참조하는 행을 그대로 넣으면 FK 위반이 난다.

- 시드를 JSON(`catalogue/item_types.json`)으로 옮긴다.
- 로더는 **적재된 종류를 참조하는 행만** 넣고, 건너뛴 행 수를 출력한다.
- 별칭 중복 4건의 대표 지정(원 결정 Q3: B03·G01·C01·S04)은 해당 별칭이 있을 때만 적용한다.

## 5. 변경 절차

1. `catalogue/starter.json`을 고친다(종류 추가, 필드 추가, 연결 규칙 수정).
2. `docctl catalogue load <json>`으로 다시 적재한다. `cat_version`에 버전이 남는다.
3. 카탈로그 변경은 사람이 승인한다(원문 5.3 분담: "R 카탈로그 — 사람: 카탈로그 변경 승인"). 에이전트에는 카탈로그 변경 도구를 노출하지 않는다.
4. 적용 판정(`kind_applicability`)을 다시 계산한다.
