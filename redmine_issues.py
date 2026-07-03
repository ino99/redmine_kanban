#!/usr/bin/env python3
"""Fetch Redmine issues with the REST API."""

import html
import hashlib
import json
import os
import sys
import time
from argparse import ArgumentParser
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock, Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


PAGE_LIMIT = 100
TIMEOUT_SECONDS = 20
DEFAULT_TIME_ENTRY_TIMEOUT_SECONDS = 8
DEFAULT_FETCH_WORKERS = 4
DEFAULT_FETCH_RETRIES = 0
DEFAULT_TIME_ENTRY_RETRIES = 0
FETCH_RETRY_DELAY_SECONDS = 1.0
TIME_ENTRY_PAGE_LIMIT = 100
DEFAULT_TIME_ENTRY_PAGES = 2
OUTPUT_HTML = "kanban.html"
CACHE_DIR = Path(".cache")
CACHE_SCHEMA_VERSION = 1
DEFAULT_SERVE_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 8000
DEFAULT_PROJECT_ID = "my-redmine-project"
STATUS_ORDER = [
    "new",
    "assigned",
    "in progress",
    "feedback",
    "resolved",
    "closed",
    "canceled",
]
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


@dataclass
class IssueCacheEntry:
    redmine_url: str
    issues: list[dict[str, Any]]
    refreshed_at: datetime


ISSUE_CACHE: dict[str, IssueCacheEntry] = {}
ISSUE_CACHE_LOCK = RLock()
ISSUE_REFRESH_IN_PROGRESS: set[str] = set()
ISSUE_REFRESH_ERRORS: dict[str, str] = {}
ISSUE_STARTUP_REFRESH_STARTED: set[str] = set()


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


def issue_numeric_id(issue: dict[str, Any]) -> int | None:
    try:
        return int(issue.get("id"))
    except (TypeError, ValueError):
        return None


def assignee_name(issue: dict[str, Any]) -> str:
    return issue_field(issue, "assigned_to", "未設定")


def fixed_version_name(issue: dict[str, Any]) -> str:
    return issue_field(issue, "fixed_version", "未設定")


def remaining_work_time(issue: dict[str, Any]) -> str:
    custom_fields = issue.get("custom_fields")
    if not isinstance(custom_fields, list):
        return "-"

    for field in custom_fields:
        if not isinstance(field, dict):
            continue
        if not str(field.get("name") or "").startswith("残作業時間"):
            continue
        value = field.get("value")
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value if item)
        if value not in (None, ""):
            return str(value)

    return "-"


def format_remaining_work_time(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned == "-":
        return "-"
    if cleaned.endswith(("時間", "h", "H")):
        return cleaned
    return f"{cleaned}時間"


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


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name)
    if not raw_value:
        return default

    try:
        value = int(raw_value)
    except ValueError:
        return default

    return max(minimum, value)


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
            "custom_fields": [{"name": "残作業時間", "value": "3.5"}],
            "_latest_time_entry": {
                "spent_on": "2026-06-30",
                "user": "佐藤",
                "hours": "1.0h",
                "comment": "文言案を確認中。レビュー後に反映予定。",
            },
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
            "custom_fields": [{"name": "残作業時間", "value": ""}],
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


def fetch_issues_page(
    endpoint: str,
    api_key: str,
    project_id: str,
    offset: int,
    updated_since: date | None = None,
) -> tuple[list[dict[str, Any]], int]:
    params = {
        "project_id": project_id,
        "status_id": "*",
        "limit": PAGE_LIMIT,
        "offset": offset,
    }
    if updated_since:
        params["updated_on"] = f">={updated_since.isoformat()}"

    url = f"{endpoint}?{urlencode(params)}"
    request = Request(url, headers={"X-Redmine-API-Key": api_key})
    retries = env_int("REDMINE_FETCH_RETRIES", DEFAULT_FETCH_RETRIES, minimum=0)

    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                data = json.load(response)
            break
        except HTTPError as exc:
            raise RuntimeError(
                f"Redmine API がエラーを返しました。HTTP {exc.code}: {endpoint}"
            ) from exc
        except TimeoutError as exc:
            if attempt < retries:
                time.sleep(FETCH_RETRY_DELAY_SECONDS)
                continue
            raise RuntimeError("Redmine API への接続がタイムアウトしました。") from exc
        except URLError as exc:
            if attempt < retries:
                time.sleep(FETCH_RETRY_DELAY_SECONDS)
                continue
            reason = getattr(exc, "reason", exc)
            raise RuntimeError(
                f"Redmine API に接続できませんでした。REDMINE_URL を確認してください。詳細: {reason}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Redmine API のレスポンスをJSONとして読み取れませんでした。") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Redmine API のレスポンス形式が不正です。")

    page_issues = data.get("issues")
    if not isinstance(page_issues, list):
        raise RuntimeError("Redmine API のレスポンスに issues 配列がありません。")

    try:
        total_count = int(data.get("total_count", len(page_issues)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Redmine API の total_count が数値ではありません。") from exc

    return page_issues, total_count


def fetch_issues(
    redmine_url: str, api_key: str, project_id: str, updated_since: date | None = None
) -> list[dict[str, Any]]:
    endpoint = urljoin(redmine_url.rstrip("/") + "/", "issues.json")
    first_page, total_count = fetch_issues_page(
        endpoint, api_key, project_id, 0, updated_since
    )
    issues: list[dict[str, Any]] = list(first_page)

    if not first_page or len(issues) >= total_count:
        return issues

    offsets = list(range(PAGE_LIMIT, total_count, PAGE_LIMIT))
    worker_count = min(env_int("REDMINE_FETCH_WORKERS", DEFAULT_FETCH_WORKERS), len(offsets))
    pages: dict[int, list[dict[str, Any]]] = {}

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_offset = {
            executor.submit(fetch_issues_page, endpoint, api_key, project_id, offset, updated_since): offset
            for offset in offsets
        }
        for future in as_completed(future_to_offset):
            offset = future_to_offset[future]
            page_issues, _ = future.result()
            pages[offset] = page_issues

    for offset in offsets:
        issues.extend(pages.get(offset, []))

    return issues


def fetch_time_entries_page(
    endpoint: str,
    api_key: str,
    project_id: str,
    offset: int,
) -> list[dict[str, Any]]:
    params = {
        "project_id": project_id,
        "limit": TIME_ENTRY_PAGE_LIMIT,
        "offset": offset,
        "sort": "spent_on:desc,created_on:desc",
    }
    url = f"{endpoint}?{urlencode(params)}"
    request = Request(url, headers={"X-Redmine-API-Key": api_key})
    retries = env_int("REDMINE_TIME_ENTRY_RETRIES", DEFAULT_TIME_ENTRY_RETRIES, minimum=0)
    timeout_seconds = env_int(
        "REDMINE_TIME_ENTRY_TIMEOUT_SECONDS",
        DEFAULT_TIME_ENTRY_TIMEOUT_SECONDS,
        minimum=1,
    )

    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                data = json.load(response)
            break
        except HTTPError as exc:
            raise RuntimeError(
                f"Redmine 作業時間API がエラーを返しました。HTTP {exc.code}: {endpoint}"
            ) from exc
        except TimeoutError as exc:
            if attempt < retries:
                time.sleep(FETCH_RETRY_DELAY_SECONDS)
                continue
            raise RuntimeError(
                f"Redmine 作業時間API への接続が{timeout_seconds}秒でタイムアウトしました。"
            ) from exc
        except URLError as exc:
            if attempt < retries:
                time.sleep(FETCH_RETRY_DELAY_SECONDS)
                continue
            reason = getattr(exc, "reason", exc)
            raise RuntimeError(
                f"Redmine 作業時間API に接続できませんでした。詳細: {reason}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Redmine 作業時間API のレスポンスをJSONとして読み取れませんでした。") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Redmine 作業時間API のレスポンス形式が不正です。")

    entries = data.get("time_entries")
    if not isinstance(entries, list):
        raise RuntimeError("Redmine 作業時間API のレスポンスに time_entries 配列がありません。")

    return entries


def time_entry_user_name(entry: dict[str, Any]) -> str:
    user = entry.get("user")
    if isinstance(user, dict):
        return str(user.get("name") or "-")
    return "-"


def format_time_entry_hours(entry: dict[str, Any]) -> str:
    hours = entry.get("hours")
    if hours in (None, ""):
        return "-"
    return f"{hours}h"


def latest_time_entry_comments(
    redmine_url: str,
    api_key: str,
    project_id: str,
    issues: list[dict[str, Any]],
) -> dict[int, dict[str, str]]:
    issue_ids = {
        issue_id for issue in issues if (issue_id := issue_numeric_id(issue)) is not None
    }
    if not issue_ids:
        return {}

    endpoint = urljoin(redmine_url.rstrip("/") + "/", "time_entries.json")
    max_pages = env_int("REDMINE_TIME_ENTRY_PAGES", DEFAULT_TIME_ENTRY_PAGES, minimum=0)
    comments: dict[int, dict[str, str]] = {}

    for page in range(max_pages):
        entries = fetch_time_entries_page(
            endpoint,
            api_key,
            project_id,
            page * TIME_ENTRY_PAGE_LIMIT,
        )
        if not entries:
            break

        for entry in entries:
            issue = entry.get("issue")
            if not isinstance(issue, dict):
                continue
            issue_id = issue.get("id")
            if issue_id not in issue_ids or issue_id in comments:
                continue
            comment = str(entry.get("comments") or "").strip()
            if comment:
                comments[issue_id] = {
                    "comment": comment,
                    "spent_on": str(entry.get("spent_on") or "-"),
                    "user": time_entry_user_name(entry),
                    "hours": format_time_entry_hours(entry),
                }

        if comments.keys() >= issue_ids:
            break

    return comments


def attach_time_entry_comments(
    issues: list[dict[str, Any]],
    comments: dict[int, dict[str, str]],
) -> None:
    for issue in issues:
        issue_id = issue_numeric_id(issue)
        if issue_id is None:
            continue
        comment = comments.get(issue_id)
        if comment:
            issue["_latest_time_entry"] = comment


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

    ordered_grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for status_key in STATUS_ORDER:
        for status_name, status_issues in grouped.items():
            if status_order_key(status_name) == status_key:
                ordered_grouped[status_name] = status_issues

    for status_name, status_issues in grouped.items():
        if status_name not in ordered_grouped:
            ordered_grouped[status_name] = status_issues

    return ordered_grouped


def status_order_key(status_name: str) -> str:
    return " ".join(status_name.lower().split())


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
          <button type="submit" name="refresh_mode" value="incremental" formmethod="post" formaction="/refresh">更新</button>
          <button type="submit" name="refresh_mode" value="full" formmethod="post" formaction="/refresh">全更新</button>
          <span class="refresh-status" id="refresh-status" role="status" aria-live="polite"></span>
        </form>"""


def render_filter_controls(issues: list[dict[str, Any]]) -> str:
    return f"""
    <div class="filter-controls">
{render_assignee_filter(issues)}
{render_version_filter(issues)}
{render_theme_filter()}
      <button type="button" id="reset-filters">フィルタ解除</button>
    </div>"""


def workload_bar_width(value: int, max_value: int) -> int:
    if value <= 0 or max_value <= 0:
        return 0
    return max(8, round(value / max_value * 100))


def render_workload_metric(label: str, value: int, max_value: int, bar_class: str) -> str:
    width = workload_bar_width(value, max_value)
    value_class = " workload-bar-value-on-fill" if width >= 50 and bar_class in {"open", "overdue", "stale"} else ""
    return f"""
          <div class="workload-metric">
            <dt>{escape_text(label)}</dt>
            <dd>
              <span class="workload-bar-shell">
                <span class="workload-bar-fill workload-bar-{escape_text(bar_class)}" style="width: {width}%"></span>
                <span class="workload-bar-value{value_class}">{value}</span>
              </span>
            </dd>
          </div>"""


def render_workload_summary(issues: list[dict[str, Any]]) -> str:
    workload = calculate_workload(issues)
    if not workload:
        return ""

    cards = []
    for item in workload:
        level_label, level_class = workload_level(item["open_count"])
        max_value = max(
            item["open_count"],
            item["overdue_count"],
            item["high_priority_count"],
            item["stale_count"],
            1,
        )
        cards.append(
            f"""
      <article class="workload-card workload-{escape_text(level_class)}">
        <header>
          <h2>{escape_text(item["assignee"])}</h2>
          <span>{escape_text(level_label)}</span>
        </header>
        <dl>
{render_workload_metric("未完了", item["open_count"], max_value, "open")}
{render_workload_metric("期限超過", item["overdue_count"], max_value, "overdue")}
{render_workload_metric("高優先度", item["high_priority_count"], max_value, "priority")}
{render_workload_metric("7日以上更新無", item["stale_count"], max_value, "stale")}
        </dl>
      </article>"""
        )

    return f"""
  <section class="workload-summary">
    <div class="workload-summary-header">
      <h1>担当者別作業負荷</h1>
    </div>
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
    latest_time_entry = issue.get("_latest_time_entry")
    latest_time_entry_comment = ""
    latest_time_entry_meta = ""
    if isinstance(latest_time_entry, dict):
        latest_time_entry_comment = str(latest_time_entry.get("comment") or "").strip()
        latest_time_entry_meta = (
            f'{latest_time_entry.get("spent_on") or "-"} / '
            f'{latest_time_entry.get("user") or "-"} / '
            f'{latest_time_entry.get("hours") or "-"}'
        )
    time_entry_comment_html = (
        f"""
          <section class="time-entry-comment">
            <h3>最新作業時間コメント</h3>
            <p class="time-entry-meta">{escape_text(latest_time_entry_meta)}</p>
            <p>{escape_text(latest_time_entry_comment)}</p>
          </section>"""
        if latest_time_entry_comment
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
    remaining_work = remaining_work_time(issue)
    if remaining_work != "-":
        fields.append(("残作業時間", format_remaining_work_time(remaining_work)))
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
{time_entry_comment_html}
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
      --note-bg: #ecfdf5;
      --note-border: #86efac;
      --note-text: #14532d;
      --note-heading: #166534;
      --workload-high-bg: #fee2e2;
      --workload-high-text: #7f1d1d;
      --workload-high-border: #ef4444;
      --workload-warning-bg: #fef3c7;
      --workload-warning-text: #78350f;
      --workload-warning-border: #f59e0b;
      --workload-normal-bg: #dcfce7;
      --workload-normal-text: #14532d;
      --workload-total-bg: #a5f3fc;
      --workload-total-border: #0284c7;
      --workload-bar-track: #e5e7eb;
      --workload-bar-open: #334155;
      --workload-bar-overdue: #dc2626;
      --workload-bar-priority: #d97706;
      --workload-bar-stale: #7c3aed;
      --workload-bar-value: #111827;
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
      --note-bg: #052e1a;
      --note-border: #15803d;
      --note-text: #bbf7d0;
      --note-heading: #86efac;
      --workload-high-bg: #7f1d1d;
      --workload-high-text: #fee2e2;
      --workload-high-border: #f87171;
      --workload-warning-bg: #713f12;
      --workload-warning-text: #fef3c7;
      --workload-warning-border: #fbbf24;
      --workload-normal-bg: #14532d;
      --workload-normal-text: #dcfce7;
      --workload-total-bg: #164e63;
      --workload-total-border: #22d3ee;
      --workload-bar-track: #334155;
      --workload-bar-open: #94a3b8;
      --workload-bar-overdue: #f87171;
      --workload-bar-priority: #fbbf24;
      --workload-bar-stale: #a78bfa;
      --workload-bar-value: #f8fafc;
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
        --note-bg: #052e1a;
        --note-border: #15803d;
        --note-text: #bbf7d0;
        --note-heading: #86efac;
        --workload-high-bg: #7f1d1d;
        --workload-high-text: #fee2e2;
        --workload-high-border: #f87171;
        --workload-warning-bg: #713f12;
        --workload-warning-text: #fef3c7;
        --workload-warning-border: #fbbf24;
        --workload-normal-bg: #14532d;
        --workload-normal-text: #dcfce7;
        --workload-total-bg: #164e63;
        --workload-total-border: #22d3ee;
        --workload-bar-track: #334155;
        --workload-bar-open: #94a3b8;
        --workload-bar-overdue: #f87171;
        --workload-bar-priority: #fbbf24;
        --workload-bar-stale: #a78bfa;
        --workload-bar-value: #f8fafc;
      }}
    }}

    html,
    body {{
      height: 100%;
    }}

    body {{
      display: flex;
      flex-direction: column;
      margin: 0;
      overflow: hidden;
      color: var(--text-color);
      background: var(--bg-color);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}

    .page-header {{
      flex: 0 0 auto;
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
      grid-template-columns: minmax(0, 1fr) auto auto auto;
      align-items: end;
      gap: 8px;
      max-width: 720px;
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

    .project-control button.is-refreshing {{
      opacity: 0.72;
      cursor: progress;
    }}

    .refresh-status {{
      display: none;
      grid-column: 1 / -1;
      align-items: center;
      gap: 8px;
      min-height: 18px;
      color: var(--body-muted-text);
      font-size: 12px;
      font-weight: 700;
    }}

    .refresh-status.is-visible {{
      display: inline-flex;
    }}

    .refresh-status::before {{
      width: 12px;
      height: 12px;
      border: 2px solid var(--control-border);
      border-top-color: var(--link-color);
      border-radius: 999px;
      content: "";
      animation: refresh-spin 0.8s linear infinite;
    }}

    @keyframes refresh-spin {{
      to {{ transform: rotate(360deg); }}
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

    .workload-summary-header {{
      display: flex;
      align-items: center;
      justify-content: flex-start;
      flex-wrap: wrap;
      gap: 8px 14px;
      margin: 0 0 8px;
    }}

    .workload-summary h1 {{
      margin: 0;
      color: var(--body-muted-text);
      font-size: 14px;
      font-weight: 800;
    }}

    .workload-grid {{
      display: flex;
      gap: 8px;
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
      flex: 0 0 240px;
      padding: 8px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
    }}

    .workload-card header {{
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 10px;
    }}

    .workload-card h2 {{
      min-width: 0;
      max-width: 100%;
      margin: 0;
      color: var(--header-text);
      font-size: 13px;
      line-height: 1.25;
      overflow-x: auto;
      overflow-y: hidden;
      white-space: nowrap;
      scrollbar-width: thin;
    }}

    .workload-card header > span {{
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

    .workload-high header > span {{
      color: var(--workload-high-text);
      background: var(--workload-high-bg);
    }}

    .workload-warning {{
      border-color: var(--workload-warning-border);
      box-shadow: inset 4px 0 0 var(--workload-warning-border);
    }}

    .workload-warning header > span {{
      color: var(--workload-warning-text);
      background: var(--workload-warning-bg);
    }}

    .workload-normal header > span {{
      color: var(--workload-normal-text);
      background: var(--workload-normal-bg);
    }}

    .workload-total {{
      background: var(--workload-total-bg);
      border-color: var(--workload-total-border);
      box-shadow: inset 4px 0 0 var(--workload-total-border);
    }}

    .workload-card dl {{
      display: grid;
      gap: 7px;
      margin: 0;
    }}

    .workload-metric {{
      display: grid;
      grid-template-columns: 82px minmax(0, 1fr);
      align-items: center;
      gap: 8px;
      min-width: 0;
    }}

    .workload-card dt {{
      color: var(--muted-text);
      font-size: 11px;
      font-weight: 700;
      line-height: 1.2;
      text-align: left;
      overflow-wrap: anywhere;
    }}

    .workload-card dd {{
      margin: 0;
    }}

    .workload-bar-shell {{
      position: relative;
      display: block;
      width: 100%;
      height: 24px;
      overflow: hidden;
      background: var(--workload-bar-track);
      border: 1px solid var(--card-border);
      border-radius: 0;
    }}

    .workload-bar-fill {{
      position: absolute;
      top: 0;
      bottom: 0;
      left: 0;
      min-width: 0;
      border-radius: 0;
    }}

    .workload-bar-open {{
      background: var(--workload-bar-open);
    }}

    .workload-bar-overdue {{
      background: var(--workload-bar-overdue);
    }}

    .workload-bar-priority {{
      background: var(--workload-bar-priority);
    }}

    .workload-bar-stale {{
      background: var(--workload-bar-stale);
    }}

    .workload-bar-value {{
      position: absolute;
      top: 50%;
      left: 50%;
      z-index: 1;
      transform: translate(-50%, -50%);
      color: var(--workload-bar-value);
      font-size: 14px;
      font-weight: 800;
      line-height: 1;
      text-shadow: 0 1px 2px var(--shadow-color);
    }}

    .workload-bar-value-on-fill {{
      color: #ffffff;
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
      flex: 1 1 auto;
      display: flex;
      gap: 16px;
      min-height: 0;
      overflow: auto;
      padding: 16px 24px 24px;
    }}

    .kanban-column {{
      flex: 0 0 340px;
      max-width: 340px;
      align-self: flex-start;
      min-height: 160px;
      max-height: 100%;
      overflow-y: auto;
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

    .time-entry-comment {{
      margin-top: 12px;
      padding: 10px;
      background: var(--note-bg);
      border: 1px solid var(--note-border);
      border-radius: 8px;
    }}

    .time-entry-comment h3 {{
      margin: 0 0 6px;
      color: var(--note-heading);
      font-size: 12px;
      font-weight: 800;
    }}

    .time-entry-comment p {{
      margin: 0;
      color: var(--note-text);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }}

    .time-entry-comment .time-entry-meta {{
      margin-bottom: 6px;
      color: var(--muted-text);
      font-size: 11px;
      font-weight: 700;
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
    const projectControl = document.querySelector(".project-control");
    const refreshStatus = document.getElementById("refresh-status");

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

    function workloadBarWidth(value, maxValue) {{
      if (value <= 0 || maxValue <= 0) {{
        return 0;
      }}
      return Math.max(8, Math.round(value / maxValue * 100));
    }}

    function shouldUseLightBarText(width, barClass) {{
      return width >= 50 && ["open", "overdue", "stale"].includes(barClass);
    }}

    function workloadMetric(label, value, maxValue, barClass) {{
      const wrapper = document.createElement("div");
      wrapper.className = "workload-metric";

      const term = document.createElement("dt");
      const description = document.createElement("dd");
      const shell = document.createElement("span");
      const fill = document.createElement("span");
      const valueLabel = document.createElement("span");

      term.textContent = label;
      shell.className = "workload-bar-shell";
      fill.className = `workload-bar-fill workload-bar-${{barClass}}`;
      const width = workloadBarWidth(value, maxValue);
      fill.style.width = `${{width}}%`;
      valueLabel.className = "workload-bar-value";
      valueLabel.classList.toggle("workload-bar-value-on-fill", shouldUseLightBarText(width, barClass));
      valueLabel.textContent = value;

      shell.append(fill, valueLabel);
      description.append(shell);
      wrapper.append(term, description);
      return wrapper;
    }}

    function createWorkloadCard(item) {{
      const [levelLabel, levelClass] = workloadLevel(item.openCount);
      const card = document.createElement("article");
      card.className = `workload-card workload-${{levelClass}}`;
      if (item.isTotal) {{
        card.classList.add("workload-total");
      }}

      const header = document.createElement("header");
      const title = document.createElement("h2");
      const badge = document.createElement("span");
      title.textContent = item.assignee;
      badge.textContent = levelLabel;
      header.append(title);
      if (!item.isTotal) {{
        header.append(badge);
      }}

      const maxValue = Math.max(
        item.openCount,
        item.overdueCount,
        item.highPriorityCount,
        item.staleCount,
        1,
      );
      const details = document.createElement("dl");
      details.append(
        workloadMetric("未完了", item.openCount, maxValue, "open"),
        workloadMetric("期限超過", item.overdueCount, maxValue, "overdue"),
        workloadMetric("高優先度", item.highPriorityCount, maxValue, "priority"),
        workloadMetric("7日以上更新無", item.staleCount, maxValue, "stale"),
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

      if (assigneeFilter.value === "__all__") {{
        const totalItem = items.reduce((total, item) => {{
          total.openCount += item.openCount;
          total.overdueCount += item.overdueCount;
          total.highPriorityCount += item.highPriorityCount;
          total.staleCount += item.staleCount;
          return total;
        }}, {{
          assignee: "全体",
          isTotal: true,
          openCount: 0,
          overdueCount: 0,
          highPriorityCount: 0,
          staleCount: 0,
        }});
        workloadGrid.append(createWorkloadCard(totalItem));
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

    function initializeRefreshStatus() {{
      if (!projectControl || !refreshStatus) {{
        return;
      }}

      projectControl.addEventListener("submit", (event) => {{
        const submitter = event.submitter;
        if (!submitter || submitter.name !== "refresh_mode") {{
          return;
        }}

        const isFullRefresh = submitter.value === "full";
        refreshStatus.textContent = isFullRefresh ? "全更新中..." : "更新中...";
        refreshStatus.classList.add("is-visible");
        submitter.classList.add("is-refreshing");
        submitter.setAttribute("aria-busy", "true");
        window.requestAnimationFrame(() => {{
          submitter.disabled = true;
        }});
      }});
    }}

    assigneeFilter.addEventListener("change", applyFilters);
    versionAll.addEventListener("change", handleVersionAllChange);
    versionCheckboxes.forEach((checkbox) => checkbox.addEventListener("change", handleVersionCheckboxChange));
    resetFiltersButton.addEventListener("click", resetFilters);
    initializeThemeSelector();
    initializeRefreshStatus();
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


def disk_cache_redmine_url() -> str | None:
    if env_flag("USE_SAMPLE_DATA"):
        return None
    return os.getenv("REDMINE_URL")


def disk_cache_path(project_id: str, redmine_url: str) -> Path:
    cache_key = hashlib.sha256(
        f"{redmine_url.rstrip('/')}|{project_id}".encode("utf-8")
    ).hexdigest()[:16]
    return CACHE_DIR / f"issues-{cache_key}.json"


def load_issue_cache_from_disk(project_id: str) -> IssueCacheEntry | None:
    redmine_url = disk_cache_redmine_url()
    if not redmine_url:
        return None

    cache_path = disk_cache_path(project_id, redmine_url)
    if not cache_path.exists():
        return None

    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[server] Failed to read disk cache {cache_path}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None

    if data.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    if data.get("project_id") != project_id:
        return None
    if str(data.get("redmine_url") or "").rstrip("/") != redmine_url.rstrip("/"):
        return None

    issues = data.get("issues")
    if not isinstance(issues, list):
        return None

    refreshed_at = parse_datetime(data.get("refreshed_at"))
    if refreshed_at is None:
        return None

    return IssueCacheEntry(redmine_url=redmine_url, issues=issues, refreshed_at=refreshed_at)


def save_issue_cache_to_disk(project_id: str, cache_entry: IssueCacheEntry) -> None:
    cache_redmine_url = disk_cache_redmine_url()
    if not cache_redmine_url:
        return

    try:
        CACHE_DIR.mkdir(exist_ok=True)
        cache_path = disk_cache_path(project_id, cache_redmine_url)
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "redmine_url": cache_entry.redmine_url,
            "project_id": project_id,
            "refreshed_at": cache_entry.refreshed_at.isoformat(),
            "issues": cache_entry.issues,
        }
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except OSError as exc:
        print(
            f"[server] Failed to write disk cache: {exc}",
            file=sys.stderr,
            flush=True,
        )


def ensure_issue_cache_loaded_from_disk(project_id: str) -> bool:
    with ISSUE_CACHE_LOCK:
        if project_id in ISSUE_CACHE:
            return True

    cache_entry = load_issue_cache_from_disk(project_id)
    if cache_entry is None:
        return False

    with ISSUE_CACHE_LOCK:
        ISSUE_CACHE.setdefault(project_id, cache_entry)

    print(
        f"[server] Loaded issue cache from disk: project_id={project_id}, issues={len(cache_entry.issues)}",
        file=sys.stderr,
        flush=True,
    )
    return True


def resolve_project_id(project_id_override: str | None = None) -> str:
    project_id = (project_id_override or os.getenv("PROJECT_ID") or DEFAULT_PROJECT_ID).strip()
    return project_id or DEFAULT_PROJECT_ID


def load_issue_data(
    project_id_override: str | None = None,
    updated_since: date | None = None,
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    load_env()
    project_id = resolve_project_id(project_id_override)
    started_at = time.monotonic()

    if env_flag("USE_SAMPLE_DATA"):
        redmine_url = os.getenv("REDMINE_URL", "https://redmine.example.com")
        issues = sample_issues()
    else:
        redmine_url = require_env("REDMINE_URL")
        api_key = require_env("REDMINE_API_KEY")
        print(
            f"[server] Issue取得を開始します。PROJECT_ID={project_id}",
            file=sys.stderr,
            flush=True,
        )
        issues = fetch_issues(redmine_url, api_key, project_id, updated_since)
        print(
            f"[server] Issue取得完了: {len(issues)}件 ({time.monotonic() - started_at:.1f}秒)",
            file=sys.stderr,
            flush=True,
        )

    visible_issues = displayable_issues(issues)
    if not env_flag("USE_SAMPLE_DATA"):
        try:
            comment_started_at = time.monotonic()
            print(
                "[server] 作業時間コメント取得を開始します。",
                file=sys.stderr,
                flush=True,
            )
            comments = latest_time_entry_comments(
                redmine_url,
                api_key,
                project_id,
                visible_issues,
            )
            attach_time_entry_comments(visible_issues, comments)
            print(
                f"[server] 作業時間コメント取得完了: {len(comments)}件 ({time.monotonic() - comment_started_at:.1f}秒)",
                file=sys.stderr,
                flush=True,
            )
        except RuntimeError as exc:
            print(
                f"[server] 作業時間コメントを取得できませんでした: {exc}",
                file=sys.stderr,
                flush=True,
            )
    return redmine_url, project_id, issues, visible_issues


def request_project_id(query: dict[str, list[str]]) -> str | None:
    value = query.get("project_id", [None])[0]
    if value is None:
        return None
    value = value.strip()
    return value or None


def merge_issues(
    current_issues: list[dict[str, Any]], updated_issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    updated_by_id = {issue.get("id"): issue for issue in updated_issues if issue.get("id")}
    merged = []
    seen_ids = set()

    for issue in current_issues:
        issue_id = issue.get("id")
        if issue_id in updated_by_id:
            merged.append(updated_by_id[issue_id])
            seen_ids.add(issue_id)
        else:
            merged.append(issue)

    for issue in updated_issues:
        issue_id = issue.get("id")
        if issue_id not in seen_ids:
            merged.append(issue)
            if issue_id:
                seen_ids.add(issue_id)

    return merged


def load_cached_issue_data(
    project_id_override: str | None = None, refresh_mode: str | None = None
) -> tuple[str, str, list[dict[str, Any]]]:
    load_env()
    project_id = resolve_project_id(project_id_override)

    with ISSUE_CACHE_LOCK:
        cached = ISSUE_CACHE.get(project_id)
        if cached and refresh_mode is None:
            return cached.redmine_url, project_id, displayable_issues(cached.issues)

        cached_issues = list(cached.issues) if cached else []
        cached_refreshed_at = cached.refreshed_at if cached else None

    updated_since = None
    if refresh_mode == "incremental" and cached_refreshed_at:
        updated_since = (cached_refreshed_at - timedelta(days=1)).date()

    redmine_url, resolved_project_id, issues, visible_issues = load_issue_data(
        project_id, updated_since
    )
    if updated_since:
        issues = merge_issues(cached_issues, issues)
        visible_issues = displayable_issues(issues)

    cache_entry = IssueCacheEntry(
        redmine_url=redmine_url,
        issues=issues,
        refreshed_at=datetime.now(timezone.utc),
    )
    with ISSUE_CACHE_LOCK:
        ISSUE_CACHE[resolved_project_id] = cache_entry

    save_issue_cache_to_disk(resolved_project_id, cache_entry)

    return redmine_url, resolved_project_id, visible_issues


def start_background_refresh(
    project_id: str, refresh_mode: str = "full", once_per_startup: bool = False
) -> None:
    with ISSUE_CACHE_LOCK:
        if once_per_startup and project_id in ISSUE_STARTUP_REFRESH_STARTED:
            return
        if project_id in ISSUE_REFRESH_IN_PROGRESS:
            return
        if once_per_startup:
            ISSUE_STARTUP_REFRESH_STARTED.add(project_id)
        ISSUE_REFRESH_IN_PROGRESS.add(project_id)
        ISSUE_REFRESH_ERRORS.pop(project_id, None)

    def refresh() -> None:
        try:
            load_cached_issue_data(project_id, refresh_mode=refresh_mode)
        except (ValueError, RuntimeError) as exc:
            with ISSUE_CACHE_LOCK:
                ISSUE_REFRESH_ERRORS[project_id] = str(exc)
            print(
                f"[server] バックグラウンド取得に失敗しました: {exc}",
                file=sys.stderr,
                flush=True,
            )
        finally:
            with ISSUE_CACHE_LOCK:
                ISSUE_REFRESH_IN_PROGRESS.discard(project_id)

    Thread(target=refresh, daemon=True).start()


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


def render_loading_html(project_id: str | None) -> str:
    project_id_value = project_id or resolve_project_id(None)
    reload_url = f"/{OUTPUT_HTML}?{urlencode({'project_id': project_id_value})}"
    reload_url_json = json.dumps(reload_url)
    return f"""<!doctype html>
<html lang="ja" data-theme="system">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Redmine Kanban Loading</title>
  <style>
    :root {{
      color-scheme: light;
      --bg-color: #f3f4f6;
      --text-color: #111827;
      --muted-text: #6b7280;
      --card-bg: #ffffff;
      --card-border: #d1d5db;
      --link-color: #0f766e;
      --shadow-color: rgba(15, 23, 42, 0.08);
    }}

    @media (prefers-color-scheme: dark) {{
      :root {{
        color-scheme: dark;
        --bg-color: #0f172a;
        --text-color: #f9fafb;
        --muted-text: #9ca3af;
        --card-bg: #111827;
        --card-border: #374151;
        --link-color: #5eead4;
        --shadow-color: rgba(0, 0, 0, 0.35);
      }}
    }}

    body {{
      display: grid;
      min-height: 100vh;
      margin: 0;
      place-items: center;
      color: var(--text-color);
      background: var(--bg-color);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}

    main {{
      width: min(560px, calc(100vw - 32px));
      padding: 22px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      box-shadow: 0 8px 24px var(--shadow-color);
    }}

    h1 {{
      margin: 0 0 8px;
      font-size: 20px;
      line-height: 1.35;
    }}

    p {{
      margin: 0;
      color: var(--muted-text);
      line-height: 1.6;
    }}

    .progress {{
      height: 8px;
      margin-top: 18px;
      overflow: hidden;
      background: var(--card-border);
      border-radius: 999px;
    }}

    .progress span {{
      display: block;
      width: 42%;
      height: 100%;
      background: var(--link-color);
      border-radius: inherit;
      animation: loading 1.1s ease-in-out infinite;
    }}

    @keyframes loading {{
      0% {{ transform: translateX(-110%); }}
      100% {{ transform: translateX(260%); }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Redmine Kanban を読み込み中</h1>
    <p>PROJECT_ID: {escape_text(project_id_value)}</p>
    <p id="loading-message">Issueを取得しています。完了すると自動で表示します。</p>
    <div class="progress" aria-hidden="true"><span></span></div>
  </main>
  <script>
    window.addEventListener("load", () => {{
      window.setTimeout(() => {{
        window.location.replace({reload_url_json});
      }}, 5000);
    }});
  </script>
</body>
</html>
"""


class KanbanRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        if parsed_url.path == "/.well-known/appspecific/com.chrome.devtools.json":
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if parsed_url.path not in {"/", f"/{OUTPUT_HTML}"}:
            self.send_error(404, "Not Found")
            return

        query = parse_qs(parsed_url.query)
        project_id = request_project_id(query)

        try:
            resolved_project_id = resolve_project_id(project_id)
            ensure_issue_cache_loaded_from_disk(resolved_project_id)
            with ISSUE_CACHE_LOCK:
                has_cache = resolved_project_id in ISSUE_CACHE
                refresh_error = ISSUE_REFRESH_ERRORS.get(resolved_project_id)
            if not has_cache:
                if refresh_error:
                    response_body = render_error_html(refresh_error)
                    status_code = 500
                    encoded_body = response_body.encode("utf-8")
                    self.send_response(status_code)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(encoded_body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(encoded_body)
                    return

                start_background_refresh(resolved_project_id)
                response_body = render_loading_html(resolved_project_id)
                status_code = 200
                encoded_body = response_body.encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded_body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded_body)
                return

            redmine_url, resolved_project_id, visible_issues = load_cached_issue_data(project_id)
            start_background_refresh(
                resolved_project_id,
                refresh_mode="incremental",
                once_per_startup=True,
            )
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

    def do_POST(self) -> None:
        parsed_url = urlparse(self.path)
        if parsed_url.path != "/refresh":
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length).decode("utf-8")
        form = parse_qs(body)
        project_id = request_project_id(form)
        refresh_mode = form.get("refresh_mode", ["incremental"])[0]
        if refresh_mode not in {"incremental", "full"}:
            refresh_mode = "incremental"

        try:
            print(
                f"[server] 更新リクエスト開始: mode={refresh_mode}, project_id={project_id or resolve_project_id(None)}",
                file=sys.stderr,
                flush=True,
            )
            _, resolved_project_id, _ = load_cached_issue_data(
                project_id, refresh_mode=refresh_mode
            )
            print(
                f"[server] 更新リクエスト完了: project_id={resolved_project_id}",
                file=sys.stderr,
                flush=True,
            )
        except (ValueError, RuntimeError) as exc:
            response_body = render_error_html(str(exc))
            encoded_body = response_body.encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded_body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded_body)
            return

        location = f"/{OUTPUT_HTML}?{urlencode({'project_id': resolved_project_id})}"
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

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
    print("画面の更新ボタンで差分取得、全更新ボタンで全件取得します。終了は Ctrl+C です。")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバーを終了しました。")
    finally:
        server.server_close()

    return 0


def parse_args() -> Any:
    parser = ArgumentParser(description="Fetch or serve a Redmine Kanban Board.")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start a local server. The refresh button fetches Redmine again.",
    )
    parser.add_argument(
        "--write-html",
        action="store_true",
        help="Write kanban.html. By default, no HTML file is generated.",
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
        output_path = (
            write_kanban_html(visible_issues, redmine_url, project_id)
            if args.write_html
            else None
        )
    except (ValueError, RuntimeError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    print(f"取得したIssue数: {len(issues)}")
    print(f"表示対象Issue数: {len(visible_issues)}")
    print_issue_summary(visible_issues)
    print_alert_issues(visible_issues, redmine_url)
    print()
    if output_path:
        print(f"kanban.html: {output_path}")
    else:
        print("kanban.html generation skipped. Use --serve to view in a browser.")
        print("Use --write-html only when you need to create a standalone HTML file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
