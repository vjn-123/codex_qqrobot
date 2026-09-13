#!/usr/bin/env python3
import base64
import html
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

try:
    import websocket
except Exception:
    websocket = None

from codex_bridge_client import (
    append_history,
    approval_prompt_text,
    cancel_current_task,
    debug_startup_diagnostics,
    current_runtime,
    create_pending_approval,
    handle_bridge_command,
    job_session_key,
    list_native_sessions,
    list_native_sessions_for_workdir,
    load_state,
    load_dotenv,
    native_session_groups,
    native_session_group_token,
    parse_resume_args,
    prepare_bridge_approval,
    run_codex,
    save_state,
    session_title,
    start_text,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
load_dotenv(BASE_DIR / ".env")

DEFAULT_INTENTS = (1 << 25) | (1 << 26)
USER_AGENT = "CodexRemoteBridge/0.2 QQGateway"
RESUME_CARD_GROUP_PAGE_SIZE = 3
RESUME_CARD_SESSION_PAGE_SIZE = 3


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return int(value, 0)


def env_csv(name: str) -> Set[str]:
    raw = os.getenv(name, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def parse_intents(raw: str) -> int:
    raw = raw.strip()
    if not raw:
        return DEFAULT_INTENTS

    total = 0
    for part in re.split(r"[|,+]", raw):
        part = part.strip()
        if not part:
            continue
        if "<<" in part:
            left, right = part.split("<<", 1)
            if int(left.strip(), 0) != 1:
                raise ValueError(f"only 1<<n intent expressions are supported: {part}")
            total |= 1 << int(right.strip(), 0)
        else:
            total |= int(part, 0)
    return total


QQ_APP_ID = os.getenv("QQ_APP_ID", "").strip()
QQ_APP_SECRET = os.getenv("QQ_APP_SECRET", "").strip()
QQ_AUTH_URL = os.getenv("QQ_AUTH_URL", "https://bots.qq.com/app/getAppAccessToken").strip()
QQ_API_BASE = os.getenv("QQ_API_BASE", "https://api.sgroup.qq.com").rstrip("/")
QQ_GATEWAY_PATH = os.getenv("QQ_GATEWAY_PATH", "/gateway").strip() or "/gateway"
QQ_GATEWAY_INTENTS = parse_intents(os.getenv("QQ_GATEWAY_INTENTS", str(DEFAULT_INTENTS)))
QQ_REPLY_MAX_CHARS = env_int("QQ_REPLY_MAX_CHARS", 1500)
QQ_MAX_REPLY_CHUNKS = max(1, min(5, env_int("QQ_MAX_REPLY_CHUNKS", 5)))
QQ_JOB_QUEUE_SIZE = env_int("QQ_JOB_QUEUE_SIZE", 5)
QQ_CODEX_MAX_PARALLEL = max(1, min(6, env_int("QQ_CODEX_MAX_PARALLEL", 5)))
QQ_TASK_STATUS_INTERVAL_SECONDS = max(15, env_int("QQ_TASK_STATUS_INTERVAL_SECONDS", 300))
QQ_TASK_PARTIAL_INTERVAL_SECONDS = max(15, env_int("QQ_TASK_PARTIAL_INTERVAL_SECONDS", 60))
QQ_TASK_PARTIAL_MAX_CHARS = max(200, env_int("QQ_TASK_PARTIAL_MAX_CHARS", 1200))
QQ_SEND_PARTIAL_OUTPUTS = env_bool("QQ_SEND_PARTIAL_OUTPUTS", False)
QQ_SHOW_TASK_CONTEXT_ON_FINAL = env_bool("QQ_SHOW_TASK_CONTEXT_ON_FINAL", True)
QQ_TRUNCATE_LONG_REPLIES = env_bool("QQ_TRUNCATE_LONG_REPLIES", True)
QQ_RECONNECT_SECONDS = env_int("QQ_RECONNECT_SECONDS", 5)
QQ_DEDUP_SECONDS = env_int("QQ_DEDUP_SECONDS", 600)
QQ_SEND_PROCESSING_MESSAGE = env_bool("QQ_SEND_PROCESSING_MESSAGE", False)
QQ_PROCESSING_TEXT = os.getenv("QQ_PROCESSING_TEXT", "收到，正在处理。").strip()
QQ_USE_RESUME = env_bool("QQ_USE_RESUME", True)
QQ_SEND_UNION_APPID_HEADER = env_bool("QQ_SEND_UNION_APPID_HEADER", True)
QQ_ALLOWED_EVENTS = env_csv("QQ_ALLOWED_EVENTS") or {
    "C2C_MESSAGE_CREATE",
    "GROUP_AT_MESSAGE_CREATE",
    "INTERACTION_CREATE",
}
QQ_ALLOWED_USER_OPENIDS = env_csv("QQ_ALLOWED_USER_OPENIDS")
QQ_ALLOWED_GROUP_OPENIDS = env_csv("QQ_ALLOWED_GROUP_OPENIDS")
QQ_BUTTON_ACTION_TYPE = env_int("QQ_BUTTON_ACTION_TYPE", 2)
QQ_BUTTON_AUTO_ENTER = env_bool("QQ_BUTTON_AUTO_ENTER", True)
QQ_SEND_STARTUP_TO_ALLOWED_USERS = env_bool("QQ_SEND_STARTUP_TO_ALLOWED_USERS", True)
QQ_ATTACHMENT_DOWNLOAD = env_bool("QQ_ATTACHMENT_DOWNLOAD", True)
QQ_ATTACHMENT_MAX_COUNT = max(1, min(10, env_int("QQ_ATTACHMENT_MAX_COUNT", 4)))
QQ_ATTACHMENT_MAX_BYTES = max(1024 * 1024, env_int("QQ_ATTACHMENT_MAX_BYTES", 25 * 1024 * 1024))
QQ_SEND_LOCAL_IMAGES = env_bool("QQ_SEND_LOCAL_IMAGES", True)
QQ_SEND_IMAGE_MAX_COUNT = max(1, min(10, env_int("QQ_SEND_IMAGE_MAX_COUNT", 4)))
QQ_SEND_IMAGE_MAX_BYTES = max(1024 * 1024, env_int("QQ_SEND_IMAGE_MAX_BYTES", 10 * 1024 * 1024))
QQ_RESTART_DELAY_SECONDS = max(1, min(30, env_int("QQ_RESTART_DELAY_SECONDS", 2)))
auto_start_sent_contacts: Set[str] = set()
auto_start_lock = threading.Lock()
send_partial_outputs = QQ_SEND_PARTIAL_OUTPUTS
show_task_context_on_final = QQ_SHOW_TASK_CONTEXT_ON_FINAL
truncate_long_replies = QQ_TRUNCATE_LONG_REPLIES
task_status_interval_seconds = QQ_TASK_STATUS_INTERVAL_SECONDS
task_output_settings_lock = threading.Lock()


def save_runtime_setting(name: str, value: Any) -> None:
    state = load_state()
    state[name] = value
    save_state(state)


def initialize_runtime_settings() -> None:
    global truncate_long_replies, task_status_interval_seconds
    state = load_state()
    changed = False
    with task_output_settings_lock:
        if "truncate_long_replies" in state:
            truncate_long_replies = bool(state.get("truncate_long_replies"))
        else:
            state["truncate_long_replies"] = truncate_long_replies
            changed = True
        if "task_status_interval_seconds" in state:
            task_status_interval_seconds = max(0, int(state.get("task_status_interval_seconds") or 0))
        else:
            state["task_status_interval_seconds"] = task_status_interval_seconds
            changed = True
    if changed:
        save_state(state)


initialize_runtime_settings()


def mark_auto_start_sent(contact_id: str) -> bool:
    contact_id = str(contact_id or "").strip()
    if not contact_id:
        return False
    with auto_start_lock:
        if contact_id in auto_start_sent_contacts:
            return False
        auto_start_sent_contacts.add(contact_id)
        return True


def is_cancel_command(text: str) -> bool:
    command, _ = split_command(text)
    return command == "/cancel"


def is_restart_command(text: str) -> bool:
    command, _ = split_command(text)
    return command == "/restart"


def schedule_restart(reason: str = "remote command") -> None:
    def restart_later() -> None:
        print(f"[restart] scheduled reason={reason} delay={QQ_RESTART_DELAY_SECONDS}s", flush=True)
        time.sleep(QQ_RESTART_DELAY_SECONDS)
        os._exit(0)

    threading.Thread(target=restart_later, name="qq-remote-restart", daemon=True).start()


def split_command(text: str) -> tuple[str, str]:
    text = (text or "").strip()
    if not text.startswith("/"):
        return "", ""
    command, _, args = text.partition(" ")
    return command.lower().strip(), args.strip()


def is_allowed_interaction_user(data: Dict[str, Any]) -> bool:
    openid = first_string(data, {"user_openid", "group_member_openid"}).strip()
    if QQ_ALLOWED_USER_OPENIDS and not openid:
        return False
    if not openid:
        return True
    return not QQ_ALLOWED_USER_OPENIDS or openid in QQ_ALLOWED_USER_OPENIDS


def first_string(value: Any, keys: Set[str]) -> str:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        for item in value.values():
            found = first_string(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = first_string(item, keys)
            if found:
                return found
    return ""


def collect_strings(value: Any, keys: Set[str]) -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, str) and item.strip():
                found.append(item.strip())
            found.extend(collect_strings(item, keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(collect_strings(item, keys))
    return found


def safe_filename(name: str, fallback: str) -> str:
    name = unquote((name or "").split("?", 1)[0].split("#", 1)[0]).strip()
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = fallback
    return name[:120]


def filename_from_url(url: str, index: int, content_type: str = "") -> str:
    parsed = urlparse(url)
    name = safe_filename(Path(parsed.path).name, f"attachment-{index}")
    if "." not in name:
        ext_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }
        name += ext_map.get(content_type.lower().split(";", 1)[0].strip(), ".bin")
    return name


def collect_attachment_candidates(value: Any) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen: Set[str] = set()
    container_keys = {"attachments", "attachment", "images", "image", "files", "file", "media"}
    url_keys = {"url", "file_url", "download_url", "image_url", "src", "proxy_url"}
    attachment_markers = {"filename", "file_name", "content_type", "size", "width", "height"}

    def add(url: str, kind: str = "", name: str = "") -> None:
        url = (url or "").strip()
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return
        if url in seen:
            return
        seen.add(url)
        candidates.append({"url": url, "kind": kind.strip(), "name": name.strip()})

    def walk(item: Any, hint: str = "") -> None:
        if isinstance(item, dict):
            local_hint = hint
            item_type = str(item.get("type") or item.get("content_type") or item.get("file_type") or "").lower()
            if "image" in item_type:
                local_hint = "image"
            if not local_hint and (attachment_markers & set(str(key) for key in item.keys())):
                local_hint = "file"
            name = first_string(item, {"filename", "file_name", "name"})
            for key in url_keys:
                value = item.get(key)
                if isinstance(value, str) and (local_hint or key != "url"):
                    key_hint = "image" if "image" in key.lower() or local_hint == "image" else local_hint
                    add(value, key_hint, name)
            for key in container_keys:
                if key in item:
                    next_hint = "image" if "image" in key.lower() else local_hint
                    walk(item.get(key), next_hint)
        elif isinstance(item, list):
            for child in item:
                walk(child, hint)
        elif isinstance(item, str) and hint:
            add(item, hint)

    walk(value)
    return candidates[:QQ_ATTACHMENT_MAX_COUNT]


def download_attachments(job_id: str, data: Dict[str, Any]) -> List[Dict[str, str]]:
    if not QQ_ATTACHMENT_DOWNLOAD:
        return []
    attachments: List[Dict[str, str]] = []
    candidates = collect_attachment_candidates(data)
    if not candidates:
        return []

    target_dir = ATTACHMENTS_DIR / safe_filename(job_id, uuid.uuid4().hex)
    target_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    for index, candidate in enumerate(candidates, 1):
        url = candidate["url"]
        try:
            req = Request(url, headers=headers, method="GET")
            with urlopen(req, timeout=30) as resp:
                content_type = str(resp.headers.get("Content-Type") or "")
                content_length = str(resp.headers.get("Content-Length") or "").strip()
                if content_length and int(content_length) > QQ_ATTACHMENT_MAX_BYTES:
                    raise RuntimeError(f"file too large: {content_length} bytes")
                filename = safe_filename(candidate.get("name", ""), "")
                if not filename:
                    filename = filename_from_url(url, index, content_type)
                path = target_dir / filename
                total = 0
                with path.open("wb") as f:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > QQ_ATTACHMENT_MAX_BYTES:
                            raise RuntimeError(f"file too large: >{QQ_ATTACHMENT_MAX_BYTES} bytes")
                        f.write(chunk)
            attachments.append({
                "path": str(path.resolve()),
                "url": url,
                "kind": candidate.get("kind", ""),
                "content_type": content_type,
                "bytes": str(total),
            })
            print(f"[qq-attachment] saved {path} bytes={total}", flush=True)
        except Exception as exc:
            print(f"[qq-attachment-error] url={url[:120]} {exc}", flush=True)
            attachments.append({
                "path": "",
                "url": url,
                "kind": candidate.get("kind", ""),
                "error": str(exc),
            })
    return attachments


def format_attachments_for_codex(attachments: List[Dict[str, str]]) -> str:
    if not attachments:
        return ""
    lines = ["", "用户随消息附带了以下本地附件。请在需要时直接读取这些本地路径；图片可以按视觉内容分析："]
    for index, item in enumerate(attachments, 1):
        path = item.get("path", "")
        if path:
            label = item.get("kind") or item.get("content_type") or "file"
            lines.append(f"{index}. {label}: {path}")
        else:
            lines.append(f"{index}. 附件下载失败：{item.get('error', 'unknown error')} | {item.get('url', '')}")
    return "\n".join(lines)


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def is_local_image_path(value: str) -> bool:
    raw = (value or "").strip().strip('"').strip("'")
    if not raw or re.match(r"^[a-z]+://", raw, flags=re.IGNORECASE):
        return False
    path = Path(raw)
    return path.suffix.lower() in IMAGE_EXTENSIONS and path.exists() and path.is_file()


def parse_outbound_images(text: str) -> tuple[str, List[str]]:
    if not QQ_SEND_LOCAL_IMAGES:
        return text, []
    image_paths: List[str] = []

    def add_path(raw: str) -> None:
        raw = (raw or "").strip().strip("<>").strip()
        if is_local_image_path(raw):
            resolved = str(Path(raw).resolve())
            if resolved not in image_paths:
                image_paths.append(resolved)

    remaining_lines: List[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        match = re.match(r"^(?:SEND_IMAGE|IMAGE|图片|发图)\s*[:：]\s*(.+)$", stripped, flags=re.IGNORECASE)
        if match:
            add_path(match.group(1))
            continue
        remaining_lines.append(line)

    remaining = "\n".join(remaining_lines)
    markdown_pattern = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

    def replace_markdown(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        add_path(target)
        return "" if is_local_image_path(target) else match.group(0)

    remaining = markdown_pattern.sub(replace_markdown, remaining).strip()
    return remaining, image_paths[:QQ_SEND_IMAGE_MAX_COUNT]


def extract_button_command(data: Dict[str, Any]) -> str:
    def normalize_candidate(value: str) -> str:
        value = value.strip()
        if value.startswith("cmd:/"):
            return value[4:].strip()
        if value.startswith("/"):
            return value
        return ""

    nested = data.get("data")
    if isinstance(nested, dict):
        resolved = nested.get("resolved")
        if isinstance(resolved, dict):
            for key in ("button_data", "data", "value", "command", "custom_id"):
                value = resolved.get(key)
                if isinstance(value, str) and value.strip():
                    command = normalize_candidate(value)
                    if command:
                        return command

    for candidate in collect_strings(data, {"button_data", "data", "value", "command", "custom_id"}):
        command = normalize_candidate(candidate)
        if command:
            return command
    return ""


def interaction_reply(data: Dict[str, Any], interaction_id: str) -> Optional[Dict[str, str]]:
    group_openid = first_string(data, {"group_openid"})
    user_openid = first_string(data, {"user_openid", "group_member_openid"})
    channel_id = first_string(data, {"channel_id"})
    guild_id = first_string(data, {"guild_id"})
    msg_id = first_string(data, {"message_id", "msg_id"})

    if group_openid:
        reply = {"kind": "group", "group_openid": group_openid}
    elif user_openid:
        reply = {"kind": "c2c", "openid": user_openid}
    elif channel_id:
        reply = {"kind": "channel", "channel_id": channel_id}
    elif guild_id:
        reply = {"kind": "dm", "guild_id": guild_id}
    else:
        return None

    if msg_id:
        reply["msg_id"] = msg_id
    return reply


def extract_interaction_job(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    interaction_id = str(data.get("id") or "").strip()
    command = clean_content(extract_button_command(data))
    if not interaction_id or not command:
        print(
            f"[gateway] interaction ignored keys={','.join(sorted(data.keys()))}",
            flush=True,
        )
        return None

    if not is_allowed_interaction_user(data):
        user_openid = first_string(data, {"user_openid", "group_member_openid"})
        print(f"[qq] blocked interaction user {user_openid[:12]}", flush=True)
        return None

    reply = interaction_reply(data, interaction_id)
    if not reply:
        print(
            f"[gateway] interaction has no reply target id={interaction_id[:12]} keys={','.join(sorted(data.keys()))}",
            flush=True,
        )
        return None

    if reply["kind"] == "group":
        source = "qq:group:" + reply["group_openid"][:12]
    elif reply["kind"] == "c2c":
        source = "qq:c2c:" + reply["openid"][:12]
    elif reply["kind"] == "channel":
        source = "qq:channel:" + reply["channel_id"]
    else:
        source = "qq:dm:" + reply["guild_id"]

    return {
        "id": uuid.uuid4().hex,
        "source": "qq-gateway",
        "from": source,
        "text": command,
        "event_type": "INTERACTION_CREATE",
        "msg_id": interaction_id,
        "interaction_id": interaction_id,
        "reply": reply,
    }


def sanitize_button_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "-", (value or "").strip())
    value = value.strip("-")
    return value[:64] or uuid.uuid4().hex[:8]


def shorten_label(text: str, limit: int = 18) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def compact_time(text: Any) -> str:
    value = str(text or "").strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", value)
    if match:
        return f"{match.group(2)}-{match.group(3)} {match.group(4)}:{match.group(5)}"
    return value[:16]


def short_id(value: str, size: int = 6) -> str:
    value = (value or "").strip()
    return value[-size:] if len(value) > size else value


def short_cwd_label(cwd: str) -> str:
    cwd = re.sub(r"\s+", " ", (cwd or "").strip())
    if not cwd:
        return "未记录目录"
    path = Path(cwd)
    name = path.name or cwd
    parent = path.parent.name if path.parent != path else ""

    replacements = {
        "files-mentioned-by-the-user": "files",
    }
    for raw, replacement in replacements.items():
        if name.startswith(raw):
            name = name.replace(raw, replacement, 1)
            break
    if name.startswith("codex-home-"):
        name = "codex-home"

    if len(name) > 22:
        name = shorten_label(name, 22)
    if parent and parent not in {"", ".", "\\"}:
        if len(parent) > 18:
            parent = shorten_label(parent, 18)
        return f"{name} / {parent}"
    return name


def clean_card_title(text: str, fallback: str) -> str:
    title = re.sub(r"\s+", " ", (text or "").strip())
    if not title or title.lower() in {"codex", "codex cli", "codex desktop", "codex desktop app"}:
        return fallback
    title = re.sub(r"^#*\s*", "", title).strip()
    return title or fallback


def chunked(items: List[Any], size: int) -> List[List[Any]]:
    if size <= 0:
        return [list(items)]
    return [items[index:index + size] for index in range(0, len(items), size)]


def keyboard_button(
    label: str,
    data: str,
    *,
    button_id: Optional[str] = None,
    visited_label: Optional[str] = None,
    style: int = 1,
) -> Dict[str, Any]:
    label = label.strip() or "action"
    visited_label = (visited_label or label).strip() or label
    return {
        "id": button_id or sanitize_button_id(data),
        "render_data": {
            "label": label,
            "visited_label": visited_label,
            "style": style,
        },
        "action": {
            "type": QQ_BUTTON_ACTION_TYPE,
            "permission": {"type": 2},
            "data": data,
            "reply": False,
            "enter": QQ_BUTTON_AUTO_ENTER,
            "unsupport_tips": "当前客户端不支持按钮",
        },
    }


def parse_page_arg(args: str) -> int:
    parsed = parse_resume_args(args)
    return max(1, int(parsed.get("page", 1)))


def build_resume_card(args: str = "") -> Dict[str, Any]:
    parsed = parse_resume_args(args)
    page = max(1, int(parsed.get("page", 1)))
    runtime = current_runtime()
    lines: List[str] = []
    buttons: List[Dict[str, Any]] = []
    total = 0
    max_page = 1

    if runtime["context_mode"] == "native":
        active = runtime["active_native_session_id"] or ""
        if parsed["mode"] == "sessions":
            cwd = str(parsed.get("cwd", "")).strip()
            all_groups = native_session_groups(limit=500)
            group = next((item for item in all_groups if str(item.get("cwd", "")) == cwd), None)
            sessions = list((group or {}).get("items", []))
            sessions.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
            total = len(sessions)
            max_page = max(1, (total + RESUME_CARD_SESSION_PAGE_SIZE - 1) // RESUME_CARD_SESSION_PAGE_SIZE)
            page = min(page, max_page)
            start = (page - 1) * RESUME_CARD_SESSION_PAGE_SIZE
            current_sessions = sessions[start:start + RESUME_CARD_SESSION_PAGE_SIZE]
            dir_name = short_cwd_label(cwd)
            lines.append(f"选择会话：{dir_name}")
            lines.append(f"最近回复在前 · 第 {page}/{max_page} 页 · 共 {total} 个")
            for offset, item in enumerate(current_sessions, start + 1):
                session_id = str(item.get("id", ""))
                if not session_id:
                    continue
                fallback = "会话"
                title = clean_card_title(session_title(item, session_id), fallback)
                label = shorten_label(f"{title} #{short_id(session_id)}", 26)
                buttons.append(
                    keyboard_button(
                        label,
                        f"/resume #{short_id(session_id)}",
                        button_id=f"resume-session-{short_id(session_id)}",
                        visited_label=label,
                        style=1 if session_id == active else 0,
                    )
                )
            back_command = "/resume"
            page_prefix = f"/resume d @{native_session_group_token(cwd)}"
        else:
            all_groups = native_session_groups(limit=500)
            total = len(all_groups)
            max_page = max(1, (total + RESUME_CARD_GROUP_PAGE_SIZE - 1) // RESUME_CARD_GROUP_PAGE_SIZE)
            page = min(page, max_page)
            start = (page - 1) * RESUME_CARD_GROUP_PAGE_SIZE
            groups = all_groups[start:start + RESUME_CARD_GROUP_PAGE_SIZE]
            lines.append("选择目录")
            lines.append(f"最近回复在前 · 第 {page}/{max_page} 页 · 共 {total} 个")
            for group in groups:
                cwd = str(group.get("cwd", "")).strip()
                count = int(group.get("count", 0))
                button_label = shorten_label(f"{short_cwd_label(cwd)} ({count})", 24)
                buttons.append(
                    keyboard_button(
                        button_label,
                        f"/resume d @{native_session_group_token(cwd)}",
                        button_id=f"resume-dir-{native_session_group_token(cwd)}",
                        visited_label=button_label,
                        style=0,
                    )
                )
            back_command = ""
            page_prefix = "/resume"
    else:
        state = load_state()
        active = str(state["active_session_id"])
        all_sessions = sorted(
            state["sessions"].values(),
            key=lambda item: str(item.get("updated_at", "")),
            reverse=True,
        )
        total = len(all_sessions)
        max_page = max(1, (total + RESUME_CARD_SESSION_PAGE_SIZE - 1) // RESUME_CARD_SESSION_PAGE_SIZE)
        page = min(page, max_page)
        start = (page - 1) * RESUME_CARD_SESSION_PAGE_SIZE
        sessions = all_sessions[start:start + RESUME_CARD_SESSION_PAGE_SIZE]
        lines.append("当前本地会话")
        lines.append(f"最近回复在前 · 第 {page}/{max_page} 页 · 共 {total} 个")
        for offset, item in enumerate(sessions, start + 1):
            session_id = str(item.get("id", ""))
            if not session_id:
                continue
            title = clean_card_title(session_title(item, session_id), "会话")
            label = shorten_label(f"{title} #{short_id(session_id)}", 26)
            buttons.append(
                keyboard_button(
                    label,
                    f"/resume #{short_id(session_id)}",
                    button_id=f"resume-{short_id(session_id)}",
                    visited_label=label,
                    style=1 if session_id == active else 0,
                )
            )
        back_command = ""
        page_prefix = "/resume"

    control_buttons = []
    if back_command:
        control_buttons.append(keyboard_button("目录", back_command, button_id="resume-back", style=0))
    if max_page > 1:
        if page > 1:
            prev_command = f"{page_prefix} p {page - 1}"
            control_buttons.append(keyboard_button("上一页", prev_command, button_id="resume-prev", style=0))
        if page < max_page:
            next_command = f"{page_prefix} p {page + 1}"
            control_buttons.append(keyboard_button("下一页", next_command, button_id="resume-next", style=0))
    if not control_buttons:
        control_buttons.append(keyboard_button("刷新", f"{page_prefix} p {page}", button_id="resume-refresh", style=1))

    rows: List[Dict[str, Any]] = [{"buttons": control_buttons[:3]}]
    if parsed["mode"] == "groups":
        rows.extend({"buttons": row_buttons} for row_buttons in chunked(buttons, 1))
    else:
        rows.extend({"buttons": row_buttons} for row_buttons in chunked(buttons, 1))

    return {
        "markdown": "\n".join(lines + ([] if buttons else ["没有可用会话。"])),
        "keyboard": {"rows": rows},
    }


def build_model_card() -> Dict[str, Any]:
    runtime = current_runtime()
    model = runtime["model"]
    reasoning = runtime["reasoning_effort"]

    rows = [
        {
            "buttons": [
                keyboard_button(
                    "gpt-5.5",
                    "/model gpt-5.5",
                    button_id="model-gpt-5.5",
                    style=1 if model == "gpt-5.5" else 0,
                ),
                keyboard_button(
                    "gpt-5.4",
                    "/model gpt-5.4",
                    button_id="model-gpt-5.4",
                    style=1 if model == "gpt-5.4" else 0,
                ),
            ]
        },
        {
            "buttons": [
                keyboard_button("low", "/model low", button_id="reason-low", style=1 if reasoning == "low" else 0),
                keyboard_button("medium", "/model medium", button_id="reason-medium", style=1 if reasoning == "medium" else 0),
                keyboard_button("high", "/model high", button_id="reason-high", style=1 if reasoning == "high" else 0),
                keyboard_button("xhigh", "/model xhigh", button_id="reason-xhigh", style=1 if reasoning == "xhigh" else 0),
            ]
        },
        {
            "buttons": [
                keyboard_button("状态", "/status", button_id="model-status", style=0),
                keyboard_button("帮助", "/help", button_id="model-help", style=0),
            ]
        },
    ]

    return {
        "markdown": "\n".join([
            "当前模型",
            f"model: {model}",
            f"reasoning: {reasoning}",
            "点击按钮即可切换。",
        ]),
        "keyboard": {"rows": rows},
    }


def build_start_card() -> Dict[str, Any]:
    runtime = current_runtime()
    rows = [
        {
            "buttons": [
                keyboard_button("Codex会话列表", "/resume", button_id="start-resume", style=1),
                keyboard_button("模型设置", "/model", button_id="start-model", style=1),
                keyboard_button("设置", "/setup", button_id="start-setup", style=0),
            ]
        },
        {
            "buttons": [
                keyboard_button("用户信息", "/whoami", button_id="start-whoami", style=0),
                keyboard_button("状态", "/status", button_id="start-status", style=0),
                keyboard_button("帮助", "/help", button_id="start-help", style=0),
            ]
        },
    ]
    return {
        "markdown": "\n".join([
            "Codex Remote Bridge",
            "我是你的远程 Codex 助手，可以通过 QQ 帮你查看和切换 Codex 会话、继续对话、调整模型、查看状态，并把需要审批的操作发回给你确认。",
            f"model: {runtime['model']}",
            f"reasoning: {runtime['reasoning_effort']}",
            "点击按钮开始使用。",
        ]),
        "keyboard": {"rows": rows},
    }


def build_workdir_card() -> Dict[str, Any]:
    runtime = current_runtime()
    workdir = str(runtime.get("workdir", "")).strip()
    sessions = list_native_sessions_for_workdir(workdir, limit=5)
    lines = [
        "Codex 工作目录",
        workdir or "（未设置）",
        f"原生会话数：{len(sessions)}",
    ]
    if sessions:
        lines.append("点击会话可恢复。恢复前直接发普通消息，会在此目录新建会话。")
    else:
        lines.append("这里没有找到原生会话。下一条普通消息会新建会话。")

    buttons: List[Dict[str, Any]] = []
    active = str(runtime.get("active_native_session_id") or "")
    for item in sessions:
        session_id = str(item.get("id", ""))
        if not session_id:
            continue
        title = clean_card_title(session_title(item, session_id), "Session")
        label = shorten_label(f"{title} #{short_id(session_id)}", 26)
        buttons.append(
            keyboard_button(
                label,
                f"/resume #{short_id(session_id)}",
                button_id=f"workdir-resume-{short_id(session_id)}",
                visited_label=label,
                style=1 if session_id == active else 0,
            )
        )

    rows: List[Dict[str, Any]] = [
        {
            "buttons": [
                keyboard_button("刷新", "/workdir", button_id="workdir-refresh", style=1),
                keyboard_button("全部会话", "/resume", button_id="workdir-resume-all", style=0),
                keyboard_button("状态", "/status", button_id="workdir-status", style=0),
            ]
        }
    ]
    rows.extend({"buttons": row_buttons} for row_buttons in chunked(buttons, 1))
    return {
        "markdown": "\n".join(lines),
        "keyboard": {"rows": rows},
    }


def build_setup_card() -> Dict[str, Any]:
    runtime = current_runtime()
    with task_output_settings_lock:
        partial_on = send_partial_outputs
        context_on = show_task_context_on_final
        truncate_on = truncate_long_replies
        heartbeat_seconds = task_status_interval_seconds
    timeout_minutes = int((int(runtime.get("timeout_seconds") or 0) + 59) / 60)
    recent_default = int(runtime.get("recent_default_count") or 5)
    rows = [
        {
            "buttons": [
                keyboard_button("超时设置", "/timeout", button_id="setup-timeout", style=0),
                keyboard_button("任务提醒频率", "/heartbeat", button_id="setup-heartbeat", style=0),
            ]
        },
        {
            "buttons": [
                keyboard_button("最近对话默认条数", "/recent-default", button_id="setup-recent-default", style=0),
                keyboard_button("权限设置", "/permission", button_id="setup-permission", style=0),
            ]
        },
        {
            "buttons": [
                keyboard_button(
                    "关闭阶段性输出" if partial_on else "开启阶段性输出",
                    "/output stage off" if partial_on else "/output stage on",
                    button_id="setup-stage-output-toggle",
                    style=1 if partial_on else 0,
                ),
                keyboard_button(
                    "关闭最终输出带用户输入" if context_on else "开启最终输出带用户输入",
                    "/output userContext off" if context_on else "/output userContext on",
                    button_id="setup-user-context-toggle",
                    style=1 if context_on else 0,
                ),
            ]
        },
        {
            "buttons": [
                keyboard_button(
                    "关闭长内容截断" if truncate_on else "开启长内容截断",
                    "/truncate off" if truncate_on else "/truncate on",
                    button_id="setup-truncate-toggle",
                    style=1 if truncate_on else 0,
                ),
            ]
        },
    ]
    return {
        "markdown": "\n".join([
            "设置",
            f"超时设置：{timeout_minutes} 分钟",
            f"任务提醒频率：{format_duration(heartbeat_seconds)}",
            f"最近对话默认条数：{recent_default}",
            f"阶段性输出：{'开' if partial_on else '关'}",
            f"最终输出带用户输入：{'开' if context_on else '关'}",
            f"长内容截断：{'开' if truncate_on else '关'}",
            "权限设置",
            "",
            "命令：",
            "/timeout - 超时设置",
            "/heartbeat - 任务提醒频率",
            "/recent-default - 最近对话默认条数",
            "/output stage on|off - 开关阶段性输出",
            "/output userContext on|off - 开关最终输出带用户输入",
            "/truncate on|off - 开关长内容截断",
            "/permission - 权限设置",
        ]),
        "keyboard": {"rows": rows},
    }


def build_timeout_card() -> Dict[str, Any]:
    runtime = current_runtime()
    timeout_minutes = int((int(runtime.get("timeout_seconds") or 0) + 59) / 60)
    buttons = [
        keyboard_button("15分钟", "/timeout 15", button_id="timeout-15", style=1 if timeout_minutes == 15 else 0),
        keyboard_button("30分钟", "/timeout 30", button_id="timeout-30", style=1 if timeout_minutes == 30 else 0),
        keyboard_button("1小时", "/timeout 60", button_id="timeout-60", style=1 if timeout_minutes == 60 else 0),
        keyboard_button("5小时", "/timeout 300", button_id="timeout-300", style=1 if timeout_minutes == 300 else 0),
        keyboard_button("24小时", "/timeout 1440", button_id="timeout-1440", style=1 if timeout_minutes == 1440 else 0),
    ]
    return {
        "markdown": "\n".join([
            "超时设置",
            f"当前超时：{timeout_minutes} 分钟",
            "也可以手动发送 /timeout 分钟数。",
        ]),
        "keyboard": {"rows": [{"buttons": row} for row in chunked(buttons, 2)]},
    }


def build_permission_card() -> Dict[str, Any]:
    runtime = current_runtime()
    permission = str(runtime.get("permission") or "")
    buttons = [
        keyboard_button("只读", "/permission read only", button_id="permission-read", style=1 if permission == "read-only" else 0),
        keyboard_button("请求批准", "/permission ask", button_id="permission-ask", style=1 if permission == "ask" else 0),
        keyboard_button("替我审批", "/permission auto", button_id="permission-auto", style=1 if permission == "auto" else 0),
        keyboard_button("完全权限", "/permission full", button_id="permission-full", style=1 if permission == "full" else 0),
    ]
    return {
        "markdown": "\n".join([
            "权限设置",
            f"当前权限：{runtime.get('permission_profile', {}).get('label', permission)}",
            "请求批准会先把普通任务发给你确认；替我审批使用 Codex 自动审批审查；完全权限风险最高。",
            "点击按钮即可切换。",
        ]),
        "keyboard": {"rows": [{"buttons": row} for row in chunked(buttons, 2)]},
    }


def build_heartbeat_card() -> Dict[str, Any]:
    with task_output_settings_lock:
        current = task_status_interval_seconds
    options = [
        ("关闭", 0),
        ("1分钟", 60),
        ("5分钟", 300),
        ("10分钟", 600),
        ("30分钟", 1800),
    ]
    buttons = [
        keyboard_button(label, f"/heartbeat {seconds // 60}" if seconds else "/heartbeat off", button_id=f"heartbeat-{seconds}", style=1 if current == seconds else 0)
        for label, seconds in options
    ]
    return {
        "markdown": "\n".join([
            "任务提醒频率",
            f"当前频率：{'关' if current <= 0 else format_duration(current)}",
            "也可以手动发送 /heartbeat 分钟数，发送 /heartbeat off 可关闭。",
        ]),
        "keyboard": {"rows": [{"buttons": row} for row in chunked(buttons, 2)]},
    }


def build_recent_default_card() -> Dict[str, Any]:
    runtime = current_runtime()
    current = int(runtime.get("recent_default_count") or 5)
    buttons = [
        keyboard_button("5条", "/recent-default 5", button_id="recent-default-5", style=1 if current == 5 else 0),
        keyboard_button("10条", "/recent-default 10", button_id="recent-default-10", style=1 if current == 10 else 0),
        keyboard_button("15条", "/recent-default 15", button_id="recent-default-15", style=1 if current == 15 else 0),
        keyboard_button("20条", "/recent-default 20", button_id="recent-default-20", style=1 if current == 20 else 0),
    ]
    return {
        "markdown": "\n".join([
            "最近对话默认条数",
            f"当前默认：{current} 条",
            "影响 /recent 和 /last user/codex 不带数量时的默认展示条数。",
        ]),
        "keyboard": {"rows": [{"buttons": row} for row in chunked(buttons, 2)]},
    }


def build_truncate_card() -> Dict[str, Any]:
    with task_output_settings_lock:
        enabled = truncate_long_replies
    return {
        "markdown": "\n".join([
            "长内容截断",
            f"当前状态：{'开' if enabled else '关'}",
            "开启时超长回复会按最大分片数截断；关闭时会尽量分段发送完整内容。",
        ]),
        "keyboard": {
            "rows": [
                {
                    "buttons": [
                        keyboard_button("开启长内容截断", "/truncate on", button_id="truncate-on", style=1 if enabled else 0),
                        keyboard_button("关闭长内容截断", "/truncate off", button_id="truncate-off", style=0 if enabled else 1),
                    ]
                }
            ]
        },
    }


def send_startup_start_messages(api: "QQApi") -> None:
    if not QQ_SEND_STARTUP_TO_ALLOWED_USERS:
        print("[qq-startup-start] skipped by QQ_SEND_STARTUP_TO_ALLOWED_USERS=0", flush=True)
        return
    if not QQ_ALLOWED_USER_OPENIDS:
        print("[qq-startup-start] skipped because QQ_ALLOWED_USER_OPENIDS is empty", flush=True)
        return

    for index, openid in enumerate(sorted(QQ_ALLOWED_USER_OPENIDS), 1):
        reply = {"kind": "c2c", "openid": openid}
        contact_id = "qq:c2c:" + openid[:12]
        card = build_start_card()
        try:
            api.send_markdown_keyboard(reply, card["markdown"], card["keyboard"], index)
            mark_auto_start_sent(contact_id)
            print(f"[qq-startup-start] sent markdown to user={openid[:12]}", flush=True)
            continue
        except Exception as exc:
            print(f"[qq-startup-start-error] markdown user={openid[:12]} {exc}", flush=True)

        try:
            api.send_message(reply, start_text(), index)
            mark_auto_start_sent(contact_id)
            print(f"[qq-startup-start] sent text to user={openid[:12]}", flush=True)
        except Exception as exc:
            print(f"[qq-startup-start-error] text user={openid[:12]} {exc}", flush=True)


def build_recent_card() -> Dict[str, Any]:
    return {
        "markdown": "选择要查看的会话内容范围。也可以手动发送 /recent N S，N=1-20，S 为从最近第几条开始。",
        "keyboard": {
            "rows": [
                {
                    "buttons": [
                        keyboard_button("最近5条", "/recent", button_id="recent-5", style=1),
                        keyboard_button("最近10条", "/recent 10", button_id="recent-10", style=0),
                        keyboard_button("最近20条", "/recent 20", button_id="recent-20", style=0),
                    ]
                },
                {
                    "buttons": [
                        keyboard_button("我最近5句", "/last user", button_id="recent-last-user", style=0),
                        keyboard_button("Codex最近5句", "/last codex", button_id="recent-last-codex", style=0),
                    ]
                },
                {
                    "buttons": [
                        keyboard_button("会话列表", "/resume", button_id="recent-resume", style=0),
                    ]
                },
            ]
        },
    }


def recent_nav_card(command: str, args: str) -> Optional[Dict[str, Any]]:
    if command not in {"/recent", "/last"}:
        return None
    tokens = (args or "").split()
    mode = ""
    rest = tokens
    if command == "/last" and tokens and not re.fullmatch(r"\d+", tokens[0]):
        mode = tokens[0]
        rest = tokens[1:]
    numbers = [int(token) for token in rest if re.fullmatch(r"\d+", token)]
    count = max(1, min(20, numbers[0] if numbers else 5))
    start = max(1, numbers[1] if len(numbers) > 1 else 1)
    previous_start = start + count
    next_start = max(1, start - count)
    prefix = command if command == "/recent" else f"/last {mode or 'codex'}"
    return {
        "markdown": "翻阅聊天记录",
        "keyboard": {
            "rows": [
                {
                    "buttons": [
                        keyboard_button(f"前{count}条", f"{prefix} {count} {previous_start}", button_id=f"recent-prev-{command.strip('/')}-{count}-{previous_start}", style=0),
                        keyboard_button(f"后{count}条", f"{prefix} {count} {next_start}", button_id=f"recent-next-{command.strip('/')}-{count}-{next_start}", style=0),
                    ]
                }
            ]
        },
    }


def build_resume_switched_card(session_id: str) -> Dict[str, Any]:
    short = short_id(session_id, 8)
    return {
        "markdown": "\n".join([
            "已切换 Codex 会话",
            f"id: {short}",
            "可以直接查看最近对话内容。",
        ]),
        "keyboard": {
            "rows": [
                {
                    "buttons": [
                        keyboard_button("查看最近对话内容", "/recent", button_id=f"resume-recent-{short}", style=1),
                    ]
                },
                {
                    "buttons": [
                        keyboard_button("最近5条", "/recent 5", button_id=f"resume-recent5-{short}", style=0),
                        keyboard_button("最近10条", "/recent 10", button_id=f"resume-recent10-{short}", style=0),
                    ]
                },
                {
                    "buttons": [
                        keyboard_button("我最近5句", "/last user", button_id=f"resume-last-user-{short}", style=0),
                        keyboard_button("Codex最近5句", "/last codex", button_id=f"resume-last-codex-{short}", style=0),
                    ]
                },
            ]
        },
    }


def build_task_queued_card(task_id: str) -> Dict[str, Any]:
    short = short_task_id(task_id)
    return {
        "markdown": f"任务已加入队列：{task_id}",
        "keyboard": {
            "rows": [
                {
                    "buttons": [
                        keyboard_button("查看任务队列", "/tasks", button_id=f"tasks-{short}", style=1),
                        keyboard_button("取消当前任务", f"/cancel {task_id}", button_id=f"cancel-task-{short}", style=0),
                    ]
                }
            ]
        },
    }


def build_tasks_card() -> Optional[Dict[str, Any]]:
    with task_lock:
        active = [
            item for item in tasks.values()
            if item.get("status") in {"queued", "queued-session", "queued-capacity", "running"}
        ]
    if not active:
        return None
    order = {"running": 0, "queued-capacity": 1, "queued-session": 2, "queued": 3}
    active.sort(key=lambda item: (order.get(str(item.get("status")), 9), float(item.get("created_at", 0))))
    buttons = []
    for item in active[:6]:
        task_id = str(item.get("id", ""))
        if task_id:
            buttons.append(keyboard_button(f"取消{task_id}", f"/cancel {task_id}", button_id=f"tasks-cancel-{short_task_id(task_id)}", style=0))
    return {
        "markdown": "任务操作",
        "keyboard": {"rows": [{"buttons": row} for row in chunked(buttons, 2)]},
    }


def build_approval_card(item: Dict[str, Any]) -> Dict[str, Any]:
    item_id = str(item.get("id", "")).strip()
    source = re.sub(r"\s+", " ", str(item.get("user_text", "")).strip())
    plan = str(item.get("plan", "")).strip()
    if len(source) > 120:
        source = source[:120].rstrip() + "..."
    if len(plan) > 900:
        plan = plan[:900].rstrip() + "\n[计划过长，已截断显示]"
    markdown = "\n".join([
        f"待批准操作 #{item_id}",
        "",
        f"来源：{source or '(空)'}",
        "",
        plan or "没有生成计划。",
        "",
        "请选择：",
    ]).strip()
    rows = [
        {
            "buttons": [
                keyboard_button("批准执行", f"/allow {item_id}", button_id=f"allow-{item_id}", style=1),
                keyboard_button("拒绝", f"/reject {item_id}", button_id=f"reject-{item_id}", style=0),
            ]
        },
        {
            "buttons": [
                keyboard_button("查看待批", "/pending", button_id=f"pending-{item_id}", style=0),
                keyboard_button("取消任务", "/cancel", button_id=f"cancel-{item_id}", style=0),
            ]
        },
    ]
    return {
        "markdown": markdown,
        "keyboard": {"rows": rows},
    }


def build_pending_approvals_card() -> Optional[Dict[str, Any]]:
    from codex_bridge_client import list_pending_approvals

    items = list_pending_approvals()
    buttons = []
    for item in items[:6]:
        item_id = str(item.get("id", "")).strip()
        if item_id:
            buttons.append(keyboard_button(f"批准{item_id}", f"/allow {item_id}", button_id=f"pending-allow-{item_id}", style=1))
            buttons.append(keyboard_button(f"拒绝{item_id}", f"/reject {item_id}", button_id=f"pending-reject-{item_id}", style=0))
    if not buttons:
        return None
    return {
        "markdown": "待批准操作",
        "keyboard": {"rows": [{"buttons": row} for row in chunked(buttons, 2)]},
    }


def send_approval_item(api: "QQApi", reply: Dict[str, str], item: Dict[str, Any], seq: int) -> int:
    card = build_approval_card(item)
    try:
        api.send_markdown_keyboard(reply, card["markdown"], card["keyboard"], seq)
        print(f"[qq-send-approval-card] id={item.get('id')}", flush=True)
        return seq + 1
    except Exception as exc:
        print(f"[qq-send-approval-card-error] {exc}", flush=True)
    try:
        api.send_message(reply, approval_prompt_text(item), seq)
        print(f"[qq-send-approval-text] id={item.get('id')}", flush=True)
        return seq + 1
    except Exception as exc:
        print(f"[qq-send-approval-text-error] {exc}", flush=True)
    return seq


def send_approval_test(api: "QQApi", job: Dict[str, Any], seq: int) -> int:
    test_job = dict(job)
    test_job["text"] = "审批测试：这是一条测试待批准请求，不会执行真实任务。"
    item = create_pending_approval(test_job)
    return send_approval_item(api, job["reply"], item, seq)


def http_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None,
              headers: Optional[Dict[str, str]] = None, timeout: int = 20) -> Dict[str, Any]:
    body = None
    req_headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers:
        req_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json; charset=utf-8"

    req = Request(url, data=body, headers=req_headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {exc.code}: {raw[:1000]}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc

    if not raw:
        return {}
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("code") not in (None, 0, "0"):
        raise RuntimeError(f"{method} {url} returned QQ error: {json.dumps(data, ensure_ascii=False)[:1000]}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{method} {url} returned non-object JSON")
    return data


class QQApi:
    def __init__(self) -> None:
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def access_token(self) -> str:
        with self._lock:
            if self._token and self._expires_at > time.time() + 60:
                return self._token

            data = http_json("POST", QQ_AUTH_URL, {
                "appId": QQ_APP_ID,
                "clientSecret": QQ_APP_SECRET,
            }, timeout=15)
            token = str(data.get("access_token", ""))
            expires_in = int(data.get("expires_in", 7200))
            if not token:
                raise RuntimeError("QQ access token response missing access_token")
            self._token = token
            self._expires_at = time.time() + max(60, expires_in - 60)
            return token

    def headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": "QQBot " + self.access_token(),
            "User-Agent": USER_AGENT,
        }
        if QQ_SEND_UNION_APPID_HEADER:
            headers["X-Union-Appid"] = QQ_APP_ID
        return headers

    def api_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return QQ_API_BASE + path

    def gateway_url(self) -> str:
        data = http_json("GET", self.api_url(QQ_GATEWAY_PATH), headers=self.headers(), timeout=15)
        url = str(data.get("url", ""))
        if not url:
            raise RuntimeError("QQ gateway response missing url")
        return url

    def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return http_json("POST", self.api_url(path), payload=payload, headers=self.headers(), timeout=20)

    def put(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return http_json("PUT", self.api_url(path), payload=payload, headers=self.headers(), timeout=20)

    def interaction_response(self, interaction_id: str, code: int = 0) -> Dict[str, Any]:
        return self.put(f"/interactions/{quote(interaction_id, safe='')}", {"code": code})

    def target_path(self, reply: Dict[str, str], suffix: str) -> str:
        kind = reply["kind"]
        if kind == "c2c":
            base = f"/v2/users/{quote(reply['openid'], safe='')}"
        elif kind == "group":
            base = f"/v2/groups/{quote(reply['group_openid'], safe='')}"
        elif kind == "channel":
            base = f"/channels/{quote(reply['channel_id'], safe='')}"
        elif kind == "dm":
            base = f"/dms/{quote(reply['guild_id'], safe='')}"
        else:
            raise RuntimeError(f"unsupported reply kind: {kind}")
        return base + suffix

    def send_payload(self, reply: Dict[str, str], payload: Dict[str, Any], msg_seq: int) -> Dict[str, Any]:
        path = self.target_path(reply, "/messages")
        payload = dict(payload)
        if reply.get("event_id"):
            payload["event_id"] = reply["event_id"]
        elif reply.get("msg_id"):
            payload["msg_id"] = reply["msg_id"]
        payload["msg_seq"] = msg_seq
        return self.post(path, payload)

    def send_message(self, reply: Dict[str, str], content: str, msg_seq: int) -> Dict[str, Any]:
        return self.send_payload(reply, {
            "content": content,
            "msg_type": 0,
        }, msg_seq)

    def upload_media(self, reply: Dict[str, str], path: str, file_type: int = 1) -> str:
        image_path = Path(path)
        size = image_path.stat().st_size
        if size > QQ_SEND_IMAGE_MAX_BYTES:
            raise RuntimeError(f"image too large: {size} bytes")
        file_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
        data = self.post(self.target_path(reply, "/files"), {
            "file_type": file_type,
            "srv_send_msg": False,
            "file_data": file_data,
        })
        file_info = str(data.get("file_info") or "")
        if not file_info:
            raise RuntimeError("QQ upload response missing file_info")
        return file_info

    def send_image(self, reply: Dict[str, str], path: str, msg_seq: int) -> Dict[str, Any]:
        file_info = self.upload_media(reply, path, file_type=1)
        return self.send_payload(reply, {
            "msg_type": 7,
            "media": {"file_info": file_info},
        }, msg_seq)

    def send_markdown_keyboard(
        self,
        reply: Dict[str, str],
        markdown: str,
        keyboard: Dict[str, Any],
        msg_seq: int,
    ) -> Dict[str, Any]:
        return self.send_payload(reply, {
            "msg_type": 2,
            "markdown": {"content": markdown},
            "keyboard": {"content": keyboard},
        }, msg_seq)


class TTLSet:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.items: Dict[str, float] = {}
        self.lock = threading.Lock()

    def add_once(self, key: str) -> bool:
        now_time = time.time()
        with self.lock:
            for item_key, expires_at in list(self.items.items()):
                if expires_at <= now_time:
                    del self.items[item_key]
            if key in self.items:
                return False
            self.items[key] = now_time + self.ttl_seconds
            return True


def clean_content(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<@!?\w+>", "", text)
    text = re.sub(r"^\s*[/!]?codex[:,：]?\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def extract_job(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    event_type = str(payload.get("t") or "")
    if event_type not in QQ_ALLOWED_EVENTS:
        return None

    data = payload.get("d") or {}
    if not isinstance(data, dict):
        return None

    if event_type == "INTERACTION_CREATE":
        return extract_interaction_job(data)

    msg_id = str(data.get("id", ""))
    content = clean_content(str(data.get("content", "")))
    job_id = uuid.uuid4().hex
    if not msg_id:
        return None

    def enrich_job(reply_job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        attachments = download_attachments(job_id, data)
        final_content = content
        if not final_content and attachments:
            final_content = "请分析这条消息附带的图片或文件。"
        if not final_content:
            return None
        reply_job["text"] = final_content
        reply_job["attachments"] = attachments
        reply_job["attachments_text"] = format_attachments_for_codex(attachments)
        return reply_job

    if event_type == "C2C_MESSAGE_CREATE":
        openid = str((data.get("author") or {}).get("user_openid", ""))
        if not openid:
            return None
        if QQ_ALLOWED_USER_OPENIDS and openid not in QQ_ALLOWED_USER_OPENIDS:
            print(f"[qq] blocked c2c user {openid[:12]}", flush=True)
            return None
        return enrich_job({
            "id": job_id,
            "source": "qq-gateway",
            "from": "qq:c2c:" + openid[:12],
            "text": content,
            "event_type": event_type,
            "msg_id": msg_id,
            "reply": {"kind": "c2c", "openid": openid, "msg_id": msg_id},
        })

    if event_type in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
        group_openid = str(data.get("group_openid", ""))
        if not group_openid:
            return None
        if QQ_ALLOWED_GROUP_OPENIDS and group_openid not in QQ_ALLOWED_GROUP_OPENIDS:
            print(f"[qq] blocked group {group_openid[:12]}", flush=True)
            return None
        return enrich_job({
            "id": job_id,
            "source": "qq-gateway",
            "from": "qq:group:" + group_openid[:12],
            "text": content,
            "event_type": event_type,
            "msg_id": msg_id,
            "reply": {"kind": "group", "group_openid": group_openid, "msg_id": msg_id},
        })

    if event_type == "AT_MESSAGE_CREATE":
        channel_id = str(data.get("channel_id", ""))
        if not channel_id:
            return None
        return enrich_job({
            "id": job_id,
            "source": "qq-gateway",
            "from": "qq:channel:" + channel_id,
            "text": content,
            "event_type": event_type,
            "msg_id": msg_id,
            "reply": {"kind": "channel", "channel_id": channel_id, "msg_id": msg_id},
        })

    if event_type == "DIRECT_MESSAGE_CREATE":
        guild_id = str(data.get("guild_id", ""))
        if not guild_id:
            return None
        return enrich_job({
            "id": job_id,
            "source": "qq-gateway",
            "from": "qq:dm:" + guild_id,
            "text": content,
            "event_type": event_type,
            "msg_id": msg_id,
            "reply": {"kind": "dm", "guild_id": guild_id, "msg_id": msg_id},
        })

    return None


def split_reply(text: str, max_chars: int, max_chunks: int, truncate: bool = True) -> List[str]:
    text = (text or "").strip() or "(Codex returned an empty response.)"
    parts = [text[index:index + max_chars] for index in range(0, len(text), max_chars)]
    if len(parts) <= max_chunks or not truncate:
        return parts

    parts = parts[:max_chunks]
    suffix = "\n\n[内容过长，已截断]"
    parts[-1] = (parts[-1][:max(0, max_chars - len(suffix))] + suffix).strip()
    return parts


def send_text_and_images(
    api: QQApi,
    reply: Dict[str, str],
    text: str,
    msg_seq: int,
    max_chunks: int,
    log_label: str,
) -> int:
    clean_text, image_paths = parse_outbound_images(text)
    seq = msg_seq
    with task_output_settings_lock:
        truncate = truncate_long_replies
    chunks = split_reply(clean_text, QQ_REPLY_MAX_CHARS, max_chunks, truncate) if clean_text.strip() else []
    if not chunks and not image_paths:
        chunks = ["(Codex returned an empty response.)"]

    for part in chunks:
        try:
            api.send_message(reply, part, seq)
            print(f"[{log_label}] text chars={len(part)}", flush=True)
            seq += 1
            time.sleep(0.4)
        except Exception as exc:
            print(f"[{log_label}-error] text {exc}", flush=True)
            return seq

    for image_path in image_paths:
        try:
            api.send_image(reply, image_path, seq)
            print(f"[{log_label}] image path={image_path}", flush=True)
            seq += 1
            time.sleep(0.4)
        except Exception as exc:
            print(f"[{log_label}-error] image {image_path}: {exc}", flush=True)
            try:
                api.send_message(reply, f"图片发送失败：{image_path}\n{exc}", seq)
                seq += 1
            except Exception as send_exc:
                print(f"[{log_label}-error] image fallback {send_exc}", flush=True)
    return seq


task_lock = threading.RLock()
task_sequence = 0
tasks: Dict[str, Dict[str, Any]] = {}
session_locks: Dict[str, threading.Lock] = {}
codex_parallel = threading.Semaphore(QQ_CODEX_MAX_PARALLEL)


def next_task_id() -> str:
    global task_sequence
    with task_lock:
        task_sequence += 1
        return f"t{task_sequence:04d}"


def short_task_id(task_id: str) -> str:
    return (task_id or "")[-8:]


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        if sec == 0:
            return f"{minutes}m"
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def get_session_lock(session_key: str) -> threading.Lock:
    with task_lock:
        lock = session_locks.get(session_key)
        if lock is None:
            lock = threading.Lock()
            session_locks[session_key] = lock
        return lock


def tasks_text() -> str:
    now = time.time()
    with task_lock:
        snapshot = list(tasks.values())
    if not snapshot:
        return "当前没有运行中或排队中的 Codex 任务。"
    order = {"running": 0, "queued-capacity": 1, "queued-session": 2, "queued": 3, "done": 4, "failed": 5, "cancelled": 6}
    snapshot.sort(key=lambda item: (order.get(str(item.get("status")), 9), float(item.get("created_at", 0))))
    lines = [f"Codex 任务：{len(snapshot)} 个"]
    for item in snapshot[:20]:
        status = str(item.get("status", ""))
        age = format_duration(now - float(item.get("started_at") or item.get("created_at") or now))
        title = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()[:40]
        label = {
            "queued-capacity": "排队等并发位",
            "queued-session": "排队等同会话",
            "queued": "排队中",
            "running": "运行中",
            "done": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
        }.get(status, status)
        lines.append(f"- {item.get('id')} | {label} | {age} | session={str(item.get('session_key', ''))[-12:]} | {title}")
    lines.append("用法：/cancel <task_id> 取消指定任务。")
    return "\n".join(lines)


def task_prompt_preview(text: str, limit: int = 240) -> str:
    preview = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(preview) > limit:
        preview = preview[:limit].rstrip() + "..."
    return preview or "(空)"


def final_task_answer_text(task_id: str, job: Dict[str, Any], answer: str) -> str:
    with task_output_settings_lock:
        enabled = show_task_context_on_final
    if not enabled:
        return answer
    return "\n\n".join([
        f"任务 {task_id} 的最终输出",
        f"用户输入：{task_prompt_preview(str(job.get('text', '')))}",
        "Codex输出：",
        answer,
    ])


def extract_agent_message(event: Dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    if event_type == "item.completed":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            return str(item.get("text") or "").strip()
    if event_type == "response_item":
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "message":
            content = payload.get("content")
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                        text = str(part.get("text") or "").strip()
                        if text:
                            parts.append(text)
                return "\n".join(parts).strip()
    return ""


def update_task(task_id: str, **updates: Any) -> None:
    with task_lock:
        item = tasks.get(task_id)
        if item is not None:
            item.update(updates)


def cleanup_finished_tasks(max_age_seconds: int = 3600) -> None:
    now = time.time()
    with task_lock:
        for task_id, item in list(tasks.items()):
            if item.get("status") in {"done", "failed", "cancelled"}:
                ended_at = float(item.get("ended_at") or now)
                if now - ended_at > max_age_seconds:
                    del tasks[task_id]


def task_event_callback(task_id: str, api: QQApi, reply: Dict[str, str]):
    def on_event(event: Dict[str, Any]) -> None:
        with task_output_settings_lock:
            partial_enabled = send_partial_outputs
        if not partial_enabled:
            return
        message = extract_agent_message(event)
        if not message:
            return
        now = time.time()
        send_partial = False
        with task_lock:
            item = tasks.get(task_id)
            if not item:
                return
            item["last_agent_message"] = message
            last_sent_text = str(item.get("last_partial_text", ""))
            last_sent_at = float(item.get("last_partial_at") or 0)
            if message != last_sent_text and now - last_sent_at >= QQ_TASK_PARTIAL_INTERVAL_SECONDS:
                item["last_partial_text"] = message
                item["last_partial_at"] = now
                seq = int(item.get("next_seq", 1))
                item["next_seq"] = seq + 1
                send_partial = True
            else:
                seq = 0
        if send_partial:
            preview = message.strip()
            if len(preview) > QQ_TASK_PARTIAL_MAX_CHARS:
                preview = preview[:QQ_TASK_PARTIAL_MAX_CHARS].rstrip() + "\n[阶段性输出已截断]"
            try:
                api.send_message(reply, f"阶段性输出 {task_id}：\n{preview}", seq)
                print(f"[task-partial] id={task_id} chars={len(preview)}", flush=True)
            except Exception as exc:
                print(f"[task-partial-error] id={task_id} {exc}", flush=True)

    return on_event


def task_status_loop(task_id: str, api: QQApi, reply: Dict[str, str], stop_event: threading.Event) -> None:
    while True:
        with task_output_settings_lock:
            interval = task_status_interval_seconds
        if interval <= 0:
            return
        if stop_event.wait(interval):
            return
        with task_lock:
            item = tasks.get(task_id)
            if not item or item.get("status") != "running":
                return
            seq = int(item.get("next_seq", 1))
            item["next_seq"] = seq + 1
            started_at = float(item.get("started_at") or time.time())
        try:
            api.send_message(reply, f"任务 {task_id} 仍在运行，已用 {format_duration(time.time() - started_at)}。/tasks 查看，/cancel {task_id} 取消。", seq)
            print(f"[task-heartbeat] id={task_id}", flush=True)
        except Exception as exc:
            print(f"[task-heartbeat-error] id={task_id} {exc}", flush=True)


def run_task(task_id: str, api: QQApi) -> None:
    with task_lock:
        item = tasks.get(task_id)
        if not item:
            return
        job = item["job"]
        reply = job["reply"]
        session_key = str(item.get("session_key", ""))
    session_lock = get_session_lock(session_key)
    update_task(task_id, status="queued-session")
    with session_lock:
        with task_lock:
            if tasks.get(task_id, {}).get("status") == "cancelled":
                return
        update_task(task_id, status="queued-capacity")
        with codex_parallel:
            with task_lock:
                if tasks.get(task_id, {}).get("status") == "cancelled":
                    return
            cleanup_finished_tasks()
            stop_event = threading.Event()
            with task_lock:
                item = tasks.get(task_id)
                if not item:
                    return
                item.update({"status": "running", "started_at": time.time(), "next_seq": max(2, int(item.get("next_seq", 2)))})
            threading.Thread(target=task_status_loop, args=(task_id, api, reply, stop_event), name=f"task-status-{task_id}", daemon=True).start()
            try:
                history_extra = str(job.get("attachments_text", "")).strip()
                append_history("User", f"[{job['from']}]\n{job['text']}\n{history_extra}".strip())
                answer = run_codex(job, on_event=task_event_callback(task_id, api, reply))
                append_history("Codex", answer)
                with task_lock:
                    item = tasks.get(task_id)
                    seq = int(item.get("next_seq", 2)) if item else 2
                    if item:
                        item["next_seq"] = seq + 1
                final_answer = final_task_answer_text(task_id, job, answer)
                send_text_and_images(api, reply, final_answer, seq, QQ_MAX_REPLY_CHUNKS, "qq-send-answer")
                update_task(task_id, status="done", ended_at=time.time())
            except Exception as exc:
                answer = f"Codex 调用失败：{exc}"
                append_history("BridgeError", str(exc))
                print(
                    f"[codex-task-error] id={task_id} type={type(exc).__name__} "
                    f"message={exc}",
                    flush=True,
                )
                print(traceback.format_exc(), flush=True)
                with task_lock:
                    item = tasks.get(task_id)
                    seq = int(item.get("next_seq", 2)) if item else 2
                    if item:
                        item["next_seq"] = seq + 1
                try:
                    api.send_message(reply, answer, seq)
                except Exception as send_exc:
                    print(f"[task-error-send-failed] id={task_id} {send_exc}", flush=True)
                update_task(task_id, status="failed", error=str(exc), ended_at=time.time())
            finally:
                stop_event.set()


def enqueue_codex_task(api: QQApi, job: Dict[str, Any], first_seq: int) -> None:
    task_id = next_task_id()
    job["id"] = task_id
    try:
        session_key = job_session_key(job)
    except Exception:
        session_key = "unknown"
    with task_lock:
        tasks[task_id] = {
            "id": task_id,
            "job": job,
            "reply": job["reply"],
            "text": job.get("text", ""),
            "from": job.get("from", ""),
            "session_key": session_key,
            "status": "queued",
            "created_at": time.time(),
            "next_seq": first_seq + 1,
        }
        ahead = sum(1 for item in tasks.values() if item.get("status") in {"queued", "queued-session", "queued-capacity"})
    queue_text = f"已加入 Codex 任务队列：{task_id}\n排队中：{ahead} 个。"
    try:
        card = build_task_queued_card(task_id)
        api.send_markdown_keyboard(job["reply"], queue_text + "\n\n" + card["markdown"], card["keyboard"], first_seq)
    except Exception as exc:
        print(f"[task-queued-card-error] id={task_id} {exc}", flush=True)
        try:
            api.send_message(job["reply"], f"{queue_text}\n/tasks 查看，/cancel {task_id} 取消。", first_seq)
        except Exception as send_exc:
            print(f"[task-queued-send-error] id={task_id} {send_exc}", flush=True)
    threading.Thread(target=run_task, args=(task_id, api), name=f"codex-task-{task_id}", daemon=True).start()


def event_debug_summary(event_type: str, data: Any) -> str:
    if not isinstance(data, dict):
        return f"event={event_type} data_type={type(data).__name__}"
    keys = ",".join(sorted(str(key) for key in data.keys()))
    author = data.get("author") or {}
    author_keys = ",".join(sorted(str(key) for key in author.keys())) if isinstance(author, dict) else ""
    content_len = len(str(data.get("content") or ""))
    return f"event={event_type} keys={keys} author_keys={author_keys} content_len={content_len}"


def command_card(text: str) -> Optional[Dict[str, Any]]:
    command, args = split_command(text)
    if command == "/start":
        return build_start_card()
    if command == "/setup":
        return build_setup_card()
    if command == "/timeout" and not args.strip():
        return build_timeout_card()
    if command == "/permission" and not args.strip():
        return build_permission_card()
    if command == "/workdir":
        raw = args.strip().strip('"').strip("'")
        if raw and not Path(raw).expanduser().is_dir():
            return None
        return build_workdir_card()
    if command == "/heartbeat" and not args.strip():
        return build_heartbeat_card()
    if command == "/recent-default" and not args.strip():
        return build_recent_default_card()
    if command == "/truncate" and not args.strip():
        return build_truncate_card()
    if command == "/resume":
        parsed = parse_resume_args(args)
        if parsed["mode"] in {"groups", "sessions"}:
            return build_resume_card(args)
    if command == "/model":
        return build_model_card()
    return None


def output_settings_text(args: str) -> str:
    global send_partial_outputs, show_task_context_on_final
    normalized = re.sub(r"\s+", " ", (args or "").strip().lower())
    with task_output_settings_lock:
        if normalized in {"on", "stage on", "partial on", "partials on", "阶段性 on", "阶段性 开"}:
            send_partial_outputs = True
        elif normalized in {"off", "stage off", "partial off", "partials off", "阶段性 off", "阶段性 关"}:
            send_partial_outputs = False
        elif normalized in {"stage", "partial", "partials", "阶段性"}:
            pass
        elif normalized in {"usercontext on", "user context on", "用户输入 on", "用户输入 开", "最终输出 on", "最终输出 开"}:
            show_task_context_on_final = True
        elif normalized in {"usercontext off", "user context off", "用户输入 off", "用户输入 关", "最终输出 off", "最终输出 关"}:
            show_task_context_on_final = False
        elif normalized in {"usercontext", "user context", "用户输入", "最终输出"}:
            pass
        elif normalized:
            return "无法识别设置。用法：/output stage on|off 或 /output userContext on|off"
        partial_text = "开" if send_partial_outputs else "关"
        final_text = "开" if show_task_context_on_final else "关"
    return "\n".join([
        "输出设置",
        f"阶段性输出：{partial_text}",
        f"最终输出带用户输入：{final_text}",
        "用法：",
        "/output stage on - 开启阶段性输出",
        "/output stage off - 关闭阶段性输出",
        "/output userContext on - 最终输出带用户输入",
        "/output userContext off - 最终输出不带用户输入",
    ])


def heartbeat_settings_text(args: str) -> str:
    global task_status_interval_seconds
    normalized = re.sub(r"\s+", " ", (args or "").strip().lower())
    changed = False
    with task_output_settings_lock:
        if normalized in {"off", "0", "关", "关闭"}:
            task_status_interval_seconds = 0
            changed = True
        elif normalized:
            match = re.search(r"\d+", normalized)
            if not match:
                return "无法识别任务提醒频率。用法：/heartbeat 5 或 /heartbeat off"
            minutes = max(1, min(1440, int(match.group(0))))
            task_status_interval_seconds = minutes * 60
            changed = True
        current = task_status_interval_seconds
    if changed:
        save_runtime_setting("task_status_interval_seconds", current)
    return "\n".join([
        "任务提醒频率",
        f"当前频率：{'关' if current <= 0 else format_duration(current)}",
        "用法：",
        "/heartbeat 5 - 每 5 分钟提醒一次",
        "/heartbeat off - 关闭任务运行提醒",
    ])


def truncate_settings_text(args: str) -> str:
    global truncate_long_replies
    normalized = re.sub(r"\s+", " ", (args or "").strip().lower())
    changed = False
    with task_output_settings_lock:
        if normalized in {"on", "开", "开启", "true", "1"}:
            truncate_long_replies = True
            changed = True
        elif normalized in {"off", "关", "关闭", "false", "0"}:
            truncate_long_replies = False
            changed = True
        elif normalized:
            return "无法识别长内容截断设置。用法：/truncate on|off"
        enabled = truncate_long_replies
    if changed:
        save_runtime_setting("truncate_long_replies", enabled)
    return "\n".join([
        "长内容截断",
        f"当前状态：{'开' if enabled else '关'}",
        "用法：",
        "/truncate on - 超长回复按最大分片数截断",
        "/truncate off - 超长回复尽量分段发完整",
    ])


def local_gateway_command_reply(text: str) -> Optional[str]:
    command, args = split_command(text)
    if command == "/tasks":
        return tasks_text()
    if command == "/output":
        return output_settings_text(args)
    if command == "/heartbeat":
        return heartbeat_settings_text(args)
    if command == "/truncate":
        return truncate_settings_text(args)
    if command == "/cancel":
        target = args.strip()
        if target:
            with task_lock:
                item = tasks.get(target)
                if item and item.get("status") in {"queued", "queued-session", "queued-capacity"}:
                    item["status"] = "cancelled"
                    item["ended_at"] = time.time()
                    return f"已取消排队任务：{target}"
            return cancel_current_task(target)
        return cancel_current_task()
    return None


def post_command_card(text: str, command_reply: str) -> Optional[Dict[str, Any]]:
    command, args = split_command(text)
    if command == "/resume" and command_reply.startswith("已切换到 Codex 原生会话："):
        first_line = command_reply.splitlines()[0]
        _, _, session_id = first_line.partition("：")
        session_id = session_id.strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]{4,120}", session_id):
            return build_resume_switched_card(session_id)
    if command in {"/recent", "/last"} and not command_reply.startswith("当前没有 active"):
        return recent_nav_card(command, args)
    if command == "/tasks":
        return build_tasks_card()
    if command == "/pending":
        return build_pending_approvals_card()
    if command == "/timeout":
        return build_timeout_card()
    if command == "/permission":
        return build_permission_card()
    if command == "/heartbeat":
        return build_heartbeat_card()
    if command == "/recent-default":
        return build_recent_default_card()
    if command == "/truncate":
        return build_truncate_card()
    if command == "/output":
        return build_setup_card()
    return None


def send_command_reply(api: QQApi, reply: Dict[str, str], text: str, msg_seq: int = 1) -> int:
    card = command_card(text)
    if card is not None:
        try:
            api.send_markdown_keyboard(reply, card["markdown"], card["keyboard"], msg_seq)
            return msg_seq + 1
        except Exception as exc:
            print(f"[qq-send-card-error] {exc}", flush=True)

    command_reply = local_gateway_command_reply(text)
    if command_reply is None:
        command_reply = handle_bridge_command(text, {"reply": reply})
    if command_reply is None:
        return msg_seq

    seq = msg_seq
    with task_output_settings_lock:
        truncate = truncate_long_replies
    for part in split_reply(command_reply, QQ_REPLY_MAX_CHARS, QQ_MAX_REPLY_CHUNKS, truncate):
        try:
            api.send_message(reply, part, seq)
            seq += 1
            time.sleep(0.4)
        except Exception as exc:
            print(f"[qq-send-command-error] {exc}", flush=True)
            break
    followup_card = post_command_card(text, command_reply)
    if followup_card is not None:
        try:
            api.send_markdown_keyboard(reply, followup_card["markdown"], followup_card["keyboard"], seq)
            seq += 1
        except Exception as exc:
            print(f"[qq-send-followup-card-error] {exc}", flush=True)
    return seq


def worker_loop(api: QQApi, jobs: "queue.Queue[Dict[str, Any]]", stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            job = jobs.get(timeout=0.5)
        except queue.Empty:
            continue

        print(
            f"[job] {job['event_type']} from={job['from']} chars={len(job['text'])} "
            f"attachments={len(job.get('attachments') or [])}",
            flush=True,
        )
        seq = 1
        command_name, _ = split_command(job["text"])
        if command_name == "/restart":
            try:
                api.send_message(job["reply"], "收到，正在重启 QQ Gateway 客户端。稍等几秒后再发送 /status 验证。", seq)
            except Exception as exc:
                print(f"[qq-send-restart-error] {exc}", flush=True)
            jobs.task_done()
            schedule_restart("worker command")
            continue
        if command_name == "/start":
            mark_auto_start_sent(str(job.get("from", "")))
        if command_name != "/start" and mark_auto_start_sent(str(job.get("from", ""))):
            seq = send_command_reply(api, job["reply"], "/start", seq)
            print(f"[qq-send-auto-start] event={job['event_type']} from={job['from']}", flush=True)

        if command_name == "/approval-test":
            send_approval_test(api, job, seq)
            jobs.task_done()
            continue

        if command_name in {"/allow", "/revise"} and QQ_SEND_PROCESSING_MESSAGE:
            status_text = "收到，正在执行已批准操作。" if command_name == "/allow" else "收到，正在重新生成审批计划。"
            try:
                api.send_message(job["reply"], status_text, seq)
                seq += 1
            except Exception as exc:
                print(f"[qq-send-command-processing-error] {exc}", flush=True)

        command_reply = local_gateway_command_reply(job["text"])
        if command_reply is None:
            command_reply = handle_bridge_command(job["text"], job)
        if command_reply is not None:
            card = command_card(job["text"])
            if card is not None:
                try:
                    api.send_markdown_keyboard(job["reply"], card["markdown"], card["keyboard"], seq)
                    print(f"[qq-send-card] event={job['event_type']} text={job['text']}", flush=True)
                    jobs.task_done()
                    continue
                except Exception as exc:
                    print(f"[qq-send-card-error] {exc}", flush=True)

            with task_output_settings_lock:
                truncate = truncate_long_replies
            for part in split_reply(command_reply, QQ_REPLY_MAX_CHARS, QQ_MAX_REPLY_CHUNKS, truncate):
                try:
                    api.send_message(job["reply"], part, seq)
                    print(f"[qq-send-command] event={job['event_type']} chars={len(part)}", flush=True)
                    seq += 1
                    time.sleep(0.4)
                except Exception as exc:
                    print(f"[qq-send-command-error] {exc}", flush=True)
                    break
            followup_card = post_command_card(job["text"], command_reply)
            if followup_card is not None:
                try:
                    api.send_markdown_keyboard(job["reply"], followup_card["markdown"], followup_card["keyboard"], seq)
                    print(f"[qq-send-followup-card] event={job['event_type']} text={job['text']}", flush=True)
                    seq += 1
                except Exception as exc:
                    print(f"[qq-send-followup-card-error] {exc}", flush=True)
            jobs.task_done()
            continue

        pending_item = prepare_bridge_approval(job)
        if pending_item is not None:
            send_approval_item(api, job["reply"], pending_item, seq)
            jobs.task_done()
            continue

        enqueue_codex_task(api, job, seq)
        jobs.task_done()


class QQGatewayClient:
    def __init__(self, api: QQApi, jobs: "queue.Queue[Dict[str, Any]]") -> None:
        self.api = api
        self.jobs = jobs
        self.dedup = TTLSet(QQ_DEDUP_SECONDS)
        self.ws = None
        self.latest_seq: Optional[int] = None
        self.session_id = ""
        self.heartbeat_stop: Optional[threading.Event] = None
        self.lock = threading.Lock()

    def connect_once(self) -> None:
        if websocket is None:
            raise RuntimeError("Python package websocket-client is required")

        gateway_url = self.api.gateway_url()
        print(f"[gateway] connecting {gateway_url}", flush=True)

        self.ws = websocket.WebSocketApp(
            gateway_url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        self.ws.run_forever(ping_interval=0)

    def send_gateway(self, payload: Dict[str, Any]) -> None:
        with self.lock:
            if not self.ws:
                raise RuntimeError("websocket is not connected")
            self.ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def identify_or_resume(self) -> None:
        token = "QQBot " + self.api.access_token()
        if QQ_USE_RESUME and self.session_id and self.latest_seq is not None:
            print(f"[gateway] resume seq={self.latest_seq}", flush=True)
            self.send_gateway({
                "op": 6,
                "d": {
                    "token": token,
                    "session_id": self.session_id,
                    "seq": self.latest_seq,
                },
            })
            return

        print(f"[gateway] identify intents={QQ_GATEWAY_INTENTS}", flush=True)
        self.send_gateway({
            "op": 2,
            "d": {
                "token": token,
                "intents": QQ_GATEWAY_INTENTS,
                "shard": [0, 1],
                "properties": {
                    "$os": "windows",
                    "$browser": "codex-remote-bridge",
                    "$device": "codex-remote-bridge",
                },
            },
        })

    def start_heartbeat(self, interval_ms: int) -> None:
        if self.heartbeat_stop:
            self.heartbeat_stop.set()
        stop_event = threading.Event()
        self.heartbeat_stop = stop_event
        interval = max(5.0, interval_ms / 1000.0 * 0.9)

        def loop() -> None:
            while not stop_event.wait(interval):
                try:
                    self.send_gateway({"op": 1, "d": self.latest_seq})
                    print(f"[heartbeat] seq={self.latest_seq}", flush=True)
                except Exception as exc:
                    print(f"[heartbeat-error] {exc}", flush=True)
                    return

        threading.Thread(target=loop, name="qq-heartbeat", daemon=True).start()

    def stop_heartbeat(self) -> None:
        if self.heartbeat_stop:
            self.heartbeat_stop.set()
            self.heartbeat_stop = None

    def on_message(self, _ws, message: str) -> None:
        try:
            payload = json.loads(message)
        except Exception:
            print("[gateway] ignored non-json message", flush=True)
            return

        try:
            if payload.get("s") is not None:
                self.latest_seq = payload.get("s")

            op = payload.get("op")
            if op == 10:
                interval_ms = int((payload.get("d") or {}).get("heartbeat_interval", 45000))
                print(f"[gateway] hello heartbeat_interval={interval_ms}ms", flush=True)
                self.start_heartbeat(interval_ms)
                self.identify_or_resume()
                return

            if op == 11:
                return

            if op == 7:
                print("[gateway] server requested reconnect", flush=True)
                self.close()
                return

            if op == 9:
                print("[gateway] invalid session; reconnecting with identify", flush=True)
                self.session_id = ""
                self.latest_seq = None
                self.close()
                return

            if op != 0:
                print(f"[gateway] op={op}", flush=True)
                return

            event_type = str(payload.get("t") or "")
            if event_type not in {"READY", "RESUMED"}:
                data = payload.get("d") or {}
                print(f"[gateway-event] {event_debug_summary(event_type, data)}", flush=True)

            if event_type == "READY":
                data = payload.get("d") or {}
                self.session_id = str(data.get("session_id", ""))
                user = data.get("user") or {}
                print(f"[gateway] READY bot={user.get('username', '')} session={self.session_id[:8]}", flush=True)
                return

            if event_type == "RESUMED":
                print("[gateway] RESUMED", flush=True)
                return

            if event_type == "INTERACTION_CREATE":
                data = payload.get("d") or {}
                interaction_id = str(data.get("id") or "").strip() if isinstance(data, dict) else ""
                if interaction_id:
                    try:
                        self.api.interaction_response(interaction_id)
                        print(f"[qq-interaction-response] id={interaction_id[:12]}", flush=True)
                    except Exception as exc:
                        print(f"[qq-interaction-response-error] {exc}", flush=True)

            job = extract_job(payload)
            if not job:
                if event_type:
                    data = payload.get("d") or {}
                    if event_type not in QQ_ALLOWED_EVENTS:
                        print(f"[gateway] ignored event {event_debug_summary(event_type, data)}", flush=True)
                    else:
                        print(f"[gateway] allowed event produced no job {event_debug_summary(event_type, data)}", flush=True)
                return

            print(
                f"[gateway] extracted job event={job['event_type']} reply={job['reply']['kind']} "
                f"id={job['id']} attachments={len(job.get('attachments') or [])}",
                flush=True,
            )

            dedup_key = f"{job['event_type']}:{job['msg_id']}"
            if not self.dedup.add_once(dedup_key):
                print(f"[gateway] duplicate skipped {dedup_key}", flush=True)
                return

            if is_cancel_command(job["text"]):
                print(f"[gateway] immediate cancel from={job['from']}", flush=True)
                try:
                    command, args = split_command(job["text"])
                    self.api.send_message(job["reply"], local_gateway_command_reply(job["text"]) or cancel_current_task(args), 1)
                except Exception as exc:
                    print(f"[qq-send-cancel-error] {exc}", flush=True)
                return

            if is_restart_command(job["text"]):
                print(f"[gateway] immediate restart from={job['from']}", flush=True)
                try:
                    self.api.send_message(job["reply"], "收到，正在重启 QQ Gateway 客户端。稍等几秒后再发送 /status 验证。", 1)
                except Exception as exc:
                    print(f"[qq-send-restart-error] {exc}", flush=True)
                schedule_restart("gateway command")
                return

            try:
                self.jobs.put_nowait(job)
                print(f"[gateway] queued {job['event_type']} id={job['id']}", flush=True)
            except queue.Full:
                print("[gateway] local job queue full", flush=True)
                try:
                    self.api.send_message(job["reply"], "当前 Codex 队列已满，请稍后再试。", 1)
                except Exception as exc:
                    print(f"[qq-send-queue-full-error] {exc}", flush=True)
        except Exception as exc:
            print(f"[gateway-error] on_message exception: {exc}", flush=True)
            print(traceback.format_exc(), flush=True)

    def on_error(self, _ws, error) -> None:
        print(f"[gateway-error] {error}", flush=True)

    def on_close(self, _ws, code, reason) -> None:
        self.stop_heartbeat()
        print(f"[gateway] closed code={code} reason={reason}", flush=True)

    def close(self) -> None:
        self.stop_heartbeat()
        with self.lock:
            if self.ws:
                self.ws.close()


def validate_config() -> None:
    if not QQ_APP_ID:
        raise RuntimeError("QQ_APP_ID must be set in client/.env")
    if not QQ_APP_SECRET or QQ_APP_SECRET.startswith("replace-"):
        raise RuntimeError("QQ_APP_SECRET must be set in client/.env")
    if QQ_REPLY_MAX_CHARS < 100:
        raise RuntimeError("QQ_REPLY_MAX_CHARS is too small")


def main() -> int:
    try:
        validate_config()
    except Exception as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if websocket is None:
        print("config error: install websocket-client first: pip install websocket-client", file=sys.stderr)
        return 2

    debug_startup_diagnostics()
    websocket.enableTrace(env_bool("QQ_WEBSOCKET_TRACE", False))
    api = QQApi()
    jobs: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=QQ_JOB_QUEUE_SIZE)
    stop_event = threading.Event()
    threading.Thread(target=worker_loop, args=(api, jobs, stop_event), name="codex-worker", daemon=True).start()
    client = QQGatewayClient(api, jobs)

    print(
        f"qq gateway bridge starting app_id={QQ_APP_ID} intents={QQ_GATEWAY_INTENTS} "
        f"events={','.join(sorted(QQ_ALLOWED_EVENTS))}",
        flush=True,
    )
    send_startup_start_messages(api)

    try:
        while True:
            try:
                client.connect_once()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                print(f"[gateway-loop-error] {exc}", flush=True)
            time.sleep(QQ_RECONNECT_SECONDS)
    finally:
        stop_event.set()
        client.close()
        print("stopping", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
