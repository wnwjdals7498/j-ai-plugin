"""forge-ledger 설계서 v4 형식 검사. 종료 코드 0이면 통과."""

import re
import sys
from pyexpat import ExpatError, ParserCreate
from pathlib import Path

HERE = Path(__file__).resolve().parent
html = (HERE / "forge-ledger-design-v4.html").read_text(encoding="utf-8")
fails = []

ids = [
    f"prd-{x}"
    for x in ("header", "goals", "context", "strategy", "assumptions", "stories", "mockups", "questions", "out")
]
ids += [
    f"impl-{x}"
    for x in ("data", "features", "screens", "logging", "alerts", "security", "phases", "maintenance", "deploy")
]
ids += [f"method-{x}" for x in ("docs", "flows", "gates", "check", "review", "package")]
ids += ["changes", "sources"]
for i in ids:
    if html.count(f'<section id="{i}">') != 1:
        fails.append(f"절 없음 또는 중복: {i}")
# 절 순서: PRD → 구현 → 규격 → 부록
pos = [html.find(f'<section id="{i}">') for i in ids]
if pos != sorted(pos):
    fails.append("절 순서가 규격과 다름")

if "{{" in html:
    fails.append("채우지 않은 자리표시자 있음")
MERMAID = '<pre class="mermaid">'
if html.count(MERMAID) != 5:
    fails.append(f"mermaid 블록 수 {html.count(MERMAID)} (기대 5)")
if not re.search(r"<title>[^<]{2,40}</title>", html[:8192]):
    fails.append("title 없음")

# 민감값: IPv4 주소, 토큰 형태 문자열
if re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", html):
    fails.append("IPv4 주소 형태 문자열 있음")
if re.search(r"(ghp_|glpat-|plane_api_|figd_)[A-Za-z0-9_-]{8,}", html):
    fails.append("토큰 형태 문자열 있음")

# 내부 앵커가 모두 존재하는지
for ref in set(re.findall(r'href="#([a-z0-9-]+)"', html)):
    if f'id="{ref}"' not in html:
        fails.append(f"깨진 앵커: #{ref}")

# 결정 번호는 PMT D1–D21 범위
for n in set(re.findall(r'class="tag t-d">(?:PMT )?D(\d+)<', html)):
    if not 1 <= int(n) <= 21:
        fails.append(f"없는 결정 번호: D{n}")

# 바뀐 점 절을 뺀 본문에 v3 숫자가 남았는지
body = re.sub(r'<section id="changes">.*?</section>', "", html, flags=re.S)
for old in ("17종", "구현 문서 8종", "M1–M10", "R1–R5", "구현 문서 1–5", "1–8 전부", "S1–S8"):
    if old in body:
        fails.append(f"v3 표현 남음: {old}")


def cell_count(path):
    """XML 선언을 검증하고 DTD와 외부 엔티티를 거부하며 mxCell을 센다."""
    parser = ParserCreate()
    count = 0

    def start(name, attributes):
        nonlocal count
        if name == "mxCell":
            count += 1

    def reject(*args):
        raise ValueError("DTD와 엔티티는 허용하지 않음")

    parser.StartElementHandler = start
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.Parse(path.read_bytes(), True)
    return count


for name in ("architecture", "data-model", "logging-flow"):
    path = HERE / f"{name}.drawio"
    try:
        if cell_count(path) < 10:
            fails.append(f"{name}.drawio 셀 수 부족")
    except (ExpatError, ValueError, FileNotFoundError) as exc:
        fails.append(f"{name}.drawio 파싱 실패: {exc}")

for f in fails:
    print("FAIL", f)
print("OK" if not fails else f"{len(fails)}건 실패")
sys.exit(1 if fails else 0)
