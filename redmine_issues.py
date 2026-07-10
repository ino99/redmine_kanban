#!/usr/bin/env python3
"""Fetch Redmine issues with the REST API."""

import html
import hashlib
import json
import os
import re
import sys
import time
from argparse import ArgumentParser
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
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
SUB_ASSIGNEE_FIELD_NAME = "副担当者"
SUB_ASSIGNEE_CACHE_FIELD = "_sub_assignees"
BALL_POSSESSION_FIELD_NAME = "ボール所持"
BUG_CATEGORY_FIELD_NAME = "不具合のカテゴリ"
OUTPUT_HTML = "kanban.html"
WORKLOAD_HTML = "workload.html"
COMBINED_HTML = "combined.html"
WORKTIME_HTML = "worktime.html"
QUALITY_HTML = "quality.html"
CACHE_DIR = Path(".cache")
CACHE_SCHEMA_VERSION = 1
DEFAULT_SERVE_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 8000
DEFAULT_PROJECT_ID = "my-redmine-project"
PROJECT_ID_COOKIE_NAME = "redmine_kanban_project_id"
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


def script_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


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


def user_reference_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if name:
        return str(name)
    full_name = " ".join(
        str(part).strip()
        for part in (value.get("firstname"), value.get("lastname"))
        if str(part or "").strip()
    )
    if full_name:
        return full_name
    login = value.get("login")
    if login:
        return str(login)
    return None


def user_reference_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    user_id = value.get("id")
    if user_id in (None, ""):
        return None
    return str(user_id)


def user_id_name_map(issues: list[dict[str, Any]]) -> dict[str, str]:
    results: dict[str, str] = {}
    for issue in issues:
        for field_name in ("assigned_to", "author"):
            user = issue.get(field_name)
            user_id = user_reference_id(user)
            user_name = user_reference_name(user)
            if user_id and user_name:
                results[user_id] = user_name
    return results


def sub_assignee_ids(issue: dict[str, Any]) -> list[str]:
    custom_fields = issue.get("custom_fields")
    if not isinstance(custom_fields, list):
        return []

    for field in custom_fields:
        if not isinstance(field, dict):
            continue
        if str(field.get("name") or "") != SUB_ASSIGNEE_FIELD_NAME:
            continue

        value = field.get("value")
        if isinstance(value, list):
            return [str(item) for item in value if item not in (None, "")]
        if value not in (None, ""):
            return [str(value)]
        return []

    return []


def sub_assignee_names(issue: dict[str, Any]) -> list[str]:
    cached = issue.get(SUB_ASSIGNEE_CACHE_FIELD)
    if isinstance(cached, list):
        return [str(name) for name in cached if name]
    return []


def participant_names(issue: dict[str, Any]) -> list[str]:
    names = [assignee_name(issue)]
    names.extend(sub_assignee_names(issue))

    unique_names = []
    seen = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        unique_names.append(name)
    return unique_names


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


def custom_field_values(issue: dict[str, Any], field_name: str) -> list[str]:
    custom_fields = issue.get("custom_fields")
    if not isinstance(custom_fields, list):
        return []

    for field in custom_fields:
        if not isinstance(field, dict):
            continue
        if str(field.get("name") or "") != field_name:
            continue

        value = field.get("value")
        if isinstance(value, list):
            return [str(item) for item in value if item not in (None, "")]
        if isinstance(value, dict):
            name = value.get("name")
            if name:
                return [str(name)]
            value_id = value.get("id")
            return [str(value_id)] if value_id not in (None, "") else []
        if value not in (None, ""):
            return [str(value)]
        return []

    return []


def has_custom_field(issue: dict[str, Any], field_name: str) -> bool:
    custom_fields = issue.get("custom_fields")
    if not isinstance(custom_fields, list):
        return False

    return any(
        isinstance(field, dict) and str(field.get("name") or "") == field_name
        for field in custom_fields
    )


def ball_possession_values(issue: dict[str, Any]) -> list[str]:
    return custom_field_values(issue, BALL_POSSESSION_FIELD_NAME)


def bug_category_values(issue: dict[str, Any]) -> list[str]:
    values = custom_field_values(issue, BUG_CATEGORY_FIELD_NAME)
    return values or ["未設定"]


def format_remaining_work_time(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned == "-":
        return "-"
    if cleaned.endswith(("時間", "h", "H")):
        return cleaned
    return f"{cleaned}時間"


def remaining_work_hours(issue: dict[str, Any]) -> float:
    value = remaining_work_time(issue)
    if value == "-":
        return 0.0

    match = re.search(r"\d+(?:\.\d+)?", value)
    if not match:
        return 0.0

    try:
        return float(match.group(0))
    except ValueError:
        return 0.0


def format_hours(value: float) -> str:
    if value <= 0:
        return "-"
    if value.is_integer():
        return f"{int(value)}h"
    return f"{value:.1f}h"


def format_refreshed_at(value: datetime | None) -> str:
    if value is None:
        return "未取得"

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local_value = value.astimezone()
    return local_value.strftime("%Y/%m/%d %H:%M")


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
            "custom_fields": [
                {"name": "残作業時間", "value": "3.5"},
                {"name": "副担当者", "value": ["田中"]},
            ],
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


def possible_value_label_map(possible_values: Any) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not isinstance(possible_values, list):
        return labels

    for item in possible_values:
        if isinstance(item, dict):
            label = item.get("label") or item.get("name") or item.get("value")
            for key_name in ("value", "id"):
                key = item.get(key_name)
                if key not in (None, "") and label not in (None, ""):
                    labels[str(key)] = str(label)
        elif item not in (None, ""):
            labels[str(item)] = str(item)
    return labels


def fetch_custom_field_value_labels(
    redmine_url: str,
    api_key: str,
    field_name: str,
) -> dict[str, str]:
    endpoint = urljoin(redmine_url.rstrip("/") + "/", "custom_fields.json")
    request = Request(endpoint, headers={"X-Redmine-API-Key": api_key})

    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.load(response)
    except (HTTPError, TimeoutError, URLError, json.JSONDecodeError) as exc:
        print(
            f"[server] カスタムフィールド定義を取得できませんでした: field={field_name}, {exc}",
            file=sys.stderr,
            flush=True,
        )
        return {}

    if not isinstance(data, dict):
        return {}

    custom_fields = data.get("custom_fields")
    if not isinstance(custom_fields, list):
        return {}

    for field in custom_fields:
        if not isinstance(field, dict):
            continue
        if str(field.get("name") or "") != field_name:
            continue
        return possible_value_label_map(field.get("possible_values"))
    return {}


def fetch_user_name(redmine_url: str, api_key: str, user_id: str) -> str | None:
    endpoint = urljoin(redmine_url.rstrip("/") + "/", f"users/{user_id}.json")
    request = Request(endpoint, headers={"X-Redmine-API-Key": api_key})

    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.load(response)
    except (HTTPError, TimeoutError, URLError, json.JSONDecodeError) as exc:
        print(
            f"[server] 副担当者の名前を取得できませんでした: user_id={user_id}, {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None

    if not isinstance(data, dict):
        return None

    user = data.get("user")
    if not isinstance(user, dict):
        return None

    return user_reference_name(user)


def attach_sub_assignee_names(
    issues: list[dict[str, Any]], redmine_url: str | None = None, api_key: str | None = None
) -> None:
    name_by_id = user_id_name_map(issues)
    unknown_user_ids = sorted(
        {
            user_id
            for issue in issues
            for user_id in sub_assignee_ids(issue)
            if user_id not in name_by_id
        }
    )

    if redmine_url and api_key:
        for user_id in unknown_user_ids:
            user_name = fetch_user_name(redmine_url, api_key, user_id)
            if user_name:
                name_by_id[user_id] = user_name

    for issue in issues:
        names = []
        for user_id in sub_assignee_ids(issue):
            names.append(name_by_id.get(user_id) or user_id)
        if names:
            issue[SUB_ASSIGNEE_CACHE_FIELD] = names
        else:
            issue.pop(SUB_ASSIGNEE_CACHE_FIELD, None)


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


def time_entry_hours_value(entry: dict[str, Any]) -> float:
    hours = entry.get("hours")
    try:
        return float(hours)
    except (TypeError, ValueError):
        return 0.0


def fetch_worktime_entries(
    redmine_url: str,
    api_key: str,
    project_id: str,
    date_from: date,
    date_to: date,
) -> list[dict[str, Any]]:
    endpoint = urljoin(redmine_url.rstrip("/") + "/", "time_entries.json")
    retries = env_int("REDMINE_TIME_ENTRY_RETRIES", DEFAULT_TIME_ENTRY_RETRIES, minimum=0)
    timeout_seconds = env_int(
        "REDMINE_TIME_ENTRY_TIMEOUT_SECONDS",
        DEFAULT_TIME_ENTRY_TIMEOUT_SECONDS,
        minimum=1,
    )
    max_pages = env_int("REDMINE_WORKTIME_PAGES", 20, minimum=1)
    results: list[dict[str, Any]] = []

    for page_index in range(max_pages):
        params = {
            "project_id": project_id,
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
            "limit": TIME_ENTRY_PAGE_LIMIT,
            "offset": page_index * TIME_ENTRY_PAGE_LIMIT,
            "sort": "spent_on:desc,created_on:desc",
        }
        request = Request(
            f"{endpoint}?{urlencode(params)}",
            headers={"X-Redmine-API-Key": api_key},
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

        results.extend(entries)
        total_count = data.get("total_count")
        if isinstance(total_count, int) and len(results) >= total_count:
            break
        if len(entries) < TIME_ENTRY_PAGE_LIMIT:
            break

    return results


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
    return sorted({name for issue in issues for name in participant_names(issue)})


def fixed_version_names(issues: list[dict[str, Any]]) -> list[str]:
    names = {fixed_version_name(issue) for issue in issues}
    configured_names = sorted(name for name in names if name != "未設定")
    configured_names.append("未設定")
    return configured_names


def ball_possession_names(issues: list[dict[str, Any]]) -> list[str]:
    names = {name for issue in issues for name in ball_possession_values(issue)}
    return sorted(names)


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


def render_ball_possession_filter(issues: list[dict[str, Any]]) -> str:
    names = ball_possession_names(issues)
    if not names:
        return ""

    options = ['          <option value="__all__">全て</option>']
    for name in names:
        options.append(
            f'          <option value="{escape_text(name)}">{escape_text(name)}</option>'
        )

    return f"""
    <label class="ball-possession-filter">
      <span>ボール所持</span>
      <select id="ball-possession-filter">
{chr(10).join(options)}
      </select>
    </label>"""


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
        <form class="project-control" action="/project" method="post">
          <label>
            <span>PROJECT_ID</span>
            <div class="project-id-picker">
              <input type="text" id="project-id-input" name="project_id" value="{escape_text(project_id)}" autocomplete="off" aria-haspopup="listbox" aria-expanded="false">
              <button type="button" class="project-id-history-toggle" id="project-id-history-toggle" aria-label="PROJECT_ID履歴を開く" aria-controls="project-id-history-menu">▼</button>
              <div class="project-id-history-menu" id="project-id-history-menu" role="listbox" hidden></div>
            </div>
          </label>
          <button class="control-action control-action-display" type="submit">表示</button>
          <button class="control-action control-action-refresh" type="submit" name="refresh_mode" value="incremental" formmethod="post" formaction="/refresh">更新</button>
          <button class="control-action control-action-refresh" type="submit" name="refresh_mode" value="full" formmethod="post" formaction="/refresh">全更新</button>
          <a class="control-link control-link-workload" href="/{WORKLOAD_HTML}" target="_top">作業負荷状況</a>
          <a class="control-link control-link-worktime" href="/{WORKTIME_HTML}" target="_blank" rel="noopener noreferrer">作業時間</a>
          <a class="control-link control-link-quality" href="/{QUALITY_HTML}" target="_top">品質改善</a>
          <a class="control-link control-link-combined" href="/{COMBINED_HTML}" target="_top">同時表示</a>
          <span class="refresh-status" id="refresh-status" role="status" aria-live="polite"></span>
        </form>"""


def render_filter_controls(issues: list[dict[str, Any]]) -> str:
    return f"""
    <div class="filter-controls">
      <label class="issue-id-filter">
        <span>Issue番号</span>
        <input type="search" id="issue-id-filter" inputmode="numeric" placeholder="例: 6750 6741" autocomplete="off">
      </label>
{render_assignee_filter(issues)}
{render_version_filter(issues)}
{render_ball_possession_filter(issues)}
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
          <h2><button type="button" class="workload-assignee-button" data-assignee="{escape_text(item["assignee"])}">{escape_text(item["assignee"])}</button></h2>
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
    sub_assignees = sub_assignee_names(issue)
    participants_json = json.dumps(participant_names(issue), ensure_ascii=False)
    version = fixed_version_name(issue)
    ball_possession_json = json.dumps(ball_possession_values(issue), ensure_ascii=False)
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
            <h3>状況確認</h3>
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
    if sub_assignees:
        fields.insert(2, ("副担当者", ", ".join(sub_assignees)))
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
        <article class="issue-card" data-issue-id="{escape_text(issue_id)}" data-assignee="{escape_text(assignee)}" data-participants="{escape_text(participants_json)}" data-version="{escape_text(version)}" data-ball-possession="{escape_text(ball_possession_json)}" data-is-closed="{str(is_closed_or_canceled(issue)).lower()}" data-overdue="{str(flags["overdue"]).lower()}" data-high-priority="{str(flags["high_priority"]).lower()}" data-stale="{str(flags["stale"]).lower()}">
          <a class="issue-id" href="{escape_text(url)}" target="_blank" rel="noopener noreferrer">#{escape_text(issue_id)}</a>
{labels_html}
          <h2>{escape_text(subject)}</h2>
          <dl class="meta-list">{field_items}
          </dl>
{questions_html}
{time_entry_comment_html}
        </article>"""


def workload_level_by_score(score: float) -> tuple[str, str]:
    if score >= 8:
        return "高負荷", "high"
    if score >= 4:
        return "注意", "warning"
    return "通常", "normal"


def workload_balance_class(value: float, max_value: float) -> str:
    if value <= 0 or max_value <= 0:
        return "balance-empty"
    ratio = value / max_value
    if ratio >= 0.75:
        return "balance-high"
    if ratio >= 0.4:
        return "balance-medium"
    return "balance-low"


def workload_issue_summary(
    issue: dict[str, Any], redmine_url: str, role: str, remaining_hours: float
) -> dict[str, Any]:
    flags = classify_issue_flags(issue)
    return {
        "id": issue.get("id", "-"),
        "url": issue_url(issue, redmine_url),
        "subject": issue.get("subject") or "-",
        "role": role,
        "status": issue_field(issue, "status"),
        "priority": issue_field(issue, "priority"),
        "due_date": issue.get("due_date") or "-",
        "updated_on": issue.get("updated_on") or "-",
        "remaining_hours": remaining_hours,
        "overdue": flags["overdue"],
        "high_priority": flags["high_priority"],
        "stale": flags["stale"],
    }


def calculate_workload_dashboard(
    issues: list[dict[str, Any]], redmine_url: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    people: dict[str, dict[str, Any]] = {}
    open_issues = [issue for issue in issues if not is_closed_or_canceled(issue)]

    for issue in open_issues:
        primary = assignee_name(issue)
        sub_names = sub_assignee_names(issue)
        participants = participant_names(issue)
        flags = classify_issue_flags(issue)
        remaining_hours = remaining_work_hours(issue)
        assigned_remaining_hours = (
            remaining_hours / len(participants)
            if sub_names and participants
            else remaining_hours
        )

        for name in participants:
            role = "主担当" if name == primary else "副担当"
            item = people.setdefault(
                name,
                {
                    "assignee": name,
                    "open_count": 0,
                    "primary_count": 0,
                    "sub_count": 0,
                    "overdue_count": 0,
                    "high_priority_count": 0,
                    "stale_count": 0,
                    "primary_remaining_hours": 0.0,
                    "issues": [],
                },
            )
            item["open_count"] += 1
            item["primary_count"] += int(role == "主担当")
            item["sub_count"] += int(role == "副担当")
            item["overdue_count"] += int(flags["overdue"])
            item["high_priority_count"] += int(flags["high_priority"])
            item["stale_count"] += int(flags["stale"])
            item["primary_remaining_hours"] += assigned_remaining_hours
            item["issues"].append(
                workload_issue_summary(issue, redmine_url, role, assigned_remaining_hours)
            )

        if not participants and not sub_names:
            people.setdefault(
                "未設定",
                {
                    "assignee": "未設定",
                    "open_count": 0,
                    "primary_count": 0,
                    "sub_count": 0,
                    "overdue_count": 0,
                    "high_priority_count": 0,
                    "stale_count": 0,
                    "primary_remaining_hours": 0.0,
                    "issues": [],
                },
            )

    for item in people.values():
        item["score"] = (
            item["open_count"]
            + item["overdue_count"] * 2
            + item["high_priority_count"] * 2
            + item["stale_count"]
            + item["primary_remaining_hours"] / 8
        )
        level_label, level_class = workload_level_by_score(item["score"])
        item["level_label"] = level_label
        item["level_class"] = level_class
        item["issues"].sort(
            key=lambda issue: (
                issue["role"] != "主担当",
                not issue["overdue"],
                not issue["high_priority"],
                str(issue["due_date"]),
                str(issue["id"]),
            )
        )

    people_list = sorted(
        people.values(),
        key=lambda item: (-item["score"], -item["open_count"], item["assignee"]),
    )
    totals = {
        "open_count": len(open_issues),
        "person_count": len(people_list),
        "high_load_count": sum(1 for item in people_list if item["level_class"] == "high"),
        "overdue_count": sum(1 for issue in open_issues if classify_issue_flags(issue)["overdue"]),
        "high_priority_count": sum(
            1 for issue in open_issues if classify_issue_flags(issue)["high_priority"]
        ),
        "primary_remaining_hours": sum(remaining_work_hours(issue) for issue in open_issues),
    }
    return people_list, totals


def render_workload_status_html(
    issues: list[dict[str, Any]], redmine_url: str, project_id: str
) -> str:
    people, _totals = calculate_workload_dashboard(issues, redmine_url)
    people_json = script_json(people)
    has_remaining_work = any(remaining_work_hours(issue) > 0 for issue in issues)
    has_remaining_work_json = script_json(has_remaining_work)
    max_values = {
        "open_count": max([item["open_count"] for item in people] or [0]),
        "overdue_count": max([item["overdue_count"] for item in people] or [0]),
        "high_priority_count": max([item["high_priority_count"] for item in people] or [0]),
        "stale_count": max([item["stale_count"] for item in people] or [0]),
        "primary_remaining_hours": max(
            [item["primary_remaining_hours"] for item in people] or [0]
        ),
        "score": max([item["score"] for item in people] or [0]),
    }

    selected_totals = {
        "open_count": sum(item["open_count"] for item in people),
        "person_count": len(people),
        "high_load_count": sum(1 for item in people if item["level_class"] == "high"),
        "overdue_count": sum(item["overdue_count"] for item in people),
        "high_priority_count": sum(item["high_priority_count"] for item in people),
        "primary_remaining_hours": sum(item["primary_remaining_hours"] for item in people),
    }
    stat_cards = [
        ("open_count", "関与Issue", selected_totals["open_count"]),
        ("person_count", "対象者", selected_totals["person_count"]),
        ("high_load_count", "高負荷", selected_totals["high_load_count"]),
        ("overdue_count", "期限超過", selected_totals["overdue_count"]),
        ("high_priority_count", "高優先度", selected_totals["high_priority_count"]),
    ]
    if has_remaining_work:
        stat_cards.append(
            (
                "primary_remaining_hours",
                "残作業時間",
                format_hours(selected_totals["primary_remaining_hours"]),
            )
        )
    stat_cards_html = "\n".join(
        f"""
        <article class="stat-card">
          <span>{escape_text(label)}</span>
          <strong data-stat-key="{escape_text(key)}">{escape_text(value)}</strong>
        </article>"""
        for key, label, value in stat_cards
    )

    person_filter_options = "\n".join(
        f"""
          <label class="person-filter-option">
            <input type="checkbox" class="person-filter-checkbox" value="{escape_text(item["assignee"])}" checked>
            <span>{escape_text(item["assignee"])}</span>
          </label>"""
        for item in people
    )

    person_cards = []
    for index, item in enumerate(people):
        selected_class = " is-selected" if index == 0 else ""
        remaining_metric_html = (
            f'<span><b>{format_hours(item["primary_remaining_hours"])}</b>残</span>'
            if has_remaining_work
            else ""
        )
        person_cards.append(
            f"""
        <button type="button" class="person-card workload-{escape_text(item["level_class"])}{selected_class}" data-assignee="{escape_text(item["assignee"])}">
          <span class="person-card-name">{escape_text(item["assignee"])}</span>
          <span class="person-card-level">{escape_text(item["level_label"])}</span>
          <span class="person-card-score">score {item["score"]:.1f}</span>
          <span class="person-card-grid">
            <span><b>{item["open_count"]}</b>関与</span>
            <span><b>{item["primary_count"]}</b>主</span>
            <span><b>{item["sub_count"]}</b>副</span>
            {remaining_metric_html}
          </span>
        </button>"""
        )

    columns = [
        ("open_count", "関与"),
        ("primary_count", "主担当"),
        ("sub_count", "副担当"),
        ("overdue_count", "期限超過"),
        ("high_priority_count", "高優先度"),
        ("stale_count", "更新停滞"),
        ("score", "スコア"),
    ]
    if has_remaining_work:
        columns.insert(-1, ("primary_remaining_hours", "残作業h"))
    table_rows = []
    for item in people:
        cells = []
        for key, label in columns:
            value = item[key]
            display_value = format_hours(value) if key == "primary_remaining_hours" else (
                f"{value:.1f}" if key == "score" else str(value)
            )
            balance_class = workload_balance_class(float(value), float(max_values.get(key, 0)))
            cells.append(
                f'<td class="{balance_class}" data-label="{escape_text(label)}">{escape_text(display_value)}</td>'
            )
        table_rows.append(
            f"""
          <tr data-assignee="{escape_text(item["assignee"])}">
            <th scope="row"><button type="button" class="table-assignee">{escape_text(item["assignee"])}</button></th>
            {''.join(cells)}
          </tr>"""
        )

    if not people:
        person_cards_html = '<p class="empty-message">表示対象の未完了Issueはありません。</p>'
        table_body_html = """
          <tr>
            <td colspan="9" class="empty-cell">表示対象の未完了Issueはありません。</td>
          </tr>"""
    else:
        person_cards_html = "\n".join(person_cards)
        table_body_html = "\n".join(table_rows)

    remaining_work_panel_html = (
        """
    <section class="chart-panel-grid">
      <section class="panel assignment-panel" aria-label="割り当てチケット数">
        <div class="panel-header compact-panel-header">
          <div>
            <h2>割り当てチケット数</h2>
            <p>主担当 / 副担当</p>
          </div>
        </div>
        <div class="assignment-only-body">
          <div class="assignment-bar-list" id="assignment-bar-list"></div>
        </div>
      </section>
      <section class="panel remaining-pie-panel" aria-label="残作業時間円グラフ">
        <div class="panel-header compact-panel-header">
          <div>
            <h2>残作業時間</h2>
            <p>担当者別割合</p>
          </div>
        </div>
        <div class="remaining-pie-body">
          <div class="remaining-pie-content">
            <div class="remaining-pie-chart" id="remaining-pie-chart" role="img" aria-label="残作業時間の割合">
              <div class="remaining-pie-center">
                <span id="remaining-pie-total">-</span>
                <small>合計</small>
              </div>
            </div>
            <ul class="remaining-pie-legend" id="remaining-pie-legend"></ul>
          </div>
        </div>
      </section>
    </section>"""
        if has_remaining_work
        else """
    <section class="panel assignment-panel" aria-label="割り当てチケット数">
      <div class="panel-header">
        <div>
          <h2>割り当てチケット数</h2>
          <p>担当者フィルタに連動して、主担当 / 副担当の件数を表示します</p>
        </div>
      </div>
      <div class="assignment-only-body">
        <div class="assignment-bar-list" id="assignment-bar-list"></div>
      </div>
    </section>"""
    )

    return f"""<!doctype html>
<html lang="ja" data-theme="system">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>作業負荷状況 - Redmine Kanban</title>
  <style>
    * {{
      box-sizing: border-box;
    }}

    :root {{
      color-scheme: light;
      --bg-color: #f6f7f9;
      --text-color: #1f2937;
      --muted-text: #64748b;
      --panel-bg: #ffffff;
      --panel-border: #d8dee8;
      --button-bg: #374151;
      --button-hover-bg: #111827;
      --button-text: #ffffff;
      --link-color: #0f766e;
      --shadow-color: rgba(15, 23, 42, 0.08);
      --high-bg: #fee2e2;
      --high-text: #7f1d1d;
      --high-border: #ef4444;
      --warning-bg: #fef3c7;
      --warning-text: #78350f;
      --warning-border: #f59e0b;
      --normal-bg: #dcfce7;
      --normal-text: #14532d;
      --low-bg: #e0f2fe;
      --medium-bg: #fde68a;
    }}

    @media (prefers-color-scheme: dark) {{
      :root {{
        color-scheme: dark;
        --bg-color: #0f172a;
        --text-color: #e5e7eb;
        --muted-text: #cbd5e1;
        --panel-bg: #111827;
        --panel-border: #374151;
        --button-bg: #0f766e;
        --button-hover-bg: #14b8a6;
        --button-text: #ffffff;
        --link-color: #5eead4;
        --shadow-color: rgba(0, 0, 0, 0.35);
        --high-bg: #7f1d1d;
        --high-text: #fee2e2;
        --high-border: #f87171;
        --warning-bg: #713f12;
        --warning-text: #fef3c7;
        --warning-border: #fbbf24;
        --normal-bg: #14532d;
        --normal-text: #dcfce7;
        --low-bg: #164e63;
        --medium-bg: #713f12;
      }}
    }}

    html[data-theme="light"] {{
      color-scheme: light;
      --bg-color: #f6f7f9;
      --text-color: #1f2937;
      --muted-text: #64748b;
      --panel-bg: #ffffff;
      --panel-border: #d8dee8;
      --button-bg: #374151;
      --button-hover-bg: #111827;
      --button-text: #ffffff;
      --link-color: #0f766e;
      --shadow-color: rgba(15, 23, 42, 0.08);
      --high-bg: #fee2e2;
      --high-text: #7f1d1d;
      --high-border: #ef4444;
      --warning-bg: #fef3c7;
      --warning-text: #78350f;
      --warning-border: #f59e0b;
      --normal-bg: #dcfce7;
      --normal-text: #14532d;
      --low-bg: #e0f2fe;
      --medium-bg: #fde68a;
    }}

    html[data-theme="dark"] {{
      color-scheme: dark;
      --bg-color: #0f172a;
      --text-color: #e5e7eb;
      --muted-text: #cbd5e1;
      --panel-bg: #111827;
      --panel-border: #374151;
      --button-bg: #0f766e;
      --button-hover-bg: #14b8a6;
      --button-text: #ffffff;
      --link-color: #5eead4;
      --shadow-color: rgba(0, 0, 0, 0.35);
      --high-bg: #7f1d1d;
      --high-text: #fee2e2;
      --high-border: #f87171;
      --warning-bg: #713f12;
      --warning-text: #fef3c7;
      --warning-border: #fbbf24;
      --normal-bg: #14532d;
      --normal-text: #dcfce7;
      --low-bg: #164e63;
      --medium-bg: #713f12;
    }}

    body {{
      margin: 0;
      color: var(--text-color);
      background: var(--bg-color);
      font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    }}

    .page-header {{
      position: sticky;
      top: 0;
      z-index: 10;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      padding: 18px 24px;
      background: color-mix(in srgb, var(--bg-color) 94%, transparent);
      border-bottom: 1px solid var(--panel-border);
      backdrop-filter: blur(10px);
    }}

    .page-header h1 {{
      margin: 0 0 4px;
      font-size: 24px;
      letter-spacing: 0;
    }}

    .page-header p {{
      margin: 0;
      color: var(--muted-text);
      font-size: 13px;
    }}

    .header-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}

    .button {{
      display: inline-flex;
      min-height: 36px;
      align-items: center;
      justify-content: center;
      padding: 8px 12px;
      color: var(--button-text);
      background: var(--button-bg);
      border-radius: 8px;
      text-decoration: none;
      font-size: 13px;
      font-weight: 700;
    }}

    .button:hover {{
      background: var(--button-hover-bg);
    }}

    main {{
      display: grid;
      gap: 16px;
      padding: 18px 24px 28px;
    }}

    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 10px;
    }}

    .remaining-pie-panel {{
      overflow: visible;
    }}

    .assignment-panel {{
      overflow: visible;
    }}

    .assignment-only-body {{
      padding: 16px;
    }}

    .chart-panel-grid {{
      display: grid;
      grid-template-columns: minmax(360px, 1.1fr) minmax(360px, 0.9fr);
      gap: 16px;
      align-items: start;
    }}

    .compact-panel-header {{
      padding-block: 12px;
    }}

    .assignment-bar-list {{
      display: grid;
      gap: 9px;
    }}

    .assignment-bar-row {{
      display: grid;
      grid-template-columns: minmax(96px, 150px) minmax(0, 1fr) auto;
      gap: 9px;
      align-items: center;
      min-height: 34px;
      font-size: 12px;
      font-weight: 700;
    }}

    .assignment-bar-name {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

    .assignment-bar-track {{
      display: flex;
      height: 18px;
      min-width: 0;
      overflow: hidden;
      background: color-mix(in srgb, var(--panel-border) 26%, transparent);
      border-radius: 999px;
    }}

    .assignment-bar-primary,
    .assignment-bar-sub {{
      min-width: 0;
    }}

    .assignment-bar-primary {{
      background: var(--link-color);
    }}

    .assignment-bar-sub {{
      background: #a78bfa;
    }}

    .assignment-bar-value {{
      color: var(--muted-text);
      white-space: nowrap;
    }}

    .assignment-bar-legend {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 12px;
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 700;
    }}

    .assignment-bar-legend span {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}

    .assignment-bar-legend i {{
      width: 12px;
      height: 12px;
      border-radius: 3px;
    }}

    .assignment-bar-legend .primary {{
      background: var(--link-color);
    }}

    .assignment-bar-legend .sub {{
      background: #a78bfa;
    }}

    .remaining-pie-body {{
      padding: 16px;
    }}

    .remaining-pie-content {{
      display: grid;
      grid-template-columns: minmax(170px, 220px) minmax(0, 1fr);
      gap: 16px;
      align-items: center;
    }}

    .remaining-pie-chart {{
      position: relative;
      width: min(220px, 100%);
      aspect-ratio: 1;
      margin: 0 auto;
      border: 1px solid var(--panel-border);
      border-radius: 50%;
      background: color-mix(in srgb, var(--panel-border) 32%, transparent);
      box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--panel-bg) 80%, transparent);
    }}

    .remaining-pie-chart.is-hidden {{
      display: none;
    }}

    .remaining-pie-chart::after {{
      position: absolute;
      inset: 25%;
      content: "";
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 50%;
      box-shadow: 0 1px 2px var(--shadow-color);
    }}

    .remaining-pie-center {{
      position: absolute;
      z-index: 1;
      inset: 31%;
      display: grid;
      place-content: center;
      text-align: center;
    }}

    .remaining-pie-center span {{
      font-size: 22px;
      font-weight: 900;
      line-height: 1;
    }}

    .remaining-pie-center small {{
      margin-top: 4px;
      color: var(--muted-text);
      font-size: 11px;
      font-weight: 800;
    }}

    .remaining-pie-legend {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}

    .remaining-pie-legend li {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      min-height: 34px;
      padding: 7px 9px;
      background: color-mix(in srgb, var(--panel-border) 20%, transparent);
      border-radius: 8px;
      font-size: 12px;
      font-weight: 700;
    }}

    .remaining-pie-swatch {{
      width: 12px;
      height: 12px;
      border-radius: 3px;
    }}

    .remaining-pie-name {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

    .remaining-pie-value {{
      color: var(--muted-text);
      white-space: nowrap;
    }}

    .people-filter {{
      padding: 14px 16px;
    }}

    .people-filter-header {{
      display: flex;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
    }}

    .people-filter-header h2 {{
      margin: 0;
      font-size: 16px;
    }}

    .people-filter-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}

    .filter-action-button {{
      min-height: 32px;
      padding: 6px 10px;
      color: var(--button-text);
      background: var(--button-bg);
      border: 1px solid var(--button-bg);
      border-radius: 8px;
      cursor: pointer;
      font: inherit;
      font-size: 12px;
      font-weight: 800;
    }}

    .filter-action-button:hover {{
      background: var(--button-hover-bg);
    }}

    .person-filter-options {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}

    .person-filter-option {{
      display: inline-flex;
      min-height: 34px;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border: 1px solid var(--panel-border);
      border-radius: 999px;
      background: color-mix(in srgb, var(--panel-bg) 86%, var(--low-bg));
      font-size: 13px;
      font-weight: 700;
    }}

    .person-filter-option input {{
      width: 16px;
      height: 16px;
      margin: 0;
    }}

    .stat-card,
    .panel {{
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      box-shadow: 0 1px 2px var(--shadow-color);
    }}

    .stat-card {{
      min-height: 76px;
      padding: 12px;
      border-radius: 8px;
    }}

    .stat-card span {{
      display: block;
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 700;
    }}

    .stat-card strong {{
      display: block;
      margin-top: 8px;
      font-size: 24px;
      line-height: 1;
    }}

    .dashboard-layout {{
      display: grid;
      grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }}

    .panel {{
      border-radius: 8px;
      overflow: hidden;
    }}

    .panel-header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 14px 16px;
      border-bottom: 1px solid var(--panel-border);
    }}

    .panel-header h2 {{
      margin: 0;
      font-size: 16px;
    }}

    .panel-header p {{
      margin: 0;
      color: var(--muted-text);
      font-size: 12px;
    }}

    .person-list {{
      display: grid;
      gap: 8px;
      padding: 12px;
      max-height: calc(100vh - 240px);
      overflow: auto;
    }}

    .person-card {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      width: 100%;
      min-height: 96px;
      padding: 12px;
      color: var(--text-color);
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-left-width: 5px;
      border-radius: 8px;
      cursor: pointer;
      text-align: left;
    }}

    .person-card.is-selected {{
      outline: 2px solid var(--link-color);
      outline-offset: 1px;
    }}

    .is-filter-hidden {{
      display: none;
    }}

    .person-card-name {{
      overflow-wrap: anywhere;
      font-weight: 800;
    }}

    .person-card-level {{
      align-self: start;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
    }}

    .person-card-score {{
      color: var(--muted-text);
      font-size: 12px;
    }}

    .person-card-grid {{
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 6px;
      font-size: 12px;
    }}

    .person-card-grid span {{
      padding: 6px;
      background: color-mix(in srgb, var(--panel-border) 24%, transparent);
      border-radius: 6px;
    }}

    .workload-high {{
      border-left-color: var(--high-border);
    }}

    .workload-high .person-card-level {{
      color: var(--high-text);
      background: var(--high-bg);
    }}

    .workload-warning {{
      border-left-color: var(--warning-border);
    }}

    .workload-warning .person-card-level {{
      color: var(--warning-text);
      background: var(--warning-bg);
    }}

    .workload-normal {{
      border-left-color: #22c55e;
    }}

    .workload-normal .person-card-level {{
      color: var(--normal-text);
      background: var(--normal-bg);
    }}

    .table-wrap {{
      overflow: auto;
    }}

    table {{
      width: 100%;
      min-width: 860px;
      border-collapse: collapse;
    }}

    th,
    td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--panel-border);
      text-align: right;
      font-size: 13px;
      white-space: nowrap;
    }}

    th {{
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 800;
    }}

    tbody th {{
      text-align: left;
    }}

    .table-assignee {{
      padding: 0;
      color: var(--link-color);
      background: none;
      border: 0;
      cursor: pointer;
      font: inherit;
      font-weight: 800;
    }}

    .balance-empty {{
      color: var(--muted-text);
    }}

    .balance-low {{
      background: color-mix(in srgb, var(--low-bg) 40%, transparent);
    }}

    .balance-medium {{
      background: color-mix(in srgb, var(--medium-bg) 55%, transparent);
    }}

    .balance-high {{
      background: color-mix(in srgb, var(--high-bg) 76%, transparent);
      color: var(--high-text);
      font-weight: 800;
    }}

    .detail-body {{
      display: grid;
      gap: 10px;
      padding: 12px;
    }}

    .issue-row {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) repeat(4, auto);
      gap: 10px;
      align-items: center;
      min-height: 48px;
      padding: 10px 12px;
      border: 1px solid var(--panel-border);
      border-radius: 8px;
    }}

    .issue-row.no-remaining-work {{
      grid-template-columns: auto minmax(0, 1fr) repeat(3, auto);
    }}

    .issue-row a {{
      color: var(--link-color);
      font-weight: 800;
      text-decoration: none;
    }}

    .issue-subject {{
      overflow-wrap: anywhere;
      font-weight: 700;
    }}

    .chip {{
      padding: 4px 7px;
      border-radius: 999px;
      background: color-mix(in srgb, var(--panel-border) 28%, transparent);
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}

    .chip-alert {{
      color: var(--high-text);
      background: var(--high-bg);
    }}

    .empty-message,
    .empty-cell {{
      padding: 18px;
      color: var(--muted-text);
      text-align: center;
    }}

    @media (max-width: 980px) {{
      .page-header,
      .dashboard-layout {{
        grid-template-columns: 1fr;
      }}

      .page-header {{
        display: grid;
      }}

      .summary-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}

      .chart-panel-grid {{
        grid-template-columns: 1fr;
      }}

      .remaining-pie-content {{
        grid-template-columns: 1fr;
      }}

      .person-list {{
        max-height: none;
      }}

      .issue-row {{
        grid-template-columns: auto minmax(0, 1fr);
      }}

      .issue-row .chip {{
        justify-self: start;
      }}
    }}
  </style>
</head>
<body>
  <header class="page-header">
    <div>
      <h1>作業負荷状況</h1>
      <p>PROJECT_ID: {escape_text(project_id)} / 主担当と副担当を含めた関与状況</p>
    </div>
    <div class="header-actions">
      <a class="button" href="/{OUTPUT_HTML}" target="_top">かんばんボード</a>
      <a class="button" href="/{COMBINED_HTML}" target="_top">同時表示</a>
      <a class="button" href="/{WORKLOAD_HTML}" target="_top">再読み込み</a>
    </div>
  </header>
  <main>
    <section class="summary-grid" aria-label="全体サマリー">
{stat_cards_html}
    </section>
{remaining_work_panel_html}
    <section class="panel people-filter" aria-label="担当者フィルタ">
      <div class="people-filter-header">
        <h2>担当者フィルタ</h2>
        <div class="people-filter-actions">
          <button type="button" class="filter-action-button" id="select-all-people">全選択</button>
          <button type="button" class="filter-action-button" id="clear-people">解除</button>
        </div>
      </div>
      <div class="person-filter-options" id="person-filter-options">
{person_filter_options}
      </div>
    </section>
    <section class="dashboard-layout">
      <aside class="panel">
        <div class="panel-header">
          <div>
            <h2>担当者別</h2>
            <p>スコア順</p>
          </div>
        </div>
        <div class="person-list" id="person-list">
{person_cards_html}
        </div>
      </aside>
      <div class="panel">
        <div class="panel-header">
          <div>
            <h2>負荷バランス表</h2>
            <p>濃いセルほど偏りが大きい指標です</p>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">担当者</th>
                {''.join(f'<th scope="col">{escape_text(label)}</th>' for _, label in columns)}
              </tr>
            </thead>
            <tbody>
{table_body_html}
            </tbody>
          </table>
        </div>
      </div>
    </section>
    <section class="panel" id="detail-panel">
      <div class="panel-header">
        <div>
          <h2 id="detail-title">担当者詳細</h2>
          <p id="detail-summary">担当者を選択するとIssue一覧を表示します</p>
        </div>
      </div>
      <div class="detail-body" id="detail-body"></div>
    </section>
  </main>
  <script type="application/json" id="workload-data">{people_json}</script>
  <script>
    const THEME_STORAGE_KEY = "redmine-kanban-theme";
    const people = JSON.parse(document.getElementById("workload-data").textContent || "[]");
    const projectId = {script_json(project_id)};
    const hasRemainingWork = {has_remaining_work_json};
    const WORKLOAD_PEOPLE_STORAGE_KEY = `redmine-kanban-workload-people:${{projectId}}`;
    const personList = document.getElementById("person-list");
    const filterCheckboxes = Array.from(document.querySelectorAll(".person-filter-checkbox"));
    const selectAllPeopleButton = document.getElementById("select-all-people");
    const clearPeopleButton = document.getElementById("clear-people");
    const detailTitle = document.getElementById("detail-title");
    const detailSummary = document.getElementById("detail-summary");
    const detailBody = document.getElementById("detail-body");
    const assignmentBarList = document.getElementById("assignment-bar-list");
    const remainingPieChart = document.getElementById("remaining-pie-chart");
    const remainingPieTotal = document.getElementById("remaining-pie-total");
    const remainingPieLegend = document.getElementById("remaining-pie-legend");
    const PIE_COLORS = [
      "#0f766e",
      "#2563eb",
      "#7c3aed",
      "#dc2626",
      "#d97706",
      "#16a34a",
      "#0891b2",
      "#be185d",
      "#4f46e5",
      "#65a30d",
    ];

    function getSavedTheme() {{
      const savedTheme = localStorage.getItem(THEME_STORAGE_KEY);
      return ["light", "dark", "system"].includes(savedTheme) ? savedTheme : "system";
    }}

    function applyTheme(theme) {{
      document.documentElement.dataset.theme = ["light", "dark", "system"].includes(theme) ? theme : "system";
    }}

    function initializeThemeSync() {{
      applyTheme(getSavedTheme());
      window.addEventListener("storage", (event) => {{
        if (event.key === THEME_STORAGE_KEY) {{
          applyTheme(getSavedTheme());
        }}
      }});
    }}

    function formatHours(value) {{
      if (!value || value <= 0) {{
        return "-";
      }}
      return Number.isInteger(value) ? `${{value}}h` : `${{value.toFixed(1)}}h`;
    }}

    function selectedAssignees() {{
      return new Set(filterCheckboxes.filter((checkbox) => checkbox.checked).map((checkbox) => checkbox.value));
    }}

    function loadSavedAssignees() {{
      try {{
        const parsed = JSON.parse(localStorage.getItem(WORKLOAD_PEOPLE_STORAGE_KEY) || "null");
        return Array.isArray(parsed) ? new Set(parsed.map((value) => String(value))) : null;
      }} catch {{
        return null;
      }}
    }}

    function saveSelectedAssignees() {{
      try {{
        localStorage.setItem(WORKLOAD_PEOPLE_STORAGE_KEY, JSON.stringify(Array.from(selectedAssignees())));
      }} catch {{
      }}
    }}

    function applySavedAssignees() {{
      const saved = loadSavedAssignees();
      if (saved === null) {{
        return;
      }}

      filterCheckboxes.forEach((checkbox) => {{
        checkbox.checked = saved.has(checkbox.value);
      }});
    }}

    function filteredPeople() {{
      const selected = selectedAssignees();
      return people.filter((person) => selected.has(person.assignee));
    }}

    function updateStat(key, value) {{
      const target = document.querySelector(`[data-stat-key="${{key}}"]`);
      if (target) {{
        target.textContent = value;
      }}
    }}

    function updateSummary(selectedPeople) {{
      updateStat("open_count", selectedPeople.reduce((total, person) => total + person.open_count, 0));
      updateStat("person_count", selectedPeople.length);
      updateStat("high_load_count", selectedPeople.filter((person) => person.level_class === "high").length);
      updateStat("overdue_count", selectedPeople.reduce((total, person) => total + person.overdue_count, 0));
      updateStat("high_priority_count", selectedPeople.reduce((total, person) => total + person.high_priority_count, 0));
      if (hasRemainingWork) {{
        updateStat("primary_remaining_hours", formatHours(selectedPeople.reduce((total, person) => total + person.primary_remaining_hours, 0)));
      }}
    }}

    function updateAssignmentBars(selectedPeople) {{
      const rows = selectedPeople
        .map((person) => ({{
          assignee: person.assignee,
          primary: Number(person.primary_count || 0),
          sub: Number(person.sub_count || 0),
          total: Number(person.primary_count || 0) + Number(person.sub_count || 0),
        }}))
        .filter((item) => item.total > 0)
        .sort((a, b) => b.total - a.total || a.assignee.localeCompare(b.assignee, "ja"));
      const maxTotal = Math.max(...rows.map((item) => item.total), 1);

      assignmentBarList.replaceChildren();
      if (!rows.length) {{
        const empty = document.createElement("p");
        empty.className = "empty-message";
        empty.textContent = "割り当てチケットはありません";
        assignmentBarList.append(empty);
        return;
      }}

      rows.forEach((item) => {{
        const row = document.createElement("div");
        const name = document.createElement("span");
        const track = document.createElement("span");
        const primary = document.createElement("span");
        const sub = document.createElement("span");
        const value = document.createElement("span");
        const totalWidth = item.total / maxTotal * 100;
        const primaryWidth = item.total > 0 ? item.primary / item.total * totalWidth : 0;
        const subWidth = item.total > 0 ? item.sub / item.total * totalWidth : 0;

        row.className = "assignment-bar-row";
        name.className = "assignment-bar-name";
        name.textContent = item.assignee;
        name.title = item.assignee;
        track.className = "assignment-bar-track";
        primary.className = "assignment-bar-primary";
        primary.style.width = `${{primaryWidth}}%`;
        sub.className = "assignment-bar-sub";
        sub.style.width = `${{subWidth}}%`;
        value.className = "assignment-bar-value";
        value.textContent = `${{item.total}}件`;
        value.title = `主担当 ${{item.primary}}件 / 副担当 ${{item.sub}}件`;

        track.append(primary, sub);
        row.append(name, track, value);
        assignmentBarList.append(row);
      }});

      const legend = document.createElement("div");
      const primaryLegend = document.createElement("span");
      const primarySwatch = document.createElement("i");
      const subLegend = document.createElement("span");
      const subSwatch = document.createElement("i");
      legend.className = "assignment-bar-legend";
      primarySwatch.className = "primary";
      subSwatch.className = "sub";
      primaryLegend.append(primarySwatch, "主担当");
      subLegend.append(subSwatch, "副担当");
      legend.append(primaryLegend, subLegend);
      assignmentBarList.append(legend);
    }}

    function updateRemainingPie(selectedPeople) {{
      if (!hasRemainingWork || !remainingPieChart || !remainingPieTotal || !remainingPieLegend) {{
        return;
      }}

      const slices = selectedPeople
        .map((person) => ({{
          assignee: person.assignee,
          hours: Number(person.primary_remaining_hours || 0),
        }}))
        .filter((item) => item.hours > 0)
        .sort((a, b) => b.hours - a.hours || a.assignee.localeCompare(b.assignee, "ja"));
      const totalHours = slices.reduce((total, item) => total + item.hours, 0);

      remainingPieTotal.textContent = formatHours(totalHours);
      remainingPieLegend.replaceChildren();

      if (totalHours <= 0) {{
        remainingPieChart.classList.add("is-hidden");
        remainingPieChart.setAttribute("aria-label", "残作業時間はありません");
        const empty = document.createElement("li");
        empty.className = "empty-message";
        empty.textContent = "残作業時間はありません";
        remainingPieLegend.append(empty);
        return;
      }}

      remainingPieChart.classList.remove("is-hidden");
      let current = 0;
      const segments = slices.map((item, index) => {{
        const percent = item.hours / totalHours * 100;
        const start = current;
        current += percent;
        const color = PIE_COLORS[index % PIE_COLORS.length];
        return `${{color}} ${{start.toFixed(3)}}% ${{current.toFixed(3)}}%`;
      }});
      remainingPieChart.style.background = `conic-gradient(${{segments.join(", ")}})`;
      remainingPieChart.setAttribute("aria-label", `残作業時間 合計 ${{formatHours(totalHours)}}`);

      slices.forEach((item, index) => {{
        const percent = item.hours / totalHours * 100;
        const row = document.createElement("li");
        const swatch = document.createElement("span");
        const name = document.createElement("span");
        const value = document.createElement("span");

        swatch.className = "remaining-pie-swatch";
        swatch.style.background = PIE_COLORS[index % PIE_COLORS.length];
        name.className = "remaining-pie-name";
        name.textContent = item.assignee;
        value.className = "remaining-pie-value";
        value.textContent = `${{formatHours(item.hours)}} / ${{percent.toFixed(1)}}%`;

        row.append(swatch, name, value);
        remainingPieLegend.append(row);
      }});
    }}

    function showEmptyDetail() {{
      detailTitle.textContent = "担当者詳細";
      detailSummary.textContent = "担当者を選択するとIssue一覧を表示します";
      detailBody.replaceChildren();
      const empty = document.createElement("p");
      empty.className = "empty-message";
      empty.textContent = "担当者が選択されていません。";
      detailBody.append(empty);
    }}

    function selectAssignee(assignee) {{
      const person = people.find((item) => item.assignee === assignee);
      if (!person) {{
        return;
      }}

      document.querySelectorAll("[data-assignee]").forEach((element) => {{
        element.classList.toggle("is-selected", element.dataset.assignee === assignee);
      }});

      detailTitle.textContent = `${{person.assignee}} のIssue`;
      detailSummary.textContent = hasRemainingWork
        ? `関与 ${{person.open_count}}件 / 主担当 ${{person.primary_count}}件 / 副担当 ${{person.sub_count}}件 / 案分残作業 ${{formatHours(person.primary_remaining_hours)}}`
        : `関与 ${{person.open_count}}件 / 主担当 ${{person.primary_count}}件 / 副担当 ${{person.sub_count}}件`;
      detailBody.replaceChildren();

      if (!person.issues.length) {{
        const empty = document.createElement("p");
        empty.className = "empty-message";
        empty.textContent = "表示対象のIssueはありません。";
        detailBody.append(empty);
        return;
      }}

      person.issues.forEach((issue) => {{
        const row = document.createElement("article");
        row.className = "issue-row";
        row.classList.toggle("no-remaining-work", !hasRemainingWork);

        const id = document.createElement("a");
        id.href = issue.url;
        id.target = "_blank";
        id.rel = "noopener noreferrer";
        id.textContent = `#${{issue.id}}`;

        const subject = document.createElement("span");
        subject.className = "issue-subject";
        subject.textContent = issue.subject;

        const role = document.createElement("span");
        role.className = "chip";
        role.textContent = issue.role;

        const status = document.createElement("span");
        status.className = "chip";
        status.textContent = issue.status;

        const due = document.createElement("span");
        due.className = issue.overdue ? "chip chip-alert" : "chip";
        due.textContent = issue.due_date === "-" ? "期日 -" : `期日 ${{issue.due_date}}`;

        row.append(id, subject, role, status, due);
        if (hasRemainingWork) {{
          const remaining = document.createElement("span");
          remaining.className = "chip";
          remaining.textContent = `残 ${{formatHours(issue.remaining_hours)}}`;
          row.append(remaining);
        }}
        detailBody.append(row);
      }});
    }}

    function applyPeopleFilter() {{
      saveSelectedAssignees();
      const selected = selectedAssignees();
      const selectedPeople = filteredPeople();
      updateSummary(selectedPeople);
      updateAssignmentBars(selectedPeople);
      updateRemainingPie(selectedPeople);

      document.querySelectorAll(".person-card").forEach((card) => {{
        card.classList.toggle("is-filter-hidden", !selected.has(card.dataset.assignee || ""));
      }});

      document.querySelectorAll("tbody tr[data-assignee]").forEach((row) => {{
        row.classList.toggle("is-filter-hidden", !selected.has(row.dataset.assignee || ""));
      }});

      const selectedCard = document.querySelector(".person-card.is-selected:not(.is-filter-hidden)");
      if (selectedCard) {{
        selectAssignee(selectedCard.dataset.assignee || "");
      }} else if (selectedPeople.length) {{
        selectAssignee(selectedPeople[0].assignee);
      }} else {{
        document.querySelectorAll("[data-assignee]").forEach((element) => element.classList.remove("is-selected"));
        showEmptyDetail();
      }}
    }}

    personList.addEventListener("click", (event) => {{
      const button = event.target.closest(".person-card");
      if (button) {{
        selectAssignee(button.dataset.assignee || "");
      }}
    }});

    document.querySelector("tbody").addEventListener("click", (event) => {{
      const button = event.target.closest(".table-assignee");
      if (button) {{
        selectAssignee(button.closest("tr").dataset.assignee || "");
      }}
    }});

    filterCheckboxes.forEach((checkbox) => checkbox.addEventListener("change", applyPeopleFilter));
    selectAllPeopleButton.addEventListener("click", () => {{
      filterCheckboxes.forEach((checkbox) => {{
        checkbox.checked = true;
      }});
      applyPeopleFilter();
    }});
    clearPeopleButton.addEventListener("click", () => {{
      filterCheckboxes.forEach((checkbox) => {{
        checkbox.checked = false;
      }});
      applyPeopleFilter();
    }});

    initializeThemeSync();
    applySavedAssignees();
    if (people.length) {{
      applyPeopleFilter();
    }} else {{
      updateAssignmentBars([]);
      updateRemainingPie([]);
      showEmptyDetail();
    }}
  </script>
</body>
</html>
"""


def parse_worktime_date(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError:
        return fallback


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))


def time_entry_issue_id(entry: dict[str, Any]) -> str:
    issue = entry.get("issue")
    if isinstance(issue, dict) and issue.get("id") not in (None, ""):
        return str(issue.get("id"))
    return "-"


def time_entry_activity(entry: dict[str, Any]) -> str:
    activity = entry.get("activity")
    if isinstance(activity, dict):
        return str(activity.get("name") or "-")
    return "-"


def time_entry_comment(entry: dict[str, Any]) -> str:
    return str(entry.get("comments") or "").strip()


def issue_fixed_version_map(issues: list[dict[str, Any]]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for issue in issues:
        issue_id = issue.get("id")
        if issue_id in (None, ""):
            continue
        versions[str(issue_id)] = fixed_version_name(issue)
    return versions


def issue_subject_map(issues: list[dict[str, Any]]) -> dict[str, str]:
    subjects: dict[str, str] = {}
    for issue in issues:
        issue_id = issue.get("id")
        if issue_id in (None, ""):
            continue
        subjects[str(issue_id)] = str(issue.get("subject") or "-")
    return subjects


def render_worktime_html(
    entries: list[dict[str, Any]],
    redmine_url: str,
    project_id: str,
    date_from: date,
    date_to: date,
    issue_versions: dict[str, str] | None = None,
    issue_subjects: dict[str, str] | None = None,
) -> str:
    issue_versions = issue_versions or {}
    issue_subjects = issue_subjects or {}
    users = sorted({time_entry_user_name(entry) for entry in entries if time_entry_user_name(entry) != "-"})
    user_options = ['<option value="__all__">全員</option>']
    user_options.extend(
        f'<option value="{escape_text(user)}">{escape_text(user)}</option>' for user in users
    )
    unset_version = fixed_version_name({})
    worktime_entries = []
    for entry in entries:
        issue_id = time_entry_issue_id(entry)
        worktime_entries.append(
            {
                "spent_on": str(entry.get("spent_on") or "-"),
                "user": time_entry_user_name(entry),
                "issue_id": issue_id,
                "issue_url": f"{redmine_url.rstrip('/')}/issues/{issue_id}"
                if issue_id != "-"
                else "",
                "issue_subject": issue_subjects.get(issue_id, "-"),
                "version": issue_versions.get(issue_id, unset_version),
                "activity": time_entry_activity(entry),
                "hours": time_entry_hours_value(entry),
                "comment": time_entry_comment(entry),
            }
        )
    worktime_version_names = {item["version"] for item in worktime_entries if item["version"]}
    worktime_versions = sorted(name for name in worktime_version_names if name != unset_version)
    if unset_version in worktime_version_names:
        worktime_versions.append(unset_version)
    version_items = [
        """
        <label class="checkbox-option">
          <input type="checkbox" id="worktime-version-all" checked>
          <span>全て</span>
        </label>"""
    ]
    for version in worktime_versions:
        version_items.append(
            f"""
        <label class="checkbox-option">
          <input type="checkbox" class="worktime-version-checkbox" value="{escape_text(version)}">
          <span>{escape_text(version)}</span>
        </label>"""
        )
    version_filter_html = f"""
        <fieldset class="worktime-version-filter">
          <legend>対象バージョン</legend>
          <div class="worktime-version-options">
{''.join(version_items)}
          </div>
        </fieldset>""" if worktime_versions else ""
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)
    month_start = today.replace(day=1)
    next_month_start = (
        date(today.year + 1, 1, 1)
        if today.month == 12
        else date(today.year, today.month + 1, 1)
    )
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    presets = [
        ("当日", today, today),
        ("昨日", today - timedelta(days=1), today - timedelta(days=1)),
        ("今週", week_start, week_start + timedelta(days=6)),
        ("先週", last_week_start, last_week_start + timedelta(days=6)),
        ("今月", month_start, next_month_start - timedelta(days=1)),
        ("先月", last_month_start, last_month_end),
    ]
    preset_links_html = "\n".join(
        f"""
          <a class="preset-link" href="/{WORKTIME_HTML}?{urlencode({'from': preset_from.isoformat(), 'to': preset_to.isoformat()})}">{escape_text(label)}</a>"""
        for label, preset_from, preset_to in presets
    )
    entries_json = script_json(worktime_entries)

    return f"""<!doctype html>
<html lang="ja" data-theme="system">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>作業時間 - Redmine Kanban</title>
  <style>
    * {{
      box-sizing: border-box;
    }}

    :root {{
      color-scheme: light;
      --bg-color: #f6f7f9;
      --text-color: #1f2937;
      --muted-text: #64748b;
      --panel-bg: #ffffff;
      --panel-border: #d8dee8;
      --control-bg: #ffffff;
      --control-border: #9ca3af;
      --control-text: #111827;
      --button-bg: #0f766e;
      --button-hover-bg: #0f5f59;
      --button-text: #ffffff;
      --link-color: #0f766e;
      --row-bg: #f8fafc;
    }}

    @media (prefers-color-scheme: dark) {{
      :root {{
        color-scheme: dark;
        --bg-color: #0f172a;
        --text-color: #e5e7eb;
        --muted-text: #cbd5e1;
        --panel-bg: #111827;
        --panel-border: #374151;
        --control-bg: #0f172a;
        --control-border: #475569;
        --control-text: #f9fafb;
        --button-bg: #0f766e;
        --button-hover-bg: #14b8a6;
        --button-text: #ffffff;
        --link-color: #5eead4;
        --row-bg: #172033;
      }}
    }}

    html[data-theme="light"] {{
      color-scheme: light;
      --bg-color: #f6f7f9;
      --text-color: #1f2937;
      --muted-text: #64748b;
      --panel-bg: #ffffff;
      --panel-border: #d8dee8;
      --control-bg: #ffffff;
      --control-border: #9ca3af;
      --control-text: #111827;
      --button-bg: #0f766e;
      --button-hover-bg: #0f5f59;
      --button-text: #ffffff;
      --link-color: #0f766e;
      --row-bg: #f8fafc;
    }}

    html[data-theme="dark"] {{
      color-scheme: dark;
      --bg-color: #0f172a;
      --text-color: #e5e7eb;
      --muted-text: #cbd5e1;
      --panel-bg: #111827;
      --panel-border: #374151;
      --control-bg: #0f172a;
      --control-border: #475569;
      --control-text: #f9fafb;
      --button-bg: #0f766e;
      --button-hover-bg: #14b8a6;
      --button-text: #ffffff;
      --link-color: #5eead4;
      --row-bg: #172033;
    }}

    body {{
      margin: 0;
      color: var(--text-color);
      background: var(--bg-color);
      font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    }}

    .page-header {{
      position: sticky;
      top: 0;
      z-index: 2;
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: center;
      padding: 16px 20px;
      background: color-mix(in srgb, var(--bg-color) 94%, transparent);
      border-bottom: 1px solid var(--panel-border);
      backdrop-filter: blur(10px);
    }}

    .page-header h1 {{
      margin: 0 0 4px;
      font-size: 22px;
    }}

    .page-header p {{
      margin: 0;
      color: var(--muted-text);
      font-size: 13px;
    }}

    .button {{
      display: inline-flex;
      min-height: 34px;
      align-items: center;
      justify-content: center;
      padding: 7px 11px;
      color: var(--button-text);
      background: var(--button-bg);
      border: 1px solid var(--button-bg);
      border-radius: 8px;
      text-decoration: none;
      font: inherit;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
    }}

    .button:hover {{
      background: var(--button-hover-bg);
    }}

    main {{
      display: grid;
      gap: 14px;
      padding: 16px 20px 24px;
    }}

    .panel {{
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 8px;
      overflow: hidden;
    }}

    .filters {{
      display: grid;
      grid-template-columns: repeat(2, minmax(150px, 180px)) minmax(180px, 240px) minmax(260px, 1fr) auto;
      gap: 10px;
      align-items: end;
      padding: 14px;
    }}

    .worktime-version-filter {{
      min-width: 0;
      margin: 0;
      padding: 8px;
      border: 1px solid var(--panel-border);
      border-radius: 8px;
    }}

    .worktime-version-filter legend {{
      padding: 0 4px;
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 800;
    }}

    .worktime-version-options {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      max-height: 82px;
      overflow: auto;
    }}

    .worktime-version-options .checkbox-option {{
      display: inline-flex;
      min-height: 28px;
      align-items: center;
      gap: 5px;
      padding: 4px 8px;
      color: var(--control-text);
      background: var(--row-bg);
      border: 1px solid var(--panel-border);
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
    }}

    .preset-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 0 14px 14px;
    }}

    .preset-link {{
      display: inline-flex;
      min-height: 30px;
      align-items: center;
      padding: 5px 10px;
      color: var(--control-text);
      background: var(--row-bg);
      border: 1px solid var(--panel-border);
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
      text-decoration: none;
    }}

    .preset-link:hover {{
      border-color: var(--link-color);
    }}

    label {{
      display: grid;
      gap: 4px;
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 800;
    }}

    input,
    select {{
      min-height: 34px;
      padding: 6px 9px;
      color: var(--control-text);
      background: var(--control-bg);
      border: 1px solid var(--control-border);
      border-radius: 8px;
      font: inherit;
      font-weight: 700;
    }}

    .summary {{
      display: grid;
      grid-template-columns: repeat(3, minmax(120px, 1fr));
      gap: 10px;
      padding: 0 14px 14px;
    }}

    .summary-card {{
      padding: 10px;
      background: var(--row-bg);
      border: 1px solid var(--panel-border);
      border-radius: 8px;
    }}

    .summary-card span {{
      display: block;
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 800;
    }}

    .summary-card strong {{
      display: block;
      margin-top: 6px;
      font-size: 22px;
    }}

    .chart-panel {{
      display: grid;
      gap: 12px;
      padding: 14px;
    }}

    .chart-header {{
      display: flex;
      gap: 12px;
      align-items: end;
      justify-content: space-between;
      flex-wrap: wrap;
    }}

    .chart-header h2 {{
      margin: 0 0 4px;
      font-size: 16px;
    }}

    .chart-header p {{
      margin: 0;
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 700;
    }}

    .chart-type-control {{
      min-width: 210px;
    }}

    .chart-breakdown-control {{
      min-width: 170px;
      margin: 0;
      padding: 7px 9px;
      border: 1px solid var(--panel-border);
      border-radius: 8px;
    }}

    .chart-breakdown-control legend {{
      padding: 0 4px;
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 800;
    }}

    .chart-breakdown-options {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }}

    .chart-breakdown-options label {{
      display: inline-flex;
      gap: 5px;
      align-items: center;
      color: var(--control-text);
      white-space: nowrap;
    }}

    .chart-actions {{
      display: flex;
      gap: 8px;
      align-items: end;
      flex-wrap: wrap;
    }}

    .chart-actions button {{
      white-space: nowrap;
    }}

    .worktime-chart {{
      min-height: 220px;
    }}

    .worktime-bar-chart {{
      display: grid;
      gap: 10px;
    }}

    .worktime-bar-row {{
      display: grid;
      grid-template-columns: minmax(120px, 220px) minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      font-size: 13px;
      font-weight: 800;
    }}

    .worktime-bar-row.is-total {{
      padding-bottom: 8px;
      border-bottom: 1px solid var(--panel-border);
    }}

    .worktime-bar-name {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

    .worktime-bar-track {{
      display: flex;
      height: 18px;
      overflow: hidden;
      background: color-mix(in srgb, var(--panel-border) 30%, transparent);
      border-radius: 999px;
    }}

    .worktime-bar-fill {{
      display: block;
      height: 100%;
      background: var(--link-color);
      border-radius: inherit;
    }}

    .worktime-bar-segment {{
      display: block;
      flex: 0 0 auto;
      height: 100%;
      min-width: 2px;
    }}

    .worktime-bar-segment:first-child {{
      border-radius: 999px 0 0 999px;
    }}

    .worktime-bar-segment:last-child {{
      border-radius: 0 999px 999px 0;
    }}

    .worktime-bar-breakdown {{
      grid-column: 2 / 4;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: -4px;
      color: var(--muted-text);
      font-size: 11px;
      font-weight: 700;
    }}

    .worktime-bar-breakdown span {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 6px;
      background: var(--row-bg);
      border: 1px solid var(--panel-border);
      border-radius: 999px;
      white-space: nowrap;
    }}

    .worktime-issue-swatch {{
      width: 8px;
      height: 8px;
      border-radius: 999px;
      flex: 0 0 auto;
    }}

    .worktime-chart-value {{
      color: var(--muted-text);
      white-space: nowrap;
    }}

    .worktime-pie-layout {{
      display: grid;
      grid-template-columns: minmax(180px, 240px) minmax(0, 1fr);
      gap: 18px;
      align-items: center;
    }}

    .worktime-pie {{
      position: relative;
      width: min(220px, 100%);
      aspect-ratio: 1;
      margin: 0 auto;
      border: 1px solid var(--panel-border);
      border-radius: 50%;
      background: color-mix(in srgb, var(--panel-border) 32%, transparent);
      cursor: crosshair;
      touch-action: none;
    }}

    .worktime-pie::after {{
      position: absolute;
      inset: 27%;
      content: "";
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 50%;
    }}

    .worktime-pie-center {{
      position: absolute;
      z-index: 1;
      inset: 33%;
      display: grid;
      place-content: center;
      text-align: center;
      font-weight: 900;
    }}

    .worktime-pie-tooltip {{
      position: absolute;
      z-index: 3;
      min-width: 150px;
      max-width: 220px;
      padding: 7px 9px;
      color: var(--control-text);
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 8px;
      box-shadow: 0 10px 24px rgb(15 23 42 / 22%);
      font-size: 12px;
      font-weight: 800;
      pointer-events: none;
      transform: translate(-50%, calc(-100% - 10px));
      white-space: nowrap;
    }}

    .worktime-pie-tooltip strong,
    .worktime-pie-tooltip span {{
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .worktime-pie-tooltip span {{
      margin-top: 2px;
      color: var(--muted-text);
    }}

    .worktime-chart-legend {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}

    .worktime-chart-legend li {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      min-height: 32px;
      padding: 6px 8px;
      background: var(--row-bg);
      border-radius: 8px;
      font-size: 12px;
      font-weight: 800;
    }}

    .worktime-swatch {{
      width: 12px;
      height: 12px;
      border-radius: 3px;
    }}

    .table-wrap {{
      overflow: auto;
    }}

    table {{
      width: 100%;
      min-width: 860px;
      border-collapse: collapse;
    }}

    th,
    td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--panel-border);
      text-align: left;
      font-size: 13px;
      vertical-align: top;
    }}

    th {{
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 900;
      white-space: nowrap;
    }}

    td.hours {{
      text-align: right;
      font-weight: 900;
      white-space: nowrap;
    }}

    a {{
      color: var(--link-color);
      font-weight: 800;
      text-decoration: none;
    }}

    .worktime-issue-cell {{
      display: flex;
      gap: 8px;
      align-items: baseline;
      min-width: 240px;
    }}

    .worktime-issue-cell a {{
      flex: 0 0 auto;
    }}

    .worktime-issue-subject {{
      color: var(--text-color);
      font-weight: 700;
      overflow-wrap: anywhere;
    }}

    .comment {{
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }}

    .empty-message {{
      padding: 18px;
      color: var(--muted-text);
      text-align: center;
      font-weight: 700;
    }}

    @media (max-width: 760px) {{
      .page-header {{
        display: grid;
      }}

      .filters,
      .summary {{
        grid-template-columns: 1fr;
      }}

      .worktime-bar-row,
      .worktime-pie-layout {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <header class="page-header">
    <div>
      <h1>作業時間</h1>
      <p>PROJECT_ID: {escape_text(project_id)}</p>
    </div>
    <a class="button" href="/{OUTPUT_HTML}" target="_blank" rel="noopener noreferrer">かんばん</a>
  </header>
  <main>
    <section class="panel">
      <form class="filters" action="/{WORKTIME_HTML}" method="get">
        <label>
          <span>開始日</span>
          <input type="date" name="from" value="{escape_text(date_from.isoformat())}">
        </label>
        <label>
          <span>終了日</span>
          <input type="date" name="to" value="{escape_text(date_to.isoformat())}">
        </label>
        <label>
          <span>担当者</span>
          <select id="user-filter">
            {''.join(user_options)}
          </select>
        </label>
{version_filter_html}
        <button class="button" type="submit">表示</button>
      </form>
      <div class="preset-links" aria-label="期間プリセット">
{preset_links_html}
      </div>
      <div class="summary" aria-label="作業時間サマリー">
        <article class="summary-card">
          <span>表示件数</span>
          <strong id="visible-entry-count">0</strong>
        </article>
        <article class="summary-card">
          <span>合計時間</span>
          <strong id="visible-entry-hours">-</strong>
        </article>
        <article class="summary-card">
          <span>対象期間</span>
          <strong>{escape_text(date_from.isoformat())} - {escape_text(date_to.isoformat())}</strong>
        </article>
      </div>
    </section>
    <section class="panel chart-panel" aria-label="作業時間割合グラフ">
      <div class="chart-header">
        <div>
          <h2>作業時間の割合</h2>
          <p>おすすめは横棒グラフです。担当者ごとの比較が一番読みやすく、人数が増えても崩れにくいです。</p>
        </div>
        <div class="chart-actions">
          <label class="chart-type-control">
            <span>グラフ種類</span>
            <select id="chart-type">
              <option value="bar">横棒グラフ（おすすめ）</option>
              <option value="pie">円グラフ / パイチャート</option>
            </select>
          </label>
          <fieldset class="chart-breakdown-control">
            <legend>内訳</legend>
            <div class="chart-breakdown-options">
              <label>
                <input type="radio" name="chart-breakdown" value="issue" checked>
                <span>Issue単位</span>
              </label>
              <label>
                <input type="radio" name="chart-breakdown" value="activity">
                <span>活動単位</span>
              </label>
            </div>
          </fieldset>
          <button type="button" id="random-order-button">ランダム順</button>
        </div>
      </div>
      <div class="worktime-chart" id="worktime-chart"></div>
    </section>
    <section class="panel">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">日付</th>
              <th scope="col">担当者</th>
              <th scope="col">Issue</th>
              <th scope="col">対象バージョン</th>
              <th scope="col">活動</th>
              <th scope="col">時間</th>
              <th scope="col">コメント</th>
            </tr>
          </thead>
          <tbody id="worktime-body"></tbody>
        </table>
      </div>
      <p class="empty-message" id="worktime-empty" hidden>対象の作業時間はありません。</p>
    </section>
  </main>
  <script type="application/json" id="worktime-data">{entries_json}</script>
  <script>
    const THEME_STORAGE_KEY = "redmine-kanban-theme";
    const entries = JSON.parse(document.getElementById("worktime-data").textContent || "[]");
    const userFilter = document.getElementById("user-filter");
    const worktimeVersionAll = document.getElementById("worktime-version-all");
    const worktimeVersionCheckboxes = Array.from(document.querySelectorAll(".worktime-version-checkbox"));
    const body = document.getElementById("worktime-body");
    const empty = document.getElementById("worktime-empty");
    const visibleEntryCount = document.getElementById("visible-entry-count");
    const visibleEntryHours = document.getElementById("visible-entry-hours");
    const chartType = document.getElementById("chart-type");
    const chartBreakdownRadios = Array.from(document.querySelectorAll('input[name="chart-breakdown"]'));
    const randomOrderButton = document.getElementById("random-order-button");
    const worktimeChart = document.getElementById("worktime-chart");
    const WORKTIME_CHART_STORAGE_KEY = "redmine-kanban-worktime-chart";
    const WORKTIME_BREAKDOWN_STORAGE_KEY = "redmine-kanban-worktime-breakdown";
    const WORKTIME_RANDOM_ORDER_STORAGE_KEY = "redmine-kanban-worktime-random-order";
    const WORKTIME_PROJECT_ID = {script_json(project_id)};
    const WORKTIME_DATE_FROM = {script_json(date_from.isoformat())};
    const WORKTIME_DATE_TO = {script_json(date_to.isoformat())};
    const CHART_COLORS = [
      "#0f766e",
      "#2563eb",
      "#7c3aed",
      "#dc2626",
      "#d97706",
      "#16a34a",
      "#0891b2",
      "#be185d",
      "#4f46e5",
      "#65a30d",
    ];

    function getSavedTheme() {{
      const savedTheme = localStorage.getItem(THEME_STORAGE_KEY);
      return ["light", "dark", "system"].includes(savedTheme) ? savedTheme : "system";
    }}

    function applyTheme(theme) {{
      document.documentElement.dataset.theme = ["light", "dark", "system"].includes(theme) ? theme : "system";
    }}

    function formatHours(value) {{
      if (!value || value <= 0) {{
        return "-";
      }}
      return Number.isInteger(value) ? `${{value}}h` : `${{value.toFixed(2)}}h`;
    }}

    function appendCell(row, text, className = "") {{
      const cell = document.createElement("td");
      cell.textContent = text;
      if (className) {{
        cell.className = className;
      }}
      row.append(cell);
      return cell;
    }}

    function getSelectedWorktimeVersions() {{
      if (!worktimeVersionAll || worktimeVersionAll.checked) {{
        return null;
      }}

      const selected = worktimeVersionCheckboxes
        .filter((checkbox) => checkbox.checked)
        .map((checkbox) => checkbox.value);
      return selected.length ? new Set(selected) : null;
    }}

    function handleWorktimeVersionAllChange() {{
      if (!worktimeVersionAll) {{
        return;
      }}

      if (worktimeVersionAll.checked) {{
        worktimeVersionCheckboxes.forEach((checkbox) => {{
          checkbox.checked = false;
        }});
      }} else if (!worktimeVersionCheckboxes.some((checkbox) => checkbox.checked)) {{
        worktimeVersionAll.checked = true;
      }}
      renderRows();
    }}

    function handleWorktimeVersionCheckboxChange() {{
      if (!worktimeVersionAll) {{
        return;
      }}

      if (worktimeVersionCheckboxes.some((checkbox) => checkbox.checked)) {{
        worktimeVersionAll.checked = false;
      }} else {{
        worktimeVersionAll.checked = true;
      }}
      renderRows();
    }}

    function currentBreakdownType() {{
      const selected = chartBreakdownRadios.find((radio) => radio.checked);
      return selected && selected.value === "activity" ? "activity" : "issue";
    }}

    function breakdownItem(entry, breakdownType) {{
      if (breakdownType === "activity") {{
        const activity = entry.activity || "-";
        return {{ key: activity, label: activity }};
      }}

      const issueId = entry.issue_id || "-";
      return {{ key: issueId, label: `#${{issueId}}` }};
    }}

    function groupedHours(filteredEntries, breakdownType = currentBreakdownType()) {{
      const groups = new Map();
      filteredEntries.forEach((entry) => {{
        const user = entry.user || "-";
        const part = breakdownItem(entry, breakdownType);
        const hours = Number(entry.hours || 0);
        const group = groups.get(user) || {{
          user,
          hours: 0,
          parts: new Map(),
        }};
        group.hours += hours;
        const existing = group.parts.get(part.key) || {{ key: part.key, label: part.label, hours: 0 }};
        existing.hours += hours;
        group.parts.set(part.key, existing);
        groups.set(user, group);
      }});
      return Array.from(groups.values())
        .map((group) => ({{
          user: group.user,
          hours: group.hours,
          parts: Array.from(group.parts.values())
            .filter((item) => item.hours > 0)
            .sort((a, b) => b.hours - a.hours || a.label.localeCompare(b.label, "ja")),
        }}))
        .filter((item) => item.hours > 0)
        .sort((a, b) => b.hours - a.hours || a.user.localeCompare(b.user, "ja"));
    }}

    function groupedBreakdownHours(filteredEntries, breakdownType) {{
      const groups = new Map();
      filteredEntries.forEach((entry) => {{
        const part = breakdownItem(entry, breakdownType);
        groups.set(part.label, (groups.get(part.label) || 0) + Number(entry.hours || 0));
      }});
      return Array.from(groups, ([label, hours]) => ({{ label, hours }}))
        .filter((item) => item.hours > 0)
        .sort((a, b) => b.hours - a.hours || a.label.localeCompare(b.label, "ja"));
    }}

    function randomOrderStorageKey() {{
      return `${{WORKTIME_PROJECT_ID}}|${{WORKTIME_DATE_FROM}}|${{WORKTIME_DATE_TO}}`;
    }}

    function loadRandomOrderMap() {{
      try {{
        const saved = JSON.parse(localStorage.getItem(WORKTIME_RANDOM_ORDER_STORAGE_KEY) || "{{}}");
        return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {{}};
      }} catch {{
        return {{}};
      }}
    }}

    function saveRandomOrder(order) {{
      const saved = loadRandomOrderMap();
      saved[randomOrderStorageKey()] = order;
      localStorage.setItem(WORKTIME_RANDOM_ORDER_STORAGE_KEY, JSON.stringify(saved));
    }}

    function savedRandomOrder() {{
      const order = loadRandomOrderMap()[randomOrderStorageKey()];
      return Array.isArray(order) ? order.filter((name) => typeof name === "string") : [];
    }}

    function shuffled(values) {{
      const result = [...values];
      for (let index = result.length - 1; index > 0; index -= 1) {{
        const swapIndex = Math.floor(Math.random() * (index + 1));
        [result[index], result[swapIndex]] = [result[swapIndex], result[index]];
      }}
      return result;
    }}

    function orderedGroupsForBar(groups) {{
      const order = savedRandomOrder();
      if (!order.length) {{
        return groups;
      }}

      const orderIndex = new Map(order.map((name, index) => [name, index]));
      const missing = groups
        .map((item) => item.user)
        .filter((name) => !orderIndex.has(name));
      if (missing.length) {{
        const mergedOrder = [...order, ...shuffled(missing)];
        saveRandomOrder(mergedOrder);
        mergedOrder.forEach((name, index) => orderIndex.set(name, index));
      }}

      return [...groups].sort((a, b) => {{
        const aIndex = orderIndex.has(a.user) ? orderIndex.get(a.user) : Number.MAX_SAFE_INTEGER;
        const bIndex = orderIndex.has(b.user) ? orderIndex.get(b.user) : Number.MAX_SAFE_INTEGER;
        return aIndex - bIndex || a.user.localeCompare(b.user, "ja");
      }});
    }}

    function decideRandomOrder(groups) {{
      const existingOrder = savedRandomOrder();
      if (existingOrder.length) {{
        return existingOrder;
      }}

      const order = shuffled(groups.map((item) => item.user));
      saveRandomOrder(order);
      return order;
    }}

    function totalGroupForBar(groups) {{
      const parts = new Map();
      let hours = 0;
      groups.forEach((group) => {{
        hours += group.hours;
        group.parts.forEach((part) => {{
          const existing = parts.get(part.key) || {{ key: part.key, label: part.label, hours: 0 }};
          existing.hours += part.hours;
          parts.set(part.key, existing);
        }});
      }});

      return {{
        user: "全員合計",
        hours,
        isTotal: true,
        parts: Array.from(parts.values())
          .filter((item) => item.hours > 0)
          .sort((a, b) => b.hours - a.hours || a.label.localeCompare(b.label, "ja")),
      }};
    }}

    function renderBarChart(groups, totalHours) {{
      worktimeChart.replaceChildren();
      worktimeChart.className = "worktime-chart worktime-bar-chart";
      if (!groups.length) {{
        const emptyChart = document.createElement("p");
        emptyChart.className = "empty-message";
        emptyChart.textContent = "作業時間はありません。";
        worktimeChart.append(emptyChart);
        return;
      }}

      const totalGroup = totalGroupForBar(groups);
      const orderedGroups = [totalGroup, ...orderedGroupsForBar(groups)];
      const maxHours = Math.max(...orderedGroups.map((item) => item.hours), 1);
      const partColorMap = new Map(
        totalGroup.parts
          .sort((a, b) => b.hours - a.hours || a.label.localeCompare(b.label, "ja"))
          .map((item, index) => [item.key, CHART_COLORS[index % CHART_COLORS.length]])
      );
      orderedGroups.forEach((item) => {{
        const row = document.createElement("div");
        const name = document.createElement("span");
        const track = document.createElement("span");
        const value = document.createElement("span");
        const breakdown = document.createElement("div");
        const percent = totalHours > 0 ? item.hours / totalHours * 100 : 0;

        row.className = item.isTotal ? "worktime-bar-row is-total" : "worktime-bar-row";
        name.className = "worktime-bar-name";
        name.textContent = item.user;
        name.title = item.user;
        track.className = "worktime-bar-track";
        item.parts.forEach((part, index) => {{
          const segment = document.createElement("span");
          const color = partColorMap.get(part.key) || CHART_COLORS[index % CHART_COLORS.length];
          segment.className = "worktime-bar-segment";
          segment.style.width = `${{part.hours / maxHours * 100}}%`;
          segment.style.background = color;
          segment.title = `${{part.label}} ${{formatHours(part.hours)}}`;
          track.append(segment);
        }});
        value.className = "worktime-chart-value";
        value.textContent = `${{formatHours(item.hours)}} / ${{percent.toFixed(1)}}%`;
        breakdown.className = "worktime-bar-breakdown";
        item.parts.forEach((part, index) => {{
          const chip = document.createElement("span");
          const swatch = document.createElement("i");
          const color = partColorMap.get(part.key) || CHART_COLORS[index % CHART_COLORS.length];
          swatch.className = "worktime-issue-swatch";
          swatch.style.background = color;
          chip.style.borderColor = color;
          chip.append(swatch, `${{part.label}} ${{formatHours(part.hours)}}`);
          breakdown.append(chip);
        }});

        row.append(name, track, value);
        row.append(breakdown);
        worktimeChart.append(row);
      }});
    }}

    function renderPieChart(groups, totalHours) {{
      worktimeChart.replaceChildren();
      worktimeChart.className = "worktime-chart";
      if (!groups.length || totalHours <= 0) {{
        const emptyChart = document.createElement("p");
        emptyChart.className = "empty-message";
        emptyChart.textContent = "作業時間はありません。";
        worktimeChart.append(emptyChart);
        return;
      }}

      const layout = document.createElement("div");
      const pie = document.createElement("div");
      const center = document.createElement("div");
      const tooltip = document.createElement("div");
      const legend = document.createElement("ul");
      let current = 0;
      const segments = groups.map((item, index) => {{
        const percent = item.hours / totalHours * 100;
        const start = current;
        current += percent;
        const color = CHART_COLORS[index % CHART_COLORS.length];
        return {{
          item,
          percent,
          start,
          end: current,
          color,
          gradient: `${{color}} ${{start.toFixed(3)}}% ${{current.toFixed(3)}}%`,
        }};
      }});

      layout.className = "worktime-pie-layout";
      pie.className = "worktime-pie";
      pie.style.background = `conic-gradient(${{segments.map((segment) => segment.gradient).join(", ")}})`;
      center.className = "worktime-pie-center";
      center.textContent = formatHours(totalHours);
      tooltip.className = "worktime-pie-tooltip";
      tooltip.hidden = true;
      legend.className = "worktime-chart-legend";

      function segmentAtPointer(event) {{
        const rect = pie.getBoundingClientRect();
        const x = event.clientX - rect.left - rect.width / 2;
        const y = event.clientY - rect.top - rect.height / 2;
        const angle = (Math.atan2(y, x) * 180 / Math.PI + 90 + 360) % 360;
        const percent = angle / 3.6;
        return segments.find((segment) => percent >= segment.start && percent < segment.end) || segments.at(-1);
      }}

      function showPieTooltip(event) {{
        const segment = segmentAtPointer(event);
        if (!segment) {{
          tooltip.hidden = true;
          return;
        }}

        const rect = pie.getBoundingClientRect();
        const left = Math.min(Math.max(event.clientX - rect.left, 12), rect.width - 12);
        const top = Math.min(Math.max(event.clientY - rect.top, 12), rect.height - 12);
        tooltip.innerHTML = "";
        const label = document.createElement("strong");
        const value = document.createElement("span");
        label.textContent = segment.item.label;
        value.textContent = `${{formatHours(segment.item.hours)}} / ${{segment.percent.toFixed(1)}}%`;
        tooltip.append(label, value);
        tooltip.style.left = `${{left}}px`;
        tooltip.style.top = `${{top}}px`;
        tooltip.hidden = false;
      }}

      pie.addEventListener("pointerdown", (event) => {{
        pie.setPointerCapture(event.pointerId);
        showPieTooltip(event);
      }});
      pie.addEventListener("pointermove", (event) => {{
        showPieTooltip(event);
      }});
      pie.addEventListener("pointerup", (event) => {{
        showPieTooltip(event);
        if (pie.hasPointerCapture(event.pointerId)) {{
          pie.releasePointerCapture(event.pointerId);
        }}
      }});
      pie.addEventListener("pointerleave", () => {{
        tooltip.hidden = true;
      }});

      groups.forEach((item, index) => {{
        const percent = item.hours / totalHours * 100;
        const row = document.createElement("li");
        const swatch = document.createElement("span");
        const name = document.createElement("span");
        const value = document.createElement("span");

        swatch.className = "worktime-swatch";
        swatch.style.background = CHART_COLORS[index % CHART_COLORS.length];
        name.textContent = item.label;
        value.className = "worktime-chart-value";
        value.textContent = `${{formatHours(item.hours)}} / ${{percent.toFixed(1)}}%`;
        row.append(swatch, name, value);
        legend.append(row);
      }});

      pie.append(center, tooltip);
      layout.append(pie, legend);
      worktimeChart.append(layout);
    }}

    function renderChart(filteredEntries, totalHours) {{
      const breakdownType = currentBreakdownType();
      const groups = groupedHours(filteredEntries, breakdownType);
      if (chartType.value === "pie") {{
        const pieGroups = userFilter.value === "__all__" && breakdownType === "issue"
          ? groups.map((item) => ({{ label: item.user, hours: item.hours }}))
          : groupedBreakdownHours(filteredEntries, breakdownType);
        renderPieChart(pieGroups, totalHours);
      }} else {{
        renderBarChart(groups, totalHours);
      }}
    }}

    function renderRows() {{
      const selectedUser = userFilter.value;
      const selectedVersions = getSelectedWorktimeVersions();
      const filtered = entries.filter((entry) => {{
        const userMatches = selectedUser === "__all__" || entry.user === selectedUser;
        const versionMatches = selectedVersions === null || selectedVersions.has(entry.version || "-");
        return userMatches && versionMatches;
      }});
      const totalHours = filtered.reduce((total, entry) => total + Number(entry.hours || 0), 0);

      body.replaceChildren();
      empty.hidden = filtered.length > 0;
      visibleEntryCount.textContent = filtered.length;
      visibleEntryHours.textContent = formatHours(totalHours);
      renderChart(filtered, totalHours);

      filtered.forEach((entry) => {{
        const row = document.createElement("tr");
        appendCell(row, entry.spent_on || "-");
        appendCell(row, entry.user || "-");

        const issueCell = document.createElement("td");
        issueCell.className = "worktime-issue-cell";
        if (entry.issue_url) {{
          const link = document.createElement("a");
          link.href = entry.issue_url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = `#${{entry.issue_id}}`;
          issueCell.append(link);
        }} else {{
          issueCell.textContent = "-";
        }}
        if (entry.issue_subject && entry.issue_subject !== "-") {{
          const subject = document.createElement("span");
          subject.className = "worktime-issue-subject";
          subject.textContent = entry.issue_subject;
          issueCell.append(subject);
        }}
        row.append(issueCell);

        appendCell(row, entry.version || "-");
        appendCell(row, entry.activity || "-");
        appendCell(row, formatHours(Number(entry.hours || 0)), "hours");
        appendCell(row, entry.comment || "", "comment");
        body.append(row);
      }});
    }}

    applyTheme(getSavedTheme());
    window.addEventListener("storage", (event) => {{
      if (event.key === THEME_STORAGE_KEY) {{
        applyTheme(getSavedTheme());
      }}
    }});
    const savedChartType = localStorage.getItem(WORKTIME_CHART_STORAGE_KEY);
    if (["bar", "pie"].includes(savedChartType)) {{
      chartType.value = savedChartType;
    }}
    const savedBreakdownType = localStorage.getItem(WORKTIME_BREAKDOWN_STORAGE_KEY);
    if (["issue", "activity"].includes(savedBreakdownType)) {{
      chartBreakdownRadios.forEach((radio) => {{
        radio.checked = radio.value === savedBreakdownType;
      }});
    }}
    chartType.addEventListener("change", () => {{
      localStorage.setItem(WORKTIME_CHART_STORAGE_KEY, chartType.value);
      renderRows();
    }});
    chartBreakdownRadios.forEach((radio) => {{
      radio.addEventListener("change", () => {{
        localStorage.setItem(WORKTIME_BREAKDOWN_STORAGE_KEY, currentBreakdownType());
        renderRows();
      }});
    }});
    randomOrderButton.addEventListener("click", () => {{
      decideRandomOrder(groupedHours(entries, currentBreakdownType()));
      chartType.value = "bar";
      localStorage.setItem(WORKTIME_CHART_STORAGE_KEY, chartType.value);
      renderRows();
    }});
    if (worktimeVersionAll) {{
      worktimeVersionAll.addEventListener("change", handleWorktimeVersionAllChange);
    }}
    worktimeVersionCheckboxes.forEach((checkbox) => {{
      checkbox.addEventListener("change", handleWorktimeVersionCheckboxChange);
    }});
    userFilter.addEventListener("change", renderRows);
    renderRows();
  </script>
</body>
</html>
"""


def issue_quality_date(issue: dict[str, Any], date_basis: str) -> date | None:
    primary_field = "created_on" if date_basis == "created" else "updated_on"
    fallback_field = "updated_on" if date_basis == "created" else "created_on"
    issue_datetime = parse_datetime(issue.get(primary_field)) or parse_datetime(issue.get(fallback_field))
    return issue_datetime.date() if issue_datetime else None


def quality_issue_rows(
    issues: list[dict[str, Any]],
    redmine_url: str,
    date_from: date,
    date_to: date,
    category_labels: dict[str, str] | None = None,
    date_basis: str = "updated",
) -> list[dict[str, Any]]:
    category_labels = category_labels or {}
    rows: list[dict[str, Any]] = []
    for issue in issues:
        if not has_custom_field(issue, BUG_CATEGORY_FIELD_NAME):
            continue
        issue_date = issue_quality_date(issue, date_basis)
        if issue_date is None or issue_date < date_from or issue_date > date_to:
            continue
        created_date = parse_datetime(issue.get("created_on"))
        updated_date = parse_datetime(issue.get("updated_on"))
        categories = [
            category_labels.get(category, category)
            for category in bug_category_values(issue)
        ]
        categories = [category for category in categories if category != "未設定"]
        if not categories:
            continue
        rows.append(
            {
                "date": issue_date,
                "created_date": created_date.date().isoformat() if created_date else "-",
                "updated_date": updated_date.date().isoformat() if updated_date else "-",
                "id": str(issue.get("id") or "-"),
                "url": issue_url(issue, redmine_url),
                "subject": str(issue.get("subject") or "-"),
                "tracker": issue_field(issue, "tracker"),
                "status": issue_field(issue, "status"),
                "assignee": assignee_name(issue),
                "version": fixed_version_name(issue),
                "categories": categories,
                "category_text": ", ".join(categories),
            }
        )
    return sorted(rows, key=lambda item: (item["date"], item["id"]), reverse=True)


def quality_category_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        for category in row["categories"]:
            if category == "未設定":
                continue
            counts[category] = counts.get(category, 0) + 1
    return [
        {"category": category, "count": count}
        for category, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def fiscal_quarter_periods(today: date) -> list[tuple[str, date, date]]:
    fiscal_year = today.year if today.month >= 4 else today.year - 1
    quarter_specs = [
        ("1Q", fiscal_year, 4, 6),
        ("2Q", fiscal_year, 7, 9),
        ("3Q", fiscal_year, 10, 12),
        ("4Q", fiscal_year + 1, 1, 3),
    ]
    periods: list[tuple[str, date, date]] = []

    for label, year, start_month, end_month in quarter_specs:
        start = date(year, start_month, 1)
        next_month = date(year + 1, 1, 1) if end_month == 12 else date(year, end_month + 1, 1)
        end = next_month - timedelta(days=1)
        if end > today:
            start = date(start.year - 1, start.month, start.day)
            end = date(end.year - 1, end.month, end.day)
        periods.append((label, start, end))

    return periods


def render_quality_html(
    issues: list[dict[str, Any]],
    redmine_url: str,
    project_id: str,
    date_from: date,
    date_to: date,
    category_labels: dict[str, str] | None = None,
    date_basis: str = "updated",
) -> str:
    date_basis = "created" if date_basis == "created" else "updated"
    rows = quality_issue_rows(issues, redmine_url, date_from, date_to, category_labels, date_basis)
    category_counts = quality_category_counts(rows)
    chart_rows = [{"category": "全体", "count": len(rows), "is_total": True}, *category_counts]
    category_color_indexes = {
        str(item["category"]): index % 8
        for index, item in enumerate(chart_rows)
    }
    max_count = max(1, *(item["count"] for item in chart_rows))
    quarter_links_html = "\n".join(
        f"""
          <a class="preset-link" href="/{QUALITY_HTML}?{urlencode({'from': start.isoformat(), 'to': end.isoformat(), 'basis': date_basis})}">{escape_text(label)} <span>{escape_text(start.isoformat())} - {escape_text(end.isoformat())}</span></a>"""
        for label, start, end in fiscal_quarter_periods(date.today())
    )

    def render_quality_bar_row(index: int, item: dict[str, Any]) -> str:
        color_class = f" quality-bar-color-{index % 8}"
        total_class = " is-total" if item.get("is_total") else ""
        width = item["count"] / max_count * 100
        return f"""
        <div class="quality-bar-row{total_class}{color_class}">
          <span class="quality-bar-name">{escape_text(item["category"])}</span>
          <span class="quality-bar-track"><span style="width: {width:.2f}%"></span></span>
          <strong>{item["count"]}件</strong>
        </div>"""

    chart_html = "\n".join(
        render_quality_bar_row(index, item)
        for index, item in enumerate(chart_rows)
    ) or '<p class="empty-message">対象チケットはありません。</p>'

    def row_quality_color_class(row: dict[str, Any]) -> str:
        categories = row.get("categories") or []
        color_index = category_color_indexes.get(categories[0]) if categories else None
        return f" quality-bar-color-{color_index}" if color_index is not None else ""

    primary_date_key = "created_date" if date_basis == "created" else "updated_date"
    secondary_date_key = "updated_date" if date_basis == "created" else "created_date"
    primary_date_label = "作成日" if date_basis == "created" else "更新日"
    secondary_date_label = "更新日" if date_basis == "created" else "作成日"

    table_rows = "\n".join(
        f"""
          <tr class="{row_quality_color_class(row).strip()}">
            <td>{escape_text(row[primary_date_key])}</td>
            <td>{escape_text(row[secondary_date_key])}</td>
            <td class="issue-cell">
              <a href="{escape_text(row["url"])}" target="_blank" rel="noopener noreferrer">#{escape_text(row["id"])}</a>
              <span>{escape_text(row["subject"])}</span>
            </td>
            <td>{escape_text(row["category_text"])}</td>
            <td>{escape_text(row["tracker"])}</td>
            <td>{escape_text(row["status"])}</td>
            <td>{escape_text(row["assignee"])}</td>
            <td>{escape_text(row["version"])}</td>
          </tr>"""
        for row in rows
    )
    if not table_rows:
        table_rows = """
          <tr>
            <td colspan="8" class="empty-message">対象チケットはありません。</td>
          </tr>"""

    return f"""<!doctype html>
<html lang="ja" data-theme="system">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>品質改善 - Redmine Kanban</title>
  <style>
    * {{
      box-sizing: border-box;
    }}

    :root {{
      color-scheme: light;
      --bg-color: #f6f7f9;
      --text-color: #1f2937;
      --muted-text: #64748b;
      --panel-bg: #ffffff;
      --panel-border: #d8dee8;
      --control-bg: #ffffff;
      --control-border: #9ca3af;
      --control-text: #111827;
      --button-bg: #be123c;
      --button-hover-bg: #9f1239;
      --button-text: #ffffff;
      --link-color: #be123c;
      --row-bg: #f8fafc;
      --bar-bg: #fee2e2;
      --bar-fill: #be123c;
    }}

    @media (prefers-color-scheme: dark) {{
      :root {{
        color-scheme: dark;
        --bg-color: #0f172a;
        --text-color: #e5e7eb;
        --muted-text: #cbd5e1;
        --panel-bg: #111827;
        --panel-border: #374151;
        --control-bg: #0f172a;
        --control-border: #475569;
        --control-text: #f9fafb;
        --button-bg: #be123c;
        --button-hover-bg: #e11d48;
        --button-text: #ffffff;
        --link-color: #fb7185;
        --row-bg: #172033;
        --bar-bg: #3f1624;
        --bar-fill: #fb7185;
      }}
    }}

    html[data-theme="light"] {{
      color-scheme: light;
      --bg-color: #f6f7f9;
      --text-color: #1f2937;
      --muted-text: #64748b;
      --panel-bg: #ffffff;
      --panel-border: #d8dee8;
      --control-bg: #ffffff;
      --control-border: #9ca3af;
      --control-text: #111827;
      --button-bg: #be123c;
      --button-hover-bg: #9f1239;
      --button-text: #ffffff;
      --link-color: #be123c;
      --row-bg: #f8fafc;
      --bar-bg: #fee2e2;
      --bar-fill: #be123c;
    }}

    html[data-theme="dark"] {{
      color-scheme: dark;
      --bg-color: #0f172a;
      --text-color: #e5e7eb;
      --muted-text: #cbd5e1;
      --panel-bg: #111827;
      --panel-border: #374151;
      --control-bg: #0f172a;
      --control-border: #475569;
      --control-text: #f9fafb;
      --button-bg: #be123c;
      --button-hover-bg: #e11d48;
      --button-text: #ffffff;
      --link-color: #fb7185;
      --row-bg: #172033;
      --bar-bg: #3f1624;
      --bar-fill: #fb7185;
    }}

    body {{
      margin: 0;
      color: var(--text-color);
      background: var(--bg-color);
      font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    }}

    .page-header {{
      position: sticky;
      top: 0;
      z-index: 2;
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: center;
      padding: 16px 20px;
      background: color-mix(in srgb, var(--bg-color) 94%, transparent);
      border-bottom: 1px solid var(--panel-border);
      backdrop-filter: blur(10px);
    }}

    .page-header h1 {{
      margin: 0 0 4px;
      font-size: 22px;
    }}

    .page-header p {{
      margin: 0;
      color: var(--muted-text);
      font-size: 13px;
    }}

    .button {{
      display: inline-flex;
      min-height: 34px;
      align-items: center;
      justify-content: center;
      padding: 7px 11px;
      color: var(--button-text);
      background: var(--button-bg);
      border: 1px solid var(--button-bg);
      border-radius: 8px;
      text-decoration: none;
      font: inherit;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
    }}

    .button:hover {{
      background: var(--button-hover-bg);
      border-color: var(--button-hover-bg);
    }}

    main {{
      display: grid;
      gap: 14px;
      padding: 16px 20px 24px;
    }}

    .panel {{
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 8px;
      overflow: hidden;
    }}

    .filters {{
      display: flex;
      gap: 10px;
      align-items: end;
      flex-wrap: wrap;
      padding: 14px;
    }}

    .preset-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 0 14px 14px;
    }}

    .preset-link {{
      display: inline-flex;
      min-height: 30px;
      align-items: center;
      gap: 6px;
      padding: 5px 10px;
      color: var(--control-text);
      background: var(--row-bg);
      border: 1px solid var(--panel-border);
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
      text-decoration: none;
    }}

    .preset-link span {{
      color: var(--muted-text);
      font-weight: 700;
    }}

    .preset-link:hover {{
      border-color: var(--link-color);
    }}

    label {{
      display: grid;
      gap: 4px;
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 800;
    }}

    input {{
      min-height: 34px;
      padding: 6px 9px;
      color: var(--control-text);
      background: var(--control-bg);
      border: 1px solid var(--control-border);
      border-radius: 8px;
      font: inherit;
      font-weight: 700;
    }}

    .date-basis-filter {{
      margin: 0;
      padding: 7px 9px;
      border: 1px solid var(--panel-border);
      border-radius: 8px;
    }}

    .date-basis-filter legend {{
      padding: 0 4px;
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 800;
    }}

    .date-basis-options {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }}

    .date-basis-options label {{
      display: inline-flex;
      gap: 5px;
      align-items: center;
      color: var(--control-text);
      white-space: nowrap;
    }}

    .summary {{
      display: grid;
      grid-template-columns: repeat(3, minmax(130px, 1fr));
      gap: 10px;
      padding: 0 14px 14px;
    }}

    .summary-card {{
      padding: 10px;
      background: var(--row-bg);
      border: 1px solid var(--panel-border);
      border-radius: 8px;
    }}

    .summary-card span {{
      display: block;
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 800;
    }}

    .summary-card strong {{
      display: block;
      margin-top: 6px;
      font-size: 22px;
    }}

    .quality-bars {{
      display: grid;
      gap: 10px;
      padding: 14px;
    }}

    .quality-bar-row {{
      display: grid;
      grid-template-columns: minmax(130px, 220px) minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      font-size: 13px;
      font-weight: 800;
      --quality-bar-bg: var(--bar-bg);
      --quality-bar-fill: var(--bar-fill);
    }}

    .quality-bar-row.is-total {{
      padding-bottom: 8px;
      border-bottom: 1px solid var(--panel-border);
    }}

    .quality-bar-name {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

    .quality-bar-track {{
      height: 18px;
      overflow: hidden;
      background: var(--quality-bar-bg);
      border-radius: 999px;
    }}

    .quality-bar-track span {{
      display: block;
      min-width: 2px;
      height: 100%;
      background: var(--quality-bar-fill);
      border-radius: inherit;
    }}

    .quality-bar-color-0 {{
      --quality-bar-bg: #fee2e2;
      --quality-bar-fill: #fb7185;
    }}

    .quality-bar-color-1 {{
      --quality-bar-bg: #ffedd5;
      --quality-bar-fill: #f97316;
    }}

    .quality-bar-color-2 {{
      --quality-bar-bg: #fef9c3;
      --quality-bar-fill: #eab308;
    }}

    .quality-bar-color-3 {{
      --quality-bar-bg: #dcfce7;
      --quality-bar-fill: #22c55e;
    }}

    .quality-bar-color-4 {{
      --quality-bar-bg: #dbeafe;
      --quality-bar-fill: #3b82f6;
    }}

    .quality-bar-color-5 {{
      --quality-bar-bg: #e0e7ff;
      --quality-bar-fill: #6366f1;
    }}

    .quality-bar-color-6 {{
      --quality-bar-bg: #f3e8ff;
      --quality-bar-fill: #a855f7;
    }}

    .quality-bar-color-7 {{
      --quality-bar-bg: #ccfbf1;
      --quality-bar-fill: #14b8a6;
    }}

    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme="light"]) .quality-bar-color-0 {{
        --quality-bar-bg: #4c1d2a;
        --quality-bar-fill: #fb7185;
      }}

      :root:not([data-theme="light"]) .quality-bar-color-1 {{
        --quality-bar-bg: #431f0b;
        --quality-bar-fill: #fb923c;
      }}

      :root:not([data-theme="light"]) .quality-bar-color-2 {{
        --quality-bar-bg: #3f3209;
        --quality-bar-fill: #facc15;
      }}

      :root:not([data-theme="light"]) .quality-bar-color-3 {{
        --quality-bar-bg: #12331f;
        --quality-bar-fill: #4ade80;
      }}

      :root:not([data-theme="light"]) .quality-bar-color-4 {{
        --quality-bar-bg: #172554;
        --quality-bar-fill: #60a5fa;
      }}

      :root:not([data-theme="light"]) .quality-bar-color-5 {{
        --quality-bar-bg: #25245a;
        --quality-bar-fill: #818cf8;
      }}

      :root:not([data-theme="light"]) .quality-bar-color-6 {{
        --quality-bar-bg: #32174d;
        --quality-bar-fill: #c084fc;
      }}

      :root:not([data-theme="light"]) .quality-bar-color-7 {{
        --quality-bar-bg: #123c38;
        --quality-bar-fill: #2dd4bf;
      }}
    }}

    html[data-theme="dark"] .quality-bar-color-0 {{
      --quality-bar-bg: #4c1d2a;
      --quality-bar-fill: #fb7185;
    }}

    html[data-theme="dark"] .quality-bar-color-1 {{
      --quality-bar-bg: #431f0b;
      --quality-bar-fill: #fb923c;
    }}

    html[data-theme="dark"] .quality-bar-color-2 {{
      --quality-bar-bg: #3f3209;
      --quality-bar-fill: #facc15;
    }}

    html[data-theme="dark"] .quality-bar-color-3 {{
      --quality-bar-bg: #12331f;
      --quality-bar-fill: #4ade80;
    }}

    html[data-theme="dark"] .quality-bar-color-4 {{
      --quality-bar-bg: #172554;
      --quality-bar-fill: #60a5fa;
    }}

    html[data-theme="dark"] .quality-bar-color-5 {{
      --quality-bar-bg: #25245a;
      --quality-bar-fill: #818cf8;
    }}

    html[data-theme="dark"] .quality-bar-color-6 {{
      --quality-bar-bg: #32174d;
      --quality-bar-fill: #c084fc;
    }}

    html[data-theme="dark"] .quality-bar-color-7 {{
      --quality-bar-bg: #123c38;
      --quality-bar-fill: #2dd4bf;
    }}

    .table-wrap {{
      overflow: auto;
    }}

    table {{
      width: 100%;
      min-width: 980px;
      border-collapse: collapse;
    }}

    th,
    td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--panel-border);
      text-align: left;
      font-size: 13px;
      vertical-align: top;
    }}

    th {{
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 900;
      white-space: nowrap;
    }}

    a {{
      color: var(--link-color);
      font-weight: 800;
      text-decoration: none;
    }}

    .issue-cell {{
      display: flex;
      gap: 8px;
      align-items: baseline;
      min-width: 260px;
    }}

    .issue-cell a {{
      flex: 0 0 auto;
      color: var(--quality-bar-fill, var(--link-color));
    }}

    .issue-cell span {{
      overflow-wrap: anywhere;
      font-weight: 700;
    }}

    .empty-message {{
      padding: 18px;
      color: var(--muted-text);
      text-align: center;
      font-weight: 700;
    }}

    @media (max-width: 760px) {{
      .page-header {{
        display: grid;
      }}

      .summary,
      .quality-bar-row {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <header class="page-header">
    <div>
      <h1>品質改善</h1>
      <p>PROJECT_ID: {escape_text(project_id)} / {escape_text(BUG_CATEGORY_FIELD_NAME)} 別の発生状況</p>
    </div>
    <a class="button" href="/{OUTPUT_HTML}" target="_top">かんばん</a>
  </header>
  <main>
    <section class="panel">
      <form class="filters" action="/{QUALITY_HTML}" method="get">
        <label>
          <span>開始日</span>
          <input type="date" name="from" value="{escape_text(date_from.isoformat())}">
        </label>
        <label>
          <span>終了日</span>
          <input type="date" name="to" value="{escape_text(date_to.isoformat())}">
        </label>
        <fieldset class="date-basis-filter">
          <legend>日付基準</legend>
          <div class="date-basis-options">
            <label>
              <input type="radio" name="basis" value="updated"{' checked' if date_basis == 'updated' else ''}>
              <span>更新日</span>
            </label>
            <label>
              <input type="radio" name="basis" value="created"{' checked' if date_basis == 'created' else ''}>
              <span>作成日</span>
            </label>
          </div>
        </fieldset>
        <button class="button" type="submit">表示</button>
      </form>
      <div class="preset-links" aria-label="四半期プリセット">
{quarter_links_html}
      </div>
      <div class="summary" aria-label="品質改善サマリー">
        <article class="summary-card">
          <span>対象チケット</span>
          <strong>{len(rows)}</strong>
        </article>
        <article class="summary-card">
          <span>カテゴリ数</span>
          <strong>{len(category_counts)}</strong>
        </article>
        <article class="summary-card">
          <span>対象期間</span>
          <strong>{escape_text(date_from.isoformat())} - {escape_text(date_to.isoformat())}</strong>
        </article>
      </div>
    </section>
    <section class="panel quality-bars" aria-label="カテゴリ別件数">
{chart_html}
    </section>
    <section class="panel">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">{primary_date_label}</th>
              <th scope="col">{secondary_date_label}</th>
              <th scope="col">Issue</th>
              <th scope="col">不具合のカテゴリ</th>
              <th scope="col">トラッカー</th>
              <th scope="col">ステータス</th>
              <th scope="col">担当者</th>
              <th scope="col">対象バージョン</th>
            </tr>
          </thead>
          <tbody>
{table_rows}
          </tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    const THEME_STORAGE_KEY = "redmine-kanban-theme";
    function getSavedTheme() {{
      const savedTheme = localStorage.getItem(THEME_STORAGE_KEY);
      return ["light", "dark", "system"].includes(savedTheme) ? savedTheme : "system";
    }}
    function applyTheme(theme) {{
      document.documentElement.dataset.theme = ["light", "dark", "system"].includes(theme) ? theme : "system";
    }}
    applyTheme(getSavedTheme());
    window.addEventListener("storage", (event) => {{
      if (event.key === THEME_STORAGE_KEY) {{
        applyTheme(getSavedTheme());
      }}
    }});
  </script>
</body>
</html>
"""


def render_combined_html(project_id: str) -> str:
    return f"""<!doctype html>
<html lang="ja" data-theme="system">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>同時表示 - Redmine Kanban</title>
  <style>
    * {{
      box-sizing: border-box;
    }}

    :root {{
      color-scheme: light;
      --bg-color: #f3f4f6;
      --text-color: #1f2937;
      --muted-text: #64748b;
      --panel-border: #cbd5e1;
      --button-bg: #374151;
      --button-hover-bg: #111827;
      --button-text: #ffffff;
    }}

    @media (prefers-color-scheme: dark) {{
      :root {{
        color-scheme: dark;
        --bg-color: #0f172a;
        --text-color: #e5e7eb;
        --muted-text: #cbd5e1;
        --panel-border: #374151;
        --button-bg: #0f766e;
        --button-hover-bg: #14b8a6;
        --button-text: #ffffff;
      }}
    }}

    html[data-theme="light"] {{
      color-scheme: light;
      --bg-color: #f3f4f6;
      --text-color: #1f2937;
      --muted-text: #64748b;
      --panel-border: #cbd5e1;
      --button-bg: #374151;
      --button-hover-bg: #111827;
      --button-text: #ffffff;
    }}

    html[data-theme="dark"] {{
      color-scheme: dark;
      --bg-color: #0f172a;
      --text-color: #e5e7eb;
      --muted-text: #cbd5e1;
      --panel-border: #374151;
      --button-bg: #0f766e;
      --button-hover-bg: #14b8a6;
      --button-text: #ffffff;
    }}

    html,
    body {{
      width: 100%;
      height: 100%;
      margin: 0;
    }}

    body {{
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      color: var(--text-color);
      background: var(--bg-color);
      font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    }}

    .combined-header {{
      display: flex;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      padding: 10px 14px;
      border-bottom: 1px solid var(--panel-border);
    }}

    .combined-header h1 {{
      margin: 0;
      font-size: 17px;
      letter-spacing: 0;
    }}

    .combined-header p {{
      margin: 2px 0 0;
      color: var(--muted-text);
      font-size: 12px;
    }}

    .combined-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}

    .button {{
      display: inline-flex;
      min-height: 32px;
      align-items: center;
      justify-content: center;
      padding: 6px 10px;
      color: var(--button-text);
      background: var(--button-bg);
      border-radius: 8px;
      text-decoration: none;
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
    }}

    .button:hover {{
      background: var(--button-hover-bg);
    }}

    .combined-layout {{
      display: grid;
      grid-template-columns: minmax(420px, 1.2fr) minmax(420px, 1fr);
      min-height: 0;
    }}

    .pane {{
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      min-width: 0;
      min-height: 0;
      border-right: 1px solid var(--panel-border);
    }}

    .pane:last-child {{
      border-right: 0;
    }}

    .pane-title {{
      padding: 8px 12px;
      border-bottom: 1px solid var(--panel-border);
      color: var(--muted-text);
      font-size: 12px;
      font-weight: 800;
    }}

    iframe {{
      width: 100%;
      height: 100%;
      border: 0;
      background: var(--bg-color);
    }}

    @media (max-width: 980px) {{
      body {{
        height: auto;
        min-height: 100%;
      }}

      .combined-header {{
        display: grid;
      }}

      .combined-layout {{
        grid-template-columns: 1fr;
      }}

      .pane {{
        height: 80vh;
        border-right: 0;
        border-bottom: 1px solid var(--panel-border);
      }}
    }}
  </style>
</head>
<body>
  <header class="combined-header">
    <div>
      <h1>かんばん・作業負荷 同時表示</h1>
      <p>PROJECT_ID: {escape_text(project_id)}</p>
    </div>
    <nav class="combined-actions" aria-label="画面切り替え">
      <a class="button" href="/{OUTPUT_HTML}">かんばん</a>
      <a class="button" href="/{WORKLOAD_HTML}">作業負荷状況</a>
      <a class="button" href="/{COMBINED_HTML}">再読み込み</a>
    </nav>
  </header>
  <main class="combined-layout">
    <section class="pane">
      <div class="pane-title">かんばんボード</div>
      <iframe title="かんばんボード" src="/{OUTPUT_HTML}"></iframe>
    </section>
    <section class="pane">
      <div class="pane-title">作業負荷状況</div>
      <iframe title="作業負荷状況" src="/{WORKLOAD_HTML}"></iframe>
    </section>
  </main>
  <script>
    const THEME_STORAGE_KEY = "redmine-kanban-theme";

    function getSavedTheme() {{
      const savedTheme = localStorage.getItem(THEME_STORAGE_KEY);
      return ["light", "dark", "system"].includes(savedTheme) ? savedTheme : "system";
    }}

    function applyTheme(theme) {{
      document.documentElement.dataset.theme = ["light", "dark", "system"].includes(theme) ? theme : "system";
    }}

    applyTheme(getSavedTheme());
    window.addEventListener("storage", (event) => {{
      if (event.key === THEME_STORAGE_KEY) {{
        applyTheme(getSavedTheme());
      }}
    }});
  </script>
</body>
</html>
"""


def render_kanban_html(
    issues: list[dict[str, Any]],
    redmine_url: str,
    project_id: str,
    refreshed_at: datetime | None = None,
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

    .page-header .data-refreshed-at {{
      margin-top: 6px;
      font-size: 12px;
      font-weight: 700;
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
      grid-template-columns: minmax(130px, 180px) minmax(160px, 200px) minmax(240px, 1fr) minmax(160px, 200px) auto;
      align-items: end;
      gap: 12px;
    }}

    .project-control {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) repeat(7, auto);
      align-items: end;
      gap: 8px;
      max-width: 1100px;
      margin-top: 12px;
    }}

    .project-control label {{
      display: grid;
      gap: 4px;
      color: var(--body-muted-text);
      font-size: 12px;
      font-weight: 700;
    }}

    .project-id-picker {{
      position: relative;
    }}

    .project-control input {{
      width: 100%;
      min-height: 34px;
      padding: 6px 38px 6px 10px;
      color: var(--control-text);
      background: var(--control-bg);
      border: 1px solid var(--control-border);
      border-radius: 8px;
      font: inherit;
      font-weight: 600;
    }}

    .project-control .project-id-history-toggle {{
      position: absolute;
      top: 1px;
      right: 1px;
      width: 34px;
      min-height: 32px;
      padding: 0;
      color: var(--control-text);
      background: transparent;
      border: 0;
      border-left: 1px solid transparent;
      border-radius: 0 7px 7px 0;
      font-size: 12px;
      line-height: 1;
      cursor: pointer;
    }}

    .project-control .project-id-history-toggle:hover,
    .project-control .project-id-history-toggle[aria-expanded="true"] {{
      background: color-mix(in srgb, var(--control-border) 22%, transparent);
      border-left-color: var(--control-border);
    }}

    .project-id-history-menu {{
      position: absolute;
      z-index: 30;
      top: calc(100% + 4px);
      right: 0;
      left: 0;
      max-height: 220px;
      overflow-y: auto;
      padding: 4px;
      background: var(--control-bg);
      border: 1px solid var(--control-border);
      border-radius: 8px;
      box-shadow: 0 14px 30px rgba(0, 0, 0, 0.28);
    }}

    .project-id-history-option {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 4px;
      border-radius: 6px;
    }}

    .project-id-history-option:hover,
    .project-id-history-option:focus-within {{
      background: color-mix(in srgb, var(--button-bg) 20%, transparent);
    }}

    .project-control .project-id-history-select {{
      min-width: 0;
      min-height: 30px;
      padding: 6px 8px;
      color: var(--control-text);
      background: transparent;
      border: 0;
      border-radius: 6px;
      font: inherit;
      font-size: 12px;
      font-weight: 700;
      text-align: left;
      cursor: pointer;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

    .project-control .project-id-history-delete {{
      width: 28px;
      min-width: 28px;
      min-height: 30px;
      padding: 0 8px;
      color: var(--control-text);
      background: transparent;
      border: 0;
      border-radius: 6px;
      font: inherit;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
    }}

    .project-control .project-id-history-select:focus-visible,
    .project-control .project-id-history-delete:focus-visible {{
      outline: none;
    }}

    .project-control .project-id-history-delete:hover,
    .project-control .project-id-history-delete:focus-visible {{
      color: #fca5a5;
      background: rgba(239, 68, 68, 0.18);
    }}

    .project-control button,
    .project-control .control-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
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
      text-decoration: none;
      white-space: nowrap;
    }}

    .project-control button:hover,
    .project-control .control-link:hover {{
      background: var(--button-hover-bg);
    }}

    .project-control .control-action-display {{
      background: #0f766e;
      border-color: #0f766e;
    }}

    .project-control .control-action-display:hover {{
      background: #0f5f59;
      border-color: #0f5f59;
    }}

    .project-control .control-action-refresh {{
      background: #b45309;
      border-color: #b45309;
    }}

    .project-control .control-action-refresh:hover {{
      background: #92400e;
      border-color: #92400e;
    }}

    .project-control .control-link-workload {{
      background: #1d4ed8;
      border-color: #1d4ed8;
    }}

    .project-control .control-link-workload:hover {{
      background: #1e40af;
      border-color: #1e40af;
    }}

    .project-control .control-link-worktime {{
      background: #7c3aed;
      border-color: #7c3aed;
    }}

    .project-control .control-link-worktime:hover {{
      background: #6d28d9;
      border-color: #6d28d9;
    }}

    .project-control .control-link-quality {{
      background: #be123c;
      border-color: #be123c;
    }}

    .project-control .control-link-quality:hover {{
      background: #9f1239;
      border-color: #9f1239;
    }}

    .project-control .control-link-combined {{
      background: #475569;
      border-color: #475569;
    }}

    .project-control .control-link-combined:hover {{
      background: #334155;
      border-color: #334155;
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

    .issue-id-filter,
    .assignee-filter,
    .ball-possession-filter,
    .theme-filter {{
      display: grid;
      gap: 4px;
      color: var(--body-muted-text);
      font-size: 12px;
      font-weight: 700;
    }}

    .issue-id-filter input,
    .assignee-filter select,
    .ball-possession-filter select,
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

    .workload-assignee-button {{
      display: inline;
      min-width: 0;
      max-width: 100%;
      padding: 0;
      border: 0;
      background: transparent;
      color: inherit;
      font: inherit;
      text-align: left;
      cursor: pointer;
    }}

    .workload-assignee-button:hover,
    .workload-assignee-button:focus-visible {{
      text-decoration: underline;
      text-underline-offset: 3px;
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
      <p class="data-refreshed-at">取得: {escape_text(format_refreshed_at(refreshed_at))}</p>
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
    const KANBAN_PROJECT_ID = {script_json(project_id)};
    const KANBAN_FILTER_STORAGE_KEY = `redmine-kanban-filters:${{KANBAN_PROJECT_ID}}`;
    const PROJECT_ID_HISTORY_STORAGE_KEY = "redmine-kanban-project-id-history";
    const PROJECT_ID_HISTORY_LIMIT = 20;
    const themeSelector = document.getElementById("theme-selector");
    const issueIdFilter = document.getElementById("issue-id-filter");
    const assigneeFilter = document.getElementById("assignee-filter");
    const ballPossessionFilter = document.getElementById("ball-possession-filter");
    const versionAll = document.getElementById("version-all");
    const versionCheckboxes = Array.from(document.querySelectorAll(".version-checkbox"));
    const resetFiltersButton = document.getElementById("reset-filters");
    const visibleIssueCount = document.getElementById("visible-issue-count");
    const workloadGrid = document.getElementById("workload-grid");
    const projectControl = document.querySelector(".project-control");
    const projectIdInput = document.getElementById("project-id-input");
    const projectIdHistoryToggle = document.getElementById("project-id-history-toggle");
    const projectIdHistoryMenu = document.getElementById("project-id-history-menu");
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

    function loadProjectIdHistory() {{
      try {{
        const parsed = JSON.parse(localStorage.getItem(PROJECT_ID_HISTORY_STORAGE_KEY) || "[]");
        if (Array.isArray(parsed)) {{
          return parsed
            .filter((value) => typeof value === "string" && value.trim())
            .slice(0, PROJECT_ID_HISTORY_LIMIT);
        }}
      }} catch {{
        return [];
      }}
      return [];
    }}

    function saveProjectIdHistory(history) {{
      localStorage.setItem(
        PROJECT_ID_HISTORY_STORAGE_KEY,
        JSON.stringify(history.slice(0, PROJECT_ID_HISTORY_LIMIT)),
      );
    }}

    function renderProjectIdHistory(history) {{
      if (!projectIdHistoryMenu) {{
        return;
      }}

      const menuOptions = history.map((projectId) => {{
        const row = document.createElement("div");
        const selectButton = document.createElement("button");
        const deleteButton = document.createElement("button");

        row.className = "project-id-history-option";
        row.setAttribute("role", "option");

        selectButton.type = "button";
        selectButton.className = "project-id-history-select";
        selectButton.dataset.projectId = projectId;
        selectButton.textContent = projectId;

        deleteButton.type = "button";
        deleteButton.className = "project-id-history-delete";
        deleteButton.dataset.projectId = projectId;
        deleteButton.setAttribute("aria-label", `${{projectId}} を履歴から削除`);
        deleteButton.textContent = "×";

        row.append(selectButton, deleteButton);
        return row;
      }});
      projectIdHistoryMenu.replaceChildren(...menuOptions);
    }}

    function closeProjectIdHistoryMenu() {{
      if (!projectIdHistoryMenu || !projectIdHistoryToggle || !projectIdInput) {{
        return;
      }}

      projectIdHistoryMenu.hidden = true;
      projectIdHistoryToggle.setAttribute("aria-expanded", "false");
      projectIdInput.setAttribute("aria-expanded", "false");
    }}

    function openProjectIdHistoryMenu() {{
      if (!projectIdHistoryMenu || !projectIdHistoryToggle || !projectIdInput) {{
        return;
      }}

      renderProjectIdHistory(loadProjectIdHistory());
      if (!projectIdHistoryMenu.children.length) {{
        closeProjectIdHistoryMenu();
        return;
      }}

      projectIdHistoryMenu.hidden = false;
      projectIdHistoryToggle.setAttribute("aria-expanded", "true");
      projectIdInput.setAttribute("aria-expanded", "true");
    }}

    function toggleProjectIdHistoryMenu() {{
      if (!projectIdHistoryMenu || projectIdHistoryMenu.hidden) {{
        openProjectIdHistoryMenu();
      }} else {{
        closeProjectIdHistoryMenu();
      }}
    }}

    function rememberProjectId(projectId) {{
      const normalizedProjectId = projectId.trim();
      if (!normalizedProjectId) {{
        return;
      }}

      const history = loadProjectIdHistory().filter((value) => value !== normalizedProjectId);
      history.unshift(normalizedProjectId);
      saveProjectIdHistory(history);
      renderProjectIdHistory(history);
    }}

    function removeProjectIdHistory(projectId) {{
      const nextHistory = loadProjectIdHistory().filter((value) => value !== projectId);
      saveProjectIdHistory(nextHistory);
      renderProjectIdHistory(nextHistory);
      if (!nextHistory.length) {{
        closeProjectIdHistoryMenu();
      }}
    }}

    function initializeProjectIdHistory() {{
      if (!projectIdInput) {{
        return;
      }}

      renderProjectIdHistory(loadProjectIdHistory());
      rememberProjectId(projectIdInput.value);
      projectIdInput.addEventListener("input", closeProjectIdHistoryMenu);
      projectIdInput.addEventListener("keydown", (event) => {{
        if (event.key === "Escape") {{
          closeProjectIdHistoryMenu();
        }}
      }});
      if (projectIdHistoryToggle) {{
        projectIdHistoryToggle.addEventListener("click", toggleProjectIdHistoryMenu);
      }}
      if (projectIdHistoryMenu) {{
        projectIdHistoryMenu.addEventListener("click", (event) => {{
          const deleteButton = event.target.closest(".project-id-history-delete");
          if (deleteButton && projectIdHistoryMenu.contains(deleteButton)) {{
            removeProjectIdHistory(deleteButton.dataset.projectId || "");
            return;
          }}

          const selectButton = event.target.closest(".project-id-history-select");
          if (!selectButton || !projectIdHistoryMenu.contains(selectButton)) {{
            return;
          }}

          projectIdInput.value = selectButton.dataset.projectId || "";
          rememberProjectId(projectIdInput.value);
          closeProjectIdHistoryMenu();
          projectIdInput.focus();
        }});
      }}
      document.addEventListener("click", (event) => {{
        if (!projectControl || projectControl.contains(event.target)) {{
          return;
        }}

        closeProjectIdHistoryMenu();
      }});
      if (projectControl) {{
        projectControl.addEventListener("submit", () => rememberProjectId(projectIdInput.value));
      }}
    }}

    function getSelectedVersions() {{
      const versionValues = selectedVersionValues();
      if (versionValues === null) {{
        return null;
      }}

      return new Set(versionValues);
    }}

    function selectedVersionValues() {{
      if (versionAll.checked) {{
        return null;
      }}

      return versionCheckboxes.filter((checkbox) => checkbox.checked).map((checkbox) => checkbox.value);
    }}

    function selectOptionIfExists(select, value, fallback = "__all__") {{
      if (!select) {{
        return;
      }}

      const hasOption = Array.from(select.options).some((option) => option.value === value);
      select.value = hasOption ? value : fallback;
    }}

    function loadKanbanFilterState() {{
      try {{
        const parsed = JSON.parse(localStorage.getItem(KANBAN_FILTER_STORAGE_KEY) || "null");
        return parsed && typeof parsed === "object" ? parsed : null;
      }} catch {{
        return null;
      }}
    }}

    function saveKanbanFilterState() {{
      const state = {{
        issueIds: issueIdFilter ? issueIdFilter.value : "",
        assignee: assigneeFilter.value,
        ballPossession: ballPossessionFilter ? ballPossessionFilter.value : "__all__",
        versions: selectedVersionValues(),
      }};
      try {{
        localStorage.setItem(KANBAN_FILTER_STORAGE_KEY, JSON.stringify(state));
      }} catch {{
      }}
    }}

    function restoreKanbanFilterState() {{
      const state = loadKanbanFilterState();
      if (!state) {{
        return;
      }}

      if (issueIdFilter) {{
        issueIdFilter.value = String(state.issueIds || "");
      }}
      selectOptionIfExists(assigneeFilter, String(state.assignee || "__all__"));
      if (ballPossessionFilter) {{
        selectOptionIfExists(ballPossessionFilter, String(state.ballPossession || "__all__"));
      }}

      if (Array.isArray(state.versions)) {{
        const savedVersions = new Set(state.versions.map((value) => String(value)));
        let restoredCount = 0;
        versionCheckboxes.forEach((checkbox) => {{
          checkbox.checked = savedVersions.has(checkbox.value);
          restoredCount += checkbox.checked ? 1 : 0;
        }});
        versionAll.checked = restoredCount === 0;
      }} else {{
        versionAll.checked = true;
        versionCheckboxes.forEach((checkbox) => {{
          checkbox.checked = false;
        }});
      }}
    }}

    function selectedIssueIdSet() {{
      if (!issueIdFilter) {{
        return null;
      }}

      const values = issueIdFilter.value
        .split(/[^0-9]+/)
        .map((value) => value.trim())
        .filter(Boolean);
      return values.length ? new Set(values) : null;
    }}

    function cardParticipants(card) {{
      try {{
        const participants = JSON.parse(card.dataset.participants || "[]");
        if (Array.isArray(participants)) {{
          return participants;
        }}
      }} catch {{
        return [card.dataset.assignee || "未設定"];
      }}
      return [card.dataset.assignee || "未設定"];
    }}

    function cardBallPossessionValues(card) {{
      try {{
        const values = JSON.parse(card.dataset.ballPossession || "[]");
        if (Array.isArray(values)) {{
          return values;
        }}
      }} catch {{
        return [];
      }}
      return [];
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
      if (item.isTotal) {{
        title.textContent = item.assignee;
      }} else {{
        const filterButton = document.createElement("button");
        filterButton.type = "button";
        filterButton.className = "workload-assignee-button";
        filterButton.dataset.assignee = item.assignee;
        filterButton.textContent = item.assignee;
        title.append(filterButton);
      }}
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
      const selectedAssignee = assigneeFilter.value;

      visibleCards.forEach((card) => {{
        if (card.dataset.isClosed === "true") {{
          return;
        }}

        const primaryAssignee = card.dataset.assignee || "未設定";
        const workloadAssignee = selectedAssignee === "__all__" ? primaryAssignee : selectedAssignee;
        if (selectedAssignee !== "__all__" && !cardParticipants(card).includes(selectedAssignee)) {{
          return;
        }}

        // Overall workload stays grouped by primary assignee; filtered workload follows the selected participant.
        const item = workload.get(workloadAssignee) || {{
          assignee: workloadAssignee,
          openCount: 0,
          overdueCount: 0,
          highPriorityCount: 0,
          staleCount: 0,
        }};

        item.openCount += 1;
        item.overdueCount += card.dataset.overdue === "true" ? 1 : 0;
        item.highPriorityCount += card.dataset.highPriority === "true" ? 1 : 0;
        item.staleCount += card.dataset.stale === "true" ? 1 : 0;
        workload.set(workloadAssignee, item);
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
      const selectedIssueIds = selectedIssueIdSet();
      const selectedAssignee = assigneeFilter.value;
      const selectedBallPossession = ballPossessionFilter ? ballPossessionFilter.value : "__all__";
      const selectedVersions = getSelectedVersions();
      let visibleTotal = 0;
      const visibleCards = [];

      document.querySelectorAll(".kanban-column").forEach((column) => {{
        let columnVisibleCount = 0;

        column.querySelectorAll(".issue-card").forEach((card) => {{
          const issueIdMatches = selectedIssueIds === null || selectedIssueIds.has(card.dataset.issueId || "");
          const assigneeMatches = selectedAssignee === "__all__" || cardParticipants(card).includes(selectedAssignee);
          const versionMatches = selectedVersions === null || selectedVersions.has(card.dataset.version);
          const ballPossessionMatches = selectedBallPossession === "__all__" || cardBallPossessionValues(card).includes(selectedBallPossession);
          const matches = issueIdMatches && assigneeMatches && versionMatches && ballPossessionMatches;
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

    function selectAssigneeFilter(assignee) {{
      const hasOption = Array.from(assigneeFilter.options).some((option) => option.value === assignee);
      if (!hasOption) {{
        return;
      }}

      assigneeFilter.value = assignee;
      applyFilters();
      saveKanbanFilterState();
    }}

    function initializeWorkloadAssigneeFilter() {{
      if (!workloadGrid) {{
        return;
      }}

      workloadGrid.addEventListener("click", (event) => {{
        const button = event.target.closest(".workload-assignee-button");
        if (!button || !workloadGrid.contains(button)) {{
          return;
        }}

        selectAssigneeFilter(button.dataset.assignee || "");
      }});
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
      saveKanbanFilterState();
    }}

    function handleVersionCheckboxChange() {{
      if (versionCheckboxes.some((checkbox) => checkbox.checked)) {{
        versionAll.checked = false;
      }} else {{
        versionAll.checked = true;
      }}

      applyFilters();
      saveKanbanFilterState();
    }}

    function resetFilters() {{
      if (issueIdFilter) {{
        issueIdFilter.value = "";
      }}
      assigneeFilter.value = "__all__";
      if (ballPossessionFilter) {{
        ballPossessionFilter.value = "__all__";
      }}
      versionAll.checked = true;
      versionCheckboxes.forEach((checkbox) => {{
        checkbox.checked = false;
      }});
      applyFilters();
      saveKanbanFilterState();
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

    restoreKanbanFilterState();
    if (issueIdFilter) {{
      issueIdFilter.addEventListener("input", () => {{
        applyFilters();
        saveKanbanFilterState();
      }});
    }}
    assigneeFilter.addEventListener("change", () => {{
      applyFilters();
      saveKanbanFilterState();
    }});
    if (ballPossessionFilter) {{
      ballPossessionFilter.addEventListener("change", () => {{
        applyFilters();
        saveKanbanFilterState();
      }});
    }}
    versionAll.addEventListener("change", handleVersionAllChange);
    versionCheckboxes.forEach((checkbox) => checkbox.addEventListener("change", handleVersionCheckboxChange));
    resetFiltersButton.addEventListener("click", resetFilters);
    initializeThemeSelector();
    initializeProjectIdHistory();
    initializeWorkloadAssigneeFilter();
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
        render_kanban_html(issues, redmine_url, project_id, datetime.now(timezone.utc)), encoding="utf-8"
    )
    workload_path = Path(WORKLOAD_HTML).resolve()
    workload_path.write_text(
        render_workload_status_html(issues, redmine_url, project_id), encoding="utf-8"
    )
    combined_path = Path(COMBINED_HTML).resolve()
    combined_path.write_text(render_combined_html(project_id), encoding="utf-8")
    worktime_path = Path(WORKTIME_HTML).resolve()
    today = date.today()
    worktime_path.write_text(
        render_worktime_html([], redmine_url, project_id, today, today), encoding="utf-8"
    )
    quality_path = Path(QUALITY_HTML).resolve()
    quality_path.write_text(
        render_quality_html(issues, redmine_url, project_id, add_months(today, -3), today),
        encoding="utf-8",
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

    if env_flag("USE_SAMPLE_DATA"):
        attach_sub_assignee_names(cache_entry.issues)
    else:
        load_env()
        api_key = os.getenv("REDMINE_API_KEY")
        attach_sub_assignee_names(cache_entry.issues, cache_entry.redmine_url, api_key)

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

    if env_flag("USE_SAMPLE_DATA"):
        attach_sub_assignee_names(issues)
    else:
        attach_sub_assignee_names(issues, redmine_url, api_key)

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


def request_cookie_project_id(headers: Any) -> str | None:
    cookie_header = headers.get("Cookie")
    if not cookie_header:
        return None

    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None

    morsel = cookie.get(PROJECT_ID_COOKIE_NAME)
    if morsel is None:
        return None

    value = morsel.value.strip()
    return value or None


def project_id_cookie_header(project_id: str) -> str:
    cookie = SimpleCookie()
    cookie[PROJECT_ID_COOKIE_NAME] = project_id
    cookie[PROJECT_ID_COOKIE_NAME]["path"] = "/"
    cookie[PROJECT_ID_COOKIE_NAME]["samesite"] = "Lax"
    return cookie.output(header="").strip()


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
) -> tuple[str, str, list[dict[str, Any]], datetime | None]:
    load_env()
    project_id = resolve_project_id(project_id_override)

    with ISSUE_CACHE_LOCK:
        cached = ISSUE_CACHE.get(project_id)
        if cached and refresh_mode is None:
            return (
                cached.redmine_url,
                project_id,
                displayable_issues(cached.issues),
                cached.refreshed_at,
            )

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

    return redmine_url, resolved_project_id, visible_issues, cache_entry.refreshed_at


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
        print("状況確認すること:")
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


def render_loading_html(project_id: str | None, reload_path: str = OUTPUT_HTML) -> str:
    project_id_value = project_id or resolve_project_id(None)
    reload_url = f"/{reload_path}"
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

        if parsed_url.path not in {"/", f"/{OUTPUT_HTML}", f"/{WORKLOAD_HTML}", f"/{COMBINED_HTML}", f"/{WORKTIME_HTML}", f"/{QUALITY_HTML}"}:
            self.send_error(404, "Not Found")
            return

        if parsed_url.path == f"/{WORKLOAD_HTML}":
            response_path = WORKLOAD_HTML
        elif parsed_url.path == f"/{COMBINED_HTML}":
            response_path = COMBINED_HTML
        elif parsed_url.path == f"/{WORKTIME_HTML}":
            response_path = WORKTIME_HTML
        elif parsed_url.path == f"/{QUALITY_HTML}":
            response_path = QUALITY_HTML
        else:
            response_path = OUTPUT_HTML

        query = parse_qs(parsed_url.query)
        query_project_id = request_project_id(query)
        if query_project_id:
            resolved_project_id = resolve_project_id(query_project_id)
            self.send_response(303)
            self.send_header("Location", f"/{response_path}")
            self.send_header("Set-Cookie", project_id_cookie_header(resolved_project_id))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        project_id = request_cookie_project_id(self.headers)

        try:
            resolved_project_id = resolve_project_id(project_id)
            if response_path == WORKTIME_HTML:
                load_env()
                redmine_url = require_env("REDMINE_URL")
                api_key = require_env("REDMINE_API_KEY")
                today = date.today()
                date_from = parse_worktime_date(query.get("from", [None])[0], today)
                date_to = parse_worktime_date(query.get("to", [None])[0], today)
                if date_from > date_to:
                    date_from, date_to = date_to, date_from
                entries = fetch_worktime_entries(
                    redmine_url,
                    api_key,
                    resolved_project_id,
                    date_from,
                    date_to,
                )
                ensure_issue_cache_loaded_from_disk(resolved_project_id)
                with ISSUE_CACHE_LOCK:
                    cached_entry = ISSUE_CACHE.get(resolved_project_id)
                    cached_issues = list(cached_entry.issues) if cached_entry else []
                response_body = render_worktime_html(
                    entries,
                    redmine_url,
                    resolved_project_id,
                    date_from,
                    date_to,
                    issue_fixed_version_map(cached_issues),
                    issue_subject_map(cached_issues),
                )
                status_code = 200
                encoded_body = response_body.encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded_body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded_body)
                return

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
                response_body = render_loading_html(resolved_project_id, response_path)
                status_code = 200
                encoded_body = response_body.encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded_body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded_body)
                return

            redmine_url, resolved_project_id, visible_issues, refreshed_at = load_cached_issue_data(project_id)
            start_background_refresh(
                resolved_project_id,
                refresh_mode="incremental",
                once_per_startup=True,
            )
            if response_path == WORKLOAD_HTML:
                response_body = render_workload_status_html(
                    visible_issues, redmine_url, resolved_project_id
                )
            elif response_path == QUALITY_HTML:
                today = date.today()
                date_from = parse_worktime_date(query.get("from", [None])[0], add_months(today, -3))
                date_to = parse_worktime_date(query.get("to", [None])[0], today)
                if date_from > date_to:
                    date_from, date_to = date_to, date_from
                date_basis = query.get("basis", ["updated"])[0]
                if date_basis not in {"updated", "created"}:
                    date_basis = "updated"
                with ISSUE_CACHE_LOCK:
                    cached_entry = ISSUE_CACHE.get(resolved_project_id)
                    quality_issues = list(cached_entry.issues) if cached_entry else visible_issues
                load_env()
                api_key = os.getenv("REDMINE_API_KEY")
                category_labels = (
                    fetch_custom_field_value_labels(redmine_url, api_key, BUG_CATEGORY_FIELD_NAME)
                    if api_key
                    else {}
                )
                response_body = render_quality_html(
                    quality_issues,
                    redmine_url,
                    resolved_project_id,
                    date_from,
                    date_to,
                    category_labels,
                    date_basis,
                )
            elif response_path == COMBINED_HTML:
                response_body = render_combined_html(resolved_project_id)
            else:
                response_body = render_kanban_html(
                    visible_issues, redmine_url, resolved_project_id, refreshed_at
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
        if parsed_url.path not in {"/project", "/refresh"}:
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length).decode("utf-8")
        form = parse_qs(body)
        project_id = request_project_id(form)

        if parsed_url.path == "/project":
            resolved_project_id = resolve_project_id(project_id)
            self.send_response(303)
            self.send_header("Location", f"/{OUTPUT_HTML}")
            self.send_header("Set-Cookie", project_id_cookie_header(resolved_project_id))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        refresh_mode = form.get("refresh_mode", ["incremental"])[0]
        if refresh_mode not in {"incremental", "full"}:
            refresh_mode = "incremental"

        try:
            print(
                f"[server] 更新リクエスト開始: mode={refresh_mode}, project_id={project_id or resolve_project_id(None)}",
                file=sys.stderr,
                flush=True,
            )
            _, resolved_project_id, _, _ = load_cached_issue_data(
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

        location = f"/{OUTPUT_HTML}"
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", project_id_cookie_header(resolved_project_id))
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
