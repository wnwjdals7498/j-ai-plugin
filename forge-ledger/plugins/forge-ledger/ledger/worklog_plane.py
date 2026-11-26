"""Plane 댓글을 작업 일지 저장소로 사용한다."""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any, Optional

from ledger.plane import PlaneDuplicate, PlaneLike
from ledger.worklog import Entry, RemoteEntry

PROJECT_NAME = "작업 일지"
EXTERNAL_SOURCE = "forge-ledger-worklog"
_HEADER = re.compile(r"^\[일지\] (\S+) (.+?) (auto|manual)(?: repo=(.+?))?(?: item=(.+))?$")
_PARAGRAPH = re.compile(r"<p>(.*?)</p>", re.DOTALL)
_MONTH = re.compile(r"^([^/]+)/([0-9]{4}-(?:0[1-9]|1[0-2]))$")


def format_comment(entry: Entry) -> str:
    header = f"[일지] {entry.at} {entry.host} {entry.source}"
    if entry.repo:
        header += f" repo={entry.repo}"
    if entry.item:
        header += f" item={entry.item}"
    return "".join(f"<p>{html.escape(line)}</p>" for line in [header, *entry.text.split("\n")])


def parse_comment(comment: dict[str, Any], workspace: str) -> Optional[Entry]:
    if comment.get("external_source") != EXTERNAL_SOURCE:
        return None
    identifier, content = comment.get("external_id"), comment.get("comment_html")
    if not isinstance(identifier, str) or not identifier or not isinstance(content, str):
        return None
    paragraphs = _PARAGRAPH.findall(content)
    if len(paragraphs) < 2 or "".join(f"<p>{part}</p>" for part in paragraphs) != content:
        return None
    match = _HEADER.fullmatch(html.unescape(paragraphs[0]))
    if not match:
        return None
    at, host, source, repo, item = match.groups()
    try:
        timestamp = datetime.fromisoformat(at)
        if timestamp.utcoffset() is None:
            return None
    except ValueError:
        return None
    return Entry(
        identifier,
        at,
        host,
        source,
        "\n".join(html.unescape(p) for p in paragraphs[1:]),
        workspace,
        repo or "",
        item or "",
    )


class PlaneWorklog:
    def __init__(self, client: PlaneLike, project_key: str, workspace: str) -> None:
        self.client = client
        self.project_key = project_key
        self.workspace = workspace

    def current_user(self) -> str:
        return self.client.whoami_profile()["display_name"]

    def ensure_project(self) -> str:
        for project in self.client.list_projects():
            if project.get("identifier") == self.project_key:
                return str(project["id"])
        return str(self.client.create_project(PROJECT_NAME, self.project_key)["id"])

    def ensure_month_item(self, month: str) -> str:
        project = self.ensure_project()
        profile = self.client.whoami_profile()
        external_id = f"{profile['id']}/{month}"
        found = self.client.find_item_by_external(project, EXTERNAL_SOURCE, external_id)
        if found is not None:
            return str(found["id"])
        try:
            item = self.client.create_item(
                project,
                f"{profile['display_name']} {month}",
                "",
                [],
                external_source=EXTERNAL_SOURCE,
                external_id=external_id,
            )
            return str(item["id"])
        except PlaneDuplicate as exc:
            return exc.existing_id

    def post_entry(self, entry: Entry) -> str:
        project = self.ensure_project()
        item = self.ensure_month_item(entry.month())
        try:
            comment = self.client.create_comment(
                project, item, format_comment(entry), external_source=EXTERNAL_SOURCE, external_id=entry.id
            )
            return str(comment["id"])
        except PlaneDuplicate as exc:
            return exc.existing_id

    def list_entries(self, from_: date, to: date, user: Optional[str] = None) -> list[RemoteEntry]:
        project = self.ensure_project()
        entries: list[RemoteEntry] = []
        for item in self.client.list_items(project):
            external_id = item.get("external_id")
            name = item.get("name")
            if (
                item.get("external_source") != EXTERNAL_SOURCE
                or not isinstance(external_id, str)
                or not isinstance(name, str)
            ):
                continue
            match = _MONTH.fullmatch(external_id)
            if not match:
                continue
            actor, month = match.groups()
            if not from_.strftime("%Y-%m") <= month <= to.strftime("%Y-%m") or not name.endswith(" " + month):
                continue
            author = name[: -(len(month) + 1)]
            if user is not None and author != user:
                continue
            for comment in self.client.list_comments(project, str(item["id"])):
                if comment.get("actor") != actor:
                    continue
                entry = parse_comment(comment, self.workspace)
                if entry is not None and entry.month() == month and from_ <= entry.local_date() <= to:
                    entries.append(RemoteEntry(entry, author, str(comment["id"])))
        return sorted(entries, key=lambda value: datetime.fromisoformat(value.entry.at))
