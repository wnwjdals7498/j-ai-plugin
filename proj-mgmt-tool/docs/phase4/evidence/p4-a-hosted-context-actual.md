# P4 A hosted_context 실제 검증 증거

2026-10-06 UTC. 이 증거는 A가 추가한 독립 hosted client adapter의 검증 결과다. 외부 Host·제품 설치·모델 의미 평가는 포함하지 않는다.

## 실행 대상과 결과

- 시험 정의: `p4-a-hosted-context-1` — 실제 `HttpStore` 두 client, 임시 loopback HTTPS Host, Local Git checkout, Host F5 storage/detail.
- 기준 HEAD: `ac7adedc1e7d2464e0023e70a01515c8d70cb878`; 공유 작업 디렉터리는 dirty이며 커밋하지 않았다.
- 명령: `.venv/Scripts/python.exe -m pytest tests/test_phase4_hosted_context.py --basetemp=.pmt-test/p4-a-r4-final-hosted -q --tb=short`.
- 실제 결과: **exit 0, 4 passed, 90.86s**. 별도로 hosted actual-checkpoint no-change selector test는 **exit 0, 1 passed, 25.30s**를 보였다.
- 모듈 구문 확인: `.venv/Scripts/python.exe -m compileall -q src/pmt/hosted_context.py tests/test_phase4_hosted_context.py` — **exit 0**.

## 실제로 확인한 내용

- Client adapter는 `HostedContinuityClient`의 실제 local source capture와 loopback HTTPS Host를 사용한다. 이어 Host의 F5 `build_task_context`, `read_task_context`, `read_context_detail`을 호출한다. Step 지시와 상세는 F5 owner/session private storage에 남는다.
- Current basis가 동일할 때 compose/read를 진행하고, 선택 파일 변경 후에는 현재 basis를 `changed`로 보고 F5 compose/detail을 허용하지 않는다. Checkpoint와 current Basis가 의미상 같으면 재분석을 생략한다. change pointer가 있지만 assessment/applicability/alignment가 검증되지 않은 경우는 review 상태·`complete=false`·실행 불가 next-action을 유지한다.
- 별도 actual 흐름은 Git graph 수정→hosted `collect_changes` after Basis→implementation-link index→R3 assessment→fresh basis와 graph-index 갱신→R4 Hosted selector compose를 실행했다. Bundle은 change/assessment refs를 이어 붙였고 applicability 및 current applied-alignment pointer 부재를 미완료·실행불가로 유지했다.
- 실제 persisted `decision_saved` boundary로 `purpose=current` checkpoint를 만든 뒤 다른 Hosted current capture에서 pointer를 다시 읽었다. checkpoint selector의 환경 dimension 누락/정규화가 실제 pointer와 일치해 R4는 `no_change_confirmed`를 반환했다.
- F5 detail cursor는 실제 source/context/scope binding에 묶인다. 변형한 cursor는 Host에서 거부된다.
- 실제 client B의 별도 device/environment/session에서 기존 client A의 F5 context ref는 읽히지 않는다. B가 새 current run owner가 된 fixture에서는 B가 자기 private context를 생성·읽었고, 이후 owner를 회수하자 B detail 요청이 Host에서 거부됐다.
- Hosted adapter 시험 경로는 local SQLite를 생성하지 않았다. Checkout 경로는 Host body 및 응답에 전달되지 않았다. Host source provenance는 `client_attested`, `host_git_verified=false`였다.

## 미포함·다음 연결

- 시험은 실제 storage selector CLI의 Hosted FILE routing을 통해 adapter를 호출했다. Host에 checkout 경로·private Step 원문을 게시하지 않았으며 F5 private storage를 재사용했다.
- Host/HTTP/C bridge 및 Local HostedContinuityClient 수정은 별도 소유자의 공유 변경이다. 아래 해시는 그 통합 기준을 기록하며 A의 소유 파일을 구분한다.
- Offline mode, 외부 Host, 새 native model/session 의미 평가는 실행하지 않았다.
- B의 actual Hosted F1/F3 apply fixture에 이어 R4 compose/detail 경로를 통합했다. 이전 run attempt는 C receipt 본문에 존재하지 않는 `unknown`/`effect_refs` fields를 요구해 `applied=false`였고, authoritative receipt hash·개별 effect refs·current-owner graph/document readback으로 predicate를 맞춘 뒤 Root가 동일 actual test의 **exit 0, 1 passed, 55.39s**를 확인했다. 해당 결과의 bundle은 actual change/assessment/resolution/applicability/alignment refs를 보존하고 current detail owner/source revalidation을 통과했다.
- Hosted positive fixture에서 applicability lookup 실제 ref는 존재하지만 P2 verification row는 없는 상태다. 따라서 P2 pass preservation/후속 invalidation의 Hosted end-to-end 결과는 **미실행/unknown**이다. Local R1 facts fixture의 P2 row는 isolated SQLite에 seed한 상태이므로 actual Hosted P2 product flow 증거로 일반화하지 않는다.

SHA-256:

| 파일 | 구분 | SHA-256 |
|---|---|---|
| `src/pmt/hosted_context.py` | A 소유 모듈 | `63E494E894094A2E8F179E7BBED447C1232A279F4533D7CB6F7AAEEEEC73C7D5` |
| `tests/test_phase4_hosted_context.py` | A 소유 시험 | `3B0668512986F756BA4E093CE5964DD438C23ECE6BD65EB9E12EE0A2D88D6C1C` |
| `src/pmt/hosted_continuity.py` | C 공유 client capture 기반 | `C6124AB1D035DFE56EB82B0987C4FD8620807419BED42E56ADD0DDF3E989CB1C` |
| `src/pmt/host/data.py` | C 공유 Host F5 detail adapter | `BBF81728DCFC1F06578DBDF2C14BF3825DDC8245348CA012953DF0B67D62E3B5` |
