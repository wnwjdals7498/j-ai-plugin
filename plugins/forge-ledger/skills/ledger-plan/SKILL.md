---
name: ledger-plan
description: 신규 착수·큰 기능의 작업 계획서(PRD·구현 문서)를 작성·점검하고 결정 항목을 기록할 때 사용한다.
---

# ledger-plan

신규 프로젝트 착수나 큰 기능을 시작할 때 Plane 단위 문서와 저장소 `.ledger/` 문서를 차례로 작성·점검한다.

## 참고 문서

- 문서 18종 규격·검사 기준: `references/docs.md`
- 작업 종류별 흐름: `references/flows.md`
- 안내·게이트 기준: `references/gates.md`
- 독립 검토 지시문: `references/review-prompt.md`
- 작성 예시(설계서 원본): `examples/forge-ledger-design.html`

## CLI 명령

모든 명령은 플러그인 스크립트로 실행한다.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" init [--repo PATH]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" plan new "<제목>" --key KEY
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" plan get KEY DOC
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" plan put KEY DOC --from FILE
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" plan check [--project KEY] [--json]
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" decide "<제목>" --project KEY|* --body-file FILE
```

- `plan feature`, `plan fix`, `plan phase close`, `plan release`는 1차(후속 단계) 구현 대상이다.

## 진행 순서

1. **초기화·생성**: 저장소에 `.ledger/ledger.yml`이 없으면 `init`을 실행한다. 새 단위 프로젝트는 `plan new "<제목>" --key KEY`로 만든다.
2. **PRD 질문 순서**: 아래 순서대로 한 절씩 묻고 채운다.
   - `PRD 1 → 2 → 9 → 3 → 5 → 4 → 6 → 7`
   - `PRD 8`(미결 질문)은 대화 내내 답이 비거나 결정이 필요한 항목이 나올 때마다 채운다.
3. **예시 보여 주기**: 항목을 물을 때 `examples/forge-ledger-design.html`에서 해당 절(`id="prd-goals"`, `id="prd-out"`, `id="impl-data"`, `id="impl-security"`, `id="impl-phases"` 등)을 보여 주어 기대 수준을 맞춘다.
4. **구현 문서 작성**: `references/docs.md`의 18종 표를 따라 구현 1–9를 채운다.
5. **형식·비밀값 검사**: 작성이 끝나면 `plan check [--project KEY]`를 실행해 누락·형식 오류·비밀값 검출이 0건인지 확인한다.
6. **독립 검토**: 단위 분리 전이나 범위가 크게 바뀐 뒤 `references/review-prompt.md`에 따라 작성에 참여하지 않은 하위 에이전트에게 문서만 전달해 검토받는다.
   - 돌아온 질문은 세 갈래로 나눈다: **이미 답 있음**(문서에 반영), **작은 것**(작성자가 정하고 "제안" 표시), **사용자 몫**(`PRD 8`에 등록하고 중요한 순서로 한 번에 질문).
7. **결정 기록**: 합의된 결정은 HTML 본문 파일을 만들어 `decide "<제목>" --project KEY|* --body-file FILE`로 남긴다.

## 문서 갱신 방식

- **단위 범위 문서**(`PRD 1`–`9`, `구현 2·3·5·7`): `plan get KEY DOC`로 현재 절을 확인하고, 수정한 HTML을 임시 파일에 저장한 뒤 `plan put KEY DOC --from FILE`로 반영한다.
- **저장소 범위 문서**(`구현 1·4·6·8·9`): 저장소의 `.ledger/` 아래 해당 Markdown·drawio 파일을 직접 수정한다.

## 주의 사항

- 문서·이력·명령 출력에 IP 주소, 실제 서버 주소, API 키, 토큰, 계정 정보를 넣지 않는다. 접속 정보는 Plane 주소·Plane API 키 같은 명칭으로만 적는다.
- 해당 없음(`na`)을 적을 때는 `references/docs.md`에서 허용된 문서인지 확인하고 이유를 함께 남긴다.
