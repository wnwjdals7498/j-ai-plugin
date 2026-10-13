# 05. import — 기존 문서를 정형 데이터로 옮기기 (결정 D6)

## 1. 원칙

| 원칙 | 내용 |
|---|---|
| LLM API를 직접 호출하지 않는다 | 판단이 필요한 단계는 플러그인으로 연결된 에이전트가 CLI로 수행한다(2026-10-13 사용자 결정) |
| 에이전트는 라벨만 쓴다 | 본문을 다시 써서 출력하지 않는다. 원문 복사는 스크립트가 한다(출력 토큰이 입력의 5배 단가) |
| 규칙으로 되는 것은 규칙으로 | 문서 이름·제목·ID 패턴으로 정해지는 것은 스크립트가 처리하고, 남은 것만 에이전트에게 넘긴다 |
| 원문은 한 곳에만 | 대기 중인 구간은 파일 위치(오프셋)와 digest만 보관한다 |

현재 옮길 기존 데이터는 없다(2026-10-13 사용자 확인). 기능은 합성 샘플로 검증한다.

## 2. 흐름

```text
docctl import scan <폴더> [--export-policy approved_providers|local_only|public]
  1 scan   .md/.txt 파일을 artifact(경로·digest)로 등록. 같은 digest는 건너뜀
  2 split  ATX 제목(# ~ ######) 단위로 구간 분할. 첫 제목 앞부분은 0번 구간
  3 rules  문서 종류: 파일명·첫 제목 ↔ cat_kind.title_ko·cat_kind_alias(정규화 후 일치)
           필드: 구간 제목 ↔ field_key·description(정규화 후 일치)
           확정된 구간은 바로 5단계, 나머지는 status=pending

docctl import next [--budget 1500]                 ← 에이전트가 읽음
  문서 종류 미정:  d?:<artifact> "<파일명>" heads=<제목 목록>
  필드 미정:       s:<id> "<제목>" tok=<n> | <앞 200자> | cand=D01/behavior,D01/scope,… (최대 5)
  예산을 넘으면 omitted=N

docctl import label '<JSON 배열>'                  ← 에이전트가 씀(라벨만)
  {"doc": "d?:3", "kind": "D01"}
  {"seg": "s:12", "kind": "D01", "field": "behavior"}
  {"seg": "s:13", "skip": true, "why": "목차"}

  5 apply  원본 파일에서 구간을 다시 읽어 digest 확인 → 다르면 status=stale
           document가 없으면 생성, field_value에 append(put 경로 → change_log 체인)
           적용 후 관계 추출 규칙 R1(정의)·R2(언급) 실행 → 03-relations.md 3.1

docctl import report
  종류별·상태별 구간 수, 매핑 비율(규칙/라벨/건너뜀), 지원하지 않은 파일 수
```

후보 필드(`cand`)는 구간 제목과 필드 설명의 문자열 유사도로 스크립트가 고른다.

## 3. 저장

새 테이블:

```text
import_segment(seg_id, artifact_id, seq, heading, start_off, end_off, text_digest,
               status, kind_id, field_key, doc_id, rule, why)
status ∈ {pending, ruled, labeled, skipped, applied, stale}
```

- scan 1회는 `run(run_type='import')` 1행으로 남긴다(실행 기록은 수정 불가).
- 새 문서의 기본값: `status=draft`, `authority=normative`, `baseline_mode=desired`, `source_mode=authored`, `owner_id`=실행한 행위자, `lifecycle`·`category`=종류 기본값, `export_policy`=scan 옵션(기본 `approved_providers`).

## 4. 토큰 규칙

| 규칙 | 이유 |
|---|---|
| `next`는 예산 안에서만 보여 주고 `omitted=N` 표시 | 원칙 P6 |
| `local_only` 구간은 `next`에 핸들·제목만 | 결정 D8 |
| 큰 import는 서브에이전트나 새 세션으로 나눔(스킬이 안내) | 대화형 세션에서는 읽은 내용이 매 턴 다시 전송됨 |
| 라벨 1건은 JSON 한 줄 | 원칙 P5 |

## 5. 비용 감각

기준은 원문 100만 토큰이다. 출력 비율은 가정값이다. 출력 단가가 입력의 5배라는 점은 공식 가격표(platform.claude.com/docs/en/about-claude/pricing, 2026-10-13 확인; Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50 per MTok)로 확인했다. 에이전트 구독 사용량도 같은 방향으로 줄어든다.

| 방식 | 출력량 | 비고 |
|---|---|---|
| LLM이 전부 다시 써서 정형화 | 입력과 비슷함(100만) | 출력 단가가 입력의 5배여서 가장 비쌈 |
| 이 방식(규칙 처리 후 남은 구간만 라벨) | 입력의 수 %(라벨만) | 에이전트 입력도 제목과 앞 200자로 줄어듦 |

## 6. 범위 밖

docx·xlsx·위키 형식은 지원하지 않는다. 지원하지 않는 파일은 artifact로만 등록하고 `report`에 수를 표시한다. 필요해지면 형식별 분할기를 추가한다(docx는 표준 라이브러리의 zip·XML 처리로 가능).
