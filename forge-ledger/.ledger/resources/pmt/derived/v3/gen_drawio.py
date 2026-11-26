"""forge-ledger 설계 v3 drawio 원본 3종 생성기.

실행: python3 gen_drawio.py  (같은 폴더에 *.drawio 3개를 쓴다)
"""
from pathlib import Path
from xml.sax.saxutils import quoteattr

OUT = Path(__file__).resolve().parent
BLUE = "fillColor=#dae8fc;strokeColor=#6c8ebf;"
GREEN = "fillColor=#d5e8d4;strokeColor=#82b366;"
YELLOW = "fillColor=#fff2cc;strokeColor=#d6b656;"
ORANGE = "fillColor=#ffe6cc;strokeColor=#d79b00;"
RED = "fillColor=#f8cecc;strokeColor=#b85450;"
PURPLE = "fillColor=#e1d5e7;strokeColor=#9673a6;"
BOX = "rounded=1;whiteSpace=wrap;html=1;fontSize=14;"
LANE = "swimlane;startSize=26;html=1;fontSize=14;fontStyle=1;"
EDGE = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;defaultLabelBackgroundColor=page;labelBackgroundColor=default;"


class Diagram:
    def __init__(self, name: str):
        self.name = name
        self.cells: list[str] = []
        self.n = 1

    def _id(self) -> str:
        self.n += 1
        return f"c{self.n}"

    def v(self, label, x, y, w, h, style, parent="1", cid=None):
        cid = cid or self._id()
        self.cells.append(
            f'<mxCell id="{cid}" value={quoteattr(label)} style="{style}" vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
        )
        return cid

    def e(self, src, dst, label="", style=EDGE, parent="1"):
        cid = self._id()
        self.cells.append(
            f'<mxCell id="{cid}" value={quoteattr(label)} style="{style}" edge="1" parent="{parent}" '
            f'source="{src}" target="{dst}"><mxGeometry relative="1" as="geometry"/></mxCell>'
        )
        return cid

    def write(self):
        body = "".join(self.cells)
        xml = (
            '<mxfile host="forge-ledger-gen"><diagram name=' + quoteattr(self.name) + '>'
            '<mxGraphModel adaptiveColors="auto"><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            + body + "</root></mxGraphModel></diagram></mxfile>"
        )
        path = OUT / f"{self.name}.drawio"
        path.write_text(xml, encoding="utf-8")
        return path


def architecture():
    d = Diagram("architecture")
    d.v("forge-ledger v3 전체 구조", 40, 0, 600, 36, "text;html=1;fontSize=20;fontStyle=1;")
    users = d.v("사용자", 40, 50, 900, 100, LANE + "fillColor=#f5f5f5;")
    cc = d.v("Claude Code 세션<br><small>개발자 · 에이전트</small>", 40, 34, 220, 54, BOX + BLUE, users)
    br = d.v("브라우저<br><small>팀원 · 사내망</small>", 340, 34, 220, 54, BOX + BLUE, users)

    plug = d.v("forge-ledger 플러그인", 40, 180, 260, 330, LANE + "fillColor=#eef4fb;")
    cmd = d.v("명령<br><small>plan · next · work · decide · status · hub</small>", 20, 36, 220, 60, BOX + BLUE, plug)
    skl = d.v("스킬<br><small>ledger-plan · record · read</small>", 20, 106, 220, 56, BOX + BLUE, plug)
    hok = d.v("훅 (fail-open)<br><small>세션 요약 · 비밀값 쓰기 차단</small>", 20, 172, 220, 60, BOX + PURPLE, plug)
    cli = d.v("CLI 엔진 ledger.py<br><small>Python 표준 라이브러리</small>", 20, 250, 220, 60, BOX + BLUE, plug)

    hub = d.v("허브 컨테이너 · 개발 서버 Docker", 340, 180, 300, 330, LANE + "fillColor=#eef7ee;")
    web = d.v("웹 대시보드 · 조회 API<br><small>/api/v1 · 읽기 전용</small>", 15, 36, 140, 64, BOX + BLUE, hub)
    db = d.v("SQLite<br><small>수집 캐시 · 이벤트 · 알림</small>", 25, 130, 120, 80, "shape=cylinder3;whiteSpace=wrap;html=1;fontSize=13;" + GREEN, hub)
    lnk = d.v("링크 · 규칙 엔진<br><small>어긋남 M1–M10</small>", 15, 240, 140, 64, BOX + BLUE, hub)
    rcv = d.v("웹훅 수신<br><small>서명 · passcode 확인</small>", 170, 60, 120, 64, BOX + PURPLE, hub)
    rec = d.v("대조 작업<br><small>주기 조회</small>", 170, 200, 120, 60, BOX + "dashed=1;" + BLUE, hub)

    ext = d.v("외부 도구", 680, 180, 260, 330, LANE + "fillColor=#fdf3e7;")
    gt = d.v("Gitea<br><small>코드 · PR · 결함 이슈 · CI</small>", 20, 40, 220, 70, BOX + ORANGE, ext)
    pl = d.v("Plane CE<br><small>작업 항목 · cycle · module · 페이지</small>", 20, 136, 220, 70, BOX + ORANGE, ext)
    fg = d.v("Figma 클라우드<br><small>화면 · Dev Mode 상태</small>", 20, 232, 220, 70, BOX + ORANGE, ext)

    src = d.v("원천 문서", 40, 550, 900, 110, LANE + "fillColor=#f5f5f5;")
    led = d.v("저장소 .ledger/<br><small>짧은 이력 · 설계 원본 · 운영 문서</small>", 20, 36, 260, 60, BOX + GREEN, src)
    pmt = d.v("PMT ~/docs<br><small>작업 환경 · 로컬 환경 · 개인 이력</small>", 320, 36, 260, 60, BOX + GREEN, src)
    d.v("점선: 주기 작업 또는 조건부 경로<br>보라: 보안 단계", 640, 30, 240, 60, "text;html=1;fontSize=12;align=left;", src)

    d.e(cc, cmd, "실행")
    d.e(br, web, "HTTPS")
    d.e(hok, web, "요약 읽기")
    d.e(gt, rcv, "웹훅")
    d.e(pl, rcv, "웹훅")
    d.e(fg, rcv, "웹훅 · 공개 주소 필요", EDGE + "dashed=1;")
    d.e(rec, ext, "API 대조", EDGE + "dashed=1;")
    d.e(rcv, db, "원본 저장", parent=hub)
    d.e(rec, lnk, "차이 반영", parent=hub)
    d.e(lnk, db, "읽기 · 쓰기", parent=hub)
    d.e(web, db, "읽기", parent=hub)
    d.e(cli, ext, "작업 · 페이지 쓰기", EDGE + "exitX=0.5;exitY=1;entryX=0.5;entryY=1;")
    d.e(cli, led, "이력 한 줄 · PR")
    d.e(cli, pmt, "환경 기록")
    return d.write()


def data_model():
    d = Diagram("data-model")
    d.v("허브 SQLite 데이터 모델", 40, 0, 600, 36, "text;html=1;fontSize=20;fontStyle=1;")
    tstyle = ("swimlane;fontStyle=1;childLayout=stackLayout;horizontal=1;startSize=26;horizontalStack=0;"
              "resizeParent=1;resizeParentMax=0;collapsible=0;html=1;fontSize=14;")
    rstyle = "text;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;spacingLeft=4;html=1;fontSize=13;"
    rows: dict[tuple[str, str], str] = {}

    def table(name, x, y, cols, color):
        t = d.v(name, x, y, 240, 26 + 26 * len(cols), tstyle + color)
        for i, c in enumerate(cols):
            rows[(name, c.split()[-1])] = d.v(c, 0, 26 + 26 * i, 240, 26, rstyle, t)
        return t

    table("source", 40, 50, ["PK id", "kind gitea|plane|figma", "name", "config_key", "last_sync_at", "status"], BLUE)
    table("sync_run", 40, 300, ["PK id", "FK source_id", "mode full|incr|reconcile", "started_at", "finished_at",
                                "fetched", "changed", "errors", "rate_remaining"], BLUE)
    table("rule", 40, 620, ["PK id", "code M1–M10", "severity", "enabled", "params json"], YELLOW)
    table("event", 380, 50, ["PK id", "FK source_id", "delivery_id", "event_type", "correlation_id", "received_at",
                             "signature_ok", "payload_redacted json", "payload_sha256", "status", "error",
                             "processed_at"], PURPLE)
    table("entity_change", 380, 440, ["PK id", "FK entity_id", "FK event_id", "field", "old_value", "new_value",
                                      "changed_at"], GREEN)
    table("entity", 720, 50, ["PK id", "FK source_id", "kind", "ext_id", "ext_key", "title", "state", "url",
                              "FK parent_id", "attrs json", "updated_ext_at", "seen_at"], GREEN)
    table("ledger_entry", 720, 440, ["PK id", "FK repo_entity_id", "date", "kind decision|phase|release", "ref_id",
                                     "title", "plane_page_id", "plane_updated_at", "commit"], GREEN)
    table("link", 1060, 50, ["PK id", "FK from_entity_id", "FK to_entity_id", "rel", "origin", "first_seen",
                             "last_seen", "removed_at"], GREEN)
    table("alert", 1060, 340, ["PK id", "FK rule_id", "FK entity_id", "FK cause_event_id", "dedupe_key", "state",
                               "opened_at", "resolved_at", "notified json"], RED)

    er = "edgeStyle=entityRelationEdgeStyle;html=1;startArrow=ERmany;endArrow=ERmandOne;"
    for (tbl, fk), (ptbl) in {
        ("sync_run", "source_id"): "source", ("event", "source_id"): "source", ("entity", "source_id"): "source",
        ("entity_change", "entity_id"): "entity", ("entity_change", "event_id"): "event",
        ("link", "from_entity_id"): "entity", ("link", "to_entity_id"): "entity",
        ("alert", "rule_id"): "rule", ("alert", "entity_id"): "entity", ("alert", "cause_event_id"): "event",
        ("ledger_entry", "repo_entity_id"): "entity", ("entity", "parent_id"): "entity",
    }.items():
        d.e(rows[(tbl, fk)], rows[(ptbl, "id")], "", er)
    return d.write()


def logging_flow():
    d = Diagram("logging-flow")
    d.v("이벤트 처리 · 로깅 흐름", 40, 0, 600, 36, "text;html=1;fontSize=20;fontStyle=1;")
    X = lambda c: 40 + 200 * c  # noqa: E731
    Y = lambda r: 50 + 120 * r  # noqa: E731
    start = d.v("웹훅 도착<br><small>Gitea · Plane · Figma</small>", X(0), Y(0), 160, 60, "ellipse;whiteSpace=wrap;html=1;fontSize=14;" + GREEN)
    sig = d.v("서명 · passcode 일치?", X(0) - 0, Y(1), 160, 90, "rhombus;whiteSpace=wrap;html=1;fontSize=14;" + YELLOW)
    rej = d.v("400 응답 · status=rejected<br><small>M9 수집 이상 알림</small>", X(1), Y(1) + 15, 160, 60, BOX + RED)
    raw = d.v("원본 저장<br><small>민감 필드 제거 · sha256 · correlation_id</small>", X(0), Y(2) + 10, 160, 70, BOX + PURPLE)
    ack = d.v("200 응답<br><small>처리는 뒤에서</small>", X(0), Y(3) + 10, 160, 60, BOX + BLUE)
    rcn = d.v("대조 작업 (주기)<br><small>차이 → 합성 이벤트</small>", X(1), Y(3) + 10, 160, 60, BOX + "dashed=1;" + BLUE)
    prc = d.v("엔터티 반영<br><small>entity_change 기록</small>", X(0), Y(4) + 10, 160, 60, BOX + BLUE)
    ok = d.v("처리 성공?", X(0), Y(5), 160, 90, "rhombus;whiteSpace=wrap;html=1;fontSize=14;" + YELLOW)
    rty = d.v("status=failed · 재시도<br><small>최대 3회 · 지수 대기</small>", X(1), Y(5) + 15, 160, 60, BOX + ORANGE)
    dead = d.v("M9 수집 이상 알림<br><small>replay로 수동 재처리</small>", X(2), Y(5) + 15, 160, 60, BOX + RED)
    lk = d.v("링크 해석<br><small>R1–R5</small>", X(3), Y(0), 160, 60, BOX + BLUE)
    rl = d.v("규칙 평가<br><small>M1–M10</small>", X(3), Y(1) + 15, 160, 60, BOX + BLUE)
    al = d.v("알림 열기 · 해소<br><small>cause_event_id 기록</small>", X(3), Y(2) + 10, 160, 60, BOX + BLUE)
    done = d.v("status=processed", X(3), Y(3) + 10, 160, 60, "ellipse;whiteSpace=wrap;html=1;fontSize=14;" + GREEN)
    d.v("모든 단계: JSON 한 줄 로그<br>ts · level · component · event_id · correlation_id · result · duration_ms<br>토큰 · 본문은 쓰지 않음",
        X(2), Y(2) - 10, 180, 110, "shape=note;whiteSpace=wrap;html=1;fontSize=12;align=left;" + YELLOW)
    d.e(start, sig)
    d.e(sig, rej, "아니오", EDGE + "fontColor=#B20000;")
    d.e(sig, raw, "예", EDGE + "fontColor=#2D7600;")
    d.e(raw, ack)
    d.e(ack, prc)
    d.e(rcn, prc, "합성 이벤트", EDGE + "dashed=1;")
    d.e(prc, ok)
    d.e(ok, rty, "아니오", EDGE + "fontColor=#B20000;")
    d.e(rty, prc, "재시도")
    d.e(rty, dead, "3회 실패", EDGE + "fontColor=#B20000;")
    d.e(ok, lk, "예", EDGE + "fontColor=#2D7600;")
    d.e(lk, rl)
    d.e(rl, al)
    d.e(al, done)
    return d.write()


if __name__ == "__main__":
    for fn in (architecture, data_model, logging_flow):
        print(fn())
