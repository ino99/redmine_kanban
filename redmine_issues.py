#!/usr/bin/env python3
"""Fetch Redmine issues with the REST API."""

import html
import json
import os
import sys
from argparse import ArgumentParser
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


PAGE_LIMIT = 100
TIMEOUT_SECONDS = 30
OUTPUT_HTML = "kanban.html"
DEFAULT_SERVE_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 8000
DEFAULT_PROJECT_ID = "forkers-v3-development"
COMPLETED_STATUSES = {"終了", "完了", "キャンセル", "Closed", "Done", "Canceled"}
COMPLETED_STATUS_KEYS = {"closed", "done", "canceled"}
HIGH_PRIORITIES = {"高", "High", "Urgent", "Immediate"}
ALERT_QUESTIONS = OrderedDict(
    [
        ("期限超過", "完了予定日を再設定する必要はありますか？"),
        ("担当者未設定", "担当者を誰に割り当てますか？"),
        ("7日以上更新なし", "作業は継続中ですか、ブロックされていますか？"),
        ("高優先度", "本日中に対応方針を決める必要はありますか？"),
    ]
)


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for line_number, raw_line in enumerate(env_file, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(
                    f"{path}:{line_number} の形式が不正です。KEY=value で設定してください。"
                )

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f".env に {name} を設定してください。")
    return value


def issue_field(issue: dict[str, Any], name: str, default: str = "-") -> str:
    value = issue.get(name)
    if isinstance(value, dict):
        return str(value.get("name") or default)
    if value:
        return str(value)
    return default


def escape_text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def parse_date(value: Any) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None

    raw_value = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def issue_url(issue: dict[str, Any], redmine_url: str) -> str:
    return urljoin(redmine_url.rstrip("/") + "/", f"issues/{issue.get('id', '-')}")


def assignee_name(issue: dict[str, Any]) -> str:
    return issue_field(issue, "assigned_to", "未設定")


def fixed_version_name(issue: dict[str, Any]) -> str:
    return issue_field(issue, "fixed_version", "未設定")


def is_closed_or_canceled(issue: dict[str, Any]) -> bool:
    status_name = issue_field(issue, "status", "")
    return status_name in COMPLETED_STATUSES or status_name.lower() in COMPLETED_STATUS_KEYS


def should_display_issue(issue: dict[str, Any]) -> bool:
    if not is_closed_or_canceled(issue):
        return True

    updated_on = parse_datetime(issue.get("updated_on"))
    if updated_on is None:
        return True

    now = datetime.now(updated_on.tzinfo or timezone.utc)
    return now - updated_on < timedelta(days=7)


def displayable_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [issue for issue in issues if should_display_issue(issue)]


def classify_issue_flags(issue: dict[str, Any]) -> dict[str, bool]:
    alerts = detect_issue_alerts(issue)
    return {
        "overdue": "期限超過" in alerts,
        "high_priority": "高優先度" in alerts,
        "stale": "7日以上更新なし" in alerts,
    }


def detect_issue_alerts(issue: dict[str, Any], today: date | None = None) -> list[str]:
    alerts = []
    today = today or date.today()

    due_date = parse_date(issue.get("due_date"))
    if due_date and due_date < today:
        alerts.append("期限超過")

    if not issue.get("assigned_to"):
        alerts.append("担当者未設定")

    updated_on = parse_datetime(issue.get("updated_on"))
    if not is_closed_or_canceled(issue) and updated_on:
        now = datetime.now(updated_on.tzinfo or timezone.utc)
        if now - updated_on >= timedelta(days=7):
            alerts.append("7日以上更新なし")

    if issue_field(issue, "priority", "") in HIGH_PRIORITIES:
        alerts.append("高優先度")

    return alerts


def evening_check_questions(alerts: list[str]) -> list[str]:
    questions = []

    for alert in alerts:
        question = ALERT_QUESTIONS.get(alert)
        if question and question not in questions:
            questions.append(question)

    return questions[:3]


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def sample_issues() -> list[dict[str, Any]]:
    return [
        {
            "id": 1001,
            "subject": "ログイン画面の文言を調整する",
            "status": {"name": "new"},
            "tracker": {"name": "Task"},
            "assigned_to": {"name": "佐藤"},
            "priority": {"name": "High"},
            "fixed_version": {"name": "1.2.0"},
            "due_date": "2026-06-20",
            "updated_on": "2026-06-18T09:00:00Z",
        },
        {
            "id": 1002,
            "subject": "CSVエクスポートの仕様を確認する",
            "status": {"name": "in progress"},
            "tracker": {"name": "Feature"},
            "priority": {"name": "normal"},
            "fixed_version": {"name": "1.3.0"},
            "due_date": None,
            "updated_on": "2026-06-10T09:00:00Z",
        },
        {
            "id": 1003,
            "subject": "完了直後のIssueを表示確認する",
            "status": {"name": "closed"},
            "tracker": {"name": "Bug"},
            "assigned_to": {"name": "鈴木"},
            "priority": {"name": "normal"},
            "fixed_version": {"name": "1.2.0"},
            "due_date": None,
            "updated_on": "2026-06-25T09:00:00Z",
        },
        {
            "id": 1004,
            "subject": "古い完了Issueは非表示になる",
            "status": {"name": "closed"},
            "tracker": {"name": "Task"},
            "assigned_to": {"name": "田中"},
            "priority": {"name": "normal"},
            "due_date": None,
            "updated_on": "2026-06-01T09:00:00Z",
        },
    ]


def fetch_issues(redmine_url: str, api_key: str, project_id: str) -> list[dict[str, Any]]:
    endpoint = urljoin(redmine_url.rstrip("/") + "/", "issues.json")
    issues: list[dict[str, Any]] = []
    offset = 0
    total_count: int | None = None

    while total_count is None or offset < total_count:
        params = {
            "project_id": project_id,
            "status_id": "*",
            "limit": PAGE_LIMIT,
            "offset": offset,
        }
        url = f"{endpoint}?{urlencode(params)}"
        request = Request(url, headers={"X-Redmine-API-Key": api_key})

        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                data = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(
                f"Redmine API がエラーを返しました。HTTP {exc.code}: {endpoint}"
            ) from exc
        except TimeoutError as exc:
            raise RuntimeError("Redmine API への接続がタイムアウトしました。") from exc
        except URLError as exc:
            raise RuntimeError(
                "Redmine API に接続できませんでした。REDMINE_URL を確認してください。"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Redmine API のレスポンスをJSONとして読み取れませんでした。") from exc

        if not isinstance(data, dict):
            raise RuntimeError("Redmine API のレスポンス形式が不正です。")

        page_issues = data.get("issues")
        if not isinstance(page_issues, list):
            raise RuntimeError("Redmine API のレスポンスに issues 配列がありません。")

        issues.extend(page_issues)
        try:
            total_count = int(data.get("total_count", len(issues)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Redmine API の total_count が数値ではありません。") from exc

        if not page_issues:
            break

        offset += PAGE_LIMIT

    return issues


def print_issue_summary(issues: list[dict[str, Any]]) -> None:
    print("表示対象Issueの先頭5件:")

    for issue in issues[:5]:
        print()
        print(f"Issue番号: #{issue.get('id', '-')}")
        print(f"件名: {issue.get('subject', '-')}")
        print(f"ステータス: {issue_field(issue, 'status')}")
        print(f"担当者: {issue_field(issue, 'assigned_to')}")
        print(f"優先度: {issue_field(issue, 'priority')}")
        print(f"期日: {issue.get('due_date') or '-'}")
        print(f"最終更新日: {issue.get('updated_on') or '-'}")


def group_issues_by_status(
    issues: list[dict[str, Any]],
) -> OrderedDict[str, list[dict[str, Any]]]:
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    for issue in issues:
        status_name = issue_field(issue, "status", "ステータスなし")
        grouped.setdefault(status_name, []).append(issue)

    return grouped


def assignee_names(issues: list[dict[str, Any]]) -> list[str]:
    return sorted({assignee_name(issue) for issue in issues})


def fixed_version_names(issues: list[dict[str, Any]]) -> list[str]:
    names = {fixed_version_name(issue) for issue in issues}
    configured_names = sorted(name for name in names if name != "未設定")
    configured_names.append("未設定")
    return configured_names


def workload_level(open_issue_count: int) -> tuple[str, str]:
    if open_issue_count >= 5:
        return "負荷高", "high"
    if open_issue_count >= 3:
        return "注意", "warning"
    return "通常", "normal"


def calculate_workload(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    workload: dict[str, dict[str, Any]] = {}

    for issue in issues:
        if is_closed_or_canceled(issue):
            continue

        name = assignee_name(issue)
        item = workload.setdefault(
            name,
            {
                "assignee": name,
                "open_count": 0,
                "overdue_count": 0,
                "high_priority_count": 0,
                "stale_count": 0,
            },
        )
        flags = classify_issue_flags(issue)
        item["open_count"] += 1
        item["overdue_count"] += int(flags["overdue"])
        item["high_priority_count"] += int(flags["high_priority"])
        item["stale_count"] += int(flags["stale"])

    return sorted(
        workload.values(),
        key=lambda item: (-item["open_count"], item["assignee"]),
    )


def render_assignee_filter(issues: list[dict[str, Any]]) -> str:
    options = ['          <option value="__all__">全員</option>']
    for name in assignee_names(issues):
        options.append(
            f'          <option value="{escape_text(name)}">{escape_text(name)}</option>'
        )

    return f"""
    <label class="assignee-filter">
      <span>担当者</span>
      <select id="assignee-filter">
{chr(10).join(options)}
      </select>
    </label>"""


def render_version_filter(issues: list[dict[str, Any]]) -> str:
    version_items = [
        """
        <label class="checkbox-option">
          <input type="checkbox" id="version-all" checked>
          <span>全て</span>
        </label>"""
    ]

    for version in fixed_version_names(issues):
        version_items.append(
            f"""
        <label class="checkbox-option">
          <input type="checkbox" class="version-checkbox" value="{escape_text(version)}">
          <span>{escape_text(version)}</span>
        </label>"""
        )

    return f"""
    <fieldset class="version-filter">
      <legend>対象バージョン</legend>
      <div class="version-options">
{''.join(version_items)}
      </div>
    </fieldset>"""


def render_theme_filter() -> str:
    return """
    <label class="theme-filter">
      <span>テーマ</span>
      <select id="theme-selector">
        <option value="system">OS設定に合わせる</option>
        <option value="light">ライト</option>
        <option value="dark">ダーク</option>
      </select>
    </label>"""


def render_project_control(project_id: str) -> str:
    return f"""
        <form class="project-control" action="/{OUTPUT_HTML}" method="get">
          <label>
            <span>PROJECT_ID</span>
            <input type="text" name="project_id" value="{escape_text(project_id)}">
          </label>
          <button type="submit">表示</button>
        </form>"""


def render_filter_controls(issues: list[dict[str, Any]]) -> str:
    return f"""
    <div class="filter-controls">
{render_assignee_filter(issues)}
{render_version_filter(issues)}
{render_theme_filter()}
      <button type="button" id="reset-filters">フィルタ解除</button>
    </div>"""


def render_workload_summary(issues: list[dict[str, Any]]) -> str:
    workload = calculate_workload(issues)
    if not workload:
        return ""

    cards = []
    for item in workload:
        level_label, level_class = workload_level(item["open_count"])
        cards.append(
            f"""
      <article class="workload-card workload-{escape_text(level_class)}">
        <header>
          <h2>{escape_text(item["assignee"])}</h2>
          <span>{escape_text(level_label)}</span>
        </header>
        <dl>
          <div><dt>未完了</dt><dd>{item["open_count"]}</dd></div>
          <div><dt>期限超過</dt><dd>{item["overdue_count"]}</dd></div>
          <div><dt>高優先度</dt><dd>{item["high_priority_count"]}</dd></div>
          <div><dt>7日以上更新なし</dt><dd>{item["stale_count"]}</dd></div>
        </dl>
      </article>"""
        )

    return f"""
  <section class="workload-summary">
    <h1>担当者別作業負荷</h1>
    <div class="workload-grid" id="workload-grid">
{''.join(cards)}
    </div>
  </section>"""


def render_issue_card(issue: dict[str, Any], redmine_url: str) -> str:
    issue_id = issue.get("id", "-")
    url = issue_url(issue, redmine_url)
    subject = issue.get("subject") or "-"
    assignee = assignee_name(issue)
    version = fixed_version_name(issue)
    alerts = detect_issue_alerts(issue)
    questions = evening_check_questions(alerts)
    flags = classify_issue_flags(issue)
    labels = "\n".join(
        f"""            <span class="alert-label">{escape_text(alert)}</span>"""
        for alert in alerts
    )
    labels_html = (
        f"""
          <div class="alert-labels">
{labels}
          </div>"""
        if labels
        else ""
    )
    question_items = "\n".join(
        f"""              <li>{escape_text(question)}</li>""" for question in questions
    )
    questions_html = (
        f"""
          <section class="evening-check">
            <h3>夕会確認</h3>
            <ul>
{question_items}
            </ul>
          </section>"""
        if question_items
        else ""
    )

    fields = [
        ("トラッカー", issue_field(issue, "tracker")),
        ("担当者", assignee),
        ("対象バージョン", version),
        ("優先度", issue_field(issue, "priority")),
        ("期日", issue.get("due_date") or "-"),
        ("最終更新日", issue.get("updated_on") or "-"),
    ]
    field_items = "\n".join(
        f"""
          <div class="meta-row">
            <dt>{escape_text(label)}</dt>
            <dd>{escape_text(value)}</dd>
          </div>"""
        for label, value in fields
    )

    return f"""
        <article class="issue-card" data-assignee="{escape_text(assignee)}" data-version="{escape_text(version)}" data-is-closed="{str(is_closed_or_canceled(issue)).lower()}" data-overdue="{str(flags["overdue"]).lower()}" data-high-priority="{str(flags["high_priority"]).lower()}" data-stale="{str(flags["stale"]).lower()}">
          <a class="issue-id" href="{escape_text(url)}" target="_blank" rel="noopener noreferrer">#{escape_text(issue_id)}</a>
{labels_html}
          <h2>{escape_text(subject)}</h2>
          <dl class="meta-list">{field_items}
          </dl>
{questions_html}
        </article>"""


def render_kanban_html(
    issues: list[dict[str, Any]], redmine_url: str, project_id: str
) -> str:
    grouped = group_issues_by_status(issues)
    columns = []
    filter_html = render_filter_controls(issues)
    workload_html = render_workload_summary(issues)

    for status_name, status_issues in grouped.items():
        cards = "\n".join(render_issue_card(issue, redmine_url) for issue in status_issues)
        columns.append(
            f"""
      <section class="kanban-column">
        <header class="column-header">
          <h1>{escape_text(status_name)}</h1>
          <span class="column-count">{len(status_issues)}</span>
        </header>
        <div class="cards">
{cards}
        </div>
      </section>"""
        )

    columns_html = "\n".join(columns)
    return f"""<!doctype html>
<html lang="ja" data-theme="system">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Redmine Kanban</title>
  <style>
    * {{
      box-sizing: border-box;
    }}

    :root {{
      color-scheme: light;
      --bg-color: #f3f4f6;
      --text-color: #1f2937;
      --header-bg: #f3f4f6;
      --header-text: #111827;
      --column-bg: #e5e7eb;
      --column-header-bg: #e5e7eb;
      --card-bg: #ffffff;
      --card-border: #d1d5db;
      --muted-text: #6b7280;
      --body-muted-text: #4b5563;
      --link-color: #0f766e;
      --flag-bg: #fee2e2;
      --flag-text: #7f1d1d;
      --flag-border: #f87171;
      --control-bg: #ffffff;
      --control-border: #9ca3af;
      --control-text: #111827;
      --chip-bg: #f9fafb;
      --chip-border: #e5e7eb;
      --button-bg: #374151;
      --button-hover-bg: #111827;
      --button-text: #ffffff;
      --shadow-color: rgba(15, 23, 42, 0.08);
      --evening-bg: #fff7ed;
      --evening-border: #fdba74;
      --evening-text: #7c2d12;
      --evening-heading: #9a3412;
      --workload-high-bg: #fee2e2;
      --workload-high-text: #7f1d1d;
      --workload-high-border: #ef4444;
      --workload-warning-bg: #fef3c7;
      --workload-warning-text: #78350f;
      --workload-warning-border: #f59e0b;
      --workload-normal-bg: #dcfce7;
      --workload-normal-text: #14532d;
    }}

    html[data-theme="dark"] {{
      color-scheme: dark;
      --bg-color: #0f172a;
      --text-color: #e5e7eb;
      --header-bg: #111827;
      --header-text: #f9fafb;
      --column-bg: #1f2937;
      --column-header-bg: #243244;
      --card-bg: #111827;
      --card-border: #374151;
      --muted-text: #9ca3af;
      --body-muted-text: #cbd5e1;
      --link-color: #5eead4;
      --flag-bg: #7f1d1d;
      --flag-text: #fee2e2;
      --flag-border: #fca5a5;
      --control-bg: #0f172a;
      --control-border: #475569;
      --control-text: #f9fafb;
      --chip-bg: #1e293b;
      --chip-border: #475569;
      --button-bg: #0f766e;
      --button-hover-bg: #14b8a6;
      --button-text: #ffffff;
      --shadow-color: rgba(0, 0, 0, 0.35);
      --evening-bg: #431407;
      --evening-border: #ea580c;
      --evening-text: #fed7aa;
      --evening-heading: #fdba74;
      --workload-high-bg: #7f1d1d;
      --workload-high-text: #fee2e2;
      --workload-high-border: #f87171;
      --workload-warning-bg: #713f12;
      --workload-warning-text: #fef3c7;
      --workload-warning-border: #fbbf24;
      --workload-normal-bg: #14532d;
      --workload-normal-text: #dcfce7;
    }}

    @media (prefers-color-scheme: dark) {{
      html[data-theme="system"] {{
        color-scheme: dark;
        --bg-color: #0f172a;
        --text-color: #e5e7eb;
        --header-bg: #111827;
        --header-text: #f9fafb;
        --column-bg: #1f2937;
        --column-header-bg: #243244;
        --card-bg: #111827;
        --card-border: #374151;
        --muted-text: #9ca3af;
        --body-muted-text: #cbd5e1;
        --link-color: #5eead4;
        --flag-bg: #7f1d1d;
        --flag-text: #fee2e2;
        --flag-border: #fca5a5;
        --control-bg: #0f172a;
        --control-border: #475569;
        --control-text: #f9fafb;
        --chip-bg: #1e293b;
        --chip-border: #475569;
        --button-bg: #0f766e;
        --button-hover-bg: #14b8a6;
        --button-text: #ffffff;
        --shadow-color: rgba(0, 0, 0, 0.35);
        --evening-bg: #431407;
        --evening-border: #ea580c;
        --evening-text: #fed7aa;
        --evening-heading: #fdba74;
        --workload-high-bg: #7f1d1d;
        --workload-high-text: #fee2e2;
        --workload-high-border: #f87171;
        --workload-warning-bg: #713f12;
        --workload-warning-text: #fef3c7;
        --workload-warning-border: #fbbf24;
        --workload-normal-bg: #14532d;
        --workload-normal-text: #dcfce7;
      }}
    }}

    body {{
      margin: 0;
      color: var(--text-color);
      background: var(--bg-color);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}

    .page-header {{
      position: sticky;
      left: 0;
      z-index: 2;
      padding: 20px 24px 16px;
      background: var(--header-bg);
      border-bottom: 1px solid var(--card-border);
    }}

    .page-header h1 {{
      margin: 0 0 4px;
      color: var(--header-text);
      font-size: 24px;
      font-weight: 700;
    }}

    .page-header p {{
      margin: 0;
      color: var(--body-muted-text);
      font-size: 14px;
    }}

    .top-row {{
      display: grid;
      grid-template-columns: minmax(240px, 1fr) minmax(320px, 2fr);
      align-items: start;
      gap: 18px;
      margin-bottom: 14px;
    }}

    .filter-controls {{
      display: grid;
      grid-template-columns: minmax(160px, 200px) minmax(240px, 1fr) minmax(160px, 200px) auto;
      align-items: end;
      gap: 12px;
    }}

    .project-control {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: end;
      gap: 8px;
      max-width: 520px;
      margin-top: 12px;
    }}

    .project-control label {{
      display: grid;
      gap: 4px;
      color: var(--body-muted-text);
      font-size: 12px;
      font-weight: 700;
    }}

    .project-control input {{
      width: 100%;
      min-height: 34px;
      padding: 6px 10px;
      color: var(--control-text);
      background: var(--control-bg);
      border: 1px solid var(--control-border);
      border-radius: 8px;
      font: inherit;
      font-weight: 600;
    }}

    .project-control button {{
      min-height: 34px;
      padding: 7px 12px;
      color: var(--button-text);
      background: var(--button-bg);
      border: 1px solid var(--button-bg);
      border-radius: 8px;
      font: inherit;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
      white-space: nowrap;
    }}

    .project-control button:hover {{
      background: var(--button-hover-bg);
    }}

    .assignee-filter,
    .theme-filter {{
      display: grid;
      gap: 4px;
      color: var(--body-muted-text);
      font-size: 12px;
      font-weight: 700;
    }}

    .assignee-filter select,
    .theme-filter select {{
      width: 100%;
      min-height: 34px;
      padding: 6px 10px;
      color: var(--control-text);
      background: var(--control-bg);
      border: 1px solid var(--control-border);
      border-radius: 8px;
      font: inherit;
      font-weight: 600;
    }}

    .version-filter {{
      display: grid;
      gap: 5px;
      min-width: 0;
      margin: 0;
      padding: 0;
      border: 0;
    }}

    .version-filter legend {{
      margin: 0 0 4px;
      padding: 0;
      color: var(--body-muted-text);
      font-size: 12px;
      font-weight: 700;
    }}

    .version-options {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      max-height: 78px;
      overflow-y: auto;
      padding: 6px;
      background: var(--control-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
    }}

    .checkbox-option {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      min-height: 24px;
      padding: 3px 7px;
      color: var(--text-color);
      background: var(--chip-bg);
      border: 1px solid var(--chip-border);
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      line-height: 1.2;
    }}

    .checkbox-option input {{
      margin: 0;
    }}

    #reset-filters {{
      min-height: 34px;
      padding: 7px 12px;
      color: var(--button-text);
      background: var(--button-bg);
      border: 1px solid var(--button-bg);
      border-radius: 8px;
      font: inherit;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
      white-space: nowrap;
    }}

    #reset-filters:hover {{
      background: var(--button-hover-bg);
    }}

    .workload-summary {{
      margin-top: 4px;
    }}

    .workload-summary h1 {{
      margin: 0 0 8px;
      color: var(--body-muted-text);
      font-size: 14px;
      font-weight: 800;
    }}

    .workload-grid {{
      display: flex;
      gap: 10px;
      overflow-x: auto;
      padding-bottom: 2px;
    }}

    .workload-empty {{
      margin: 0;
      padding: 10px 12px;
      color: var(--muted-text);
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      font-size: 13px;
      font-weight: 700;
    }}

    .workload-card {{
      flex: 0 0 220px;
      padding: 10px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
    }}

    .workload-card header {{
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
    }}

    .workload-card h2 {{
      margin: 0;
      color: var(--header-text);
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}

    .workload-card span {{
      flex: 0 0 auto;
      padding: 3px 7px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 800;
    }}

    .workload-high {{
      border-color: var(--workload-high-border);
      box-shadow: inset 4px 0 0 var(--workload-high-border);
    }}

    .workload-high span {{
      color: var(--workload-high-text);
      background: var(--workload-high-bg);
    }}

    .workload-warning {{
      border-color: var(--workload-warning-border);
      box-shadow: inset 4px 0 0 var(--workload-warning-border);
    }}

    .workload-warning span {{
      color: var(--workload-warning-text);
      background: var(--workload-warning-bg);
    }}

    .workload-normal span {{
      color: var(--workload-normal-text);
      background: var(--workload-normal-bg);
    }}

    .workload-card dl {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
      margin: 0;
    }}

    .workload-card dl div {{
      display: grid;
      gap: 2px;
    }}

    .workload-card dt {{
      color: var(--muted-text);
      font-size: 11px;
      font-weight: 700;
    }}

    .workload-card dd {{
      margin: 0;
      color: var(--header-text);
      font-size: 16px;
      font-weight: 800;
    }}

    @media (max-width: 900px) {{
      .top-row {{
        grid-template-columns: 1fr;
      }}

      .filter-controls {{
        grid-template-columns: 1fr;
      }}

      .project-control {{
        grid-template-columns: 1fr;
      }}
    }}

    .kanban-board {{
      display: flex;
      gap: 16px;
      min-height: calc(100vh - 82px);
      overflow-x: auto;
      padding: 16px 24px 24px;
    }}

    .kanban-column {{
      flex: 0 0 340px;
      max-width: 340px;
      min-height: 160px;
      background: var(--column-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
    }}

    .column-header {{
      position: sticky;
      top: 0;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      background: var(--column-header-bg);
      border-bottom: 1px solid var(--card-border);
      border-radius: 8px 8px 0 0;
    }}

    .column-header h1 {{
      margin: 0;
      color: var(--header-text);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 16px;
      font-weight: 700;
    }}

    .column-header span {{
      min-width: 28px;
      padding: 3px 8px;
      text-align: center;
      color: var(--header-text);
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
    }}

    .cards {{
      display: grid;
      gap: 10px;
      padding: 12px;
    }}

    .issue-card {{
      padding: 12px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      box-shadow: 0 1px 2px var(--shadow-color);
    }}

    .issue-card.is-hidden {{
      display: none;
    }}

    .issue-id {{
      display: inline-block;
      margin-bottom: 8px;
      color: var(--link-color);
      font-size: 13px;
      font-weight: 700;
      text-decoration: none;
    }}

    .issue-id:hover {{
      text-decoration: underline;
    }}

    .alert-labels {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 0 0 10px;
    }}

    .alert-label {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 3px 8px;
      color: var(--flag-text);
      background: var(--flag-bg);
      border: 1px solid var(--flag-border);
      border-radius: 999px;
      font-size: 11px;
      font-weight: 800;
      line-height: 1.2;
    }}

    .issue-card h2 {{
      margin: 0 0 12px;
      color: var(--header-text);
      font-size: 14px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }}

    .meta-list {{
      display: grid;
      gap: 6px;
      margin: 0;
    }}

    .meta-row {{
      display: grid;
      grid-template-columns: 88px minmax(0, 1fr);
      gap: 8px;
      font-size: 12px;
      line-height: 1.45;
    }}

    .meta-row dt {{
      color: var(--muted-text);
      font-weight: 600;
    }}

    .meta-row dd {{
      margin: 0;
      color: var(--text-color);
      overflow-wrap: anywhere;
    }}

    .evening-check {{
      margin-top: 12px;
      padding: 10px;
      background: var(--evening-bg);
      border: 1px solid var(--evening-border);
      border-radius: 8px;
    }}

    .evening-check h3 {{
      margin: 0 0 6px;
      color: var(--evening-heading);
      font-size: 12px;
      font-weight: 800;
    }}

    .evening-check ul {{
      display: grid;
      gap: 5px;
      margin: 0;
      padding-left: 18px;
      color: var(--evening-text);
      font-size: 12px;
      line-height: 1.45;
    }}

    .evening-check li {{
      overflow-wrap: anywhere;
    }}
  </style>
</head>
<body>
  <header class="page-header">
    <div class="top-row">
      <div>
        <h1>Redmine Kanban</h1>
        <p><span id="visible-issue-count">{len(issues)}</span> / {len(issues)} issues</p>
{render_project_control(project_id)}
      </div>
{filter_html}
    </div>
{workload_html}
  </header>
  <main class="kanban-board">
{columns_html}
  </main>
  <script>
    const THEME_STORAGE_KEY = "redmine-kanban-theme";
    const themeSelector = document.getElementById("theme-selector");
    const assigneeFilter = document.getElementById("assignee-filter");
    const versionAll = document.getElementById("version-all");
    const versionCheckboxes = Array.from(document.querySelectorAll(".version-checkbox"));
    const resetFiltersButton = document.getElementById("reset-filters");
    const visibleIssueCount = document.getElementById("visible-issue-count");
    const workloadGrid = document.getElementById("workload-grid");

    function getSavedTheme() {{
      const savedTheme = localStorage.getItem(THEME_STORAGE_KEY);
      if (["light", "dark", "system"].includes(savedTheme)) {{
        return savedTheme;
      }}
      return "system";
    }}

    function applyTheme(theme) {{
      const nextTheme = ["light", "dark", "system"].includes(theme) ? theme : "system";
      document.documentElement.dataset.theme = nextTheme;
      themeSelector.value = nextTheme;
    }}

    function saveTheme(theme) {{
      localStorage.setItem(THEME_STORAGE_KEY, theme);
    }}

    function initializeThemeSelector() {{
      applyTheme(getSavedTheme());
      themeSelector.addEventListener("change", () => {{
        applyTheme(themeSelector.value);
        saveTheme(themeSelector.value);
      }});
    }}

    function getSelectedVersions() {{
      if (versionAll.checked) {{
        return null;
      }}

      return new Set(versionCheckboxes.filter((checkbox) => checkbox.checked).map((checkbox) => checkbox.value));
    }}

    function workloadLevel(openIssueCount) {{
      if (openIssueCount >= 5) {{
        return ["負荷高", "high"];
      }}
      if (openIssueCount >= 3) {{
        return ["注意", "warning"];
      }}
      return ["通常", "normal"];
    }}

    function workloadMetric(label, value) {{
      const wrapper = document.createElement("div");
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = label;
      description.textContent = value;
      wrapper.append(term, description);
      return wrapper;
    }}

    function createWorkloadCard(item) {{
      const [levelLabel, levelClass] = workloadLevel(item.openCount);
      const card = document.createElement("article");
      card.className = `workload-card workload-${{levelClass}}`;

      const header = document.createElement("header");
      const title = document.createElement("h2");
      const badge = document.createElement("span");
      title.textContent = item.assignee;
      badge.textContent = levelLabel;
      header.append(title, badge);

      const details = document.createElement("dl");
      details.append(
        workloadMetric("未完了", item.openCount),
        workloadMetric("期限超過", item.overdueCount),
        workloadMetric("高優先度", item.highPriorityCount),
        workloadMetric("7日以上更新なし", item.staleCount),
      );

      card.append(header, details);
      return card;
    }}

    function updateWorkloadSummary(visibleCards) {{
      const workload = new Map();

      visibleCards.forEach((card) => {{
        if (card.dataset.isClosed === "true") {{
          return;
        }}

        const assignee = card.dataset.assignee || "未設定";
        const item = workload.get(assignee) || {{
          assignee,
          openCount: 0,
          overdueCount: 0,
          highPriorityCount: 0,
          staleCount: 0,
        }};

        item.openCount += 1;
        item.overdueCount += card.dataset.overdue === "true" ? 1 : 0;
        item.highPriorityCount += card.dataset.highPriority === "true" ? 1 : 0;
        item.staleCount += card.dataset.stale === "true" ? 1 : 0;
        workload.set(assignee, item);
      }});

      workloadGrid.replaceChildren();
      const items = Array.from(workload.values()).sort((a, b) => b.openCount - a.openCount || a.assignee.localeCompare(b.assignee, "ja"));

      if (items.length === 0) {{
        const empty = document.createElement("p");
        empty.className = "workload-empty";
        empty.textContent = "表示中の未完了Issueはありません。";
        workloadGrid.append(empty);
        return;
      }}

      items.forEach((item) => workloadGrid.append(createWorkloadCard(item)));
    }}

    function applyFilters() {{
      const selectedAssignee = assigneeFilter.value;
      const selectedVersions = getSelectedVersions();
      let visibleTotal = 0;
      const visibleCards = [];

      document.querySelectorAll(".kanban-column").forEach((column) => {{
        let columnVisibleCount = 0;

        column.querySelectorAll(".issue-card").forEach((card) => {{
          const assigneeMatches = selectedAssignee === "__all__" || card.dataset.assignee === selectedAssignee;
          const versionMatches = selectedVersions === null || selectedVersions.has(card.dataset.version);
          const matches = assigneeMatches && versionMatches;
          card.classList.toggle("is-hidden", !matches);
          if (matches) {{
            columnVisibleCount += 1;
            visibleTotal += 1;
            visibleCards.push(card);
          }}
        }});

        column.querySelector(".column-count").textContent = columnVisibleCount;
      }});

      visibleIssueCount.textContent = visibleTotal;
      updateWorkloadSummary(visibleCards);
    }}

    function handleVersionAllChange() {{
      if (versionAll.checked) {{
        versionCheckboxes.forEach((checkbox) => {{
          checkbox.checked = false;
        }});
      }} else if (!versionCheckboxes.some((checkbox) => checkbox.checked)) {{
        versionAll.checked = true;
      }}

      applyFilters();
    }}

    function handleVersionCheckboxChange() {{
      if (versionCheckboxes.some((checkbox) => checkbox.checked)) {{
        versionAll.checked = false;
      }} else {{
        versionAll.checked = true;
      }}

      applyFilters();
    }}

    function resetFilters() {{
      assigneeFilter.value = "__all__";
      versionAll.checked = true;
      versionCheckboxes.forEach((checkbox) => {{
        checkbox.checked = false;
      }});
      applyFilters();
    }}

    assigneeFilter.addEventListener("change", applyFilters);
    versionAll.addEventListener("change", handleVersionAllChange);
    versionCheckboxes.forEach((checkbox) => checkbox.addEventListener("change", handleVersionCheckboxChange));
    resetFiltersButton.addEventListener("click", resetFilters);
    initializeThemeSelector();
    applyFilters();
  </script>
</body>
</html>
"""


def write_kanban_html(
    issues: list[dict[str, Any]], redmine_url: str, project_id: str
) -> Path:
    output_path = Path(OUTPUT_HTML).resolve()
    output_path.write_text(
        render_kanban_html(issues, redmine_url, project_id), encoding="utf-8"
    )
    return output_path


def load_issue_data(
    project_id_override: str | None = None,
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    load_env()
    project_id = (project_id_override or os.getenv("PROJECT_ID") or DEFAULT_PROJECT_ID).strip()
    if not project_id:
        project_id = DEFAULT_PROJECT_ID

    if env_flag("USE_SAMPLE_DATA"):
        redmine_url = os.getenv("REDMINE_URL", "https://redmine.example.com")
        issues = sample_issues()
    else:
        redmine_url = require_env("REDMINE_URL")
        api_key = require_env("REDMINE_API_KEY")
        issues = fetch_issues(redmine_url, api_key, project_id)

    visible_issues = displayable_issues(issues)
    return redmine_url, project_id, issues, visible_issues


def alert_issues(
    issues: list[dict[str, Any]], redmine_url: str
) -> list[tuple[dict[str, Any], list[str]]]:
    results = []
    for issue in issues:
        alerts = detect_issue_alerts(issue)
        if alerts:
            results.append((issue, alerts))
    return results


def print_alert_issues(issues: list[dict[str, Any]], redmine_url: str) -> None:
    issues_with_alerts = alert_issues(issues, redmine_url)

    print()
    print(f"注意すべきIssue数: {len(issues_with_alerts)}")

    for issue, alerts in issues_with_alerts[:10]:
        questions = evening_check_questions(alerts)
        print()
        print(f"#{issue.get('id', '-')} {issue.get('subject', '-')}")
        print(f"担当: {issue_field(issue, 'assigned_to')}")
        print(f"理由: {', '.join(alerts)}")
        print("夕会で確認すること:")
        for question in questions:
            print(f"- {question}")
        print(f"URL: {issue_url(issue, redmine_url)}")


def render_error_html(message: str) -> str:
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Redmine Kanban Error</title>
  <style>
    body {{
      margin: 0;
      padding: 32px;
      color: #111827;
      background: #f3f4f6;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}

    main {{
      max-width: 760px;
      padding: 20px;
      background: #ffffff;
      border: 1px solid #d1d5db;
      border-radius: 8px;
    }}

    h1 {{
      margin: 0 0 12px;
      font-size: 20px;
    }}

    p {{
      margin: 0;
      line-height: 1.6;
    }}
  </style>
</head>
<body>
  <main>
    <h1>Redmine Kanban を更新できませんでした</h1>
    <p>{escape_text(message)}</p>
  </main>
</body>
</html>
"""


class KanbanRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        if parsed_url.path not in {"/", f"/{OUTPUT_HTML}"}:
            self.send_error(404, "Not Found")
            return

        query = parse_qs(parsed_url.query)
        project_id = query.get("project_id", [DEFAULT_PROJECT_ID])[0]

        try:
            redmine_url, resolved_project_id, _, visible_issues = load_issue_data(project_id)
            response_body = render_kanban_html(
                visible_issues, redmine_url, resolved_project_id
            )
            status_code = 200
        except (ValueError, RuntimeError) as exc:
            response_body = render_error_html(str(exc))
            status_code = 500

        encoded_body = response_body.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded_body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded_body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[server] {self.address_string()} - {format % args}", file=sys.stderr)


def serve_kanban(host: str, port: int) -> int:
    try:
        server = ThreadingHTTPServer((host, port), KanbanRequestHandler)
    except OSError as exc:
        print(
            f"エラー: http://{host}:{port}/ は使用中、または起動できません。",
            file=sys.stderr,
        )
        print(
            "別のポートを指定してください。例: python3 redmine_issues.py --serve --port 8001",
            file=sys.stderr,
        )
        print(f"詳細: {exc}", file=sys.stderr)
        return 1

    print(f"Redmine Kanban server: http://{host}:{port}/{OUTPUT_HTML}")
    print("ブラウザでF5するとRedmine APIから再取得して表示します。終了は Ctrl+C です。")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバーを終了しました。")
    finally:
        server.server_close()

    return 0


def parse_args() -> Any:
    parser = ArgumentParser(description="Generate or serve a Redmine Kanban Board.")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start a local server. Browser reload fetches Redmine again.",
    )
    parser.add_argument("--host", default=DEFAULT_SERVE_HOST, help="Host for --serve.")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_SERVE_PORT,
        help="Port for --serve.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.serve:
        return serve_kanban(args.host, args.port)

    try:
        redmine_url, project_id, issues, visible_issues = load_issue_data()
        output_path = write_kanban_html(visible_issues, redmine_url, project_id)
    except (ValueError, RuntimeError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    print(f"取得したIssue数: {len(issues)}")
    print(f"表示対象Issue数: {len(visible_issues)}")
    print_issue_summary(visible_issues)
    print_alert_issues(visible_issues, redmine_url)
    print()
    print(f"kanban.html: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
