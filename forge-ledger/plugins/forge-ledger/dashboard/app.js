"use strict";

// 외부에서 온 문자열(이슈·cycle·프로젝트 이름)은 textContent로만 넣는다.
const REFRESH_MS = 60000;
const LATER_TEXT = "1차에서 제공";
const EMPTY_TEXT = "등록된 저장소 없음 — ledger sync로 등록하세요";
const SOURCE_NAMES = { gitea: "Gitea", plane: "Plane" };
const STATE_TONES = { ok: "good", stale: "warn", partial: "warn", failed: "bad" };
const ALERT_STATES = ["stale", "failed"];
const COLUMNS = ["Plane 프로젝트", "현재 cycle", "진행", "빠진 문서", "게이트 1"];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

function gateText(days) {
  if (days === null) {
    return "-";
  }
  return days < 0 ? `${-days}일 지남` : `${days}일 뒤`;
}

function projectRow(project) {
  const row = el("tr");
  const title = `${project.key} ${project.title}`.trim();
  row.append(el("td", "", project.default ? `${title} · 기본` : title));
  if (!project.found) {
    const missing = el("td", "muted", "프로젝트 없음");
    missing.colSpan = COLUMNS.length - 1;
    row.append(missing);
    return row;
  }
  const cycle = project.cycle;
  row.append(
    el("td", cycle ? "" : "muted", cycle ? cycle.name : "cycle 없음"),
    el("td", "", cycle ? `${cycle.completed} / ${cycle.total}` : "-"),
    el("td", "muted", LATER_TEXT),
    el("td", "", cycle ? gateText(cycle.gate_days) : "-"),
  );
  return row;
}

function projectTable(projects) {
  if (projects.length === 0) {
    return el("p", "muted", "연결된 Plane 프로젝트 없음");
  }
  const table = el("table");
  const head = el("tr");
  head.append(...COLUMNS.map((name) => el("th", "", name)));
  table.append(head, ...projects.map(projectRow));
  const wrap = el("div", "scroll");
  wrap.append(table);
  return wrap;
}

function tile(title, value, note) {
  const node = el("div", "tile");
  node.append(el("div", "muted", title), el("div", "value", String(value)));
  if (note) {
    node.append(el("div", "muted", note));
  }
  return node;
}

function laterTile(title) {
  const node = el("div", "tile later");
  node.append(el("div", "", title), el("div", "", LATER_TEXT));
  return node;
}

function sourceTile(sources) {
  const node = el("div", "tile");
  node.append(el("div", "muted", "수집 상태"));
  for (const [source, status] of Object.entries(sources)) {
    const tone = STATE_TONES[status.state] || "";
    const pill = el("span", `pill ${tone}`, `${SOURCE_NAMES[source]}: ${status.label}`);
    if (status.last_ok_at) {
      pill.title = `마지막 성공 ${status.last_ok_at}`;
    }
    node.append(pill);
  }
  return node;
}

function sourceBanners(sources) {
  return Object.entries(sources)
    .filter(([, status]) => ALERT_STATES.includes(status.state))
    .map(([source, status]) => {
      const since = status.last_ok_at ? `마지막 성공 ${status.last_ok_at}` : "성공한 수집 없음";
      const tone = STATE_TONES[status.state];
      return el("p", `banner ${tone}`, `${SOURCE_NAMES[source]} ${status.label} (${since})`);
    });
}

function commonTiles(repo) {
  const review = repo.review_prs;
  const oldest = review.oldest_days === null ? "" : `가장 오래된 ${review.oldest_days}일`;
  const node = el("div", "tiles");
  node.append(
    tile("열린 결함 (Gitea)", repo.open_issues),
    tile("리뷰 대기 PR", review.count, oldest),
    tile("CI 실패", repo.ci_failures, `최근 실행 ${repo.ci_window}건 기준`),
    laterTile("Penpot 변경"),
    laterTile("열린 알림"),
    sourceTile(repo.sources),
  );
  return node;
}

function repoCard(repo) {
  const card = el("section", "repo");
  const bar = el("div", "bar");
  bar.append(el("h2", "", repo.repo), el("span", "muted", `Plane 프로젝트 ${repo.projects.length}개`));
  card.append(bar, ...sourceBanners(repo.sources), projectTable(repo.projects), commonTiles(repo));
  return card;
}

function render(data) {
  const cards = data.repos.length ? data.repos.map(repoCard) : [el("p", "empty", EMPTY_TEXT)];
  document.getElementById("repos").replaceChildren(...cards);
  document.getElementById("meta").textContent = `화면 갱신 ${new Date(data.generated_at).toLocaleString("ko-KR")}`;
}

function showError(message) {
  const node = document.getElementById("error");
  node.textContent = message;
  node.hidden = !message;
}

// 실패하면 직전에 그린 화면은 그대로 두고 안내만 띄운다.
async function refresh() {
  try {
    const response = await fetch("/api/v1/summary", { cache: "no-store" });
    if (response.status === 401) {
      showError("접속 토큰이 맞지 않습니다. 터미널에 출력된 주소로 다시 접속하세요");
      return;
    }
    if (!response.ok) {
      showError(`요약을 불러오지 못했습니다 (HTTP ${response.status})`);
      return;
    }
    render(await response.json());
    showError("");
  } catch (error) {
    showError("대시보드 서버에 연결하지 못했습니다. 서버가 실행 중인지 확인하세요");
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
