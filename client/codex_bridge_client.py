#!/usr/bin/env python3
import json
import hashlib
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
STATE_FILE = DATA_DIR / "bridge-state.json"
PENDING_APPROVALS_FILE = DATA_DIR / "pending-approvals.json"
LEGACY_HISTORY_FILE = DATA_DIR / "remote-history.md"
CODEX_HOME = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
CODEX_SESSION_INDEX = CODEX_HOME / "session_index.jsonl"
CODEX_ARCHIVED_SESSIONS_DIR = CODEX_HOME / "archived_sessions"
CODEX_STATE_DB = CODEX_HOME / "state_5.sqlite"

_state_file_lock = threading.RLock()
_pending_file_lock = threading.RLock()


def write_json_atomic(path: Path, data: Dict[str, Any], lock: threading.RLock) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    with lock:
        try:
            tmp.write_text(payload, encoding="utf-8")
            for attempt in range(8):
                try:
                    tmp.replace(path)
                    return
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "开", "是"}


load_dotenv(BASE_DIR / ".env")

CODEX_COMMAND = os.getenv("CODEX_COMMAND", "codex")
CODEX_WORKDIR = os.getenv("CODEX_WORKDIR", str(Path.cwd()))
CODEX_MODEL = os.getenv("CODEX_MODEL", "gpt-5.5").strip() or "gpt-5.5"
CODEX_REASONING_EFFORT = os.getenv("CODEX_REASONING_EFFORT", "xhigh").strip() or "xhigh"
CODEX_PERMISSION = os.getenv("CODEX_PERMISSION", "read-only").strip() or "read-only"
CODEX_CONTEXT_MODE = os.getenv("CODEX_CONTEXT_MODE", "native").strip().lower() or "native"
BRIDGE_DEBUG = env_bool("BRIDGE_DEBUG", False)
QQ_OWNER_QQ = os.getenv("QQ_OWNER_QQ", "").strip()
MAX_HISTORY_CHARS = int(os.getenv("MAX_HISTORY_CHARS", "12000"))
CODEX_TIMEOUT_SECONDS = max(30, int(os.getenv("CODEX_TIMEOUT_SECONDS", "1800")))
RECENT_DEFAULT_COUNT = max(1, min(20, int(os.getenv("RECENT_DEFAULT_COUNT", "5"))))
TASK_STATUS_INTERVAL_SECONDS = max(0, int(os.getenv("QQ_TASK_STATUS_INTERVAL_SECONDS", "300")))
TRUNCATE_LONG_REPLIES = env_bool("QQ_TRUNCATE_LONG_REPLIES", True)

ALLOWED_MODELS = [
    item.strip()
    for item in os.getenv("CODEX_ALLOWED_MODELS", "gpt-5.5,gpt-5.4").split(",")
    if item.strip()
]

current_codex_lock = threading.Lock()
current_codex_proc: Optional[subprocess.Popen] = None
current_codex_job = ""
current_codex_tasks: Dict[str, subprocess.Popen] = {}
approved_run = threading.local()


def debug_log(message: str) -> None:
    if BRIDGE_DEBUG:
        print(f"[codex-debug] {message}", flush=True)


def debug_startup_diagnostics() -> None:
    if not BRIDGE_DEBUG:
        return
    resolved = codex_command_path()
    resolved_path = Path(resolved)
    workdir = Path(CODEX_WORKDIR)
    debug_log(f"python={sys.executable}")
    debug_log(f"platform={sys.platform} os.name={os.name}")
    debug_log(f"cwd={Path.cwd()}")
    debug_log(f"configured CODEX_COMMAND={CODEX_COMMAND!r}")
    debug_log(f"resolved codex={resolved!r}")
    debug_log(
        f"codex exists={resolved_path.exists()} "
        f"is_file={resolved_path.is_file() if resolved_path.exists() else False}"
    )
    debug_log(f"shutil.which(codex)={shutil.which('codex')!r}")
    debug_log(f"shutil.which(codex.exe)={shutil.which('codex.exe')!r}")
    debug_log(f"workdir={str(workdir)!r} exists={workdir.exists()} is_dir={workdir.is_dir()}")
    debug_log(
        f"context={CODEX_CONTEXT_MODE!r} model={CODEX_MODEL!r} "
        f"permission={CODEX_PERMISSION!r}"
    )


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))

PERMISSION_PROFILES = {
    "read-only": {
        "label": "只读",
        "sandbox": "read-only",
        "approval_policy": "never",
        "dangerous_bypass": False,
        "description": "只读沙箱，不写入文件，也不会生成远程审批请求",
    },
    "ask": {
        "label": "请求批准",
        "sandbox": "workspace-write",
        "approval_policy": "on-request",
        "approvals_reviewer": "user",
        "dangerous_bypass": False,
        "description": "普通任务先发到 QQ 等你批准；Codex 原生审批由用户确认",
    },
    "auto": {
        "label": "替我审批",
        "sandbox": "danger-full-access",
        "approval_policy": "on-request",
        "approvals_reviewer": "auto_review",
        "dangerous_bypass": False,
        "description": "不生成 QQ 待批准请求；不使用沙箱；Codex 对检测到的风险操作使用自动审批审查",
    },
    "full": {
        "label": "完全权限",
        "sandbox": "danger-full-access",
        "approval_policy": "never",
        "approvals_reviewer": "user",
        "dangerous_bypass": True,
        "description": "完全绕过沙箱和审批，风险最高",
    },
}


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def unix_time_to_iso(value: Any) -> str:
    try:
        seconds = float(value)
    except Exception:
        return ""
    if seconds <= 0:
        return ""
    if seconds > 10_000_000_000:
        seconds /= 1000
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(seconds))


def normalize_cwd(value: Any) -> str:
    cwd = str(value or "").strip()
    if cwd.startswith("\\\\?\\"):
        cwd = cwd[4:]
    return cwd


def normalize_workdir_for_compare(value: Any) -> str:
    cwd = normalize_cwd(value)
    if not cwd:
        return ""
    try:
        cwd = str(Path(cwd).resolve())
    except Exception:
        try:
            cwd = os.path.abspath(cwd)
        except Exception:
            pass
    return os.path.normcase(os.path.normpath(cwd))


def resolve_workdir(value: str) -> str:
    raw = (value or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.exists():
        raise ValueError(f"workdir does not exist: {raw}")
    if not path.is_dir():
        raise ValueError(f"workdir is not a directory: {raw}")
    return str(path.resolve())


def sort_key_updated_at(item: Dict[str, Any]) -> tuple[int, str]:
    raw = item.get("updated_at_raw")
    try:
        raw_int = int(raw)
    except Exception:
        raw_int = 0
    return (raw_int, str(item.get("updated_at", "")))


def codex_command_path() -> str:
    if os.name == "nt" and CODEX_COMMAND.lower() == "codex":
        return shutil.which("codex.cmd") or shutil.which("codex.exe") or CODEX_COMMAND
    return CODEX_COMMAND


def new_local_session_id() -> str:
    return time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]


def safe_local_session_id(session_id: str) -> str:
    session_id = session_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{4,80}", session_id):
        raise ValueError("invalid session id")
    return session_id


def safe_native_session_id(session_id: str) -> str:
    session_id = session_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{4,120}", session_id):
        raise ValueError("invalid session id")
    return session_id


def session_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{safe_local_session_id(session_id)}.md"


def normalize_context_mode(value: str) -> str:
    value = value.strip().lower()
    if value in {"native", "codex", "resume"}:
        return "native"
    if value in {"prompt", "local", "synthetic", "history"}:
        return "prompt"
    return "native"


def normalize_permission(value: str) -> str:
    value = re.sub(r"[\s_-]+", " ", value.strip().lower())
    compact = value.replace(" ", "")
    if compact in {"read", "readonly", "ro"}:
        return "read-only"
    if compact in {"ask", "askforapproval", "approval", "onrequest"}:
        return "ask"
    if compact in {"approve", "approveforme", "auto", "autowrite", "workspace", "write", "noapproval", "noapprovals", "noask", "direct"}:
        return "auto"
    if compact in {"full", "fullaccess", "danger", "dangerfullaccess"}:
        return "full"
    if value in PERMISSION_PROFILES:
        return value
    raise ValueError("unknown permission mode")


def normalize_reasoning(value: str) -> str:
    value = value.strip().lower()
    aliases = {
        "off": "none",
        "none": "none",
        "no": "none",
        "min": "minimal",
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "mid": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "extra": "xhigh",
        "max": "xhigh",
        "无": "none",
        "最低": "minimal",
        "低": "low",
        "中": "medium",
        "高": "high",
        "最高": "xhigh",
    }
    if value in aliases:
        return aliases[value]
    raise ValueError("unknown reasoning effort")


def normalize_model(value: str) -> str:
    value = value.strip().lower()
    compact = re.sub(r"[\s_-]+", "", value)
    if compact in {"gpt55", "gpt5.5"}:
        model = "gpt-5.5"
    elif compact in {"gpt54", "gpt5.4"}:
        model = "gpt-5.4"
    else:
        model = re.sub(r"\s+", "-", value)
        if model.startswith("gpt") and not model.startswith("gpt-"):
            model = "gpt-" + model[3:]
    if model not in ALLOWED_MODELS:
        raise ValueError("unsupported model")
    return model


def load_state() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    else:
        state = {}
    if not isinstance(state, dict):
        state = {}

    changed = False

    defaults = {
        "context_mode": normalize_context_mode(CODEX_CONTEXT_MODE),
        "workdir": CODEX_WORKDIR,
        "active_native_session_id": "",
        "model": CODEX_MODEL,
        "reasoning_effort": normalize_reasoning(CODEX_REASONING_EFFORT),
        "permission": normalize_permission(CODEX_PERMISSION),
        "timeout_seconds": CODEX_TIMEOUT_SECONDS,
        "recent_default_count": RECENT_DEFAULT_COUNT,
        "task_status_interval_seconds": TASK_STATUS_INTERVAL_SECONDS,
        "truncate_long_replies": TRUNCATE_LONG_REPLIES,
        "contacts": {},
    }
    for key, value in defaults.items():
        if key not in state:
            state[key] = value
            changed = True
    try:
        normalized_permission = normalize_permission(str(state.get("permission", defaults["permission"])))
    except ValueError:
        normalized_permission = defaults["permission"]
    if state.get("permission") != normalized_permission:
        state["permission"] = normalized_permission
        changed = True
    if int(state.get("timeout_seconds", 0) or 0) == 900 and CODEX_TIMEOUT_SECONDS > 900:
        state["timeout_seconds"] = CODEX_TIMEOUT_SECONDS
        changed = True

    sessions = state.get("sessions")
    if not isinstance(sessions, dict) or not sessions:
        session_id = new_local_session_id()
        history_text = ""
        title = "新对话"
        message_count = 0
        if LEGACY_HISTORY_FILE.exists() and LEGACY_HISTORY_FILE.stat().st_size > 0:
            history_text = LEGACY_HISTORY_FILE.read_text(encoding="utf-8", errors="replace")
            title = "迁移的旧对话"
            message_count = history_text.count("\n## ")
        session_path(session_id).write_text(history_text, encoding="utf-8")
        state["active_session_id"] = session_id
        state["sessions"] = {
            session_id: {
                "id": session_id,
                "title": title,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "message_count": message_count,
            }
        }
        changed = True
    elif state.get("active_session_id") not in sessions:
        state["active_session_id"] = next(iter(sessions))
        changed = True

    if changed:
        save_state(state)
    return state


def save_state(state: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(STATE_FILE, state, _state_file_lock)


def load_pending_approvals() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PENDING_APPROVALS_FILE.exists():
        return {"items": {}}
    try:
        data = json.loads(PENDING_APPROVALS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"items": {}}
    if not isinstance(data, dict):
        return {"items": {}}
    items = data.get("items")
    if not isinstance(items, dict):
        data["items"] = {}
    return data


def save_pending_approvals(data: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(PENDING_APPROVALS_FILE, data, _pending_file_lock)


def approval_id() -> str:
    return uuid.uuid4().hex[:8]


def cleanup_pending_approvals(max_age_seconds: int = 24 * 3600) -> None:
    data = load_pending_approvals()
    now_time = time.time()
    changed = False
    for item_id, item in list(data.get("items", {}).items()):
        try:
            created_at = float(item.get("created_at_epoch", 0))
        except Exception:
            created_at = 0
        if created_at and now_time - created_at > max_age_seconds:
            del data["items"][item_id]
            changed = True
    if changed:
        save_pending_approvals(data)


def get_pending_approval(item_id: str) -> Optional[Dict[str, Any]]:
    item_id = item_id.strip().lstrip("#")
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,32}", item_id):
        return None
    data = load_pending_approvals()
    item = data.get("items", {}).get(item_id)
    return item if isinstance(item, dict) else None


def delete_pending_approval(item_id: str) -> bool:
    item_id = item_id.strip().lstrip("#")
    data = load_pending_approvals()
    if item_id not in data.get("items", {}):
        return False
    del data["items"][item_id]
    save_pending_approvals(data)
    return True


def is_approved_execution() -> bool:
    return bool(getattr(approved_run, "enabled", False))


def current_process_runtime() -> Dict[str, Any]:
    runtime = current_runtime()
    force_permission = str(getattr(approved_run, "force_permission", "") or "")
    try:
        force_permission = normalize_permission(force_permission) if force_permission else ""
    except ValueError:
        force_permission = ""
    if force_permission in PERMISSION_PROFILES:
        runtime = dict(runtime)
        runtime["permission"] = force_permission
        runtime["permission_profile"] = PERMISSION_PROFILES[force_permission]
    return runtime


def command_head(text: str) -> str:
    text = (text or "").strip()
    if not text.startswith("/"):
        return ""
    return text.split(None, 1)[0].lower()


def is_bridge_command_text(text: str) -> bool:
    return command_head(text) in {
        "/start",
        "/help",
        "/status",
        "/whoami",
        "/model",
        "/setup",
        "/output",
        "/timeout",
        "/heartbeat",
        "/truncate",
        "/recent-default",
        "/approval-test",
        "/permission",
        "/workdir",
        "/cmd",
        "/ps",
        "/resume",
        "/recent",
        "/last",
        "/new",
        "/delete",
        "/cancel",
        "/restart",
        "/allow",
        "/reject",
        "/revise",
        "/pending",
    }


def can_run_without_approval(text: str) -> bool:
    lower = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not lower:
        return True
    risky_terms = [
        "改", "修改", "写入", "创建", "新增", "删除", "清理", "重启", "启动", "停止",
        "关闭", "安装", "卸载", "升级", "部署", "配置", "修复", "执行", "运行", "提交",
        "推送", "开放", "关闭端口", "防火墙", "证书", "nginx", "docker", "systemctl",
        "ufw", "iptables", "chmod", "chown", "remove-item", "move-item", "copy-item",
    ]
    risky_patterns = [
        r"\brm\s+-",
        r"\bdel\s+",
        r"\b(write|edit|modify|create|delete|remove|restart|start|stop|install|uninstall|upgrade|deploy|configure|fix|run|execute|commit|push|firewall|docker|nginx|systemctl|chmod|chown)\b",
    ]
    if any(term in lower for term in risky_terms) or any(re.search(pattern, lower) for pattern in risky_patterns):
        return False
    safe_terms = [
        "解释", "说明", "为什么", "怎么理解", "什么意思", "推荐", "建议", "怎么看",
        "能不能", "是否", "查一下", "看一下", "看看", "检查一下", "检查", "查询", "列出",
        "显示", "状态", "总结", "对比", "区别", "how", "why", "what", "explain", "recommend",
        "suggest", "list", "show", "check", "status",
    ]
    if any(term in lower for term in safe_terms):
        return True
    return False


def build_approval_plan(user_text: str, job: Dict[str, Any]) -> str:
    lower = user_text.lower()
    actions: List[str] = []
    impacts: List[str] = []

    if any(term in lower for term in ["重启", "restart", "启动", "停止", "systemctl", "服务"]):
        actions.extend(["确认目标服务名称和当前状态", "执行必要的启动/停止/重启操作", "检查端口、进程和最近日志"])
        impacts.append("相关服务可能会短暂中断")
    if any(term in lower for term in ["改", "修改", "写入", "配置", "修复", "edit", "modify", "configure", "fix"]):
        actions.extend(["读取相关配置或代码", "按你的要求做最小范围修改", "运行可用的检查或编译验证"])
        impacts.append("文件内容会发生变化")
    if any(term in lower for term in ["删除", "清理", "remove", "delete", "rm ", "del "]):
        actions.extend(["确认要删除或清理的目标", "只处理明确匹配的目标", "保留必要的状态和日志"])
        impacts.append("被删除的数据可能无法直接恢复")
    if any(term in lower for term in ["安装", "升级", "部署", "docker", "nginx", "install", "upgrade", "deploy"]):
        actions.extend(["检查当前环境和配置", "执行安装/升级/部署相关操作", "验证服务是否正常"])
        impacts.append("环境、依赖或服务配置可能变化")

    if not actions:
        actions = ["确认你的原始需求", "读取必要上下文", "执行最小范围操作并汇报结果"]
    if not impacts:
        impacts = ["可能读取本机文件或运行本地命令", "如涉及修改，会按批准后的需求执行"]

    deduped_actions = list(dict.fromkeys(actions))[:4]
    deduped_impacts = list(dict.fromkeys(impacts))[:3]
    lines = ["计划："]
    for index, action in enumerate(deduped_actions, 1):
        lines.append(f"{index}. {action}")
    lines.append("")
    lines.append("可能影响：")
    for impact in deduped_impacts:
        lines.append(f"- {impact}")
    lines.append("")
    lines.append("建议：")
    lines.append("- 发送 /allow {id} 批准执行，/reject {id} 拒绝，/revise {id} 修改要求。")
    return "\n".join(lines)


def create_pending_approval(job: Dict[str, Any]) -> str:
    cleanup_pending_approvals()
    item_id = approval_id()
    user_text = str(job.get("text", ""))
    plan = build_approval_plan(user_text, job).replace("{id}", item_id)
    runtime = current_runtime()
    item = {
        "id": item_id,
        "created_at": now_iso(),
        "created_at_epoch": time.time(),
        "from": str(job.get("from", "")),
        "job": job,
        "user_text": user_text,
        "plan": plan,
        "context_mode": runtime["context_mode"],
        "workdir": runtime["workdir"],
        "native_session_id": runtime["active_native_session_id"],
        "local_session_id": runtime["active_session_id"],
        "model": runtime["model"],
        "reasoning_effort": runtime["reasoning_effort"],
        "status": "pending",
    }
    data = load_pending_approvals()
    data.setdefault("items", {})[item_id] = item
    save_pending_approvals(data)
    return item


def approval_prompt_text(item: Dict[str, Any]) -> str:
    item_id = str(item.get("id", "")).strip()
    source = str(item.get("user_text", "")).strip()
    plan = str(item.get("plan", "")).strip()
    if len(source) > 700:
        source = source[:700].rstrip() + "\n[原消息过长，已截断显示]"
    return "\n".join([
        f"待批准操作 #{item_id}",
        "",
        "来源消息：",
        source or "(空)",
        "",
        plan or "没有生成计划。",
        "",
        "操作：",
        f"/allow {item_id} - 批准执行",
        f"/reject {item_id} - 拒绝并删除",
        f"/revise {item_id} <修改意见> - 修改要求后重新生成计划",
    ]).strip()


def list_pending_approvals() -> List[Dict[str, Any]]:
    cleanup_pending_approvals()
    data = load_pending_approvals()
    items = [item for item in data.get("items", {}).values() if isinstance(item, dict)]
    items.sort(
        key=lambda item: float(item.get("created_at_epoch", 0) or 0),
        reverse=True,
    )
    return items


def resolve_pending_approval_id(query: str) -> str:
    query = query.strip().lstrip("#")
    items = list_pending_approvals()
    if not query:
        return str(items[0].get("id", "")) if len(items) == 1 else ""
    exact = [str(item.get("id", "")) for item in items if str(item.get("id", "")) == query]
    if exact:
        return exact[0]
    matches = [str(item.get("id", "")) for item in items if str(item.get("id", "")).startswith(query)]
    return matches[0] if len(matches) == 1 else ""


def pending_approvals_text() -> str:
    items = list_pending_approvals()
    if not items:
        return "当前没有待批准操作。"
    lines = ["当前待批准操作："]
    for item in items[:12]:
        item_id = str(item.get("id", "")).strip()
        title = truncate_title(str(item.get("user_text", "")), 34)
        status = str(item.get("status", "pending"))
        lines.append(f"- #{item_id} | {status} | {title}")
    if len(items) > 12:
        lines.append(f"... 还有 {len(items) - 12} 个")
    lines.append("")
    lines.append("可用：/allow <id>、/reject <id>、/revise <id> <修改意见>")
    return "\n".join(lines)


def parse_pending_args(args: str) -> tuple[str, str]:
    args = (args or "").strip()
    if not args:
        return "", ""
    first, _, rest = args.partition(" ")
    return first.lstrip("#").strip(), rest.strip()


def approve_pending_approval(item_id: str) -> Optional[Dict[str, Any]]:
    item_id = item_id.strip().lstrip("#")
    if not item_id:
        return None
    data = load_pending_approvals()
    item = data.get("items", {}).get(item_id)
    if not isinstance(item, dict):
        return None
    item["status"] = "approved"
    item["approved_at"] = now_iso()
    data["items"][item_id] = item
    save_pending_approvals(data)
    return item


def reject_pending_approval(item_id: str) -> bool:
    item_id = item_id.strip().lstrip("#")
    if not item_id:
        return False
    return delete_pending_approval(item_id)


def revise_pending_approval(item_id: str, revision: str) -> Optional[Dict[str, Any]]:
    item_id = item_id.strip().lstrip("#")
    revision = revision.strip()
    if not item_id:
        return None
    data = load_pending_approvals()
    item = data.get("items", {}).get(item_id)
    if not isinstance(item, dict):
        return None
    if not revision:
        return item
    original = str(item.get("user_text", "")).strip()
    revised_text = original + "\n\n用户补充修改：\n" + revision
    item["user_text"] = revised_text
    item["plan"] = build_approval_plan(revised_text, item).replace("{id}", item_id)
    item["status"] = "pending"
    item["revised_at"] = now_iso()
    item["revision"] = revision
    data["items"][item_id] = item
    save_pending_approvals(data)
    return item


def should_request_bridge_approval(job: Dict[str, Any]) -> bool:
    runtime = current_runtime()
    if runtime["permission"] != "ask":
        return False
    if is_approved_execution():
        return False
    text = str(job.get("text", ""))
    if is_bridge_command_text(text):
        return False
    return not can_run_without_approval(text)


def prepare_bridge_approval(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not should_request_bridge_approval(job):
        return None
    return create_pending_approval(job)


def run_approved_pending_approval(item: Dict[str, Any]) -> str:
    item_id = str(item.get("id", "")).strip()
    job = dict(item.get("job") or {})
    user_text = str(item.get("user_text", "")).strip()
    if not job:
        raise RuntimeError("pending approval has no stored job")
    job["text"] = user_text

    state = load_state()
    context_mode = normalize_context_mode(str(item.get("context_mode", state.get("context_mode", CODEX_CONTEXT_MODE))))
    if context_mode == "native":
        state["context_mode"] = "native"
        state["workdir"] = str(item.get("workdir", state.get("workdir", CODEX_WORKDIR)))
        state["active_native_session_id"] = str(item.get("native_session_id", state.get("active_native_session_id", "")))
    else:
        local_id = str(item.get("local_session_id", state.get("active_session_id", "")))
        if local_id in state.get("sessions", {}):
            state["context_mode"] = "prompt"
            state["active_session_id"] = local_id
    save_state(state)

    old_enabled = getattr(approved_run, "enabled", False)
    old_force_permission = getattr(approved_run, "force_permission", "")
    approved_run.enabled = True
    approved_run.force_permission = "auto"
    try:
        append_history("User", f"[approved #{item_id}]\n{user_text}")
        answer = run_codex(job)
        append_history("Codex", answer)
    finally:
        approved_run.enabled = old_enabled
        approved_run.force_permission = old_force_permission
    delete_pending_approval(item_id)
    return answer


def current_runtime() -> Dict[str, Any]:
    state = load_state()
    permission = normalize_permission(str(state.get("permission", "read-only")))
    return {
        "context_mode": normalize_context_mode(str(state.get("context_mode", CODEX_CONTEXT_MODE))),
        "workdir": str(state.get("workdir", CODEX_WORKDIR)),
        "model": str(state.get("model", CODEX_MODEL)),
        "reasoning_effort": normalize_reasoning(str(state.get("reasoning_effort", CODEX_REASONING_EFFORT))),
        "permission": permission,
        "permission_profile": PERMISSION_PROFILES[permission],
        "active_session_id": str(state.get("active_session_id", "")),
        "active_native_session_id": str(state.get("active_native_session_id", "")),
        "timeout_seconds": int(state.get("timeout_seconds", CODEX_TIMEOUT_SECONDS) or CODEX_TIMEOUT_SECONDS),
        "recent_default_count": clamp_int(int(state.get("recent_default_count", RECENT_DEFAULT_COUNT) or RECENT_DEFAULT_COUNT), 1, 20),
        "task_status_interval_seconds": clamp_int(int(state.get("task_status_interval_seconds", TASK_STATUS_INTERVAL_SECONDS) or 0), 0, 86400),
        "truncate_long_replies": bool(state.get("truncate_long_replies", TRUNCATE_LONG_REPLIES)),
    }


def job_session_key(job: Dict[str, Any]) -> str:
    runtime = current_process_runtime()
    job.setdefault("_context_mode", runtime["context_mode"])
    job.setdefault("_workdir", runtime["workdir"])
    job.setdefault("_active_session_id", runtime["active_session_id"])
    job.setdefault("_active_native_session_id", runtime["active_native_session_id"])
    if runtime["context_mode"] == "native":
        return "native:" + (runtime["active_native_session_id"] or f"new:{runtime['workdir']}")
    return "local:" + runtime["active_session_id"]


def truncate_title(text: str, limit: int = 36) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return "新对话"
    return text[:limit] + ("..." if len(text) > limit else "")


def append_history(role: str, text: str) -> None:
    if current_runtime()["context_mode"] == "native":
        return

    state = load_state()
    session_id = str(state["active_session_id"])
    meta = state["sessions"][session_id]
    history_file = session_path(session_id)
    with history_file.open("a", encoding="utf-8") as f:
        f.write(f"\n\n## {role} {now_iso()}\n\n")
        f.write(text.strip() + "\n")

    meta["updated_at"] = now_iso()
    meta["message_count"] = int(meta.get("message_count", 0)) + 1
    if role.lower() == "user" and meta.get("title") == "新对话":
        meta["title"] = truncate_title(text)
    save_state(state)


def read_history_tail(session_id: Optional[str] = None) -> str:
    state = load_state()
    session_id = safe_local_session_id(session_id or str(state["active_session_id"]))
    history_file = session_path(session_id)
    if not history_file.exists():
        return ""
    text = history_file.read_text(encoding="utf-8", errors="replace")
    return text[-MAX_HISTORY_CHARS:]


def parse_jsonl_file(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            items.append(value)
    return items


def read_session_meta(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            line = f.readline().strip()
    except Exception:
        return None
    if not line:
        return None
    try:
        event = json.loads(line)
    except Exception:
        return None
    if event.get("type") != "session_meta":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    session_id = str(payload.get("id", ""))
    if not session_id:
        return None
    return {
        "id": session_id,
        "thread_name": "",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime)),
        "created_at": str(payload.get("timestamp", "")),
        "cwd": str(payload.get("cwd", "")),
        "originator": str(payload.get("originator", "")),
        "source": str(payload.get("source", "")),
        "path": str(path),
        "size": path.stat().st_size,
    }


GENERIC_NATIVE_TITLES = {
    "",
    "codex",
    "codex cli",
    "codex exec",
    "codex tui",
    "codex_exec",
    "codex-tui",
    "codex desktop",
    "codex desktop app",
    "openai codex",
    "openai codex desktop",
}

REQUEST_TITLE_MARKERS = (
    "My request for Codex:",
    "My request for Codex：",
    "我的请求:",
    "我的请求：",
    "远程用户消息:",
    "远程用户消息：",
    "用户消息:",
    "用户消息：",
)
NATIVE_TITLE_TAIL_BYTES = 2 * 1024 * 1024
LOW_VALUE_TITLES = {
    "ok",
    "ook",
    "okay",
    "好的",
    "好",
    "可以",
    "继续",
    "接着",
    "你好",
    "在吗",
    "codex_exec",
    "codex tui",
    "codex-tui",
}
BAD_TITLE_PREFIXES = (
    "<environment_context>",
    "<turn_aborted>",
    "<permissions instructions>",
    "<app-context>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<plugins_instructions>",
    "Another language model started",
    "The following is the Codex agent history",
    "你正在通过 QQ 远程消息桥接",
    "你正在通过远程消息桥接",
)

TITLE_LEAD_RE = re.compile(
    r"^(?:"
    r"请|麻烦|帮我|帮忙|能不能|能否|可以|可不可以|你能不能|你能|你可以|"
    r"我想|我需要|我希望|我打算|想请你|想让你|想办法|帮我看看|帮我查查|"
    r"帮我处理|帮我配置|帮我做一下|现在先|现在|继续|接着|顺便|把|将"
    r")\s*[，,。！？?：:\s]*"
)


def _extract_input_text(value: Any) -> str:
    pieces: List[str] = []
    if isinstance(value, dict):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return ""

    for item in items:
        if not isinstance(item, dict):
            if isinstance(item, str) and item.strip():
                pieces.append(item.strip())
            continue
        item_type = str(item.get("type", ""))
        if item_type in {"input_text", "output_text", "text"}:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                pieces.append(text.strip())
    return "\n".join(pieces).strip()


def _extract_native_user_text(line: str) -> str:
    if "user_message" not in line and '"role"' not in line:
        return ""
    try:
        event = json.loads(line)
    except Exception:
        return ""

    event_type = str(event.get("type") or "")
    if event_type == "response_item":
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return ""
        if payload.get("type") == "message" and payload.get("role") == "user":
            return _extract_input_text(payload.get("content"))
        return ""

    if event_type == "event_msg":
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return ""
        if payload.get("type") == "user_message":
            message = payload.get("message")
            if isinstance(message, str):
                return message.strip()
        return ""

    return ""


def _event_timestamp_text(event: Dict[str, Any]) -> str:
    raw = str(event.get("timestamp") or event.get("created_at") or event.get("time") or "").strip()
    if not raw:
        return ""
    return raw.replace("T", " ").replace("Z", "").split(".")[0]


def _extract_native_conversation_message(line: str) -> Optional[Dict[str, str]]:
    if "message" not in line and "agent_message" not in line and '"role"' not in line:
        return None
    try:
        event = json.loads(line)
    except Exception:
        return None

    event_type = str(event.get("type") or "")
    if event_type == "response_item":
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        if payload.get("type") == "message":
            role = str(payload.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                return None
            text = _extract_input_text(payload.get("content"))
            if text:
                return {"role": role, "text": text, "time": _event_timestamp_text(event)}
        return None

    if event_type in {"item.completed", "item.started"}:
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type") or "")
        if item_type == "agent_message":
            text = str(item.get("text") or "").strip()
            if text:
                return {"role": "assistant", "text": text, "time": _event_timestamp_text(event)}
        return None

    if event_type == "event_msg":
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        if payload.get("type") == "user_message":
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return {"role": "user", "text": message.strip(), "time": _event_timestamp_text(event)}
        return None

    return None


def _clean_native_title(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return ""

    for marker in REQUEST_TITLE_MARKERS:
        idx = text.rfind(marker)
        if idx >= 0:
            text = text[idx + len(marker):]
            break

    if text.startswith(BAD_TITLE_PREFIXES):
        return ""

    lines: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("```", "<image", "</image>", "![", "# Files mentioned", "## Files mentioned")):
            continue
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"^\[Image\s+#?\d+\]\s*", "", line, flags=re.IGNORECASE)
        line = re.sub(r"^[>\-\*\u2022]+\s*", "", line)
        line = re.sub(r"^\d+[.)]\s*", "", line)
        line = line.strip()
        if line:
            lines.append(line)

    if not lines:
        return ""

    text = lines[0]
    text = re.sub(r"\s+", " ", text).strip()
    text = TITLE_LEAD_RE.sub("", text)
    text = text.strip(" \t\r\n,，。！？?：:；;）)]】》\"'“”‘’")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.startswith(BAD_TITLE_PREFIXES):
        return ""
    if len(text) > 48:
        text = text[:48].rstrip()
    return text


def _is_low_value_title(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", (title or "").strip()).lower()
    if not normalized:
        return True
    if normalized in LOW_VALUE_TITLES:
        return True
    if re.fullmatch(r"/[a-z][a-z0-9_-]*(?:\s+\S+){0,2}", normalized) and len(normalized) <= 24:
        return True
    return False


def _choose_native_title(candidates: List[str]) -> str:
    for raw_text in reversed(candidates[-12:]):
        title = _clean_native_title(raw_text)
        if not title:
            continue
        if _is_low_value_title(title):
            continue
        return title
    return ""


def native_title_from_text(text: str) -> str:
    title = _clean_native_title(text)
    if not title or _is_low_value_title(title):
        return ""
    return title


@lru_cache(maxsize=512)
def _native_session_title_cached(path_str: str, mtime_ns: int, size: int) -> str:
    candidates: List[str] = []
    try:
        with Path(path_str).open("rb") as f:
            if size > NATIVE_TITLE_TAIL_BYTES:
                f.seek(size - NATIVE_TITLE_TAIL_BYTES)
            raw = f.read().decode("utf-8", errors="replace")
        for line in raw.splitlines():
            candidate = _extract_native_user_text(line.strip())
            if candidate:
                candidates.append(candidate)
    except Exception:
        return ""
    return _choose_native_title(candidates)


def native_session_recent_title(path: Path) -> str:
    try:
        stat = path.stat()
    except Exception:
        return ""
    return _native_session_title_cached(str(path), stat.st_mtime_ns, stat.st_size)


def _is_generic_native_title(title: str, originator: str = "") -> bool:
    normalized = re.sub(r"\s+", " ", (title or "").strip()).lower()
    if normalized in GENERIC_NATIVE_TITLES:
        return True
    originator = re.sub(r"\s+", " ", (originator or "").strip()).lower()
    return bool(normalized) and normalized == originator


def _clean_thread_text_title(text: Any) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    return native_title_from_text(text)


def is_meaningful_resume_title(title: str, fallback_id: str = "") -> bool:
    normalized = re.sub(r"\s+", " ", (title or "").strip()).lower()
    if not normalized:
        return False
    if fallback_id:
        fid = fallback_id.strip().lower()
        short_fid = fid[-8:]
        if normalized == fid or normalized == short_fid:
            return False
    if normalized in GENERIC_NATIVE_TITLES:
        return False
    if normalized in LOW_VALUE_TITLES:
        return False
    if re.fullmatch(r"[0-9a-f]{6,12}", normalized):
        return False
    return True


def session_title(item: Dict[str, Any], fallback_id: str) -> str:
    title = _clean_thread_text_title(item.get("thread_name") or item.get("title"))
    if not title:
        title = _clean_thread_text_title(item.get("first_user_message") or item.get("preview"))
    originator = str(item.get("originator") or "").strip()
    if _is_generic_native_title(title, originator):
        path = str(item.get("path") or "").strip()
        if path:
            title = native_session_recent_title(Path(path)) or ""
        else:
            title = ""
    if not title:
        title = fallback_id[-8:] if fallback_id else "未命名"
    title = re.sub(r"\s+", " ", title).strip()
    return title


def native_session_group_label(cwd: str) -> str:
    cwd = re.sub(r"\s+", " ", (cwd or "").strip())
    if not cwd:
        return "(未记录目录)"
    name = Path(cwd).name or cwd
    if name and name != cwd:
        return f"{name} | {cwd}"
    return cwd


def native_session_group_token(cwd: str) -> str:
    return hashlib.sha1((cwd or "").encode("utf-8", errors="surrogatepass")).hexdigest()[:10]


def resolve_native_session_group_token(token: str, limit: int = 10000) -> str:
    token = (token or "").strip().lstrip("@").lower()
    if not re.fullmatch(r"[0-9a-f]{6,40}", token):
        return ""

    matches: List[str] = []
    seen: set[str] = set()
    for item in list_native_sessions(limit=limit, include_unlisted=True):
        cwd = str(item.get("cwd") or "").strip()
        if cwd in seen:
            continue
        seen.add(cwd)
        if native_session_group_token(cwd).startswith(token):
            matches.append(cwd)
    return matches[0] if len(matches) == 1 else ""


def resolve_native_session_id(query: str, limit: int = 10000) -> str:
    query = (query or "").strip().lstrip("#")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{4,120}", query):
        return ""

    matches: List[str] = []
    for item in list_native_sessions(limit=limit):
        session_id = str(item.get("id", ""))
        if session_id == query:
            return session_id
        if session_id.startswith(query) or session_id.endswith(query):
            matches.append(session_id)
    return matches[0] if len(matches) == 1 else ""


def native_session_groups(limit: int = 200) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for item in list_native_sessions(limit=limit):
        cwd = str(item.get("cwd") or "").strip()
        group = groups.setdefault(
            cwd,
            {
                "cwd": cwd,
                "label": native_session_group_label(cwd),
                "items": [],
                "updated_at": "",
                "count": 0,
                "latest_title": "",
            },
        )
        group["items"].append(item)
        group["count"] += 1
        updated_raw = int(item.get("updated_at_raw") or 0)
        if updated_raw > int(group.get("updated_at_raw") or 0):
            group["updated_at_raw"] = updated_raw
            group["updated_at"] = str(item.get("updated_at", ""))

    grouped = list(groups.values())
    for group in grouped:
        group["items"].sort(key=sort_key_updated_at, reverse=True)
        if group["items"]:
            latest = group["items"][0]
            group["latest_title"] = session_title(latest, str(latest.get("id", "")))
    grouped.sort(key=lambda item: (int(item.get("updated_at_raw") or 0), str(item.get("updated_at", ""))), reverse=True)
    return grouped


def list_native_sessions_for_workdir(workdir: str, limit: int = 20) -> List[Dict[str, Any]]:
    target = normalize_workdir_for_compare(workdir)
    if not target:
        return []
    matched = [
        item
        for item in list_native_sessions(limit=10000)
        if normalize_workdir_for_compare(item.get("cwd")) == target
    ]
    matched.sort(key=sort_key_updated_at, reverse=True)
    return matched[:limit]


def parse_resume_args(args: str) -> Dict[str, Any]:
    raw = (args or "").strip()
    if not raw:
        return {"mode": "groups", "page": 1, "cwd": ""}

    match = re.fullmatch(r"(?:page|p)\s+(\d+)", raw, flags=re.IGNORECASE)
    if match:
        return {"mode": "groups", "page": max(1, int(match.group(1))), "cwd": ""}

    lowered = raw.lower()
    if lowered in {"dir", "d"}:
        return {"mode": "groups", "page": 1, "cwd": ""}

    dir_match = re.match(r"^(?:dir|d)\s+(.+)$", raw, flags=re.IGNORECASE)
    if dir_match:
        remainder = dir_match.group(1).strip()
        page = 1
        match = re.search(r"(?:^|\s)(?:page|p)\s+(\d+)\s*$", remainder, flags=re.IGNORECASE)
        if match:
            page = max(1, int(match.group(1)))
            remainder = remainder[:match.start()].strip()
        cwd = ""
        if remainder.startswith("@"):
            cwd = resolve_native_session_group_token(remainder)
        elif remainder:
            cwd = unquote(remainder)
        if cwd:
            return {"mode": "sessions", "page": page, "cwd": cwd}
        return {"mode": "groups", "page": page, "cwd": ""}

    return {"mode": "session_id", "page": 1, "cwd": "", "session_id": raw}


def list_native_session_files() -> Dict[str, Dict[str, Any]]:
    root = CODEX_HOME / "sessions"
    sessions: Dict[str, Dict[str, Any]] = {}
    if not root.exists():
        return sessions
    for path in root.rglob("*.jsonl"):
        meta = read_session_meta(path)
        if not meta:
            continue
        session_id = meta["id"]
        old = sessions.get(session_id)
        if not old or str(meta.get("updated_at", "")) > str(old.get("updated_at", "")):
            sessions[session_id] = meta
    return sessions


def archived_native_session_ids() -> set[str]:
    session_ids: set[str] = set()
    if not CODEX_ARCHIVED_SESSIONS_DIR.exists():
        return session_ids
    for path in CODEX_ARCHIVED_SESSIONS_DIR.glob("*.jsonl"):
        meta = read_session_meta(path)
        if meta:
            session_ids.add(str(meta.get("id", "")))
            continue
        match = re.search(r"-(019[A-Za-z0-9_.-]+)\.jsonl$", path.name)
        if match:
            session_ids.add(match.group(1))
    return {session_id for session_id in session_ids if session_id}


def list_native_threads_from_db() -> Dict[str, Dict[str, Any]]:
    threads: Dict[str, Dict[str, Any]] = {}
    if not CODEX_STATE_DB.exists():
        return threads
    try:
        uri = f"file:{CODEX_STATE_DB.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """
                select id, rollout_path, created_at, updated_at,
                       created_at_ms, updated_at_ms, cwd, title,
                       first_user_message, preview, source, thread_source,
                       archived, archived_at, has_user_event
                from threads
                where coalesce(archived, 0) = 0
                """
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return threads

    for row in rows:
        session_id = str(row["id"] or "").strip()
        if not session_id:
            continue
        updated_raw = int(row["updated_at_ms"] or row["updated_at"] or 0)
        created_raw = int(row["created_at_ms"] or row["created_at"] or 0)
        path = str(row["rollout_path"] or "").strip()
        threads[session_id] = {
            "id": session_id,
            "thread_name": str(row["title"] or "").strip(),
            "title": str(row["title"] or "").strip(),
            "first_user_message": str(row["first_user_message"] or "").strip(),
            "preview": str(row["preview"] or "").strip(),
            "updated_at": unix_time_to_iso(updated_raw),
            "updated_at_raw": updated_raw,
            "created_at": unix_time_to_iso(created_raw),
            "created_at_raw": created_raw,
            "cwd": normalize_cwd(row["cwd"]),
            "originator": str(row["source"] or "").strip(),
            "source": str(row["thread_source"] or row["source"] or "").strip(),
            "path": path,
            "size": Path(path).stat().st_size if path and Path(path).exists() else 0,
            "archived": bool(row["archived"]),
            "has_user_event": bool(row["has_user_event"]),
        }
    return threads


def list_native_sessions(limit: int = 20, include_unlisted: bool = False) -> List[Dict[str, Any]]:
    db_threads = list_native_threads_from_db()
    if db_threads:
        sessions = []
        for item in db_threads.values():
            if bool(item.get("archived")):
                continue
            sid = str(item.get("id", ""))
            if not include_unlisted:
                title = session_title(item, sid)
                if not is_meaningful_resume_title(title, sid):
                    continue
            sessions.append(item)
        sessions.sort(key=sort_key_updated_at, reverse=True)
        return sessions[:limit]

    archived = archived_native_session_ids()
    merged = {
        session_id: item
        for session_id, item in list_native_session_files().items()
        if session_id not in archived
    }
    for item in parse_jsonl_file(CODEX_SESSION_INDEX):
        session_id = str(item.get("id", ""))
        if not session_id or session_id in archived:
            continue
        existing = merged.get(session_id, {})
        merged[session_id] = {
            **existing,
            **item,
            "path": existing.get("path", ""),
            "size": existing.get("size", 0),
        }
    sessions = []
    for item in merged.values():
        if bool(item.get("archived")):
            continue
        sid = str(item.get("id", ""))
        if not include_unlisted:
            title = session_title(item, sid)
            if not is_meaningful_resume_title(title, sid):
                continue
        sessions.append(item)
    sessions.sort(
        key=lambda item: (int(item.get("updated_at_raw") or 0), str(item.get("updated_at", ""))),
        reverse=True,
    )
    return sessions[:limit]


def find_native_session(session_id: str) -> Optional[Dict[str, Any]]:
    session_id = safe_native_session_id(session_id)
    for item in list_native_sessions(limit=10000, include_unlisted=True):
        if str(item.get("id", "")) == session_id:
            return item
    return None


def native_session_messages(session_id: str, limit: int = 0) -> List[Dict[str, str]]:
    item = find_native_session(session_id)
    if not item:
        return []
    path = Path(str(item.get("path") or ""))
    if not path.exists():
        return []

    messages: List[Dict[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    for line in lines:
        message = _extract_native_conversation_message(line.strip())
        if message:
            messages.append(message)
    return messages[-limit:] if limit > 0 else messages


def current_native_session_id() -> str:
    state = load_state()
    return str(state.get("active_native_session_id", "")).strip()


def native_session_rollout_path(session_id: str) -> Optional[Path]:
    try:
        item = find_native_session(session_id)
    except ValueError:
        return None
    if not item:
        return None
    path = str(item.get("path") or "").strip()
    if not path:
        return None
    return Path(path)


def truncate_message_text(text: str, limit: int = 0) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if limit <= 0:
        return text
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 12)].rstrip() + " ...[截断]"


def format_native_messages(messages: List[Dict[str, str]], title: str = "") -> str:
    if not messages:
        return "没有找到可展示的最近对话内容。"
    blocks = [title] if title else []
    for index, message in enumerate(messages, 1):
        role = "我" if message.get("role") == "user" else "Codex"
        message_time = str(message.get("time") or "").strip()
        text = truncate_message_text(message.get("text", ""))
        time_suffix = f" | {message_time}" if message_time else ""
        blocks.append(f"--- {index}. {role}{time_suffix} ---\n{text}")
    return "\n\n".join(blocks)


def clamp_recent_count(value: int) -> int:
    return clamp_int(value, 1, 20)


def parse_recent_args(args: str) -> Dict[str, Any]:
    raw = (args or "").strip()
    default_count = int(current_runtime().get("recent_default_count", RECENT_DEFAULT_COUNT) or RECENT_DEFAULT_COUNT)
    result: Dict[str, Any] = {"count": clamp_recent_count(default_count), "start": 1, "session_id": ""}
    number_index = 0
    for token in raw.split():
        if re.fullmatch(r"\d+", token):
            number_index += 1
            if number_index == 1:
                result["count"] = clamp_recent_count(int(token))
            elif number_index == 2:
                result["start"] = max(1, int(token))
        elif token.lower() in {"last", "latest"} or token in {"最近"}:
            continue
        else:
            result["session_id"] = token
    return result


def recent_window(messages: List[Dict[str, str]], count: int, start: int) -> List[Dict[str, str]]:
    count = clamp_recent_count(count)
    start = max(1, start)
    newest_first = list(reversed(messages))
    selected = newest_first[start - 1:start - 1 + count]
    return list(reversed(selected))


def recent_command(args: str = "") -> str:
    parsed = parse_recent_args(args)
    session_id = str(parsed.get("session_id") or "").strip()
    if session_id:
        session_id = resolve_native_session_id(session_id)
    else:
        session_id = current_native_session_id()
    if not session_id:
        return "当前没有 active Codex 原生会话。先发送 /resume 选择会话，或发送 /new 创建新会话。"
    default_count = int(current_runtime().get("recent_default_count", RECENT_DEFAULT_COUNT) or RECENT_DEFAULT_COUNT)
    count = clamp_recent_count(int(parsed.get("count") or default_count))
    start = max(1, int(parsed.get("start") or 1))
    messages = native_session_messages(session_id)
    selected = recent_window(messages, count, start)
    end = start + len(selected) - 1 if selected else start + count - 1
    return format_native_messages(selected, f"最近第 {start}-{end} 条对话 | {session_id[-8:]}")


def last_command(args: str = "") -> str:
    tokens = (args or "").strip().split()
    mode = tokens[0].lower() if tokens else ""
    rest = " ".join(tokens[1:]) if tokens else ""
    parsed = parse_recent_args(rest)
    default_count = int(current_runtime().get("recent_default_count", RECENT_DEFAULT_COUNT) or RECENT_DEFAULT_COUNT)
    count = clamp_recent_count(int(parsed.get("count") or default_count))
    start = max(1, int(parsed.get("start") or 1))
    session_id = current_native_session_id()
    if not session_id:
        return "当前没有 active Codex 原生会话。先发送 /resume 选择会话。"
    messages = native_session_messages(session_id)
    if mode in {"user", "me", "mine", "u", "我"}:
        messages = [item for item in messages if item.get("role") == "user"]
        selected = recent_window(messages, count, start)
        end = start + len(selected) - 1 if selected else start + count - 1
        return format_native_messages(selected, f"我发出的最近第 {start}-{end} 条")
    if mode in {"codex", "assistant", "answer", "a", "回复"}:
        messages = [item for item in messages if item.get("role") == "assistant"]
        selected = recent_window(messages, count, start)
        end = start + len(selected) - 1 if selected else start + count - 1
        return format_native_messages(selected, f"Codex 最近第 {start}-{end} 条回复")
    selected = recent_window(messages, count, start)
    end = start + len(selected) - 1 if selected else start + count - 1
    return format_native_messages(selected, f"最近第 {start}-{end} 条对话")


def extract_thread_id(stdout: str) -> str:
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("type") == "thread.started":
            return str(event.get("thread_id", ""))
    return ""


def codex_base_cmd(runtime: Dict[str, Any], output_file: Path) -> List[str]:
    profile = runtime["permission_profile"]
    cmd = [
        codex_command_path(),
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--cd",
        str(runtime.get("workdir") or CODEX_WORKDIR),
        "--output-last-message",
        str(output_file),
    ]
    if runtime["model"]:
        cmd[2:2] = ["--model", runtime["model"]]
    cmd[2:2] = [
        "-c",
        f"approval_policy=\"{profile['approval_policy']}\"",
        "-c",
        f"approvals_reviewer=\"{profile.get('approvals_reviewer', 'user')}\"",
        "-c",
        f"model_reasoning_effort=\"{runtime['reasoning_effort']}\"",
    ]
    if profile["dangerous_bypass"]:
        cmd[2:2] = ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        cmd[2:2] = ["--sandbox", profile["sandbox"]]
    return cmd


def kill_process_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    else:
        proc.kill()


def cancel_current_task(task_id: str = "") -> str:
    global current_codex_proc
    task_id = (task_id or "").strip()
    with current_codex_lock:
        if task_id:
            proc = current_codex_tasks.get(task_id)
            job = task_id
        else:
            proc = current_codex_proc
            job = current_codex_job
    if not proc or proc.poll() is not None:
        return "没有找到正在运行的 Codex 任务。" if task_id else "当前没有正在运行的 Codex 任务。"

    try:
        kill_process_tree(proc)
    except Exception as exc:
        return f"取消失败：{exc}"
    return f"已取消当前 Codex 任务。pid={proc.pid}" + (f" job={job}" if job else "")


def cancel_pending_target() -> str:
    items = list_pending_approvals()
    if not items:
        return ""
    item_id = str(items[0].get("id", "")).strip()
    if not item_id:
        return ""
    if delete_pending_approval(item_id):
        return f"已取消待批准操作 #{item_id}。"
    return ""


def run_codex_process(
    prompt: str,
    session_id: str = "",
    job_id: str = "",
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    runtime_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    global current_codex_proc, current_codex_job
    runtime = runtime_override or current_process_runtime()
    with tempfile.TemporaryDirectory(prefix="codex-bridge-") as tmp:
        output_file = Path(tmp) / "last-message.txt"
        cmd = codex_base_cmd(runtime, output_file)
        if session_id:
            cmd.extend(["resume", safe_native_session_id(session_id), "-"])
        else:
            cmd.append("-")

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        executable = cmd[0]
        debug_log(f"launch job={job_id or '(none)'} session={session_id or '(new)'}")
        debug_log(f"argv={cmd!r}")
        debug_log(f"launch cwd={runtime.get('workdir')!r}")
        debug_log(f"executable={executable!r}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except OSError as exc:
            resolved_path = Path(str(executable))
            debug_log(
                f"Popen failed type={type(exc).__name__} errno={getattr(exc, 'errno', None)} "
                f"winerror={getattr(exc, 'winerror', None)} message={exc}"
            )
            debug_log(
                f"executable exists={resolved_path.exists()} "
                f"is_file={resolved_path.is_file() if resolved_path.exists() else False}"
            )
            debug_log(
                f"workdir exists={Path(str(runtime.get('workdir') or '')).exists()} "
                f"is_dir={Path(str(runtime.get('workdir') or '')).is_dir()}"
            )
            debug_log(f"PATH={os.environ.get('PATH', '')}")
            raise RuntimeError(
                f"启动 Codex 失败：{exc}；executable={executable!r}；"
                f"workdir={runtime.get('workdir')!r}"
            ) from exc
        with current_codex_lock:
            current_codex_proc = proc
            current_codex_job = job_id
            if job_id:
                current_codex_tasks[job_id] = proc
        if proc.stdin:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except Exception:
                pass
        events: List[str] = []
        stdout_queue: "queue.Queue[str]" = queue.Queue()

        def reader() -> None:
            if not proc.stdout:
                return
            for line in proc.stdout:
                stdout_queue.put(line)

        threading.Thread(target=reader, name=f"codex-json-reader-{job_id or proc.pid}", daemon=True).start()
        try:
            timeout_seconds = int(runtime.get("timeout_seconds") or CODEX_TIMEOUT_SECONDS)
            deadline = time.time() + timeout_seconds
            while True:
                try:
                    line = stdout_queue.get(timeout=0.2)
                    events.append(line)
                    stripped = line.strip()
                    if stripped.startswith("{"):
                        try:
                            event = json.loads(stripped)
                        except Exception:
                            event = None
                        if isinstance(event, dict) and on_event:
                            on_event(event)
                except queue.Empty:
                    pass
                if proc.poll() is not None:
                    while True:
                        try:
                            line = stdout_queue.get_nowait()
                        except queue.Empty:
                            break
                        events.append(line)
                        stripped = line.strip()
                        if stripped.startswith("{"):
                            try:
                                event = json.loads(stripped)
                            except Exception:
                                event = None
                            if isinstance(event, dict) and on_event:
                                on_event(event)
                    break
                if time.time() > deadline:
                    cancel_current_task(job_id)
                    minutes = int((timeout_seconds + 59) / 60)
                    raise RuntimeError(
                        f"Codex task timed out after {minutes} min and was cancelled. "
                        "可发送 /timeout 45 调长，或 /cancel <task_id> 取消指定任务。"
                    )
        finally:
            with current_codex_lock:
                if current_codex_proc is proc:
                    current_codex_proc = None
                    current_codex_job = ""
                if job_id and current_codex_tasks.get(job_id) is proc:
                    del current_codex_tasks[job_id]

        stdout = "".join(events)
        result = ""
        if output_file.exists():
            result = output_file.read_text(encoding="utf-8", errors="replace").strip()
        if not result:
            result = stdout.strip()
        thread_id = extract_thread_id(stdout)

        if proc.returncode != 0:
            raise RuntimeError(result or stdout or f"codex exited with {proc.returncode}")
        return {
            "text": result or "(Codex returned an empty response.)",
            "thread_id": thread_id,
        }


def create_local_session(title: str = "") -> str:
    state = load_state()
    session_id = new_local_session_id()
    state["sessions"][session_id] = {
        "id": session_id,
        "title": truncate_title(title) if title else "新对话",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "message_count": 0,
    }
    state["active_session_id"] = session_id
    session_path(session_id).write_text("", encoding="utf-8")
    save_state(state)
    return session_id


def create_native_session(title: str = "") -> str:
    prompt = "创建一段新的 QQ 远程 Codex 会话。请只回复：新对话已开启。"
    if title:
        prompt += f"\n会话标题：{title}"
    result = run_codex_process(prompt, "")
    thread_id = result.get("thread_id", "")
    if not thread_id:
        raise RuntimeError("Codex did not return a thread_id")
    state = load_state()
    state["context_mode"] = "native"
    state["active_native_session_id"] = thread_id
    save_state(state)
    return thread_id


def session_list_text(args: str = "") -> str:
    runtime = current_runtime()
    if runtime["context_mode"] == "native":
        active = runtime["active_native_session_id"] or "(none)"
        parsed = parse_resume_args(args)
        groups = native_session_groups(limit=500)

        if parsed["mode"] == "sessions":
            cwd = str(parsed.get("cwd", "")).strip()
            page = max(1, int(parsed.get("page", 1)))
            group = next((item for item in groups if str(item.get("cwd", "")) == cwd), None)
            if not group:
                return "没有找到这个目录。\n\n" + session_list_text()
            sessions = list(group.get("items", []))
            total = len(sessions)
            max_page = max(1, (total + 7) // 8)
            page = min(page, max_page)
            start = (page - 1) * 8
            current = sessions[start:start + 8]
            lines = [
                f"当前原生会话：{active}",
                f"目录：{group.get('label', cwd)}",
                f"第 {page}/{max_page} 页，点击下方会话切换。",
            ]
            for offset, item in enumerate(current, start + 1):
                item_id = str(item.get("id", ""))
                if not item_id:
                    continue
                title = session_title(item, item_id)
                short_id = item_id[-8:] if item_id else ""
                lines.append(
                    f"- {offset}. {title} | {item.get('updated_at', '')} | id:{short_id} | {item_id}"
                )
            if not current:
                lines.append("(这个目录下没有会话)")
            return "\n".join(lines)

        page = max(1, int(parsed.get("page", 1)))
        total = len(groups)
        max_page = max(1, (total + 5 - 1) // 5)
        page = min(page, max_page)
        start = (page - 1) * 5
        current = groups[start:start + 5]
        lines = [
            f"当前原生会话：{active}",
            "Codex 原生目录：",
            f"第 {page}/{max_page} 页，点击目录查看会话。",
        ]
        for offset, group in enumerate(current, start + 1):
            cwd = str(group.get("cwd", "")).strip()
            label = str(group.get("label", cwd))
            count = int(group.get("count", 0))
            latest_title = str(group.get("latest_title", "")).strip()
            latest = str(group.get("updated_at", "")).strip()
            if latest_title:
                lines.append(f"- {offset}. {label} | {count} 会话 | {latest} | 最新: {latest_title}")
            else:
                lines.append(f"- {offset}. {label} | {count} 会话 | {latest}")
        if not current:
            lines.append("(没有找到 Codex 原生会话记录)")
        return "\n".join(lines)

    state = load_state()
    active = str(state["active_session_id"])
    sessions = sorted(
        state["sessions"].values(),
        key=lambda item: str(item.get("updated_at", "")),
        reverse=True,
    )
    lines = [f"当前本地会话：{active}", "本地会话列表："]
    for item in sessions:
        mark = "*" if item["id"] == active else "-"
        lines.append(
            f"{mark} {item['id']} | {item.get('updated_at', '')} | "
            f"{item.get('message_count', 0)} 条 | {item.get('title', '未命名')}"
        )
    return "\n".join(lines)


def status_text() -> str:
    state = load_state()
    runtime = current_runtime()
    profile = runtime["permission_profile"]
    pending_count = len(list_pending_approvals())
    lines = [
        "Gateway: running",
        f"Context: {runtime['context_mode']}",
        f"Workdir: {runtime['workdir']}",
        f"Native active: {runtime['active_native_session_id'] or '(none)'}",
        f"Model: {runtime['model']}",
        f"Reasoning: {runtime['reasoning_effort']}",
        f"Timeout: {runtime['timeout_seconds']}s",
        f"Recent default: {runtime['recent_default_count']}",
        f"Permission: {profile['label']} | sandbox={profile['sandbox']} | approval={profile['approval_policy']} | reviewer={profile.get('approvals_reviewer', 'user')}",
        f"Pending approvals: {pending_count}",
    ]
    if QQ_OWNER_QQ:
        lines.append(f"Owner QQ label: {QQ_OWNER_QQ} (QQ Gateway 实际鉴权使用 openid，不使用明文 QQ 号)")
    if runtime["context_mode"] == "native":
        lines.append(f"History: Codex native sessions under {CODEX_HOME / 'sessions'}")
        lines.append(f"Indexed sessions: {len(parse_jsonl_file(CODEX_SESSION_INDEX))}")
    else:
        active = str(state["active_session_id"])
        history_file = session_path(active)
        history_bytes = history_file.stat().st_size if history_file.exists() else 0
        meta = state["sessions"].get(active, {})
        lines.append(f"Local active: {active} | {meta.get('title', '未命名')}")
        lines.append(f"History: {history_bytes} bytes | {meta.get('message_count', 0)} 条")
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join([
        "可用指令：",
        "/start - 显示入口面板、当前模型和快捷按钮",
        "/help - 展示所有指令",
        "/status - 显示 Gateway、模型、思考强度、历史长度、权限",
        "/whoami - 显示当前 QQ Gateway openid，用于配置 allowlist",
        "/model - 查看当前模型和思考强度",
        "/model gpt-5.5 high - 切换模型和思考强度",
        "/model gpt-5.4 xhigh - 支持 gpt-5.5 / gpt-5.4；思考强度 none/minimal/low/medium/high/xhigh",
        "/workdir [路径] - 查看或切换 Codex 工作目录",
        "/cmd [命令] - 在当前工作目录下通过 cmd.exe /c 运行命令",
        "/ps [命令] - 在当前工作目录下通过 powershell.exe 运行命令",
        "/cancel - 取消当前正在运行的 Codex 任务",
        "/cancel <task_id> - 取消指定 Codex 任务",
        "/tasks - 查看运行中和排队中的 Codex 任务",
        "/setup - 查看设置面板",
        "/output - 查看阶段性输出和最终输出设置",
        "/output stage on|off - 开关阶段性输出",
        "/output userContext on|off - 开关最终输出是否带用户输入",
        "/timeout - 查看 Codex 单次调用超时",
        "/timeout 45 - 设置单次调用超时为 45 分钟",
        "/heartbeat - 查看任务提醒频率设置",
        "/truncate on|off - 开关长内容截断",
        "/recent-default - 查看最近对话默认条数",
        "/recent-default 10 - 设置 /recent 默认展示 10 条",
        "/restart - 重启 QQ Gateway 客户端",
        "/permission - 查看当前权限",
        "/permission read only - 只读",
        "/permission ask - 请求批准，普通任务先发待批准请求",
        "/permission auto - 替我审批，Codex 自动审查风险操作",
        "/permission full - 完全权限，绕过沙箱和审批",
        "/permission approve - 兼容旧命令，等同 /permission auto",
        "/approval-test - 生成一条测试审批请求，不执行真实任务",
        "/pending - 查看待批准操作",
        "/allow [id] - 批准待执行操作；只有一个待批准项时可省略 id",
        "/reject [id] - 拒绝并删除待执行操作",
        "/revise <id> <修改意见> - 修改要求并重新生成审批计划",
        "/resume - 按目录展示 Codex 原生会话",
        "/resume page 2 - 展示目录第 2 页",
        "/resume dir <目录> - 展示指定目录下的会话",
        "/resume <id> - 切换到指定 Codex 原生会话",
        "/recent - 查看当前会话最近 5 条对话",
        "/recent N S - 从最近第 S 条开始查看 N 条对话（N=1-20，S 可省略）",
        "/recent N S <id> - 查看指定会话对应范围的对话",
        "/last user [N S] - 查看我发出的最近 N 句，可分页",
        "/last codex [N S] - 查看 Codex 的最近 N 句回复，可分页",
        "/new [标题] - 开启一段新的 Codex 原生会话",
        "/delete - 展示 Codex 原生会话列表",
        "/delete <id> - 归档指定 Codex 原生会话",
    ])


def start_text() -> str:
    runtime = current_runtime()
    return "\n".join([
        "Codex Remote Bridge 已启动",
        "我是你的远程 Codex 助手，可以通过 QQ 帮你查看和切换 Codex 会话、继续对话、调整模型、查看状态，并把需要审批的操作发回给你确认。",
        f"model: {runtime['model']}",
        f"reasoning: {runtime['reasoning_effort']}",
        "",
        "点击按钮或发送命令开始使用：",
        "/resume - Codex 会话列表",
        "/model - 模型设置",
        "/whoami - 用户信息",
        "/status - 状态",
        "/setup - 设置",
        "/help - 帮助",
    ])


def whoami_text(job: Optional[Dict[str, Any]]) -> str:
    if not job:
        return "当前没有 QQ 身份信息。"
    reply = job.get("reply") or {}
    lines = [f"from={job.get('from', '')}"]
    if reply.get("openid"):
        lines.append(f"user_openid={reply['openid']}")
    if reply.get("group_openid"):
        lines.append(f"group_openid={reply['group_openid']}")
    if QQ_OWNER_QQ:
        lines.append(f"owner_qq_label={QQ_OWNER_QQ}")
    lines.append("严格限制用户时，把 user_openid 填入 QQ_ALLOWED_USER_OPENIDS。")
    return "\n".join(lines)


def parse_model_command(args: str) -> str:
    state = load_state()
    if not args.strip():
        return (
            f"当前模型：{state.get('model')}\n"
            f"当前思考强度：{state.get('reasoning_effort')}\n"
            f"可选模型：{', '.join(ALLOWED_MODELS)}\n"
            "用法：/model gpt-5.5 high"
        )

    model = None
    reasoning = None
    compact = re.sub(r"[\s_-]+", "", args.lower())
    if "gpt55" in compact or "gpt5.5" in compact:
        model = "gpt-5.5"
    if "gpt54" in compact or "gpt5.4" in compact:
        model = "gpt-5.4"

    for token in re.split(r"[\s,，]+", args):
        if not token:
            continue
        try:
            reasoning = normalize_reasoning(token)
        except ValueError:
            pass

    if model is None:
        try:
            model = normalize_model(args)
        except ValueError:
            model = None

    if model is None and reasoning is None:
        return "无法识别模型或思考强度。示例：/model gpt-5.5 high"

    if model is not None:
        state["model"] = model
    if reasoning is not None:
        state["reasoning_effort"] = reasoning
    save_state(state)
    return f"已更新：model={state['model']}，reasoning={state['reasoning_effort']}"


def timeout_command(args: str) -> str:
    state = load_state()
    current = int(state.get("timeout_seconds", CODEX_TIMEOUT_SECONDS) or CODEX_TIMEOUT_SECONDS)
    if not args.strip():
        return (
            f"当前 Codex 单次调用超时：{current} 秒（约 {int((current + 59) / 60)} 分钟）\n"
            "用法：/timeout 45  设置为 45 分钟；范围 1-1440 分钟。"
        )
    match = re.search(r"\d+", args)
    if not match:
        return "无法识别超时时长。示例：/timeout 45"
    minutes = max(1, min(1440, int(match.group(0))))
    state["timeout_seconds"] = minutes * 60
    save_state(state)
    return f"已设置 Codex 单次调用超时：{minutes} 分钟。"


def recent_default_command(args: str) -> str:
    state = load_state()
    current = clamp_recent_count(int(state.get("recent_default_count", RECENT_DEFAULT_COUNT) or RECENT_DEFAULT_COUNT))
    if not args.strip():
        return "\n".join([
            f"当前最近对话默认条数：{current}",
            "用法：/recent-default 10  设置 /recent 和 /last 的默认展示条数；范围 1-20。",
        ])
    match = re.search(r"\d+", args)
    if not match:
        return "无法识别条数。示例：/recent-default 10"
    count = clamp_recent_count(int(match.group(0)))
    state["recent_default_count"] = count
    save_state(state)
    return f"已设置最近对话默认条数：{count} 条。"


def parse_permission_command(args: str) -> str:
    state = load_state()
    if not args.strip():
        current = normalize_permission(str(state.get("permission", "read-only")))
        profile = PERMISSION_PROFILES[current]
        return "\n".join([
            f"当前权限：{profile['label']}",
            f"sandbox={profile['sandbox']} approval={profile['approval_policy']} reviewer={profile.get('approvals_reviewer', 'user')}",
            profile["description"],
            "可选：read only / ask / auto / full",
            "说明：ask 对应请求批准；auto 对应替我审批；full 对应完全访问权限。approve 是旧命令别名，等同 auto。",
        ])
    try:
        permission = normalize_permission(args)
    except ValueError:
        return "无法识别权限。可选：read only / ask / auto / full"
    state["permission"] = permission
    save_state(state)
    profile = PERMISSION_PROFILES[permission]
    return f"已更新权限：{profile['label']}。{profile['description']}"


def allow_command(args: str) -> str:
    item_id, _ = parse_pending_args(args)
    resolved = resolve_pending_approval_id(item_id)
    if not resolved:
        return "没有找到唯一的待批准操作。发送 /pending 查看 id。"
    item = approve_pending_approval(resolved)
    if not item:
        return "这个待批准操作不存在或已过期。发送 /pending 查看。"
    try:
        return run_approved_pending_approval(item)
    except Exception as exc:
        return f"批准后执行失败：{exc}"


def reject_command(args: str) -> str:
    item_id, _ = parse_pending_args(args)
    resolved = resolve_pending_approval_id(item_id)
    if not resolved:
        return "没有找到唯一的待批准操作。发送 /pending 查看 id。"
    if not reject_pending_approval(resolved):
        return "这个待批准操作不存在或已过期。"
    return f"已拒绝并删除待批准操作 #{resolved}。"


def revise_command(args: str) -> str:
    item_id, revision = parse_pending_args(args)
    resolved = resolve_pending_approval_id(item_id)
    if not resolved:
        return "没有找到唯一的待批准操作。发送 /pending 查看 id。"
    if not revision:
        item = get_pending_approval(resolved)
        if not item:
            return "这个待批准操作不存在或已过期。"
        return approval_prompt_text(item) + "\n\n用法：/revise " + resolved + " <修改意见>"
    item = revise_pending_approval(resolved, revision)
    if not item:
        return "这个待批准操作不存在或已过期。"
    return approval_prompt_text(item)


def resume_command(args: str) -> str:
    state = load_state()
    runtime = current_runtime()
    session_id = args.strip()
    if (
        not session_id
        or re.fullmatch(r"(?:page|p)\s+\d+", session_id, flags=re.IGNORECASE)
        or re.match(r"^(?:dir|d)(?:\s|$)", session_id, flags=re.IGNORECASE)
    ):
        return session_list_text(args)

    if runtime["context_mode"] == "native":
        session_id = resolve_native_session_id(session_id)
        if not session_id:
            return "会话 id 格式不合法。"
        session_item = find_native_session(session_id)
        if not session_item:
            return "没有在 Codex session_index 中找到这个 id。\n\n" + session_list_text()
        state["active_native_session_id"] = session_id
        state["context_mode"] = "native"
        session_workdir = str(session_item.get("cwd") or "").strip()
        if session_workdir:
            state["workdir"] = session_workdir
        save_state(state)
        return f"已切换到 Codex 原生会话：{session_id}\n发送 /recent 查看最近对话内容。"

    try:
        session_id = safe_local_session_id(session_id)
    except ValueError:
        return "会话 id 格式不合法。"
    if session_id not in state["sessions"]:
        return "没有这个会话 id。\n\n" + session_list_text()
    state["active_session_id"] = session_id
    state["sessions"][session_id]["updated_at"] = now_iso()
    save_state(state)
    return f"已切换到本地会话：{session_id}"


def new_command(args: str) -> str:
    if current_runtime()["context_mode"] == "native":
        session_id = create_native_session(args)
        return f"已开启 Codex 原生新会话：{session_id}"
    session_id = create_local_session(args)
    return f"已开启本地新对话：{session_id}"


def delete_command(args: str) -> str:
    state = load_state()
    runtime = current_runtime()
    session_id = args.strip()
    if not session_id:
        return session_list_text()

    if runtime["context_mode"] == "native":
        try:
            session_id = safe_native_session_id(session_id)
        except ValueError:
            return "会话 id 格式不合法。"
        proc = subprocess.run(
            [codex_command_path(), "archive", session_id],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        if proc.returncode != 0:
            return "归档失败：\n" + proc.stdout.strip()
        if state.get("active_native_session_id") == session_id:
            state["active_native_session_id"] = ""
            save_state(state)
        return f"已归档 Codex 原生会话：{session_id}"

    try:
        session_id = safe_local_session_id(session_id)
    except ValueError:
        return "会话 id 格式不合法。"
    if session_id not in state["sessions"]:
        return "没有这个会话 id。\n\n" + session_list_text()

    del state["sessions"][session_id]
    path = session_path(session_id)
    if path.exists():
        path.unlink()
    if not state["sessions"]:
        new_id = create_local_session()
        return f"已删除会话：{session_id}\n当前会话：{new_id}"
    if state.get("active_session_id") == session_id:
        newest = sorted(
            state["sessions"].values(),
            key=lambda item: str(item.get("updated_at", "")),
            reverse=True,
        )[0]["id"]
        state["active_session_id"] = newest
    save_state(state)
    return f"已删除本地会话：{session_id}\n当前会话：{state['active_session_id']}"


def workdir_command(args: str = "") -> str:
    state = load_state()
    current = str(state.get("workdir", CODEX_WORKDIR))
    raw = (args or "").strip()
    if not raw:
        sessions = list_native_sessions_for_workdir(current, limit=5)
        lines = [
            f"当前工作目录：{current}",
            f"该目录下的原生会话：{len(sessions)}",
            "用法：/workdir C:\\path\\to\\project",
        ]
        for index, item in enumerate(sessions, 1):
            session_id = str(item.get("id", ""))
            title = session_title(item, session_id)
            lines.append(f"{index}. {title} | {item.get('updated_at', '')} | /resume #{session_id[-8:]}")
        return "\n".join(lines)

    try:
        workdir = resolve_workdir(raw)
    except ValueError as exc:
        return str(exc)

    sessions = list_native_sessions_for_workdir(workdir, limit=10)
    state["context_mode"] = "native"
    state["workdir"] = workdir
    state["active_native_session_id"] = ""
    save_state(state)

    lines = [
        f"工作目录已切换：{workdir}",
        f"找到的原生会话：{len(sessions)}",
    ]
    if sessions:
        lines.append("可以点击下方会话按钮恢复，或者发送 /resume #id 恢复。")
        lines.append("如果在恢复前直接发普通消息，Codex 会在这个目录里新建会话。")
        for index, item in enumerate(sessions[:5], 1):
            session_id = str(item.get("id", ""))
            title = session_title(item, session_id)
            lines.append(f"{index}. {title} | {item.get('updated_at', '')} | /resume #{session_id[-8:]}")
    else:
        lines.append("这个工作目录下没有找到原生会话。")
        lines.append("下一条普通消息会在这个目录里新建 Codex 会话。")
    return "\n".join(lines)


def run_native_cmd(command: str, workdir: str, timeout_seconds: int) -> str:
    if os.name != "nt":
        raise RuntimeError("native cmd is only supported on Windows")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", command],
        cwd=workdir,
        text=True,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        creationflags=creationflags,
    )
    output = (proc.stdout or "").strip()
    if not output:
        output = "(no output)"
    return f"Exit code: {proc.returncode}\n{output}"


def run_native_powershell(command: str, workdir: str, timeout_seconds: int) -> str:
    if os.name != "nt":
        raise RuntimeError("native PowerShell is only supported on Windows")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=workdir,
        text=True,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        creationflags=creationflags,
    )
    output = (proc.stdout or "").strip()
    if not output:
        output = "(无输出)"
    return f"退出码：{proc.returncode}\n{output}"


def native_command_runtime_or_error(command_name: str) -> tuple[Optional[Dict[str, Any]], str]:
    runtime = current_runtime()
    if runtime["permission"] == "read-only":
        return None, f"当前是只读模式，不能运行 {command_name}。请先切到 /permission auto 或 /permission full。"
    if runtime["permission"] == "ask":
        return None, f"当前是请求批准模式，/{command_name} 暂不支持自动审批。请切到 /permission auto 或 /permission full。"
    return runtime, ""


def cmd_command(args: str = "") -> str:
    command = (args or "").strip()
    runtime = current_runtime()
    if not command:
        return "\n".join([
            "原生命令",
            f"当前工作目录：{runtime['workdir']}",
            "用法：/cmd dir",
            "说明：在当前工作目录下通过 cmd.exe /c 执行命令",
            "限制：只在 /permission auto 或 /permission full 下可用",
        ])
    runtime, error = native_command_runtime_or_error("cmd")
    if error:
        return error
    timeout_seconds = max(30, min(86400, int(runtime.get("timeout_seconds") or CODEX_TIMEOUT_SECONDS)))
    try:
        return run_native_cmd(command, str(runtime.get("workdir") or CODEX_WORKDIR), timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        return f"命令超时：{exc.timeout} 秒"
    except Exception as exc:
        return f"运行失败：{exc}"


def ps_command(args: str = "") -> str:
    command = (args or "").strip()
    runtime = current_runtime()
    if not command:
        return "\n".join([
            "原生 PowerShell 命令",
            f"当前工作目录：{runtime['workdir']}",
            "用法：/ps Get-ChildItem",
            "说明：在当前工作目录下通过 powershell.exe -NoProfile -ExecutionPolicy Bypass -Command 执行命令",
            "限制：只在 /permission auto 或 /permission full 下可用",
        ])
    runtime, error = native_command_runtime_or_error("ps")
    if error:
        return error
    timeout_seconds = max(30, min(86400, int(runtime.get("timeout_seconds") or CODEX_TIMEOUT_SECONDS)))
    try:
        return run_native_powershell(command, str(runtime.get("workdir") or CODEX_WORKDIR), timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        return f"命令超时：{exc.timeout} 秒"
    except Exception as exc:
        return f"运行失败：{exc}"


def handle_bridge_command(text: str, job: Optional[Dict[str, Any]] = None) -> Optional[str]:
    text = text.strip()
    if not text.startswith("/"):
        return None
    command, _, args = text.partition(" ")
    command = command.lower().strip()
    args = args.strip()

    if command == "/start":
        return start_text()
    if command == "/help":
        return help_text()
    if command == "/status":
        return status_text()
    if command == "/whoami":
        return whoami_text(job)
    if command == "/model":
        return parse_model_command(args)
    if command == "/setup":
        return "设置面板仅在 QQ Bot 按钮模式下显示。可用 /output、/permission、/timeout 等命令直接调整。"
    if command == "/output":
        return "输出展示设置由 QQ Gateway 运行态管理，请在 QQ 中发送 /setup 或 /output。"
    if command == "/heartbeat":
        return "任务提醒频率由 QQ Gateway 运行态管理，请在 QQ 中发送 /setup 或 /heartbeat。"
    if command == "/truncate":
        return "长内容截断由 QQ Gateway 运行态管理，请在 QQ 中发送 /setup 或 /truncate on|off。"
    if command == "/approval-test":
        return "审批测试需要 QQ 回复目标，请在 QQ 中发送 /approval-test。"
    if command == "/timeout":
        return timeout_command(args)
    if command == "/recent-default":
        return recent_default_command(args)
    if command == "/permission":
        return parse_permission_command(args)
    if command == "/workdir":
        return workdir_command(args)
    if command == "/cmd":
        return cmd_command(args)
    if command == "/ps":
        return ps_command(args)
    if command == "/pending":
        return pending_approvals_text()
    if command == "/allow":
        return allow_command(args)
    if command == "/reject":
        return reject_command(args)
    if command == "/revise":
        return revise_command(args)
    if command == "/resume":
        return resume_command(args)
    if command == "/recent":
        return recent_command(args)
    if command == "/last":
        return last_command(args)
    if command == "/new":
        return new_command(args)
    if command == "/delete":
        return delete_command(args)
    if command == "/cancel":
        result = cancel_current_task()
        if result == "当前没有正在运行的 Codex 任务。":
            pending_result = cancel_pending_target()
            if pending_result:
                return pending_result
        return result
    return "未知指令。发送 /help 查看可用指令。"

def run_codex_prompt_mode(job: Dict[str, Any], on_event: Optional[Callable[[Dict[str, Any]], None]] = None) -> str:
    remote_text = job.get("text", "")
    attachments_text = str(job.get("attachments_text", "")).strip()
    runtime = current_process_runtime()
    runtime = dict(runtime)
    runtime["workdir"] = str(job.get("_workdir") or runtime.get("workdir") or CODEX_WORKDIR)
    active_session_id = str(job.get("_active_session_id") or runtime["active_session_id"])
    history = read_history_tail(active_session_id)
    profile = runtime["permission_profile"]
    prompt = f"""你正在通过 QQ 远程消息桥接和用户对话。

要求：
- 用中文回答。
- 直接回答用户问题，不要提到桥接内部实现。
- 回答不要使用markdown格式，直接输出纯文本。
- 当前权限模式：{profile['label']}；sandbox={profile['sandbox']}；approval={profile['approval_policy']}；reviewer={profile.get('approvals_reviewer', 'user')}。
- 不要读取或泄露本机敏感信息，除非用户明确要求且当前权限模式允许。
- 如果需要把本地图片发回 QQ，请单独输出一行：SEND_IMAGE: <本地图片绝对路径>。
- 必要上下文在下面的当前会话历史片段里。

当前会话：{active_session_id}
当前会话历史片段：
{history}

远程用户消息：
{remote_text}
{attachments_text}
"""
    return run_codex_process(prompt, "", str(job.get("id", "")), on_event=on_event, runtime_override=runtime).get("text", "")


def run_codex_native_mode(job: Dict[str, Any], on_event: Optional[Callable[[Dict[str, Any]], None]] = None) -> str:
    state = load_state()
    session_id = str(job.get("_active_native_session_id") or state.get("active_native_session_id", ""))
    runtime = current_process_runtime()
    runtime = dict(runtime)
    runtime["workdir"] = str(job.get("_workdir") or runtime.get("workdir") or CODEX_WORKDIR)
    if session_id:
        rollout_path = native_session_rollout_path(session_id)
        if not rollout_path or not rollout_path.is_file():
            debug_log(
                f"stale native session={session_id!r}; "
                f"rollout={str(rollout_path) if rollout_path else '(missing)'}; "
                "starting a new session"
            )
            session_id = ""
            if state.get("active_native_session_id"):
                state["active_native_session_id"] = ""
                save_state(state)
    remote_text = job.get("text", "")
    attachments_text = str(job.get("attachments_text", "")).strip()
    prompt = f"""你正在通过 QQ 远程消息桥接和用户对话。

要求：
- 用中文回答。
- 直接回答用户问题。
- 不要提到桥接内部实现。
- 如果需要把本地图片发回 QQ，请单独输出一行：SEND_IMAGE: <本地图片绝对路径>。

远程用户消息：
{remote_text}
{attachments_text}
"""
    result = run_codex_process(prompt, session_id, str(job.get("id", "")), on_event=on_event, runtime_override=runtime)
    thread_id = result.get("thread_id", "")
    if thread_id:
        state = load_state()
        state["context_mode"] = "native"
        state["workdir"] = runtime["workdir"]
        state["active_native_session_id"] = thread_id
        save_state(state)
    return result["text"]


def run_codex(job: Dict[str, Any], on_event: Optional[Callable[[Dict[str, Any]], None]] = None) -> str:
    if current_process_runtime()["context_mode"] == "native":
        return run_codex_native_mode(job, on_event=on_event)
    return run_codex_prompt_mode(job, on_event=on_event)
