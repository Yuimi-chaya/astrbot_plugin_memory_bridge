# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import difflib
import hashlib
import inspect
import json
import os
import platform
import re
import secrets
import shutil
import sys
import time
import traceback
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api import logger
except Exception:
    logger = None
try:
    from astrbot.api import message_components as Comp
except Exception:
    Comp = None
try:
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    FASTAPI_AVAILABLE = True
except Exception:
    FASTAPI_AVAILABLE = False
    uvicorn = FastAPI = Request = CORSMiddleware = FileResponse = JSONResponse = StaticFiles = None
try:
    from astrbot.core.message.components import Plain as CorePlain
except Exception:
    CorePlain = None
try:
    from astrbot.core.message.components import File as CoreFile
except Exception:
    CoreFile = None
try:
    from astrbot.core.message.message_event_result import MessageChain
except Exception:
    MessageChain = None
try:
    from astrbot.core.platform.message_type import MessageType
except Exception:
    MessageType = None
try:
    from astrbot.core.platform.platform import PlatformStatus
except Exception:
    PlatformStatus = None
try:
    from astrbot.core.platform.astr_message_event import MessageSession as MessageSession
except Exception:
    try:
        from astrbot.core.platform.message_session import MessageSession as MessageSession
    except Exception:
        MessageSession = None

PLUGIN_ID = "astrbot_plugin_memory_bridge"
PLUGIN_VERSION = "0.5.0"
PLUGIN_AUTHOR = "喝益胃 / Yuimi-chaya"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def preview(text: str, limit: int = 180) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").replace("\n", " ")).strip()
    return normalized[:limit] + ("..." if len(normalized) > limit else "")


def norm(text: str) -> str:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def sha(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8", "ignore")).hexdigest()


def truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "disabled"}
    return bool(value)


def mask_secrets(text: str) -> Tuple[str, int]:
    count = 0

    def mask_pair(match):
        nonlocal count
        count += 1
        return match.group(1) + "****"

    def mask_sk(match):
        nonlocal count
        count += 1
        value = match.group(1)
        return value[:4] + "****" + value[-4:]

    output = re.sub(r"(?i)(sk-[A-Za-z0-9_\-]{12,})", mask_sk, text or "")
    patterns = [
        r"(?i)((?:api[_-]?key|access[_-]?token|token|secret|password|passwd|cookie)\s*[:=]\s*)([^\s,;\]})]{6,})",
        r"(?i)(authorization\s*[:=]\s*bearer\s+)([A-Za-z0-9._\-]{8,})",
        r"(?i)((?:token|access_key|api_key)=)([^&\s]{6,})",
    ]
    for pattern in patterns:
        output = re.sub(pattern, mask_pair, output)
    return output, count


def extract_text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("data"), dict):
                    parts.append(str(item["data"].get("text") or ""))
                elif "text" in item:
                    parts.append(str(item.get("text") or ""))
                elif "content" in item:
                    parts.append(extract_text_content(item.get("content")))
                elif "message" in item:
                    parts.append(extract_text_content(item.get("message")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
            else:
                parts.append(str(item))
        return "".join(parts)
    if isinstance(content, dict):
        if content.get("type") == "text" and isinstance(content.get("data"), dict):
            return str(content["data"].get("text") or "")
        for key in ("text", "content", "message", "raw_message", "message_str"):
            if key in content:
                return extract_text_content(content.get(key))
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(content)


def is_noisy_adapter_origin(origin: str) -> bool:
    return bool(re.match(r"^(aiocqhttp|napcat):(FriendMessage|GroupMessage):", str(origin or ""), re.I))


@dataclass
class MemoryMessage:
    index: int
    role: str
    content: str
    time: Optional[str] = None
    name: Optional[str] = None
    source: str = "conversation_manager"
    metadata: dict[str, Any] = None


class RuleCleaner:
    IMPORTANT = [
        r"我(?:喜欢|不喜欢|希望|想要|需要|记住)", r"以后(?:都|默认)", r"必须|不要|优先",
        r"已(?:确认|测试|修复|解决|正常|成功)", r"v\d+\.\d+\.\d+|版本",
        r"AstrBot|NapCat|Docker|callback_api_base|Comp\.File|WebUI", r"NotADirectoryError|explicit_root_dir|include_dir_entries",
        r"下一步|待办|路线|迁移|导入|恢复|清洗|续忆|记忆|导出|WebUI|远程触发|历史读取",
    ]
    TRACE = ["Traceback (most recent call last)", "Exception", "File \"", "NotADirectoryError", "TypeError", "ImportError", "ModuleNotFoundError", "[ERROR]", "[ERRO]"]
    CMD = ("/续忆", "续忆 ", "/memory", "/help", "/status", "/start")
    SHORT_NOISE = {"嗯", "哦", "好", "ok", "OK", "行", "测试", "？", "。", "啊", "1", "是"}
    SHORT_STATUS = {"正常", "成功", "没问题", "可以", "继续", "全正常", "报错", "失败"}
    LOG_RE = re.compile(r"^\s*\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")

    def __init__(self, mode="balanced", keep_recent=20, mask=True):
        self.mode = mode if mode in {"safe", "balanced", "aggressive"} else "balanced"
        self.keep_recent = max(0, int(keep_recent or 0))
        self.mask = mask
        self.seen = {}
        self.prev = []
        self.mask_hits = 0

    def important(self, text):
        return any(re.search(pattern, text, re.I) for pattern in self.IMPORTANT)

    def is_json_blob(self, text):
        trimmed = (text or "").strip()
        return len(trimmed) >= 220 and (((trimmed.startswith("{") and trimmed.endswith("}")) or (trimmed.startswith("[") and trimmed.endswith("]"))) or sum(trimmed.count(char) for char in '{}[]":,') > max(80, len(trimmed) * 0.12))

    def is_b64_blob(self, text):
        compact = re.sub(r"\s+", "", text or "")
        if len(compact) < 500:
            return False
        return ("data:image" in (text or "").lower() and "base64" in (text or "").lower()) or sum(1 for char in compact if char.isalnum() or char in "+/=") / max(1, len(compact)) > 0.96

    def clean(self, messages: list[MemoryMessage]) -> dict[str, Any]:
        kept, removed, masked, facts, decisions = [], [], [], [], []
        total = len(messages)
        for message in messages:
            content = norm(message.content)
            original = content
            reasons, score = [], 0
            if self.mask:
                content, hits = mask_secrets(content)
                if hits:
                    self.mask_hits += hits
                    reasons.append("mask_secret")
                    score += 2
            if not content:
                action, reasons, score = "drop", ["drop_empty"], -100
            else:
                important = self.important(content)
                recent = message.index >= max(0, total - self.keep_recent)
                if important:
                    reasons.append("important_signal")
                    score += 8
                if recent:
                    reasons.append("keep_recent_window")
                    score += 4
                if content in self.SHORT_STATUS:
                    reasons.append("short_but_status_signal")
                    score += 5
                elif len(content) <= 2 or content in self.SHORT_NOISE:
                    reasons.append("drop_short_noise")
                    score -= 7 if self.mode != "safe" else 3
                if content.startswith(self.CMD):
                    reasons.append("drop_command_message")
                    score -= 6 if self.mode != "safe" else 3
                content_hash = sha(content.lower())
                if content_hash in self.seen:
                    reasons.append("drop_duplicate_exact")
                    score -= 9 if self.mode != "safe" else 5
                else:
                    self.seen[content_hash] = message.index
                if self.mode in {"balanced", "aggressive"} and len(content) > 120:
                    for previous in self.prev[-30:]:
                        similarity = difflib.SequenceMatcher(None, previous[:1000], content[:1000]).ratio()
                        if abs(len(previous) - len(content)) < max(30, len(content) * 0.2) and similarity > (0.96 if self.mode == "balanced" else 0.90):
                            reasons.append("drop_duplicate_similar")
                            score -= 8
                            break
                if any(marker in content for marker in self.TRACE):
                    reasons.append("drop_traceback_or_exception")
                    score -= 8 if self.mode != "safe" else 4
                lines = content.splitlines()
                if len(lines) >= 8:
                    log_lines = sum(1 for line in lines if self.LOG_RE.search(line) or "[Core]" in line or "[WARN]" in line or "[INFO]" in line)
                    if log_lines >= max(5, len(lines) * 0.45):
                        reasons.append("drop_large_log_block")
                        score -= 7
                if self.is_json_blob(content):
                    reasons.append("drop_tool_json_blob")
                    score -= 6 if self.mode != "safe" else 2
                if self.is_b64_blob(content):
                    reasons.append("drop_base64_or_binary_blob")
                    score -= 12
                if len(content) > (5000 if self.mode != "aggressive" else 2500) and not important:
                    reasons.append("drop_too_long_low_value")
                    score -= 6
                threshold = (-8 if self.mode == "safe" else -10) if (important or recent) else (-4 if self.mode == "safe" else (0 if self.mode == "balanced" else 2))
                action = "keep" if score >= threshold else "drop"
            item = asdict(message)
            item["metadata"] = item.get("metadata") or {}
            item["clean_reasons"] = reasons or ["keep_default"]
            item["clean_score"] = score
            if content != original:
                item["content"] = content
                masked.append({"index": message.index, "reason": "secret_redaction", "preview": preview(content)})
            decisions.append({"index": message.index, "action": action, "reasons": item["clean_reasons"], "score": score, "content_preview": preview(message.content)})
            if action == "keep":
                kept.append(item)
                self.prev.append(content)
                facts += self.extract_facts(message.index, content)
            else:
                removed.append({"index": message.index, "role": message.role, "time": message.time, "reasons": item["clean_reasons"], "score": score, "content_preview": preview(message.content, 260), "original_len": len(message.content or "")})
        reason_counts = {}
        for removed_item in removed:
            for reason in removed_item["reasons"]:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        return {"version": PLUGIN_VERSION, "mode": self.mode, "generated_at": now_str(), "stats": {"raw_count": len(messages), "kept_count": len(kept), "removed_count": len(removed), "masked_count": len(masked), "mask_hits": self.mask_hits, "keep_ratio": round(len(kept) / max(1, len(messages)), 4)}, "reason_counts": reason_counts, "kept_messages": kept, "removed_messages": removed, "masked_messages": masked, "decisions": decisions, "extracted_facts": self.dedupe(facts), "rules_used": {"mode": self.mode, "drop": ["empty", "duplicate", "command", "traceback", "large_log", "tool_json", "base64", "short_noise"], "mask": ["token", "api_key", "password", "cookie"], "keep": ["recent", "preference", "project_status", "technical_context", "migration"], "llm_used": False}}

    def extract_facts(self, index, content):
        facts = []

        def add(fact_type, confidence="rule_medium"):
            facts.append({"type": fact_type, "content": preview(content, 400), "source_indices": [index], "confidence": confidence})

        if re.search(r"我(?:喜欢|不喜欢|希望|想要|需要)|以后(?:都|默认)|记住|不要|必须|优先", content):
            add("user_preference", "rule_high")
        if re.search(r"v\d+\.\d+\.\d+|版本|已(?:测试|确认|修复|解决|正常|成功)", content):
            add("project_status", "rule_high")
        if re.search(r"AstrBot|NapCat|Docker|callback_api_base|Comp\.File|WebUI", content, re.I):
            add("technical_context", "rule_high")
        if re.search(r"问题|报错|原因|解决|修复|NotADirectoryError|Traceback", content):
            add("resolved_issue")
        if re.search(r"下一步|待办|路线|计划|准备|继续|迁移|导入|恢复", content):
            add("open_task")
        return facts

    def dedupe(self, facts):
        seen, output = set(), []
        for fact in facts:
            key = (fact["type"], sha(fact["content"][:260]))
            if key not in seen:
                seen.add(key)
                output.append(fact)
        return output[:200]


class FakeEventForWeb:
    def __init__(self, origin: str, message: str = "[WebUI trigger]"):
        self.unified_msg_origin = origin
        self.message_str = message

    def get_unified_msg_origin(self):
        return self.unified_msg_origin

    def get_session_id(self):
        return self.unified_msg_origin


class MemoryBridgeWebAdminServer:
    def __init__(self, plugin: "MemoryBridgePlugin"):
        self.plugin = plugin
        self.app = None
        self.server = None
        self.server_task = None
        self._tokens = {}
        self._init_error = ""
        if FASTAPI_AVAILABLE:
            try:
                self._setup_app()
            except Exception as error:
                self._init_error = str(error)
                self.app = None

    @property
    def token_expire_seconds(self):
        return max(1, int(self.plugin.cfg("web_admin.token_expire_hours", 24) or 24)) * 3600

    @property
    def auth_enabled(self):
        return bool(str(self.plugin.cfg("web_admin.password", "") or "").strip())

    def _issue_token(self):
        token = secrets.token_urlsafe(24)
        self._tokens[token] = time.time() + self.token_expire_seconds
        return token

    def _verify_token(self, token):
        if not token:
            return False
        if token == "no-auth" and not self.auth_enabled:
            return True
        expires_at = self._tokens.get(token)
        if not expires_at:
            return False
        if time.time() > expires_at:
            self._tokens.pop(token, None)
            return False
        return True

    def _setup_app(self):
        self.app = FastAPI(title="续忆桥 WebUI")
        self.app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

        @self.app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            if not self.auth_enabled:
                return await call_next(request)
            request_path = request.url.path
            if request_path in {"/api/auth-info", "/api/login"} or not request_path.startswith("/api"):
                return await call_next(request)
            token = request.headers.get("Authorization", "")
            if token.startswith("Bearer "):
                token = token[7:]
            if not self._verify_token(token):
                return JSONResponse({"ok": False, "error": "未授权或登录已过期"}, status_code=401)
            return await call_next(request)

        self._register_routes()
        admin_dir = Path(__file__).resolve().parent / "admin"
        if admin_dir.exists():
            self.app.mount("/", StaticFiles(directory=str(admin_dir), html=True), name="admin")

    def _register_routes(self):
        @self.app.get("/api/auth-info")
        async def auth_info():
            return {"ok": True, "auth_required": self.auth_enabled, "token_expire_hours": int(self.plugin.cfg("web_admin.token_expire_hours", 24) or 24)}

        @self.app.post("/api/login")
        async def login(payload: dict[str, Any]):
            password = str(self.plugin.cfg("web_admin.password", "") or "")
            if not password:
                return {"ok": True, "token": "no-auth", "auth_required": False}
            if not secrets.compare_digest(str(payload.get("password", "")), password):
                return JSONResponse({"ok": False, "error": "密码错误"}, status_code=401)
            return {"ok": True, "token": self._issue_token(), "auth_required": True}

        @self.app.get("/api/status")
        async def status():
            return self.plugin.web_status_payload()

        @self.app.get("/api/diagnostics")
        async def diagnostics():
            return self.plugin.web_diagnostics_payload()

        @self.app.get("/api/files")
        async def files():
            return {"ok": True, "files": self.plugin.web_files_payload()}

        @self.app.get("/api/files/{filename}/download")
        async def download_file(filename: str):
            path = self.plugin.resolve_export_file(filename)
            if not path:
                return JSONResponse({"ok": False, "error": "文件不存在或不允许访问"}, status_code=404)
            return FileResponse(str(path), filename=path.name, media_type="application/zip")

        @self.app.get("/api/files/{filename}/inspect")
        async def inspect_file(filename: str):
            path = self.plugin.resolve_export_file(filename)
            if not path:
                return JSONResponse({"ok": False, "error": "文件不存在或不允许访问"}, status_code=404)
            return self.plugin.inspect_export_zip(path)

        @self.app.delete("/api/files/{filename}")
        async def delete_file(filename: str):
            if not truthy(self.plugin.cfg("web_admin.allow_file_delete", False), False):
                return JSONResponse({"ok": False, "error": "WebUI 删除文件未启用。可在配置中开启 web_admin_allow_file_delete。"}, status_code=403)
            path = self.plugin.resolve_export_file(filename)
            if not path:
                return JSONResponse({"ok": False, "error": "文件不存在或不允许访问"}, status_code=404)
            name = path.name
            size = path.stat().st_size
            path.unlink()
            self.plugin.append_action_log({"kind": "delete_file", "ok": True, "file": name, "size": size})
            return {"ok": True, "deleted": name, "size": size}

        @self.app.get("/api/sessions")
        async def sessions():
            return {"ok": True, "sessions": self.plugin.web_sessions_payload()}

        @self.app.get("/api/actions")
        async def actions():
            return {"ok": True, "actions": self.plugin.web_actions_payload()}

        @self.app.get("/api/history-preview")
        async def history_preview(session: str = "", limit: int = 20):
            if not truthy(self.plugin.cfg("web_admin.allow_history_preview", True), True):
                return JSONResponse({"ok": False, "error": "WebUI 历史预览已禁用"}, status_code=403)
            if not session:
                return JSONResponse({"ok": False, "error": "缺少 session"}, status_code=400)
            safe_limit = min(100, max(1, int(limit or 20)))
            fake_event = FakeEventForWeb(session, "[WebUI history_preview]")
            messages = await self.plugin.history(fake_event, limit=safe_limit)
            return {"ok": True, "session": session, "source": self.plugin.state.get("last_history_source"), "count": len(messages), "messages": [self.plugin.web_msg_preview(message) for message in messages]}

        @self.app.get("/api/config-view")
        async def config_view():
            return self.plugin.web_config_view()

        @self.app.get("/api/imports")
        async def imports():
            return {"ok": True, "imports": self.plugin.web_imports_payload()}

        @self.app.get("/api/import-preview")
        async def import_preview(filename: str = ""):
            if not truthy(self.plugin.cfg("web_admin.allow_import", True), True):
                return JSONResponse({"ok": False, "error": "WebUI 导入功能已禁用"}, status_code=403)
            return self.plugin.import_preview_payload(filename or None)

        @self.app.post("/api/import")
        async def import_memory(payload: dict[str, Any]):
            if not truthy(self.plugin.cfg("web_admin.allow_import", True), True):
                return JSONResponse({"ok": False, "error": "WebUI 导入功能已禁用"}, status_code=403)
            target_origin = str(payload.get("target_origin") or payload.get("session") or "").strip()
            filename = str(payload.get("filename") or "").strip() or None
            mode = str(payload.get("mode") or self.plugin.cfg("import.default_mode", "hybrid") or "hybrid").strip()
            if not target_origin:
                return JSONResponse({"ok": False, "error": "缺少目标会话 origin"}, status_code=400)
            result = await self.plugin.import_package_to_origin(target_origin, filename, mode)
            return JSONResponse(result, status_code=200 if result.get("ok") else 400)

        @self.app.post("/api/import-rollback")
        async def import_rollback(payload: dict[str, Any]):
            import_id = str(payload.get("import_id") or "").strip()
            if not import_id:
                return JSONResponse({"ok": False, "error": "缺少 import_id"}, status_code=400)
            result = await self.plugin.rollback_import(import_id)
            return JSONResponse(result, status_code=200 if result.get("ok") else 400)

        @self.app.post("/api/trigger")
        async def trigger(payload: dict[str, Any]):
            if not truthy(self.plugin.cfg("web_admin.allow_remote_trigger", True), True):
                return JSONResponse({"ok": False, "error": "远程触发已禁用"}, status_code=403)
            try:
                result = await self.plugin.web_trigger_command(payload)
                return JSONResponse(result, status_code=200 if result.get("ok") else 400)
            except Exception as error:
                trace = traceback.format_exc()
                self.plugin.append_action_log({"kind": "web_trigger", "ok": False, "error": str(error), "traceback": trace[-2000:]})
                return JSONResponse({"ok": False, "error": str(error), "traceback": trace[-2000:]}, status_code=500)

    async def start(self):
        if not FASTAPI_AVAILABLE or not self.app or not truthy(self.plugin.cfg("web_admin.enabled", True), True):
            if logger:
                logger.warning(f"MemoryBridge WebUI not started. fastapi={FASTAPI_AVAILABLE}, init_error={self._init_error}")
            return
        host = str(self.plugin.cfg("web_admin.host", "0.0.0.0") or "0.0.0.0")
        port = int(self.plugin.cfg("web_admin.port", 2333) or 2333)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host=host, port=port, log_level="warning", access_log=False))

        async def serve():
            try:
                await self.server.serve()
            except Exception as error:
                if logger:
                    logger.error(f"MemoryBridge WebUI server error: {error}")

        self.server_task = asyncio.create_task(serve())
        await asyncio.sleep(0.1)
        self.plugin.web_started = True
        self.plugin.web_url = f"http://{host}:{port}"
        if logger:
            logger.info(f"MemoryBridge WebUI started: {self.plugin.web_url}")

    async def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.server_task:
            try:
                await asyncio.wait_for(self.server_task, timeout=5)
            except Exception:
                pass
        self.plugin.web_started = False


@register(PLUGIN_ID, PLUGIN_AUTHOR, "续忆桥：会话记忆导出、备份、迁移与 WebUI 管理", PLUGIN_VERSION)
class MemoryBridgePlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        self.base = self.guess_base()
        self.export_dir = ensure_dir(self.base / "memory_bridge_exports")
        self.import_dir = ensure_dir(self.base / "memory_bridge_imports")
        self.state_file = self.export_dir / "state.json"
        self.records_file = self.export_dir / "export_records.json"
        self.sessions_file = self.export_dir / "known_sessions.json"
        self.actions_file = self.export_dir / "web_actions.json"
        self.import_records_file = self.import_dir / "import_records.json"
        self.state = self.load_state()
        self.sessions = self.load_json(self.sessions_file, {})
        self.web_started = False
        self.web_url = ""
        self.web_admin_server = MemoryBridgeWebAdminServer(self)
        if logger:
            logger.info(f"MemoryBridge v{PLUGIN_VERSION} initialized, export_dir={self.export_dir}, import_dir={self.import_dir}")

    async def initialize(self):
        try:
            await self.web_admin_server.start()
        except Exception as error:
            if logger:
                logger.error(f"MemoryBridge WebUI start failed: {error}")

    async def terminate(self):
        try:
            await self.web_admin_server.stop()
        except Exception:
            pass
        if logger:
            logger.info("MemoryBridge terminated")

    def cfg(self, path: str, default: Any = None) -> Any:
        flat_key = str(path).replace('.', '_')
        config = getattr(self, 'config', None)
        for key in (path, flat_key):
            try:
                if isinstance(config, dict) and key in config:
                    return config.get(key, default)
                getter = getattr(config, 'get', None)
                if callable(getter):
                    value = getter(key)
                    if value is not None:
                        return value
            except Exception:
                pass
        current = config
        for part in str(path).split('.'):
            try:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    getter = getattr(current, 'get', None)
                    current = getter(part) if callable(getter) else getattr(current, part)
            except Exception:
                return default
            if current is None:
                return default
        return current

    def guess_base(self) -> Path:
        candidates = [os.getenv("ASTRBOT_DATA_DIR"), os.getenv("DATA_DIR"), "/AstrBot/data", str(Path.cwd() / "data"), str(Path.cwd())]
        for candidate in candidates:
            if candidate:
                try:
                    return ensure_dir(Path(candidate))
                except Exception:
                    pass
        return Path.cwd()

    def load_json(self, path: Path, default: Any):
        try:
            return json.loads(path.read_text("utf-8")) if path.exists() else default
        except Exception:
            return default

    def save_json(self, path: Path, data: Any):
        try:
            ensure_dir(path.parent)
            path.write_text(dumps(data), "utf-8")
        except Exception as error:
            if logger:
                logger.warning(f"MemoryBridge failed to save {path}: {error}")

    def load_state(self):
        return self.load_json(self.state_file, {})

    def save_state(self):
        self.state["updated_at"] = now_str()
        self.save_json(self.state_file, self.state)

    def append_action_log(self, row):
        data = self.load_json(self.actions_file, [])
        data = data if isinstance(data, list) else []
        row.setdefault("time", now_str())
        data.insert(0, row)
        self.save_json(self.actions_file, data[:200])

    def record_export(self, path, kind, origin, extra=None):
        records = self.load_json(self.records_file, [])
        records = records if isinstance(records, list) else []
        record = {"name": path.name, "path": str(path), "kind": kind, "origin": origin, "size": path.stat().st_size if path.exists() else 0, "created_at": now_str()}
        if extra:
            record.update(extra)
        records.insert(0, record)
        self.save_json(self.records_file, records[:300])

    def record_import(self, record):
        records = self.load_json(self.import_records_file, [])
        records = records if isinstance(records, list) else []
        record.setdefault("created_at", now_str())
        records.insert(0, record)
        self.save_json(self.import_records_file, records[:300])

    def origin_candidates(self, event: Any) -> list[str]:
        output = []
        objects = [getattr(event, "message_obj", None), event, getattr(event, "event", None), getattr(event, "raw_event", None)]
        for current_object in objects:
            if current_object is None:
                continue
            for name in ["unified_msg_origin", "get_unified_msg_origin", "session_id", "get_session_id"]:
                try:
                    value = getattr(current_object, name, None)
                    value = value() if callable(value) else value
                    if value and str(value) not in output:
                        output.append(str(value))
                except Exception:
                    pass
        extra = []
        for origin in output:
            parts = origin.split(":", 2)
            if len(parts) == 3 and parts[0] in {"default", ""}:
                for platform_name in ["aiocqhttp", "napcat", "default"]:
                    candidate = f"{platform_name}:{parts[1]}:{parts[2]}"
                    if candidate not in output and candidate not in extra:
                        extra.append(candidate)
        output += extra
        return output or ["unknown"]

    def origin(self, event: Any) -> str:
        return self.origin_candidates(event)[0]

    def record_session(self, event: AstrMessageEvent):
        try:
            for origin in self.origin_candidates(event):
                if not origin or origin == "unknown":
                    continue
                message = getattr(event, "message_str", None)
                message = message() if callable(message) else message
                category = "group" if "group" in origin.lower() else "friend" if "friend" in origin.lower() or "private" in origin.lower() else "unknown"
                self.sessions[origin] = {"origin": origin, "category": category, "last_seen": now_str(), "last_message_preview": preview(str(message or ""), 120)}
            self.save_json(self.sessions_file, self.sessions)
        except Exception:
            pass

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=1000)
    async def remember_private_session(self, event):
        self.record_session(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def remember_group_session(self, event):
        self.record_session(event)

    async def maybe(self, fn, *args, **kwargs):
        result = fn(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    def extract_items(self, source: Any):
        if source is None:
            return []
        if isinstance(source, str):
            stripped = source.strip()
            if not stripped:
                return []
            try:
                return self.extract_items(json.loads(stripped))
            except Exception:
                return [{"role": "unknown", "content": stripped}]
        if isinstance(source, list):
            return source
        if isinstance(source, tuple):
            return list(source)
        if isinstance(source, dict):
            for key in ["messages", "history", "items", "contexts", "conversation", "data", "list"]:
                if isinstance(source.get(key), list):
                    return source[key]
                nested = self.extract_items(source.get(key)) if source.get(key) is not None else []
                if nested:
                    return nested
            if any(key in source for key in ["role", "sender", "content", "message", "text", "raw_message"]):
                return [source]
            return []
        for attr in ["messages", "history", "items", "contexts", "conversation", "data"]:
            try:
                nested = self.extract_items(getattr(source, attr, None))
                if nested:
                    return nested
            except Exception:
                pass
        return []

    def to_msg(self, index, item):
        role, content, timestamp, name, metadata = "unknown", "", None, None, {}
        if isinstance(item, dict):
            role = str(item.get("role") or item.get("sender") or item.get("type") or item.get("name") or "unknown")
            content_source = item.get("content") if "content" in item else (item.get("message") if "message" in item else (item.get("message_str") if "message_str" in item else (item.get("text") if "text" in item else item.get("raw_message", ""))))
            content = extract_text_content(content_source)
            timestamp = item.get("time") or item.get("timestamp") or item.get("created_at") or item.get("updated_at")
            name = item.get("name") or item.get("nickname")
            metadata = {key: value for key, value in item.items() if key not in {"role", "sender", "type", "content", "message", "message_str", "text", "raw_message", "time", "timestamp", "created_at", "updated_at", "name", "nickname"}}
        else:
            for attr in ["role", "sender", "type", "name"]:
                try:
                    value = getattr(item, attr, None)
                    if value:
                        role = str(value)
                        break
                except Exception:
                    pass
            for attr in ["content", "message", "message_str", "text", "raw_message"]:
                try:
                    if hasattr(item, attr):
                        content = extract_text_content(getattr(item, attr))
                        break
                except Exception:
                    pass
            for attr in ["time", "timestamp", "created_at", "updated_at"]:
                try:
                    value = getattr(item, attr, None)
                    if value:
                        timestamp = value
                        break
                except Exception:
                    pass
            if not content:
                content = str(item)
        return MemoryMessage(index, role, content, str(timestamp) if timestamp else None, name, "conversation_manager", metadata)

    async def get_current_cid(self, conversation_manager, origin):
        fn = getattr(conversation_manager, "get_curr_conversation_id", None)
        if not fn:
            return None
        for args in [(origin,), ()]:
            try:
                return await self.maybe(fn, *args)
            except Exception:
                pass
        return None

    async def try_read_for_origin(self, conversation_manager, origin, limit):
        current_cid = await self.get_current_cid(conversation_manager, origin)
        debug = []
        if current_cid:
            fn = getattr(conversation_manager, "get_conversation", None)
            if fn:
                for args in [(origin, current_cid), (current_cid,), (origin,)]:
                    try:
                        conversation = await self.maybe(fn, *args)
                        history = getattr(conversation, "history", None) if conversation is not None else None
                        if history is None and isinstance(conversation, dict):
                            history = conversation.get("history") or conversation.get("messages")
                        items = self.extract_items(history)
                        debug.append(f"get_conversation{args}: {len(items)}")
                        if items:
                            return items[-limit:], f"conversation_manager:{current_cid}", debug
                    except Exception as error:
                        debug.append(f"get_conversation{args} err:{type(error).__name__}:{error}")
        for method_name in ["get_conversation_by_origin", "get_conversation_by_unified_msg_origin", "get_messages", "get_history", "get_conversation_history", "get_context", "get_conversation"]:
            fn = getattr(conversation_manager, method_name, None)
            if not fn:
                continue
            for args in [(origin,), (origin, limit), ()]:
                try:
                    items = self.extract_items(await self.maybe(fn, *args))
                    debug.append(f"{method_name}{args}: {len(items)}")
                    if items:
                        return items[-limit:], f"conversation_manager:{method_name}", debug
                except Exception as error:
                    debug.append(f"{method_name}{args} err:{type(error).__name__}:{error}")
        return [], "", debug

    async def history(self, event: Any, limit: int | None = None) -> list[MemoryMessage]:
        limit = int(limit or self.cfg("export.max_messages", 1000) or 1000)
        conversation_manager = getattr(self.context, "conversation_manager", None)
        raw_items = []
        source = "current_message_only"
        all_debug = []
        origins = self.origin_candidates(event)
        if conversation_manager:
            for origin in origins:
                try:
                    items, source_name, debug = await self.try_read_for_origin(conversation_manager, origin, limit)
                    all_debug += [f"origin={origin}"] + debug[-20:]
                    if items:
                        raw_items = items
                        source = source_name
                        break
                except Exception as error:
                    all_debug.append(f"origin={origin} err:{type(error).__name__}:{error}")
        if not raw_items:
            try:
                message = getattr(event, "message_str", None)
                message = message() if callable(message) else message
                raw_items = [{"role": "user", "content": str(message or getattr(event, "message", "") or ""), "time": now_str(), "source": "event_fallback"}]
            except Exception:
                raw_items = [{"role": "system", "content": "未能读取会话历史", "time": now_str()}]
        self.state["last_history_source"] = source
        self.state["last_history_count"] = len(raw_items[-limit:])
        self.state["last_history_origins_tried"] = origins
        self.state["last_history_debug"] = all_debug[-80:]
        self.save_state()
        return [self.to_msg(index, item) for index, item in enumerate(raw_items[-limit:])]

    def write_json(self, path, value):
        path.write_text(dumps(value), "utf-8")

    def write_md_messages(self, path, title, messages):
        lines = [f"# {title}", ""]
        for message in messages:
            lines += [f"## #{message.get('index')} {message.get('role', '')} {message.get('time') or ''}"]
            if message.get("clean_reasons"):
                lines.append(f"- reasons: {', '.join(message.get('clean_reasons') or [])}")
            lines += ["", str(message.get("content") or ""), "\n---\n"]
        path.write_text("\n".join(lines), "utf-8")

    def zip_dir(self, source_dir, destination):
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source_dir).as_posix())

    def report_md(self, result):
        stats = result["stats"]
        lines = ["# 续忆桥记忆清洗报告", "", f"- 插件版本：v{PLUGIN_VERSION}", f"- 清洗模式：{result['mode']}", f"- 生成时间：{result['generated_at']}", f"- 原始消息数：{stats['raw_count']}", f"- 保留消息数：{stats['kept_count']}", f"- 删除消息数：{stats['removed_count']}", f"- 脱敏消息数：{stats['masked_count']}", f"- 保留比例：{stats['keep_ratio'] * 100:.1f}%", "", "## 删除原因统计"]
        for key, value in sorted((result.get("reason_counts") or {}).items(), key=lambda pair: pair[1], reverse=True):
            lines.append(f"- {key}: {value}")
        lines += ["", "## 抽取事实预览"]
        for fact in result.get("extracted_facts", [])[:40]:
            lines.append(f"- [{fact['type']}] {fact['content']}")
        lines += ["", "## 说明", "本清洗为纯代码规则清洗，未调用 LLM，未修改 AstrBot 原始历史。"]
        return "\n".join(lines)

    def build_memory_card(self, clean_result, manifest):
        stats = clean_result.get("stats") or {}
        facts = clean_result.get("extracted_facts") or []
        kept = clean_result.get("kept_messages") or []
        by_type = {}
        for fact in facts:
            by_type.setdefault(fact.get("type") or "other", []).append(fact.get("content") or "")
        lines = [
            "# 续忆迁移记忆卡",
            "",
            "## 迁移来源",
            f"- 来源会话：{manifest.get('origin') or 'unknown'}",
            f"- 导出时间：{manifest.get('created_at') or now_str()}",
            f"- 清洗模式：{clean_result.get('mode') or 'balanced'}",
            f"- 原始消息数：{stats.get('raw_count', 0)}",
            f"- 保留消息数：{stats.get('kept_count', 0)}",
            f"- 历史来源：{manifest.get('history_source') or 'unknown'}",
            "",
        ]
        sections = [("user_preference", "用户偏好"), ("project_status", "项目状态"), ("technical_context", "技术环境"), ("resolved_issue", "已解决问题"), ("open_task", "待办与下一步")]
        for fact_type, title in sections:
            values = by_type.get(fact_type) or []
            if values:
                lines += [f"## {title}"]
                for value in values[:18]:
                    lines.append(f"- {value}")
                lines.append("")
        lines += ["## 最近保留上下文"]
        for message in kept[-24:]:
            lines.append(f"- {message.get('role', 'unknown')}：{preview(message.get('content') or '', 260)}")
        lines += ["", "## 使用说明", "- 这是一份由续忆桥生成的压缩迁移记忆。", "- 后续对话中请把它作为背景资料使用，不需要主动完整复述。", "- 如用户询问旧会话进展、项目状态、偏好或待办，请优先参考本记忆卡。"]
        return "\n".join(lines)

    def build_migration_payload(self, clean_result, manifest):
        memory_card = self.build_memory_card(clean_result, manifest)
        facts = clean_result.get("extracted_facts") or []
        chunks = []
        for index, fact in enumerate(facts[:160], 1):
            chunks.append({"id": f"chunk_{index:04d}", "category": fact.get("type") or "fact", "text": fact.get("content") or "", "tags": [fact.get("type") or "fact"], "source_indices": fact.get("source_indices") or []})
        seed_history = [
            {"role": "user", "content": "以下是从旧会话迁移来的压缩记忆，请作为后续对话背景。不要主动复述，只有当用户问题相关时再使用。\n\n" + memory_card},
            {"role": "assistant", "content": "已接收迁移记忆。后续我会参考这些背景信息继续协助你。"},
        ]
        card_json = {"source": {"origin": manifest.get("origin"), "exported_at": manifest.get("created_at"), "history_source": manifest.get("history_source"), "mode": clean_result.get("mode")}, "stats": clean_result.get("stats") or {}, "facts": facts[:120], "summary_chars": len(memory_card)}
        import_plan = {"recommended_mode": "hybrid", "supported_modes": ["seed", "plugin_memory", "hybrid"], "will_create_conversation": True, "will_import_plugin_memory": True, "rollback_supported": True, "seed_messages": len(seed_history), "chunks": len(chunks)}
        return {"memory_card_md": memory_card, "memory_card_json": card_json, "seed_history": seed_history, "chunks": chunks, "import_plan": import_plan}

    async def build_raw_zip(self, event):
        messages = await self.history(event)
        timestamp = stamp()
        work_dir = ensure_dir(self.export_dir / f"work_raw_{timestamp}")
        raw_messages = [asdict(message) for message in messages]
        self.write_json(work_dir / "raw_messages.json", raw_messages)
        self.write_md_messages(work_dir / "raw_messages.md", "Raw Messages", raw_messages)
        manifest = {"plugin": PLUGIN_ID, "version": PLUGIN_VERSION, "kind": "raw", "created_at": now_str(), "origin": self.origin(event), "origins_tried": self.state.get("last_history_origins_tried"), "message_count": len(raw_messages), "history_source": self.state.get("last_history_source")}
        self.write_json(work_dir / "manifest.json", manifest)
        zip_path = self.export_dir / f"memory_bridge_raw_{timestamp}.zip"
        self.zip_dir(work_dir, zip_path)
        shutil.rmtree(work_dir, ignore_errors=True)
        self.record_export(zip_path, "raw", self.origin(event), {"message_count": len(raw_messages), "history_source": manifest.get("history_source")})
        return zip_path, manifest

    async def build_clean_zip(self, event, mode):
        messages = await self.history(event)
        cleaner = RuleCleaner(mode, int(self.cfg("cleaning.keep_recent_messages", 20) or 20), truthy(self.cfg("cleaning.mask_secrets", True), True))
        clean_result = cleaner.clean(messages)
        timestamp = stamp()
        work_dir = ensure_dir(self.export_dir / f"work_clean_{timestamp}")
        cleaning_dir = ensure_dir(work_dir / "cleaning")
        raw_dir = ensure_dir(work_dir / "raw")
        migration_dir = ensure_dir(work_dir / "migration")
        raw_messages = [asdict(message) for message in messages]
        self.write_json(raw_dir / "raw_messages.json", raw_messages)
        self.write_md_messages(raw_dir / "raw_messages.md", "Raw Messages", raw_messages)
        self.write_json(cleaning_dir / "clean_report.json", {key: value for key, value in clean_result.items() if key not in {"kept_messages", "removed_messages", "decisions"}})
        (cleaning_dir / "clean_report.md").write_text(self.report_md(clean_result), "utf-8")
        for name in ["kept_messages", "removed_messages", "masked_messages", "extracted_facts", "rules_used"]:
            self.write_json(cleaning_dir / (name + ".json"), clean_result[name])
        self.write_json(cleaning_dir / "decisions.json", clean_result["decisions"])
        self.write_md_messages(cleaning_dir / "kept_messages.md", "Kept Messages", clean_result["kept_messages"])
        (cleaning_dir / "removed_preview.md").write_text("\n".join([f"- #{item['index']} {item['reasons']}: {item['content_preview']}" for item in clean_result["removed_messages"][:200]]), "utf-8")
        manifest = {"plugin": PLUGIN_ID, "version": PLUGIN_VERSION, "kind": "clean_export", "created_at": now_str(), "origin": self.origin(event), "origins_tried": self.state.get("last_history_origins_tried"), "stats": clean_result["stats"], "history_source": self.state.get("last_history_source"), "migration_available": True}
        migration = self.build_migration_payload(clean_result, manifest)
        (migration_dir / "memory_card.md").write_text(migration["memory_card_md"], "utf-8")
        self.write_json(migration_dir / "memory_card.json", migration["memory_card_json"])
        self.write_json(migration_dir / "seed_history.json", migration["seed_history"])
        self.write_json(migration_dir / "import_plan.json", migration["import_plan"])
        with (migration_dir / "chunks.jsonl").open("w", encoding="utf-8") as chunks_file:
            for chunk in migration["chunks"]:
                chunks_file.write(json.dumps(chunk, ensure_ascii=False, default=str) + "\n")
        (migration_dir / "prompt_handoff.md").write_text("请把 migration/memory_card.md 作为迁移后的背景资料。", "utf-8")
        self.write_json(work_dir / "manifest.json", manifest)
        zip_path = self.export_dir / f"memory_bridge_clean_{mode}_{timestamp}.zip"
        self.zip_dir(work_dir, zip_path)
        shutil.rmtree(work_dir, ignore_errors=True)
        self.record_export(zip_path, f"clean_{mode}", self.origin(event), {"stats": clean_result["stats"], "history_source": manifest.get("history_source"), "migration_available": True})
        return zip_path, clean_result

    async def send_file(self, event, path):
        mode = str(self.cfg("file_delivery.send_mode", "auto") or "auto").lower()
        if mode in {"path", "path_only", "disabled"}:
            self.state.update({"last_send_status": "skipped", "last_send_action": mode, "last_send_file": str(path), "last_send_time": now_str(), "last_send_error": ""})
            self.save_state()
            yield event.plain_result(f"文件已生成：{path}")
            return
        if Comp and hasattr(Comp, "File"):
            try:
                self.state.update({"last_send_status": "success_pending_adapter", "last_send_action": "Comp.File", "last_send_file": str(path), "last_send_time": now_str(), "last_send_origin": self.origin(event), "last_send_error": ""})
                self.save_state()
                yield event.chain_result([Comp.File(file=str(path), name=path.name)])
                return
            except Exception as error:
                self.state.update({"last_send_status": "failed", "last_send_action": "Comp.File", "last_send_file": str(path), "last_send_time": now_str(), "last_send_error": str(error), "last_send_traceback": traceback.format_exc()})
                self.save_state()
                yield event.plain_result(f"文件消息段构建失败：{error}\n文件已生成：{path}")
                return
        self.state.update({"last_send_status": "failed", "last_send_action": "Comp.File unavailable", "last_send_file": str(path), "last_send_time": now_str(), "last_send_error": "Comp.File unavailable"})
        self.save_state()
        yield event.plain_result(f"Comp.File 不可用，文件已生成：{path}")

    def files_text(self):
        files = sorted(self.export_dir.glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)[:10]
        if not files:
            return "暂无导出文件。"
        return "最近导出文件：\n" + "\n".join([f"- {path.name}  {path.stat().st_size} bytes  {datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')}" for path in files])

    def latest_clean_zip(self):
        files = sorted(self.export_dir.glob("memory_bridge_clean_*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
        return files[0] if files else None

    def resolve_export_file(self, filename):
        path = (self.export_dir / Path(filename).name).resolve()
        try:
            path.relative_to(self.export_dir.resolve())
        except Exception:
            return None
        return path if path.exists() and path.is_file() and path.suffix.lower() == ".zip" else None

    def read_json_from_zip(self, archive, name, default=None):
        try:
            return json.loads(archive.read(name).decode("utf-8", "ignore"))
        except Exception:
            return default

    def load_migration_package(self, filename=None):
        path = self.resolve_export_file(filename) if filename else self.latest_clean_zip()
        if not path:
            return None, {"ok": False, "error": "没有找到可导入的 clean zip。请先执行 /续忆 clean_export balanced。"}
        info = {"ok": True, "file": path.name, "path": str(path), "size": path.stat().st_size, "warnings": []}
        try:
            with zipfile.ZipFile(path, "r") as archive:
                names = set(archive.namelist())
                manifest = self.read_json_from_zip(archive, "manifest.json", {}) or {}
                clean_report = self.read_json_from_zip(archive, "cleaning/clean_report.json", {}) or {}
                seed_history = self.read_json_from_zip(archive, "migration/seed_history.json", None)
                memory_card_json = self.read_json_from_zip(archive, "migration/memory_card.json", None)
                import_plan = self.read_json_from_zip(archive, "migration/import_plan.json", None)
                memory_card_md = ""
                chunks = []
                if "migration/memory_card.md" in names:
                    memory_card_md = archive.read("migration/memory_card.md").decode("utf-8", "ignore")
                if "migration/chunks.jsonl" in names:
                    for line in archive.read("migration/chunks.jsonl").decode("utf-8", "ignore").splitlines():
                        line = line.strip()
                        if line:
                            try:
                                chunks.append(json.loads(line))
                            except Exception:
                                pass
                if seed_history is None or not memory_card_md:
                    kept = self.read_json_from_zip(archive, "cleaning/kept_messages.json", []) or []
                    facts = self.read_json_from_zip(archive, "cleaning/extracted_facts.json", []) or []
                    fallback_result = {"mode": manifest.get("mode") or "balanced", "stats": manifest.get("stats") or clean_report.get("stats") or {}, "kept_messages": kept, "extracted_facts": facts}
                    migration = self.build_migration_payload(fallback_result, manifest)
                    seed_history = seed_history or migration["seed_history"]
                    memory_card_md = memory_card_md or migration["memory_card_md"]
                    memory_card_json = memory_card_json or migration["memory_card_json"]
                    import_plan = import_plan or migration["import_plan"]
                    chunks = chunks or migration["chunks"]
                    info["warnings"].append("该包缺少 v0.5.0 migration 文件，已从 cleaning 内容临时生成导入种子。")
                info.update({"manifest": manifest, "stats": manifest.get("stats") or clean_report.get("stats") or {}, "seed_history": seed_history or [], "memory_card_md": memory_card_md, "memory_card_json": memory_card_json or {}, "import_plan": import_plan or {}, "chunks": chunks})
                return path, info
        except Exception as error:
            return path, {"ok": False, "file": path.name, "error": str(error), "traceback": traceback.format_exc()[-2000:]}

    def import_preview_payload(self, filename=None):
        path, package = self.load_migration_package(filename)
        if not package.get("ok"):
            return package
        memory_card = package.get("memory_card_md") or ""
        stats = package.get("stats") or {}
        manifest = package.get("manifest") or {}
        warnings = list(package.get("warnings") or [])
        if stats.get("raw_count") == 1 or manifest.get("message_count") == 1:
            warnings.append("该记忆包消息数为 1，可能不是有效迁移包。")
        if manifest.get("history_source") == "current_message_only":
            warnings.append("该记忆包来源为 current_message_only，不建议导入。")
        return {"ok": True, "file": package.get("file"), "size": package.get("size"), "manifest": manifest, "stats": stats, "seed_count": len(package.get("seed_history") or []), "chunks_count": len(package.get("chunks") or []), "memory_card_preview": preview(memory_card, 1200), "import_plan": package.get("import_plan") or {}, "warnings": warnings}

    async def create_seed_conversation(self, target_origin, seed_history, title):
        conversation_manager = getattr(self.context, "conversation_manager", None)
        if not conversation_manager:
            raise RuntimeError("conversation_manager 不可用")
        platform_id = target_origin.split(":", 1)[0] if ":" in target_origin else "default"
        old_cid = await self.get_current_cid(conversation_manager, target_origin)
        new_fn = getattr(conversation_manager, "new_conversation", None) or getattr(conversation_manager, "create_conversation", None)
        if not new_fn:
            raise RuntimeError("当前 AstrBot ConversationManager 没有 new_conversation/create_conversation 方法")
        call_plans = [
            {"unified_msg_origin": target_origin, "platform_id": platform_id, "content": seed_history, "title": title, "persona_id": None},
            {"unified_msg_origin": target_origin, "content": seed_history, "title": title, "persona_id": None},
            {"unified_msg_origin": target_origin, "content": seed_history, "title": title},
            {"unified_msg_origin": target_origin, "history": seed_history, "title": title},
        ]
        last_error = None
        conversation_id = None
        for kwargs in call_plans:
            try:
                conversation_id = await self.maybe(new_fn, **kwargs)
                break
            except Exception as error:
                last_error = error
        if conversation_id is None:
            try:
                conversation_id = await self.maybe(new_fn, target_origin, platform_id, seed_history, title)
            except Exception as error:
                last_error = error
        if conversation_id is None and last_error:
            raise last_error
        switch_fn = getattr(conversation_manager, "switch_conversation", None)
        switch_ok, switch_error = False, ""
        if switch_fn and conversation_id:
            for args in [(target_origin, conversation_id), (conversation_id,), (target_origin, str(conversation_id))]:
                try:
                    await self.maybe(switch_fn, *args)
                    switch_ok = True
                    break
                except Exception as error:
                    switch_error = str(error)
        return {"conversation_id": str(conversation_id), "previous_conversation_id": str(old_cid) if old_cid else "", "switch_ok": switch_ok, "switch_error": switch_error}

    async def import_package_to_origin(self, target_origin, filename=None, mode="hybrid"):
        mode = mode if mode in {"seed", "plugin_memory", "hybrid"} else "hybrid"
        path, package = self.load_migration_package(filename)
        if not package.get("ok"):
            return package
        import_id = f"import_{stamp()}"
        import_path = ensure_dir(self.import_dir / import_id)
        manifest = package.get("manifest") or {}
        seed_history = package.get("seed_history") or []
        chunks = package.get("chunks") or []
        result = {"ok": True, "import_id": import_id, "mode": mode, "target_origin": target_origin, "source_file": path.name if path else package.get("file"), "created_at": now_str(), "conversation": None, "plugin_memory": None, "warnings": package.get("warnings") or []}
        if mode in {"plugin_memory", "hybrid"}:
            (import_path / "memory_card.md").write_text(package.get("memory_card_md") or "", "utf-8")
            self.write_json(import_path / "memory_card.json", package.get("memory_card_json") or {})
            self.write_json(import_path / "chunks.json", chunks)
            self.write_json(import_path / "manifest.json", manifest)
            if path and path.exists():
                try:
                    shutil.copy2(path, import_path / path.name)
                except Exception as error:
                    result["warnings"].append(f"源 zip 备份失败：{error}")
            result["plugin_memory"] = {"dir": str(import_path), "chunks": len(chunks), "enabled": True}
        if mode in {"seed", "hybrid"}:
            title = f"续忆迁移 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            try:
                result["conversation"] = await self.create_seed_conversation(target_origin, seed_history, title)
            except Exception as error:
                result["ok"] = False
                result["error"] = f"创建迁移会话失败：{error}"
                result["traceback"] = traceback.format_exc()[-2000:]
        record = dict(result)
        record["source_origin"] = manifest.get("origin")
        record["history_source"] = manifest.get("history_source")
        record["stats"] = package.get("stats") or {}
        record["status"] = "active" if result.get("ok") else "failed"
        self.record_import(record)
        self.append_action_log({"kind": "memory_import", "ok": result.get("ok"), "import_id": import_id, "mode": mode, "target_origin": target_origin, "source_file": result.get("source_file"), "error": result.get("error", "")})
        return result

    async def rollback_import(self, import_id):
        records = self.load_json(self.import_records_file, [])
        if not isinstance(records, list):
            records = []
        target_record = None
        for record in records:
            if record.get("import_id") == import_id:
                target_record = record
                break
        if not target_record:
            return {"ok": False, "error": f"未找到导入记录：{import_id}"}
        warnings = []
        conversation = target_record.get("conversation") or {}
        conversation_manager = getattr(self.context, "conversation_manager", None)
        deleted_conversation = False
        if conversation_manager and conversation.get("conversation_id"):
            delete_fn = getattr(conversation_manager, "delete_conversation", None)
            if delete_fn:
                for args in [(target_record.get("target_origin"), conversation.get("conversation_id")), (conversation.get("conversation_id"),)]:
                    try:
                        await self.maybe(delete_fn, *args)
                        deleted_conversation = True
                        break
                    except Exception as error:
                        warnings.append(f"删除 conversation 失败：{error}")
                        break
            switch_fn = getattr(conversation_manager, "switch_conversation", None)
            if switch_fn and conversation.get("previous_conversation_id"):
                try:
                    await self.maybe(switch_fn, target_record.get("target_origin"), conversation.get("previous_conversation_id"))
                except Exception as error:
                    warnings.append(f"切回旧 conversation 失败：{error}")
        import_path = self.import_dir / import_id
        disabled_marker = import_path / "DISABLED"
        if import_path.exists():
            disabled_marker.write_text(now_str(), "utf-8")
        target_record["status"] = "rolled_back"
        target_record["rolled_back_at"] = now_str()
        self.save_json(self.import_records_file, records)
        self.append_action_log({"kind": "memory_import_rollback", "ok": True, "import_id": import_id, "deleted_conversation": deleted_conversation})
        return {"ok": True, "import_id": import_id, "deleted_conversation": deleted_conversation, "plugin_memory_disabled": import_path.exists(), "warnings": warnings}

    @filter.command_group("续忆")
    def memory_group(self):
        pass

    @memory_group.command("version")
    async def version(self, event):
        yield event.plain_result(f"续忆桥 Memory Bridge v{PLUGIN_VERSION}\nAstrBot：unknown（仅表示未读取到版本号，不影响功能）\nPython：{sys.version.split()[0]}\n平台：{platform.platform()}\nWebUI：{'已启动 ' + self.web_url if self.web_started else '未启动'}")

    @memory_group.command("help")
    async def help(self, event):
        yield event.plain_result("续忆桥命令：\n/续忆 version\n/续忆 status\n/续忆 doctor\n/续忆 backend\n/续忆 history_debug\n/续忆 history_preview [数量]\n/续忆 export\n/续忆 files\n/续忆 send_last\n/续忆 clean_preview [safe|balanced|aggressive]\n/续忆 clean_export [safe|balanced|aggressive]\n/续忆 inspect_last\n/续忆 import_preview\n/续忆 import_to_current [hybrid|seed|plugin_memory]\n/续忆 import_to_origin <origin> [hybrid|seed|plugin_memory]\n/续忆 imports\n/续忆 import_rollback <import_id>\n/续忆 webui\n\nWebUI：" + (self.web_url or "默认 http://0.0.0.0:2333（如已启用）"))

    @memory_group.command("webui")
    async def webui_cmd(self, event):
        yield event.plain_result(f"续忆桥 WebUI：{'已启动 ' + self.web_url if self.web_started else '未启动'}\n默认监听：0.0.0.0:2333\n如在 Docker 内运行，请确认已映射 2333:2333。")

    @memory_group.command("status")
    async def status(self, event):
        yield event.plain_result(f"续忆桥 v{PLUGIN_VERSION}\n导出目录：{self.export_dir}\n导入目录：{self.import_dir}\n最近导出：{self.state.get('last_export_file', '无')}\n最近发送：{self.state.get('last_send_status', '无')}\nWebUI：{'已启动 ' + self.web_url if self.web_started else '未启动'}")

    @memory_group.command("doctor")
    async def doctor(self, event):
        yield event.plain_result(f"续忆桥自检 v{PLUGIN_VERSION}\n- Python：{sys.version.split()[0]}\n- 平台：{platform.platform()}\n- Docker 判断：{'可能是 Docker' if Path('/.dockerenv').exists() else '未检测到'}\n- 导出目录：{self.export_dir}\n- 导入目录：{self.import_dir}\n- 导出目录可写：{os.access(self.export_dir, os.W_OK)}\n- Comp.File：{'可用' if (Comp and hasattr(Comp, 'File')) else '不可用'}\n- FastAPI/Uvicorn：{'可用' if FASTAPI_AVAILABLE else '不可用'}\n- WebUI：{'已启动 ' + self.web_url if self.web_started else '未启动'}\n- v0.5.0：新增迁移包 migration 文件、导入预览、导入到新会话、导入记录与回滚。")

    @memory_group.command("backend")
    async def backend(self, event):
        yield event.plain_result(f"后端诊断：\n- conversation_manager：{type(getattr(self.context, 'conversation_manager', None)).__name__ if getattr(self.context, 'conversation_manager', None) else '未检测到'}\n- current origin：{self.origin(event)}\n- origin candidates：{self.origin_candidates(event)}\n- Comp.File：{bool(Comp and hasattr(Comp, 'File'))}\n- CorePlain：{bool(CorePlain)}\n- CoreFile：{bool(CoreFile)}\n- MessageChain：{bool(MessageChain)}\n- MessageSession：{bool(MessageSession)}\n- send_mode：{self.cfg('file_delivery.send_mode', 'auto')}\n- export_dir：{self.export_dir}\n- import_dir：{self.import_dir}\n- last_history_source：{self.state.get('last_history_source', '未知')}\n- last_history_count：{self.state.get('last_history_count', '未知')}\n- last_send_status：{self.state.get('last_send_status', '未知')}\n- last_send_error：{self.state.get('last_send_error', '无')}\n- WebUI：{'已启动 ' + self.web_url if self.web_started else '未启动'}")

    @memory_group.command("history_debug")
    async def history_debug(self, event):
        await self.history(event)
        debug = self.state.get("last_history_debug", []) or []
        yield event.plain_result("历史读取调试：\n" + f"- origins_tried: {self.state.get('last_history_origins_tried')}\n- source: {self.state.get('last_history_source')}\n- count: {self.state.get('last_history_count')}\n" + "\n".join([f"- {item}" for item in debug[-30:]]))

    @memory_group.command("history_preview")
    async def history_preview_cmd(self, event, count: int = 10):
        safe_count = min(30, max(1, int(count or 10)))
        messages = await self.history(event, limit=safe_count)
        lines = [f"最近 {len(messages)} 条历史预览", f"历史来源：{self.state.get('last_history_source')}", ""]
        for message in messages[-safe_count:]:
            lines.append(f"#{message.index} {message.role} {message.time or ''}: {preview(message.content, 160)}")
        yield event.plain_result("\n".join(lines))

    @memory_group.command("export")
    async def export(self, event):
        try:
            zip_path, manifest = await self.build_raw_zip(event)
            self.state.update({"last_export_file": str(zip_path), "last_export_name": zip_path.name, "last_export_status": "success", "last_export_time": now_str(), "last_export_size": zip_path.stat().st_size, "last_export_mode": "raw"})
            self.save_state()
            yield event.plain_result(f"已生成记忆压缩包：{zip_path.name}\n大小：{zip_path.stat().st_size} bytes\n消息数：{manifest.get('message_count')}\n历史来源：{manifest.get('history_source')}")
            async for result in self.send_file(event, zip_path):
                yield result
        except Exception as error:
            self.state.update({"last_export_status": "error", "last_export_error": traceback.format_exc()})
            self.save_state()
            yield event.plain_result(f"导出失败：{error}")

    @memory_group.command("files")
    async def files(self, event):
        yield event.plain_result(self.files_text())

    @memory_group.command("send_last")
    async def send_last(self, event):
        files = sorted(self.export_dir.glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not files:
            yield event.plain_result("暂无可发送的 zip。")
            return
        path = files[0]
        yield event.plain_result(f"正在发送最近文件：{path.name}\n大小：{path.stat().st_size} bytes")
        async for result in self.send_file(event, path):
            yield result

    @memory_group.command("inspect_last")
    async def inspect_last(self, event):
        files = sorted(self.export_dir.glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not files:
            yield event.plain_result("暂无可检查的 zip。")
            return
        info = self.inspect_export_zip(files[0])
        yield event.plain_result("最近记忆包检查：\n" + dumps(info)[:3800])

    @memory_group.command("clean_preview")
    async def clean_preview(self, event, mode="balanced"):
        try:
            mode = mode if mode in {"safe", "balanced", "aggressive"} else "balanced"
            messages = await self.history(event)
            clean_result = RuleCleaner(mode, int(self.cfg("cleaning.keep_recent_messages", 20) or 20), truthy(self.cfg("cleaning.mask_secrets", True), True)).clean(messages)
            stats = clean_result["stats"]
            top = "\n".join([f"  - {key}: {value}" for key, value in sorted((clean_result.get('reason_counts') or {}).items(), key=lambda pair: pair[1], reverse=True)[:8]]) or "  - 无"
            facts = "\n".join([f"  - [{fact['type']}] {fact['content']}" for fact in clean_result.get('extracted_facts', [])[:6]]) or "  - 暂无"
            yield event.plain_result(f"续忆清洗预览（纯代码规则，未调用 LLM，未修改原始历史）\n模式：{mode}\n历史来源：{self.state.get('last_history_source')}\n原始消息数：{stats['raw_count']}\n保留消息数：{stats['kept_count']}\n删除消息数：{stats['removed_count']}\n脱敏命中：{stats['mask_hits']}\n保留比例：{stats['keep_ratio'] * 100:.1f}%\n\n删除原因 Top：\n{top}\n\n抽取事实预览：\n{facts}\n\n如需导出清洗包：/续忆 clean_export {mode}")
        except Exception as error:
            yield event.plain_result(f"清洗预览失败：{error}")

    @memory_group.command("clean_export")
    async def clean_export(self, event, mode="balanced"):
        try:
            mode = mode if mode in {"safe", "balanced", "aggressive"} else "balanced"
            zip_path, clean_result = await self.build_clean_zip(event, mode)
            stats = clean_result["stats"]
            self.state.update({"last_export_file": str(zip_path), "last_export_name": zip_path.name, "last_export_status": "success", "last_export_mode": f"clean_{mode}", "last_export_time": now_str(), "last_export_size": zip_path.stat().st_size})
            self.save_state()
            yield event.plain_result(f"已生成清洗记忆包：{zip_path.name}\n大小：{zip_path.stat().st_size} bytes\n原始/保留/删除：{stats['raw_count']} / {stats['kept_count']} / {stats['removed_count']}\n模式：{mode}\n历史来源：{self.state.get('last_history_source')}\n迁移文件：已生成 migration/memory_card.md 与 migration/seed_history.json\n说明：纯代码规则清洗，未调用 LLM，未修改原始历史。")
            async for result in self.send_file(event, zip_path):
                yield result
        except Exception as error:
            self.state.update({"last_export_status": "error", "last_export_error": traceback.format_exc()})
            self.save_state()
            yield event.plain_result(f"清洗导出失败：{error}")

    @memory_group.command("import_preview")
    async def import_preview_cmd(self, event):
        payload = self.import_preview_payload()
        if not payload.get("ok"):
            yield event.plain_result(f"导入预览失败：{payload.get('error')}")
            return
        stats = payload.get("stats") or {}
        warnings = "\n".join([f"- {warning}" for warning in payload.get("warnings") or []]) or "- 无"
        yield event.plain_result(f"续忆导入预览\n文件：{payload.get('file')}\n原始/保留：{stats.get('raw_count', '?')} / {stats.get('kept_count', '?')}\nseed 消息数：{payload.get('seed_count')}\nchunks：{payload.get('chunks_count')}\n推荐模式：hybrid\n警告：\n{warnings}\n\n执行当前会话导入：/续忆 import_to_current hybrid")

    @memory_group.command("import_to_current")
    async def import_to_current_cmd(self, event, mode="hybrid"):
        result = await self.import_package_to_origin(self.origin(event), None, mode)
        if not result.get("ok"):
            yield event.plain_result("导入失败：" + result.get("error", "未知错误"))
            return
        conversation = result.get("conversation") or {}
        yield event.plain_result(f"已导入续忆记忆。\n模式：{result.get('mode')}\n目标会话：{result.get('target_origin')}\n导入 ID：{result.get('import_id')}\n新 conversation_id：{conversation.get('conversation_id', '无')}\n已自动切换：{conversation.get('switch_ok', False)}\n长期记忆 chunks：{(result.get('plugin_memory') or {}).get('chunks', 0)}\n\n回滚：/续忆 import_rollback {result.get('import_id')}")

    @memory_group.command("import_to_origin")
    async def import_to_origin_cmd(self, event, target_origin: str, mode="hybrid"):
        result = await self.import_package_to_origin(target_origin, None, mode)
        if not result.get("ok"):
            yield event.plain_result("导入失败：" + result.get("error", "未知错误"))
            return
        conversation = result.get("conversation") or {}
        yield event.plain_result(f"已导入续忆记忆到指定会话。\n模式：{result.get('mode')}\n目标会话：{result.get('target_origin')}\n导入 ID：{result.get('import_id')}\n新 conversation_id：{conversation.get('conversation_id', '无')}\n长期记忆 chunks：{(result.get('plugin_memory') or {}).get('chunks', 0)}")

    @memory_group.command("imports")
    async def imports_cmd(self, event):
        records = self.load_json(self.import_records_file, [])
        if not records:
            yield event.plain_result("暂无导入记录。")
            return
        lines = ["续忆导入记录："]
        for record in records[:10]:
            lines.append(f"- {record.get('import_id')}  {record.get('mode')}  {record.get('status')}  {record.get('target_origin')}  {record.get('created_at')}")
        yield event.plain_result("\n".join(lines))

    @memory_group.command("import_rollback")
    async def import_rollback_cmd(self, event, import_id: str):
        result = await self.rollback_import(import_id)
        if not result.get("ok"):
            yield event.plain_result("回滚失败：" + result.get("error", "未知错误"))
            return
        yield event.plain_result(f"已回滚导入：{import_id}\n删除 conversation：{result.get('deleted_conversation')}\n禁用插件长期记忆：{result.get('plugin_memory_disabled')}\n警告：{'; '.join(result.get('warnings') or []) or '无'}")

    def allowed_commands(self):
        raw = str(self.cfg("web_admin.allowed_commands", "status,doctor,backend,files,export,send_last,clean_preview,clean_export,import_preview,imports") or "")
        return {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}

    def web_status_payload(self):
        sessions = self.web_sessions_payload()
        return {"ok": True, "plugin": PLUGIN_ID, "version": PLUGIN_VERSION, "time": now_str(), "web_started": self.web_started, "web_url": self.web_url, "export_dir": str(self.export_dir), "import_dir": str(self.import_dir), "files_count": len(list(self.export_dir.glob('*.zip'))), "sessions_count": len(sessions), "imports_count": len(self.web_imports_payload()), "fastapi_available": FASTAPI_AVAILABLE, "comp_file": bool(Comp and hasattr(Comp, 'File')), "core_send": bool(CorePlain and MessageChain), "state": self.load_state(), "security": {"auth_enabled": bool(str(self.cfg('web_admin.password', '') or '').strip()), "remote_trigger": truthy(self.cfg('web_admin.allow_remote_trigger', True), True), "file_delete": truthy(self.cfg('web_admin.allow_file_delete', False), False), "history_preview": truthy(self.cfg('web_admin.allow_history_preview', True), True), "allow_import": truthy(self.cfg('web_admin.allow_import', True), True), "allowed_commands": sorted(self.allowed_commands())}}

    def web_diagnostics_payload(self):
        checks = [
            {"name": "FastAPI / Uvicorn", "ok": FASTAPI_AVAILABLE, "detail": "独立 WebUI 服务依赖"},
            {"name": "WebUI 监听", "ok": self.web_started, "detail": self.web_url or "未启动"},
            {"name": "导出目录", "ok": self.export_dir.exists(), "detail": str(self.export_dir)},
            {"name": "导入目录", "ok": self.import_dir.exists(), "detail": str(self.import_dir)},
            {"name": "导出目录可写", "ok": os.access(self.export_dir, os.W_OK), "detail": "用于生成 zip 与记录状态"},
            {"name": "Comp.File", "ok": bool(Comp and hasattr(Comp, 'File')), "detail": "聊天命令内发送 zip 的已验证链路"},
            {"name": "Core proactive send", "ok": bool(CorePlain and MessageChain), "detail": "WebUI 远程向会话发送文本/文件的链路"},
            {"name": "conversation_manager", "ok": bool(getattr(self.context, 'conversation_manager', None)), "detail": type(getattr(self.context, 'conversation_manager', None)).__name__ if getattr(self.context, 'conversation_manager', None) else "未检测到"},
            {"name": "迁移导入", "ok": truthy(self.cfg('web_admin.allow_import', True), True), "detail": "clean zip -> seed conversation / plugin memory"},
            {"name": "远程触发白名单", "ok": bool(self.allowed_commands()), "detail": ", ".join(sorted(self.allowed_commands()))},
        ]
        return {"ok": True, "checks": checks, "state": self.load_state()}

    def web_config_view(self):
        keys = ["export.max_messages", "cleaning.mode", "cleaning.keep_recent_messages", "cleaning.mask_secrets", "file_delivery.send_mode", "web_admin.enabled", "web_admin.host", "web_admin.port", "web_admin.password", "web_admin.allow_remote_trigger", "web_admin.allowed_commands", "web_admin.allow_file_delete", "web_admin.allow_history_preview", "web_admin.allow_import", "web_admin.token_expire_hours", "import.default_mode"]
        config = {}
        for key in keys:
            value = self.cfg(key, None)
            if "password" in key and value:
                value = "******"
            config[key] = value
        return {"ok": True, "config": config}

    def web_files_payload(self):
        records = self.load_json(self.records_file, [])
        by_name = {record.get("name"): record for record in records if isinstance(record, dict) and record.get("name")}
        output = []
        for path in sorted(self.export_dir.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True):
            record = dict(by_name.get(path.name, {}))
            record.update({"name": path.name, "path": str(path), "size": path.stat().st_size, "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"), "download_url": f"/api/files/{path.name}/download"})
            record.setdefault("kind", "clean" if "clean" in path.name else "raw" if "raw" in path.name else "unknown")
            record.setdefault("migration_available", path.name.startswith("memory_bridge_clean_"))
            output.append(record)
        return output

    def inspect_export_zip(self, path: Path):
        info = {"ok": True, "name": path.name, "size": path.stat().st_size, "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"), "entries": [], "manifest": None, "stats": None, "samples": [], "migration": None, "warnings": []}
        try:
            with zipfile.ZipFile(path, "r") as archive:
                names = archive.namelist()
                info["entries"] = [{"name": name, "size": archive.getinfo(name).file_size} for name in names[:200]]
                if "manifest.json" in names:
                    info["manifest"] = self.read_json_from_zip(archive, "manifest.json", {})
                if "migration/import_plan.json" in names or "migration/seed_history.json" in names:
                    info["migration"] = {"available": True, "import_plan": self.read_json_from_zip(archive, "migration/import_plan.json", {}) or {}, "seed_count": len(self.read_json_from_zip(archive, "migration/seed_history.json", []) or [])}
                    if "migration/memory_card.md" in names:
                        info["migration"]["memory_card_preview"] = preview(archive.read("migration/memory_card.md").decode("utf-8", "ignore"), 800)
                for candidate in ["cleaning/clean_report.json", "raw_messages.json", "raw/raw_messages.json"]:
                    if candidate in names:
                        data = self.read_json_from_zip(archive, candidate, None)
                        if isinstance(data, dict) and data.get("stats"):
                            info["stats"] = data.get("stats")
                        elif isinstance(data, list):
                            info.setdefault("stats", {})
                            info["stats"]["message_count"] = len(data)
                            for item in data[:6]:
                                if isinstance(item, dict):
                                    info["samples"].append({"role": item.get("role"), "time": item.get("time"), "content": preview(item.get("content") or item.get("message") or item.get("text") or "", 220)})
                if not info.get("samples"):
                    for candidate in ["raw_messages.md", "raw/raw_messages.md", "cleaning/kept_messages.md"]:
                        if candidate in names:
                            text = archive.read(candidate).decode("utf-8", "ignore")[:1200]
                            info["samples"].append({"role": "markdown", "time": "", "content": preview(text, 500)})
                            break
        except Exception as error:
            info["ok"] = False
            info["error"] = str(error)
        return info

    def web_sessions_payload(self):
        self.sessions = self.load_json(self.sessions_file, {})
        sessions = sorted(self.sessions.values(), key=lambda item: item.get("last_seen", ""), reverse=True) if isinstance(self.sessions, dict) else []
        return [session for session in sessions if not is_noisy_adapter_origin(session.get("origin"))]

    def web_actions_payload(self):
        data = self.load_json(self.actions_file, [])
        return data if isinstance(data, list) else []

    def web_imports_payload(self):
        data = self.load_json(self.import_records_file, [])
        return data if isinstance(data, list) else []

    def web_msg_preview(self, message: MemoryMessage):
        return {"index": message.index, "role": message.role, "time": message.time, "name": message.name, "content_preview": preview(message.content, 260), "length": len(message.content or "")}

    async def web_trigger_command(self, payload):
        session = str(payload.get("session") or "").strip()
        command = str(payload.get("command") or "").strip()
        mode = str(payload.get("mode") or "balanced").strip()
        if not session:
            return {"ok": False, "error": "缺少 session"}
        supported = {"status", "doctor", "backend", "files", "export", "send_last", "clean_preview", "clean_export", "import_preview", "imports"}
        if command not in supported:
            return {"ok": False, "error": f"不支持的命令：{command}"}
        if command not in self.allowed_commands():
            return {"ok": False, "error": f"命令不在 WebUI 白名单中：{command}"}
        fake_event = FakeEventForWeb(session, f"[WebUI trigger] {command}")
        title = f"续忆桥 WebUI 远程触发：/续忆 {command}"
        result = {"ok": True, "session": session, "command": command, "time": now_str(), "messages": [], "files": []}
        try:
            if command in {"status", "doctor", "backend", "files", "import_preview", "imports"}:
                if command == "files":
                    text = self.files_text()
                elif command == "import_preview":
                    text = dumps(self.import_preview_payload())[:3000]
                elif command == "imports":
                    text = dumps({"imports": self.web_imports_payload()[:10]})[:3000]
                else:
                    text = f"续忆桥 v{PLUGIN_VERSION}\n导出目录：{self.export_dir}\n导入目录：{self.import_dir}\n最近导出：{self.state.get('last_export_file', '无')}\n最近发送：{self.state.get('last_send_status', '无')}\nlast_history_source：{self.state.get('last_history_source', '未知')}"
                final_text = f"{title}\n\n{text}"
                await self.send_text_to_session(session, final_text)
                result["messages"].append(final_text)
            elif command == "export":
                zip_path, manifest = await self.build_raw_zip(fake_event)
                self.state.update({"last_export_file": str(zip_path), "last_export_name": zip_path.name, "last_export_status": "success", "last_export_time": now_str(), "last_export_size": zip_path.stat().st_size, "last_export_mode": "raw_web"})
                self.save_state()
                text = f"{title}\n\n已生成记忆压缩包：{zip_path.name}\n大小：{zip_path.stat().st_size} bytes\n消息数：{manifest.get('message_count')}\n历史来源：{manifest.get('history_source')}"
                await self.send_text_to_session(session, text)
                await self.send_file_to_session(session, zip_path)
                result["messages"].append(text)
                result["files"].append(zip_path.name)
            elif command == "send_last":
                files = sorted(self.export_dir.glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
                if not files:
                    return {"ok": False, "error": "暂无可发送 zip"}
                path = files[0]
                text = f"{title}\n\n正在发送最近文件：{path.name}\n大小：{path.stat().st_size} bytes"
                await self.send_text_to_session(session, text)
                await self.send_file_to_session(session, path)
                result["messages"].append(text)
                result["files"].append(path.name)
            elif command == "clean_preview":
                mode = mode if mode in {"safe", "balanced", "aggressive"} else "balanced"
                messages = await self.history(fake_event)
                clean_result = RuleCleaner(mode, int(self.cfg("cleaning.keep_recent_messages", 20) or 20), truthy(self.cfg("cleaning.mask_secrets", True), True)).clean(messages)
                stats = clean_result["stats"]
                text = f"{title}\n\n模式：{mode}\n历史来源：{self.state.get('last_history_source')}\n原始消息数：{stats['raw_count']}\n保留消息数：{stats['kept_count']}\n删除消息数：{stats['removed_count']}\n脱敏命中：{stats['mask_hits']}\n保留比例：{stats['keep_ratio'] * 100:.1f}%"
                await self.send_text_to_session(session, text)
                result["messages"].append(text)
            elif command == "clean_export":
                mode = mode if mode in {"safe", "balanced", "aggressive"} else "balanced"
                zip_path, clean_result = await self.build_clean_zip(fake_event, mode)
                stats = clean_result["stats"]
                self.state.update({"last_export_file": str(zip_path), "last_export_name": zip_path.name, "last_export_status": "success", "last_export_mode": f"clean_{mode}_web", "last_export_time": now_str(), "last_export_size": zip_path.stat().st_size})
                self.save_state()
                text = f"{title}\n\n已生成清洗记忆包：{zip_path.name}\n大小：{zip_path.stat().st_size} bytes\n原始/保留/删除：{stats['raw_count']} / {stats['kept_count']} / {stats['removed_count']}\n模式：{mode}\n历史来源：{self.state.get('last_history_source')}\n迁移文件：已生成 migration/memory_card.md 与 migration/seed_history.json"
                await self.send_text_to_session(session, text)
                await self.send_file_to_session(session, zip_path)
                result["messages"].append(text)
                result["files"].append(zip_path.name)
            self.append_action_log({"kind": "web_trigger", "ok": True, "session": session, "command": command, "mode": mode, "files": result.get("files", [])})
            return result
        except Exception as error:
            trace = traceback.format_exc()
            self.append_action_log({"kind": "web_trigger", "ok": False, "session": session, "command": command, "mode": mode, "error": str(error), "traceback": trace[-2000:]})
            return {"ok": False, "session": session, "command": command, "error": str(error), "traceback": trace[-2000:]}

    def parse_session_id(self, session_id):
        parts = str(session_id).split(":", 2)
        return tuple(parts) if len(parts) == 3 else None

    async def send_text_to_session(self, session_id, text):
        if CorePlain and MessageChain:
            return await self.send_components_to_session(session_id, [CorePlain(text=text)])
        if hasattr(self.context, "send_message"):
            return await self.context.send_message(session_id, text)
        raise RuntimeError("当前 AstrBot 运行时没有可用的主动发送文本链路")

    def build_core_file_component(self, path):
        if not CoreFile:
            return None
        for kwargs in [{"file": str(path), "name": path.name}, {"file": str(path)}, {"path": str(path), "name": path.name}, {"path": str(path)}, {"file_path": str(path), "name": path.name}, {"file_path": str(path)}]:
            try:
                return CoreFile(**kwargs)
            except Exception:
                continue
        return None

    async def send_file_to_session(self, session_id, path):
        component = self.build_core_file_component(path)
        if component is None:
            await self.send_text_to_session(session_id, f"文件已生成，但当前主动发送文件组件不可用：{path}")
            return
        await self.send_components_to_session(session_id, [component])
        self.state.update({"last_send_status": "success_pending_adapter", "last_send_action": "WebUI.CoreFile", "last_send_file": str(path), "last_send_time": now_str(), "last_send_origin": session_id, "last_send_error": ""})
        self.save_state()

    async def send_components_to_session(self, session_id, components):
        if not MessageChain:
            raise RuntimeError("MessageChain 不可用")
        chain = MessageChain(components)
        parsed = self.parse_session_id(session_id)
        if parsed and getattr(self.context, "platform_manager", None) and MessageSession and MessageType:
            platform_id, message_type_text, target_id = parsed
            try:
                platforms = self.context.platform_manager.get_insts()
            except Exception:
                platforms = getattr(self.context.platform_manager, "platform_insts", []) or []
            target_platform = None
            for platform_inst in platforms:
                try:
                    meta = platform_inst.meta()
                    if getattr(meta, "id", None) == platform_id or getattr(meta, "name", None) == platform_id:
                        target_platform = platform_inst
                        break
                except Exception:
                    pass
            if target_platform is not None:
                try:
                    if PlatformStatus is not None and getattr(target_platform, "status", None) != PlatformStatus.RUNNING:
                        raise RuntimeError(f"平台 {platform_id} 未运行")
                    message_type = MessageType.GROUP_MESSAGE if "group" in message_type_text.lower() else MessageType.FRIEND_MESSAGE
                    await target_platform.send_by_session(MessageSession(platform_name=platform_id, message_type=message_type, session_id=target_id), chain)
                    return
                except Exception as error:
                    if logger:
                        logger.warning(f"MemoryBridge precise platform send failed, fallback to context.send_message: {error}")
        if hasattr(self.context, "send_message"):
            await self.context.send_message(session_id, chain)
            return
        raise RuntimeError("无法主动发送：既没有可用平台实例，也没有 context.send_message")


# ---------------------------------------------------------------------------
# v0.5.1 runtime patch
# 说明：本段为 v0.5.1 小版本补丁，尽量不改动 v0.5.0 已验证的导出、发送、迁移主链路。
# 修复点：
# 1. WebUI/后端会话过滤新增纯数字与 数字_数字 空会话过滤。
# 2. 会话记录尽量保存展示名称、头像地址，供 WebUI 大卡片展示。
# 3. 导出文件列表尽量补充来源用户显示名。
# 4. /续忆 imports 输出更详细的导入记录，便于复制 import_id 与回滚。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.5.1"


def is_noisy_adapter_origin(origin: str) -> bool:
    text = str(origin or "").strip()
    if not text:
        return True
    if re.match(r"^(aiocqhttp|napcat):(FriendMessage|GroupMessage):", text, re.I):
        return True
    # AstrBot/适配器有时会额外留下纯数字或 群号_用户号 形式的空会话，通常读不到有效历史。
    if re.match(r"^\d+$", text):
        return True
    if re.match(r"^\d+_\d+$", text):
        return True
    return False


def _mb_v051_safe_get(obj, *names, default=None):
    current = obj
    for name in names:
        if current is None:
            return default
        try:
            if isinstance(current, dict):
                current = current.get(name, default)
            else:
                current = getattr(current, name, default)
            if callable(current):
                current = current()
        except Exception:
            return default
    return current if current is not None else default


def _mb_v051_origin_tail(origin: str) -> str:
    text = str(origin or "")
    parts = text.split(":")
    return parts[-1] if parts else text


def _mb_v051_display_name_from_origin(origin: str) -> str:
    text = str(origin or "")
    tail = _mb_v051_origin_tail(text)
    if "GroupMessage" in text:
        return f"群聊 {tail}"
    if "FriendMessage" in text:
        return f"私聊 {tail}"
    return text or "未知会话"


def _mb_v051_avatar_from_origin(origin: str, category: str = "") -> str:
    tail = _mb_v051_origin_tail(origin)
    if re.match(r"^\d+$", tail or ""):
        if category == "group" or "GroupMessage" in str(origin):
            return f"https://p.qlogo.cn/gh/{tail}/{tail}/100"
        return f"https://q1.qlogo.cn/g?b=qq&nk={tail}&s=100"
    return ""


def _mb_v051_session_label(self, origin: str) -> str:
    try:
        sessions = self.load_json(self.sessions_file, {})
        item = sessions.get(origin) if isinstance(sessions, dict) else None
        if item:
            return item.get("display_name") or item.get("sender_name") or item.get("group_name") or _mb_v051_display_name_from_origin(origin)
    except Exception:
        pass
    return _mb_v051_display_name_from_origin(origin)


def _mb_v051_record_session(self, event: AstrMessageEvent):
    try:
        msg_obj = getattr(event, "message_obj", None)
        raw = _mb_v051_safe_get(msg_obj, "raw_message", default=None) or _mb_v051_safe_get(msg_obj, "raw_event", default=None) or _mb_v051_safe_get(event, "raw_event", default=None)
        sender = raw.get("sender", {}) if isinstance(raw, dict) else {}
        user_id = sender.get("user_id") or sender.get("id") or _mb_v051_safe_get(msg_obj, "sender", "user_id", default=None)
        nickname = sender.get("card") or sender.get("nickname") or sender.get("name") or _mb_v051_safe_get(msg_obj, "sender", "nickname", default=None)
        group_id = raw.get("group_id") if isinstance(raw, dict) else None
        group_name = raw.get("group_name") if isinstance(raw, dict) else None
        message = getattr(event, "message_str", None)
        message = message() if callable(message) else message
        for origin in self.origin_candidates(event):
            if not origin or origin == "unknown":
                continue
            category = "group" if "group" in origin.lower() else "friend" if "friend" in origin.lower() or "private" in origin.lower() else "unknown"
            tail = _mb_v051_origin_tail(origin)
            if category == "group" and not group_id and re.match(r"^\d+$", tail or ""):
                group_id = tail
            display_name = ""
            if category == "group":
                display_name = group_name or (f"群聊 {group_id}" if group_id else "群聊")
                if nickname:
                    display_name += f" · {nickname}"
            else:
                display_name = nickname or _mb_v051_display_name_from_origin(origin)
            avatar_url = ""
            if category == "group" and group_id:
                avatar_url = f"https://p.qlogo.cn/gh/{group_id}/{group_id}/100"
            elif user_id:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
            if not avatar_url:
                avatar_url = _mb_v051_avatar_from_origin(origin, category)
            self.sessions[origin] = {
                "origin": origin,
                "category": category,
                "last_seen": now_str(),
                "last_message_preview": preview(str(message or ""), 120),
                "display_name": display_name,
                "sender_name": str(nickname or ""),
                "sender_id": str(user_id or ""),
                "group_name": str(group_name or ""),
                "group_id": str(group_id or ""),
                "avatar_url": avatar_url,
            }
        self.save_json(self.sessions_file, self.sessions)
    except Exception:
        pass


MemoryBridgePlugin.record_session = _mb_v051_record_session
MemoryBridgePlugin.session_label = _mb_v051_session_label


def _mb_v051_web_sessions_payload(self):
    self.sessions = self.load_json(self.sessions_file, {})
    sessions = sorted(self.sessions.values(), key=lambda item: item.get("last_seen", ""), reverse=True) if isinstance(self.sessions, dict) else []
    output = []
    for session in sessions:
        origin = session.get("origin")
        if is_noisy_adapter_origin(origin):
            continue
        item = dict(session)
        item.setdefault("display_name", _mb_v051_display_name_from_origin(origin))
        item.setdefault("avatar_url", _mb_v051_avatar_from_origin(origin, item.get("category", "")))
        output.append(item)
    return output


MemoryBridgePlugin.web_sessions_payload = _mb_v051_web_sessions_payload


def _mb_v051_web_files_payload(self):
    records = self.load_json(self.records_file, [])
    by_name = {record.get("name"): record for record in records if isinstance(record, dict) and record.get("name")}
    output = []
    for path in sorted(self.export_dir.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True):
        record = dict(by_name.get(path.name, {}))
        # 对旧记录做轻量补全：从 zip manifest 中读取 origin/stats/migration_available。
        if (not record.get("origin") or not record.get("stats")) and path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path, "r") as archive:
                    if "manifest.json" in archive.namelist():
                        manifest = self.read_json_from_zip(archive, "manifest.json", {}) or {}
                        record.setdefault("origin", manifest.get("origin"))
                        record.setdefault("history_source", manifest.get("history_source"))
                        if manifest.get("stats"):
                            record.setdefault("stats", manifest.get("stats"))
                        if manifest.get("migration_available") is not None:
                            record.setdefault("migration_available", manifest.get("migration_available"))
            except Exception:
                pass
        origin = record.get("origin") or ""
        source_label = record.get("source_label") or record.get("origin_label") or (_mb_v051_session_label(self, origin) if origin else "未知来源")
        record.update({
            "name": path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "download_url": f"/api/files/{path.name}/download",
            "source_label": source_label,
            "origin_label": source_label,
        })
        record.setdefault("kind", "clean" if "clean" in path.name else "raw" if "raw" in path.name else "unknown")
        record.setdefault("migration_available", path.name.startswith("memory_bridge_clean_"))
        output.append(record)
    return output


MemoryBridgePlugin.web_files_payload = _mb_v051_web_files_payload


_old_mb_v051_record_export = MemoryBridgePlugin.record_export

def _mb_v051_record_export(self, path, kind, origin, extra=None):
    extra = dict(extra or {})
    if origin:
        extra.setdefault("source_label", _mb_v051_session_label(self, origin))
        extra.setdefault("origin_label", extra.get("source_label"))
    return _old_mb_v051_record_export(self, path, kind, origin, extra)


MemoryBridgePlugin.record_export = _mb_v051_record_export


def _mb_v051_import_preview_payload(self, filename=None):
    path, package = self.load_migration_package(filename)
    if not package.get("ok"):
        return package
    memory_card = package.get("memory_card_md") or ""
    stats = package.get("stats") or {}
    manifest = package.get("manifest") or {}
    warnings = list(package.get("warnings") or [])
    if stats.get("raw_count") == 1 or manifest.get("message_count") == 1:
        warnings.append("该记忆包消息数为 1，可能不是有效迁移包。")
    if manifest.get("history_source") == "current_message_only":
        warnings.append("该记忆包来源为 current_message_only，不建议导入。")
    source_origin = manifest.get("origin") or ""
    return {
        "ok": True,
        "file": package.get("file"),
        "size": package.get("size"),
        "manifest": manifest,
        "stats": stats,
        "source_origin": source_origin,
        "source_label": _mb_v051_session_label(self, source_origin) if source_origin else "未知来源",
        "seed_count": len(package.get("seed_history") or []),
        "chunks_count": len(package.get("chunks") or []),
        "memory_card_preview": preview(memory_card, 1200),
        "import_plan": package.get("import_plan") or {},
        "warnings": warnings,
    }


MemoryBridgePlugin.import_preview_payload = _mb_v051_import_preview_payload


def _mb_v051_web_imports_payload(self):
    data = self.load_json(self.import_records_file, [])
    if not isinstance(data, list):
        return []
    output = []
    for record in data:
        item = dict(record)
        target_origin = item.get("target_origin") or ""
        source_origin = item.get("source_origin") or ""
        item.setdefault("target_label", _mb_v051_session_label(self, target_origin) if target_origin else "未知目标")
        item.setdefault("source_label", _mb_v051_session_label(self, source_origin) if source_origin else "未知来源")
        output.append(item)
    return output


MemoryBridgePlugin.web_imports_payload = _mb_v051_web_imports_payload


_old_mb_v051_imports_cmd = getattr(MemoryBridgePlugin, "imports_cmd", None)

async def _mb_v051_imports_cmd(self, event):
    records = self.load_json(self.import_records_file, [])
    if not records:
        yield event.plain_result("暂无导入记录。")
        return
    lines = ["续忆导入记录："]
    for idx, record in enumerate(records[:10], 1):
        conversation = record.get("conversation") or {}
        plugin_memory = record.get("plugin_memory") or {}
        stats = record.get("stats") or {}
        import_id = record.get("import_id") or "-"
        lines += [
            f"\n{idx}. {import_id}",
            f"   状态：{record.get('status', '-')}  模式：{record.get('mode', '-')}",
            f"   来源文件：{record.get('source_file', '-')}",
            f"   来源会话：{record.get('source_origin', '-')}",
            f"   目标会话：{record.get('target_origin', '-')}",
            f"   新 conversation_id：{conversation.get('conversation_id', '无')}",
            f"   旧 conversation_id：{conversation.get('previous_conversation_id', '无')}",
            f"   长期记忆 chunks：{plugin_memory.get('chunks', 0)}",
            f"   原始/保留：{stats.get('raw_count', '?')} / {stats.get('kept_count', '?')}",
            f"   时间：{record.get('created_at', '-')}",
            f"   回滚：/续忆 import_rollback {import_id}",
        ]
    yield event.plain_result("\n".join(lines)[:3800])


if _old_mb_v051_imports_cmd is not None:
    for _name in dir(_old_mb_v051_imports_cmd):
        if _name.startswith("__"):
            continue
        try:
            setattr(_mb_v051_imports_cmd, _name, getattr(_old_mb_v051_imports_cmd, _name))
        except Exception:
            pass
MemoryBridgePlugin.imports_cmd = _mb_v051_imports_cmd


# ---------------------------------------------------------------------------
# v0.5.5 integrated runtime patch
# 说明：把 v0.5.4 后端补丁正式融合进整包，补齐 WebUI 用户白名单 API，
#      并修复会话元信息被空字段覆盖、记忆包 session_meta、连续迁移链路展示等。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.5.5"


def _mb_v055_safe_get(obj, *names, default=None):
    current = obj
    for name in names:
        if current is None:
            return default
        try:
            if isinstance(current, dict):
                current = current.get(name, default)
            else:
                current = getattr(current, name, default)
            if callable(current):
                current = current()
        except Exception:
            return default
    return current if current is not None else default


def _mb_v055_origin_tail(origin: str) -> str:
    parts = str(origin or "").split(":")
    return parts[-1] if parts else str(origin or "")


def _mb_v055_parse_origin(origin: str) -> dict[str, Any]:
    text = str(origin or "")
    tail_text = _mb_v055_origin_tail(text)
    is_group = "GroupMessage" in text or text.startswith("group:")
    is_friend = "FriendMessage" in text or text.startswith("friend:")
    user_id = ""
    group_id = ""
    match = re.match(r"^(\d+)_(\d+)$", tail_text)
    if match:
        user_id, group_id = match.group(1), match.group(2)
        is_group = True
    elif re.match(r"^\d+$", tail_text):
        if is_group:
            group_id = tail_text
        else:
            user_id = tail_text
            is_friend = True
    return {"origin": text, "tail": tail_text, "is_group": is_group, "is_friend": is_friend, "user_id": user_id, "group_id": group_id}


def _mb_v055_merge_non_empty(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = dict(old or {})
    for key, value in (new or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _mb_v055_session_meta_from_event(self, event: AstrMessageEvent, origin: str) -> dict[str, Any]:
    msg_obj = getattr(event, "message_obj", None)
    raw = _mb_v055_safe_get(msg_obj, "raw_message", default=None) or _mb_v055_safe_get(msg_obj, "raw_event", default=None) or _mb_v055_safe_get(event, "raw_event", default=None)
    sender = raw.get("sender", {}) if isinstance(raw, dict) else {}
    parsed = _mb_v055_parse_origin(origin)
    user_id = str(sender.get("user_id") or sender.get("id") or _mb_v055_safe_get(msg_obj, "sender", "user_id", default="") or parsed["user_id"] or "")
    sender_name = str(sender.get("card") or sender.get("nickname") or sender.get("name") or _mb_v055_safe_get(msg_obj, "sender", "card", default="") or _mb_v055_safe_get(msg_obj, "sender", "nickname", default="") or "")
    group_id = str(raw.get("group_id") if isinstance(raw, dict) and raw.get("group_id") else parsed["group_id"] or "")
    group_name = str(raw.get("group_name") if isinstance(raw, dict) and raw.get("group_name") else "")
    category = "group" if parsed["is_group"] else "friend" if parsed["is_friend"] else "unknown"
    if category == "group":
        display_name = group_name or (f"群聊 {group_id}" if group_id else "群聊")
        if sender_name:
            display_name += f" · {sender_name}"
    else:
        display_name = sender_name or user_id or parsed["tail"] or origin
    avatar_url = ""
    group_avatar_url = ""
    member_avatar_url = ""
    if category == "group" and group_id:
        group_avatar_url = f"https://p.qlogo.cn/gh/{group_id}/{group_id}/100"
        avatar_url = group_avatar_url
    if user_id:
        member_avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
        if category != "group":
            avatar_url = member_avatar_url
    return {
        "origin": origin,
        "category": category,
        "display_name": display_name,
        "sender_name": sender_name,
        "sender_id": user_id,
        "group_name": group_name,
        "group_id": group_id,
        "avatar_url": avatar_url,
        "group_avatar_url": group_avatar_url,
        "member_avatar_url": member_avatar_url,
        "last_seen": now_str(),
    }


_old_mb_v055_record_session = getattr(MemoryBridgePlugin, "record_session", None)

def _mb_v055_record_session(self, event: AstrMessageEvent):
    try:
        if not isinstance(getattr(self, "sessions", None), dict):
            self.sessions = self.load_json(self.sessions_file, {}) or {}
        for origin in self.origin_candidates(event):
            if not origin or origin == "unknown":
                continue
            old = self.sessions.get(origin, {}) if isinstance(self.sessions, dict) else {}
            new = _mb_v055_session_meta_from_event(self, event, origin)
            merged = _mb_v055_merge_non_empty(old, new)
            message = getattr(event, "message_str", None)
            message = message() if callable(message) else message
            if message:
                merged["last_message_preview"] = preview(str(message), 120)
            else:
                merged.setdefault("last_message_preview", old.get("last_message_preview", ""))
            merged["last_seen"] = now_str()
            merged.setdefault("origin", origin)
            self.sessions[origin] = merged
        self.save_json(self.sessions_file, self.sessions)
    except Exception:
        if _old_mb_v055_record_session:
            try:
                _old_mb_v055_record_session(self, event)
            except Exception:
                pass

MemoryBridgePlugin.record_session = _mb_v055_record_session


def _mb_v055_allowed_users_file(self) -> Path:
    return self.export_dir / "allowed_users.json"


def _mb_v055_load_allowed_users_state(self) -> dict[str, Any]:
    data = self.load_json(_mb_v055_allowed_users_file(self), {})
    if not isinstance(data, dict):
        data = {}
    cfg_raw = str(self.cfg("web_admin.allowed_users", "") or "").strip()
    cfg_users = [item.strip() for item in cfg_raw.replace("；", ",").replace(";", ",").split(",") if item.strip()]
    users = []
    for item in list(data.get("users") or []) + cfg_users:
        if item and item not in users:
            users.append(item)
    return {"enabled": bool(data.get("enabled", False)), "users": users, "updated_at": data.get("updated_at", "")}


def _mb_v055_save_allowed_users_state(self, enabled: bool, users: list[str]) -> dict[str, Any]:
    normalized = []
    for item in users or []:
        item = str(item or "").strip()
        if item and item not in normalized:
            normalized.append(item)
    data = {"enabled": bool(enabled), "users": normalized, "updated_at": now_str()}
    self.save_json(_mb_v055_allowed_users_file(self), data)
    return data


def _mb_v055_is_origin_allowed(self, origin: str) -> bool:
    state = _mb_v055_load_allowed_users_state(self)
    if not state.get("enabled"):
        return True
    allowed = set(state.get("users") or [])
    if not allowed:
        return True
    parsed = _mb_v055_parse_origin(origin)
    candidates = {origin, parsed.get("tail", ""), parsed.get("user_id", ""), parsed.get("group_id", "")}
    candidates = {item for item in candidates if item}
    return bool(candidates & allowed)


def _mb_v055_is_user_allowed(self, event: Any) -> bool:
    try:
        origin = self.origin(event)
    except Exception:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
    return _mb_v055_is_origin_allowed(self, origin)

MemoryBridgePlugin.allowed_users_state = _mb_v055_load_allowed_users_state
MemoryBridgePlugin.save_allowed_users_state = _mb_v055_save_allowed_users_state
MemoryBridgePlugin.is_origin_allowed = _mb_v055_is_origin_allowed
MemoryBridgePlugin.is_user_allowed = _mb_v055_is_user_allowed


def _mb_v055_guard_asyncgen(method_name: str):
    original = getattr(MemoryBridgePlugin, method_name, None)
    if original is None:
        return
    async def guarded(self, event, *args, **kwargs):
        if not _mb_v055_is_user_allowed(self, event):
            yield event.plain_result("当前会话不在续忆桥用户白名单内，已拦截。\n如需放行，请在 WebUI 用户白名单中添加该 origin / QQ / 群号。")
            return
        async for item in original(self, event, *args, **kwargs):
            yield item
    for attr in dir(original):
        if attr.startswith("__"):
            continue
        try:
            setattr(guarded, attr, getattr(original, attr))
        except Exception:
            pass
    setattr(MemoryBridgePlugin, method_name, guarded)

for _mb_v055_name in [
    "export", "files", "send_last", "inspect_last", "clean_preview", "clean_export",
    "import_preview_cmd", "import_to_current_cmd", "import_to_origin_cmd", "imports_cmd", "import_rollback_cmd",
    "history_debug", "history_preview_cmd",
]:
    _mb_v055_guard_asyncgen(_mb_v055_name)

_old_mb_v055_web_trigger_command = getattr(MemoryBridgePlugin, "web_trigger_command", None)
async def _mb_v055_web_trigger_command(self, payload):
    session = str(payload.get("session") or "").strip()
    if not session:
        return {"ok": False, "error": "缺少 session"}
    if not _mb_v055_is_origin_allowed(self, session):
        return {"ok": False, "error": "该会话不在续忆桥用户白名单中，已拦截。"}
    return await _old_mb_v055_web_trigger_command(self, payload) if _old_mb_v055_web_trigger_command else {"ok": False, "error": "web_trigger_command unavailable"}
MemoryBridgePlugin.web_trigger_command = _mb_v055_web_trigger_command

_old_mb_v055_register_routes = getattr(MemoryBridgeWebAdminServer, "_register_routes", None)
def _mb_v055_register_routes(self):
    if _old_mb_v055_register_routes:
        _old_mb_v055_register_routes(self)

    @self.app.get("/api/allowed-users")
    async def allowed_users_get():
        state = self.plugin.allowed_users_state()
        return {"ok": True, "enabled": bool(state.get("enabled")), "users": state.get("users") or [], "updated_at": state.get("updated_at", "")}

    @self.app.post("/api/allowed-users")
    async def allowed_users_post(payload: dict[str, Any]):
        state = self.plugin.save_allowed_users_state(bool(payload.get("enabled", False)), list(payload.get("users") or []))
        self.plugin.append_action_log({"kind": "allowed_users_update", "ok": True, "enabled": state.get("enabled"), "count": len(state.get("users") or [])})
        return {"ok": True, "enabled": bool(state.get("enabled")), "users": state.get("users") or [], "updated_at": state.get("updated_at", "")}

MemoryBridgeWebAdminServer._register_routes = _mb_v055_register_routes

_old_mb_v055_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v055_web_status_payload(self):
    payload = _old_mb_v055_web_status_payload(self) if _old_mb_v055_web_status_payload else {"ok": True}
    try:
        state = self.allowed_users_state()
        payload.setdefault("security", {})
        payload["security"]["allowed_users_enabled"] = bool(state.get("enabled"))
        payload["security"]["allowed_users_count"] = len(state.get("users") or [])
        payload["version"] = PLUGIN_VERSION
    except Exception:
        pass
    return payload
MemoryBridgePlugin.web_status_payload = _mb_v055_web_status_payload

_old_mb_v055_web_config_view = getattr(MemoryBridgePlugin, "web_config_view", None)
def _mb_v055_web_config_view(self):
    payload = _old_mb_v055_web_config_view(self) if _old_mb_v055_web_config_view else {"ok": True, "config": {}}
    try:
        state = self.allowed_users_state()
        payload.setdefault("config", {})
        payload["config"]["web_admin.allowed_users_enabled"] = bool(state.get("enabled"))
        payload["config"]["web_admin.allowed_users"] = ",".join(state.get("users") or [])
    except Exception:
        pass
    return payload
MemoryBridgePlugin.web_config_view = _mb_v055_web_config_view

_old_mb_v055_build_clean_zip = getattr(MemoryBridgePlugin, "build_clean_zip", None)
async def _mb_v055_build_clean_zip(self, event, mode):
    zip_path, clean_result = await _old_mb_v055_build_clean_zip(self, event, mode)
    try:
        origin = self.origin(event)
        session_meta = self.sessions.get(origin, {}) if isinstance(getattr(self, "sessions", {}), dict) else {}
        with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("migration/session_meta.json", dumps({
                "origin": origin,
                "display_name": session_meta.get("display_name") or session_meta.get("group_name") or session_meta.get("sender_name") or origin,
                "sender_name": session_meta.get("sender_name") or "",
                "sender_id": session_meta.get("sender_id") or "",
                "group_name": session_meta.get("group_name") or "",
                "group_id": session_meta.get("group_id") or "",
                "avatar_url": session_meta.get("avatar_url") or "",
                "group_avatar_url": session_meta.get("group_avatar_url") or "",
                "member_avatar_url": session_meta.get("member_avatar_url") or "",
                "history_source": self.state.get("last_history_source") or "",
                "mode": mode,
                "exported_at": now_str(),
            }))
    except Exception:
        pass
    return zip_path, clean_result
MemoryBridgePlugin.build_clean_zip = _mb_v055_build_clean_zip

_old_mb_v055_load_migration_package = getattr(MemoryBridgePlugin, "load_migration_package", None)
def _mb_v055_load_migration_package(self, filename=None):
    path, package = _old_mb_v055_load_migration_package(self, filename) if _old_mb_v055_load_migration_package else (None, {"ok": False, "error": "load_migration_package unavailable"})
    if package.get("ok") and path and path.exists():
        try:
            with zipfile.ZipFile(path, "r") as archive:
                if "migration/session_meta.json" in archive.namelist():
                    package["session_meta"] = json.loads(archive.read("migration/session_meta.json").decode("utf-8", "ignore"))
        except Exception:
            package.setdefault("session_meta", {})
    return path, package
MemoryBridgePlugin.load_migration_package = _mb_v055_load_migration_package

_old_mb_v055_import_preview_payload = getattr(MemoryBridgePlugin, "import_preview_payload", None)
def _mb_v055_import_preview_payload(self, filename=None):
    payload = _old_mb_v055_import_preview_payload(self, filename) if _old_mb_v055_import_preview_payload else {"ok": False, "error": "import_preview_payload unavailable"}
    if payload.get("ok"):
        try:
            _path, package = self.load_migration_package(filename or payload.get("file"))
            meta = package.get("session_meta") or {}
            payload["session_meta"] = meta
            payload["source_label"] = payload.get("source_label") or meta.get("display_name") or payload.get("manifest", {}).get("origin") or "未知来源"
            payload["source_origin"] = payload.get("source_origin") or meta.get("origin") or payload.get("manifest", {}).get("origin") or ""
        except Exception:
            pass
    return payload
MemoryBridgePlugin.import_preview_payload = _mb_v055_import_preview_payload

_old_mb_v055_web_imports_payload = getattr(MemoryBridgePlugin, "web_imports_payload", None)
def _mb_v055_web_imports_payload(self):
    data = _old_mb_v055_web_imports_payload(self) if _old_mb_v055_web_imports_payload else []
    if not isinstance(data, list):
        return []
    source_to_target = {}
    for record in data:
        src = record.get("source_origin") or ""
        tgt = record.get("target_origin") or ""
        if src and tgt:
            source_to_target[src] = tgt
    def chain_for(origin):
        seen, chain, cur = set(), [], origin
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = source_to_target.get(cur, "")
        return " → ".join(chain)
    for record in data:
        origin = record.get("source_origin") or record.get("target_origin") or ""
        record["chain_preview"] = chain_for(origin) if origin else ""
    return data
MemoryBridgePlugin.web_imports_payload = _mb_v055_web_imports_payload


# ---------------------------------------------------------------------------
# v0.5.6 integrated runtime patch
# 修复点：
# 1. 会话元信息按 QQ 号跨私聊/群聊同步昵称与头像，避免同一 QQ 在不同 origin 下名称不一致。
# 2. WebUI 支持上传外部记忆包 zip，校验结构后复制到 export_dir，并在已生成文件中标记 external_import。
# 3. 导出 clean 包时继续写入 migration/session_meta.json，并尽量使用跨会话合并后的身份信息。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.5.6"


def _mb_v056_user_profile_file(self) -> Path:
    return self.export_dir / "user_profiles.json"


def _mb_v056_load_user_profiles(self) -> dict[str, Any]:
    data = self.load_json(_mb_v056_user_profile_file(self), {})
    return data if isinstance(data, dict) else {}


def _mb_v056_save_user_profiles(self, profiles: dict[str, Any]):
    self.save_json(_mb_v056_user_profile_file(self), profiles if isinstance(profiles, dict) else {})


def _mb_v056_good_name(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if re.match(r"^\d+$", text):
        return False
    if re.match(r"^\d+_\d+$", text):
        return False
    if re.match(r"^(私聊|群聊)\s*[\d_]*$", text):
        return False
    if text in {"未知", "未知成员", "未知会话", "FriendMessage", "GroupMessage"}:
        return False
    return True


def _mb_v056_parse_origin(origin: str) -> dict[str, Any]:
    try:
        return _mb_v055_parse_origin(origin)
    except Exception:
        text = str(origin or "")
        tail_text = text.split(":")[-1] if ":" in text else text
        is_group = "GroupMessage" in text
        is_friend = "FriendMessage" in text
        user_id = ""
        group_id = ""
        match = re.match(r"^(\d+)_(\d+)$", tail_text)
        if match:
            user_id, group_id, is_group = match.group(1), match.group(2), True
        elif re.match(r"^\d+$", tail_text):
            if is_group:
                group_id = tail_text
            else:
                user_id, is_friend = tail_text, True
        return {"origin": text, "tail": tail_text, "is_group": is_group, "is_friend": is_friend, "user_id": user_id, "group_id": group_id}


def _mb_v056_extract_ids_from_session(session: dict[str, Any]) -> tuple[str, str]:
    parsed = _mb_v056_parse_origin(session.get("origin") or "")
    user_id = str(session.get("sender_id") or session.get("user_id") or parsed.get("user_id") or "")
    group_id = str(session.get("group_id") or parsed.get("group_id") or "")
    return user_id, group_id


def _mb_v056_build_user_profiles_from_sessions(self) -> dict[str, Any]:
    profiles = _mb_v056_load_user_profiles(self)
    sessions = self.load_json(self.sessions_file, {})
    if not isinstance(sessions, dict):
        sessions = {}
    for origin, session in sessions.items():
        if not isinstance(session, dict):
            continue
        user_id, group_id = _mb_v056_extract_ids_from_session(session)
        if not user_id:
            continue
        profile = dict(profiles.get(user_id) or {})
        name_candidates = [session.get("sender_name"), session.get("nickname"), session.get("card")]
        display = str(session.get("display_name") or "")
        if display and " · " in display:
            name_candidates.append(display.split(" · ")[-1])
        elif display:
            name_candidates.append(display)
        for candidate in name_candidates:
            if _mb_v056_good_name(candidate):
                old_name = str(profile.get("nickname") or "")
                if not old_name or len(str(candidate)) >= len(old_name):
                    profile["nickname"] = str(candidate)
                break
        profile.setdefault("user_id", user_id)
        if group_id:
            groups = list(profile.get("groups") or [])
            if group_id not in groups:
                groups.append(group_id)
            profile["groups"] = groups
        if session.get("member_avatar_url"):
            profile["avatar_url"] = session.get("member_avatar_url")
        elif session.get("avatar_url") and not (session.get("category") == "group"):
            profile["avatar_url"] = session.get("avatar_url")
        elif user_id:
            profile.setdefault("avatar_url", f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100")
        profile["updated_at"] = now_str()
        profiles[user_id] = profile
    _mb_v056_save_user_profiles(self, profiles)
    return profiles


def _mb_v056_enrich_session_with_profiles(self, session: dict[str, Any], profiles: dict[str, Any] | None = None) -> dict[str, Any]:
    item = dict(session or {})
    user_id, group_id = _mb_v056_extract_ids_from_session(item)
    if not profiles:
        profiles = _mb_v056_load_user_profiles(self)
    profile = profiles.get(user_id) if user_id else None
    if profile:
        nickname = profile.get("nickname") or ""
        if _mb_v056_good_name(nickname):
            if not _mb_v056_good_name(item.get("sender_name")):
                item["sender_name"] = nickname
            if item.get("category") == "friend" or "FriendMessage" in str(item.get("origin")):
                if not _mb_v056_good_name(item.get("display_name")):
                    item["display_name"] = nickname
            elif item.get("category") == "group" or "GroupMessage" in str(item.get("origin")):
                group_name = item.get("group_name") or (f"群聊 {group_id}" if group_id else "群聊")
                if not _mb_v056_good_name(item.get("display_name")) or str(item.get("display_name", "")).endswith("未知成员"):
                    item["display_name"] = f"{group_name} · {nickname}"
        if profile.get("avatar_url"):
            if item.get("category") == "group" or "GroupMessage" in str(item.get("origin")):
                item.setdefault("member_avatar_url", profile.get("avatar_url"))
            else:
                item.setdefault("avatar_url", profile.get("avatar_url"))
    if user_id:
        item.setdefault("sender_id", user_id)
        if item.get("category") == "group" or "GroupMessage" in str(item.get("origin")):
            item.setdefault("member_avatar_url", f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100")
        else:
            item.setdefault("avatar_url", f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100")
    if group_id:
        item.setdefault("group_id", group_id)
        item.setdefault("group_avatar_url", f"https://p.qlogo.cn/gh/{group_id}/{group_id}/100")
        if item.get("category") == "group" or "GroupMessage" in str(item.get("origin")):
            item.setdefault("avatar_url", item.get("group_avatar_url"))
    return item


_old_mb_v056_record_session = getattr(MemoryBridgePlugin, "record_session", None)
def _mb_v056_record_session(self, event: AstrMessageEvent):
    if _old_mb_v056_record_session:
        try:
            _old_mb_v056_record_session(self, event)
        except Exception:
            pass
    try:
        profiles = _mb_v056_build_user_profiles_from_sessions(self)
        self.sessions = self.load_json(self.sessions_file, {}) or {}
        if isinstance(self.sessions, dict):
            changed = False
            for origin, session in list(self.sessions.items()):
                if isinstance(session, dict):
                    enriched = _mb_v056_enrich_session_with_profiles(self, session, profiles)
                    if enriched != session:
                        self.sessions[origin] = enriched
                        changed = True
            if changed:
                self.save_json(self.sessions_file, self.sessions)
    except Exception:
        pass
MemoryBridgePlugin.record_session = _mb_v056_record_session


_old_mb_v056_web_sessions_payload = getattr(MemoryBridgePlugin, "web_sessions_payload", None)
def _mb_v056_web_sessions_payload(self):
    try:
        profiles = _mb_v056_build_user_profiles_from_sessions(self)
    except Exception:
        profiles = _mb_v056_load_user_profiles(self)
    data = _old_mb_v056_web_sessions_payload(self) if _old_mb_v056_web_sessions_payload else []
    output = []
    for session in data if isinstance(data, list) else []:
        output.append(_mb_v056_enrich_session_with_profiles(self, session, profiles) if isinstance(session, dict) else session)
    return output
MemoryBridgePlugin.web_sessions_payload = _mb_v056_web_sessions_payload


_old_mb_v056_record_export = getattr(MemoryBridgePlugin, "record_export", None)
def _mb_v056_record_export(self, path, kind, origin, extra=None):
    extra = dict(extra or {})
    try:
        profiles = _mb_v056_build_user_profiles_from_sessions(self)
        session = (self.load_json(self.sessions_file, {}) or {}).get(origin, {})
        enriched = _mb_v056_enrich_session_with_profiles(self, session if isinstance(session, dict) else {"origin": origin}, profiles)
        label = enriched.get("display_name") or enriched.get("sender_name") or enriched.get("group_name") or origin
        if label:
            extra.setdefault("source_label", label)
            extra.setdefault("origin_label", label)
        if enriched:
            extra.setdefault("session_meta", enriched)
    except Exception:
        pass
    if _old_mb_v056_record_export:
        return _old_mb_v056_record_export(self, path, kind, origin, extra)
MemoryBridgePlugin.record_export = _mb_v056_record_export


def _mb_v056_validate_memory_zip(self, path: Path) -> dict[str, Any]:
    info = {"ok": False, "file": path.name, "warnings": [], "errors": [], "manifest": {}, "stats": {}, "session_meta": {}, "migration_available": False}
    if not path.exists() or not path.is_file() or path.suffix.lower() != ".zip":
        info["errors"].append("只允许导入 zip 文件。")
        return info
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            required_any = ["manifest.json", "cleaning/clean_report.json", "migration/memory_card.md", "migration/seed_history.json"]
            if not any(name in names for name in required_any):
                info["errors"].append("不是有效的续忆桥记忆包：缺少 manifest / cleaning / migration 文件。")
                return info
            manifest = self.read_json_from_zip(archive, "manifest.json", {}) or {}
            clean_report = self.read_json_from_zip(archive, "cleaning/clean_report.json", {}) or {}
            session_meta = self.read_json_from_zip(archive, "migration/session_meta.json", {}) or {}
            if "migration/memory_card.md" in names or "migration/seed_history.json" in names:
                info["migration_available"] = True
            if not info["migration_available"] and "cleaning/kept_messages.json" not in names:
                info["warnings"].append("该包没有 migration 文件，将只能作为旧版 clean 包兼容导入。")
            stats = manifest.get("stats") or clean_report.get("stats") or {}
            if stats and isinstance(stats, dict):
                raw_count = stats.get("raw_count") or stats.get("message_count") or 0
                if int(raw_count or 0) <= 1:
                    info["warnings"].append("该包消息数过少，可能不是有效迁移包。")
            info.update({"ok": True, "manifest": manifest, "stats": stats, "session_meta": session_meta})
            return info
    except zipfile.BadZipFile:
        info["errors"].append("zip 文件损坏或格式错误。")
    except Exception as error:
        info["errors"].append(str(error))
        info["traceback"] = traceback.format_exc()[-2000:]
    return info


def _mb_v056_external_import_dir(self) -> Path:
    return ensure_dir(self.export_dir / "external_uploads")


def _mb_v056_external_safe_name(name: str) -> str:
    base = Path(str(name or "memory_package.zip")).name
    base = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "_", base)
    if not base.lower().endswith(".zip"):
        base += ".zip"
    return base


async def _mb_v056_import_external_upload(self, upload_file: Any):
    original_name = getattr(upload_file, "filename", "") or "memory_package.zip"
    safe_name = _mb_v056_external_safe_name(original_name)
    tmp_dir = _mb_v056_external_import_dir(self)
    tmp_path = tmp_dir / f"upload_{stamp()}_{safe_name}"
    try:
        content = await upload_file.read()
        if not content:
            return {"ok": False, "error": "上传文件为空。"}
        if len(content) > 80 * 1024 * 1024:
            return {"ok": False, "error": "文件过大，当前限制 80MB。"}
        tmp_path.write_bytes(content)
        check = _mb_v056_validate_memory_zip(self, tmp_path)
        if not check.get("ok"):
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return {"ok": False, "error": "记忆包格式校验失败。", "details": check}
        dest_name = f"external_{stamp()}_{safe_name}"
        dest_path = self.export_dir / dest_name
        shutil.move(str(tmp_path), str(dest_path))
        manifest = check.get("manifest") or {}
        session_meta = check.get("session_meta") or {}
        origin = session_meta.get("origin") or manifest.get("origin") or ""
        source_label = session_meta.get("display_name") or session_meta.get("sender_name") or origin or "外部导入"
        self.record_export(dest_path, manifest.get("kind") or "external_import", origin, {
            "external_import": True,
            "original_filename": original_name,
            "stats": check.get("stats") or {},
            "migration_available": bool(check.get("migration_available")),
            "source_label": source_label,
            "origin_label": source_label,
            "session_meta": session_meta,
            "warnings": check.get("warnings") or [],
        })
        self.append_action_log({"kind": "external_package_import", "ok": True, "file": dest_name, "original_filename": original_name, "origin": origin})
        return {"ok": True, "file": dest_name, "original_filename": original_name, "external_import": True, "validation": check}
    except Exception as error:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "error": str(error), "traceback": traceback.format_exc()[-2000:]}

MemoryBridgePlugin.validate_memory_zip = _mb_v056_validate_memory_zip
MemoryBridgePlugin.import_external_upload = _mb_v056_import_external_upload


_old_mb_v056_load_migration_package = getattr(MemoryBridgePlugin, "load_migration_package", None)
def _mb_v056_load_migration_package(self, filename=None):
    path, package = _old_mb_v056_load_migration_package(self, filename) if _old_mb_v056_load_migration_package else (None, {"ok": False, "error": "load_migration_package unavailable"})
    if package.get("ok") and path and path.exists():
        try:
            check = _mb_v056_validate_memory_zip(self, path)
            package["validation"] = check
            if check.get("session_meta"):
                package["session_meta"] = check.get("session_meta")
        except Exception:
            pass
    return path, package
MemoryBridgePlugin.load_migration_package = _mb_v056_load_migration_package


_old_mb_v056_import_preview_payload = getattr(MemoryBridgePlugin, "import_preview_payload", None)
def _mb_v056_import_preview_payload(self, filename=None):
    payload = _old_mb_v056_import_preview_payload(self, filename) if _old_mb_v056_import_preview_payload else {"ok": False, "error": "import_preview_payload unavailable"}
    if payload.get("ok"):
        try:
            _path, package = self.load_migration_package(filename or payload.get("file"))
            meta = package.get("session_meta") or {}
            if meta:
                payload["session_meta"] = meta
                payload["source_origin"] = payload.get("source_origin") or meta.get("origin") or ""
                payload["source_label"] = meta.get("display_name") or meta.get("sender_name") or payload.get("source_label") or payload.get("source_origin") or "未知来源"
            if package.get("validation"):
                payload["validation"] = package.get("validation")
        except Exception:
            pass
    return payload
MemoryBridgePlugin.import_preview_payload = _mb_v056_import_preview_payload


_old_mb_v056_web_files_payload = getattr(MemoryBridgePlugin, "web_files_payload", None)
def _mb_v056_web_files_payload(self):
    data = _old_mb_v056_web_files_payload(self) if _old_mb_v056_web_files_payload else []
    records = self.load_json(self.records_file, [])
    by_name = {r.get("name"): r for r in records if isinstance(r, dict) and r.get("name")}
    profiles = _mb_v056_build_user_profiles_from_sessions(self)
    output = []
    for item in data if isinstance(data, list) else []:
        file_item = dict(item)
        rec = by_name.get(file_item.get("name"), {})
        if rec.get("external_import"):
            file_item["external_import"] = True
            file_item["original_filename"] = rec.get("original_filename", "")
        meta = rec.get("session_meta") or file_item.get("session_meta") or {}
        origin = file_item.get("origin") or meta.get("origin") or rec.get("origin") or ""
        if origin:
            enriched = _mb_v056_enrich_session_with_profiles(self, dict(meta or {"origin": origin}), profiles)
            label = enriched.get("display_name") or enriched.get("sender_name") or file_item.get("source_label") or origin
            file_item["source_label"] = label
            file_item["origin_label"] = label
            file_item.setdefault("session_meta", enriched)
        output.append(file_item)
    return output
MemoryBridgePlugin.web_files_payload = _mb_v056_web_files_payload


# 给 WebAdmin 注册上传与校验 API。必须在 _setup_app 调用前替换 _register_routes；插件初始化时会使用本 patched 方法。
_old_mb_v056_register_routes = getattr(MemoryBridgeWebAdminServer, "_register_routes", None)
def _mb_v056_register_routes(self):
    if _old_mb_v056_register_routes:
        _old_mb_v056_register_routes(self)

    @self.app.post("/api/external-import")
    async def external_import(request: Request):
        if not truthy(self.plugin.cfg("web_admin.allow_import", True), True):
            return JSONResponse({"ok": False, "error": "WebUI 导入功能已禁用"}, status_code=403)
        try:
            form = await request.form()
            upload = form.get("file")
            if upload is None:
                return JSONResponse({"ok": False, "error": "缺少上传文件字段 file"}, status_code=400)
            result = await self.plugin.import_external_upload(upload)
            return JSONResponse(result, status_code=200 if result.get("ok") else 400)
        except Exception as error:
            return JSONResponse({"ok": False, "error": str(error), "traceback": traceback.format_exc()[-2000:]}, status_code=500)

    @self.app.get("/api/validate-package")
    async def validate_package(filename: str = ""):
        path = self.plugin.resolve_export_file(filename)
        if not path:
            return JSONResponse({"ok": False, "error": "文件不存在或不允许访问"}, status_code=404)
        result = self.plugin.validate_memory_zip(path)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

MemoryBridgeWebAdminServer._register_routes = _mb_v056_register_routes


_old_mb_v056_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v056_web_status_payload(self):
    payload = _old_mb_v056_web_status_payload(self) if _old_mb_v056_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    try:
        payload.setdefault("security", {})
        payload["security"]["external_import"] = truthy(self.cfg("web_admin.allow_import", True), True)
    except Exception:
        pass
    return payload
MemoryBridgePlugin.web_status_payload = _mb_v056_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.0 runtime patch
# 主题：长期记忆实际注入 MVP。
# 说明：基于 v0.5.6 的导出/清洗/迁移/回滚稳定链路，新增 on_llm_request 钩子，
#      将 active 的 plugin_memory/hybrid 导入记忆按目标会话检索后注入到 LLM 请求。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.0"


def _mb_v060_injection_log_file(self) -> Path:
    return self.import_dir / "memory_injections.json"


def _mb_v060_recent_injections(self, limit: int = 30) -> list[dict[str, Any]]:
    data = self.load_json(_mb_v060_injection_log_file(self), [])
    data = data if isinstance(data, list) else []
    return data[:max(1, int(limit or 30))]


def _mb_v060_append_injection_log(self, row: dict[str, Any]):
    data = _mb_v060_recent_injections(self, 200)
    row = dict(row or {})
    row.setdefault("time", now_str())
    data.insert(0, row)
    self.save_json(_mb_v060_injection_log_file(self), data[:200])


def _mb_v060_cfg_int(self, path: str, default: int) -> int:
    try:
        return int(self.cfg(path, default) or default)
    except Exception:
        return default


def _mb_v060_memory_enabled(self) -> bool:
    return truthy(self.cfg("memory_injection.enabled", True), True)


def _mb_v060_tokenize(text: str) -> list[str]:
    text = str(text or "").lower()
    # 中文按 2-8 字滑窗/词块，英文数字按普通 token。保持轻量、无额外依赖。
    raw = re.findall(r"[\u4e00-\u9fff]{2,8}|[a-z0-9_#./\-]{2,}", text, re.I)
    stop = {
        "这个", "那个", "然后", "就是", "还是", "如果", "但是", "因为", "所以", "可以", "一下", "一些", "一个", "我们", "你们", "他们",
        "请问", "帮我", "继续", "问题", "记忆", "续忆", "导入", "导出", "the", "and", "for", "with", "from", "this", "that", "you", "are", "was",
    }
    out = []
    for token in raw:
        token = token.strip().lower()
        if len(token) < 2 or token in stop:
            continue
        out.append(token)
    # 去重但保序
    seen, dedup = set(), []
    for token in out:
        if token not in seen:
            seen.add(token)
            dedup.append(token)
    return dedup[:80]


def _mb_v060_message_text(event: Any, req: Any = None) -> str:
    parts = []
    try:
        msg = getattr(event, "message_str", None)
        msg = msg() if callable(msg) else msg
        if msg:
            parts.append(str(msg))
    except Exception:
        pass
    for attr in ["prompt", "query", "text", "user_prompt"]:
        try:
            value = getattr(req, attr, None) if req is not None else None
            if value:
                parts.append(str(value))
        except Exception:
            pass
    try:
        if req is not None and isinstance(getattr(req, "messages", None), list):
            for item in getattr(req, "messages")[-3:]:
                if isinstance(item, dict):
                    parts.append(extract_text_content(item.get("content") or item.get("text") or ""))
    except Exception:
        pass
    return "\n".join([p for p in parts if p]).strip()


def _mb_v060_is_command_text(text: str) -> bool:
    t = str(text or "").strip()
    return t.startswith("/续忆") or t.startswith("续忆 ") or t.startswith("/memory")


def _mb_v060_active_import_records(self, origin: str | None = None) -> list[dict[str, Any]]:
    records = self.load_json(self.import_records_file, [])
    if not isinstance(records, list):
        return []
    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("status") not in {"active", "enabled", None, ""}:
            continue
        import_id = str(rec.get("import_id") or "")
        if not import_id:
            continue
        mode = str(rec.get("mode") or "")
        if mode not in {"plugin_memory", "hybrid"}:
            continue
        target = str(rec.get("target_origin") or "")
        if origin and target != origin:
            continue
        import_path = self.import_dir / import_id
        if (import_path / "DISABLED").exists():
            continue
        if not (import_path / "memory_card.md").exists() and not (import_path / "chunks.json").exists():
            continue
        out.append(rec)
    return out


def _mb_v060_load_import_memory(self, record: dict[str, Any]) -> dict[str, Any]:
    import_id = str(record.get("import_id") or "")
    import_path = self.import_dir / import_id
    memory_card = ""
    chunks = []
    manifest = {}
    try:
        p = import_path / "memory_card.md"
        if p.exists():
            memory_card = p.read_text("utf-8", errors="ignore")
    except Exception:
        memory_card = ""
    try:
        chunks_data = self.load_json(import_path / "chunks.json", [])
        if isinstance(chunks_data, list):
            chunks = chunks_data
    except Exception:
        chunks = []
    try:
        manifest = self.load_json(import_path / "manifest.json", {})
        manifest = manifest if isinstance(manifest, dict) else {}
    except Exception:
        manifest = {}
    return {"import_id": import_id, "record": record, "dir": str(import_path), "memory_card": memory_card, "chunks": chunks, "manifest": manifest}


def _mb_v060_score_chunk(chunk: dict[str, Any], keywords: list[str]) -> float:
    text = str(chunk.get("text") or chunk.get("content") or "")
    hay = text.lower()
    if not text.strip():
        return -999.0
    score = 0.0
    for kw in keywords:
        if not kw:
            continue
        if kw in hay:
            score += 3.0 if len(kw) >= 4 else 1.5
    category = str(chunk.get("category") or "")
    if category in {"user_preference", "project_status", "open_task"}:
        score += 0.8
    elif category in {"technical_context", "resolved_issue"}:
        score += 0.5
    # 短片段更适合作为注入证据，过长略降权。
    if len(text) > 900:
        score -= 0.4
    return score


def _mb_v060_retrieve_memory(self, origin: str, query: str) -> dict[str, Any]:
    max_imports = _mb_v060_cfg_int(self, "memory_injection.max_imports", 3)
    max_chunks = _mb_v060_cfg_int(self, "memory_injection.max_chunks", 6)
    max_card_chars = _mb_v060_cfg_int(self, "memory_injection.max_card_chars", 1600)
    max_total_chars = _mb_v060_cfg_int(self, "memory_injection.max_total_chars", 3600)
    keywords = _mb_v060_tokenize(query)
    memories = []
    selected_chunks = []
    records = _mb_v060_active_import_records(self, origin)[:max_imports]
    for rec in records:
        mem = _mb_v060_load_import_memory(self, rec)
        memories.append(mem)
        scored = []
        for ch in mem.get("chunks") or []:
            if not isinstance(ch, dict):
                continue
            score = _mb_v060_score_chunk(ch, keywords)
            if score > 0 or not keywords:
                scored.append((score, ch, mem))
        scored.sort(key=lambda x: x[0], reverse=True)
        selected_chunks.extend(scored[:max_chunks])
    selected_chunks.sort(key=lambda x: x[0], reverse=True)
    selected_chunks = selected_chunks[:max_chunks]
    if not memories:
        return {"ok": False, "reason": "no_active_memory", "origin": origin, "keywords": keywords}
    parts = [
        "[续忆桥长期记忆注入]",
        "以下内容来自用户主动导入/迁移的旧会话记忆。请只在与当前问题相关时参考；不要主动完整复述；若与当前用户新指令冲突，以当前指令为准。",
        "",
    ]
    for mem in memories:
        rec = mem.get("record") or {}
        source = rec.get("source_origin") or (mem.get("manifest") or {}).get("origin") or "未知来源"
        parts.append(f"## 导入记忆 {mem.get('import_id')}｜来源：{source}｜目标：{origin}")
        card = norm(mem.get("memory_card") or "")
        if card:
            parts.append("### 记忆摘要")
            parts.append(card[:max_card_chars])
            parts.append("")
    if selected_chunks:
        parts.append("## 与当前问题可能相关的记忆片段")
        for score, ch, mem in selected_chunks:
            text = preview(ch.get("text") or ch.get("content") or "", 520)
            cat = ch.get("category") or "fact"
            cid = ch.get("id") or "chunk"
            parts.append(f"- [{mem.get('import_id')} / {cat} / {cid} / score={score:.1f}] {text}")
    context = norm("\n".join(parts))
    if len(context) > max_total_chars:
        context = context[:max_total_chars] + "\n...[续忆桥：已按注入字符上限截断]"
    return {
        "ok": True,
        "origin": origin,
        "keywords": keywords,
        "imports": [m.get("import_id") for m in memories],
        "chunks": len(selected_chunks),
        "chars": len(context),
        "context": context,
    }


def _mb_v060_apply_injection_to_req(req: Any, context_text: str) -> str:
    method = ""
    if req is None or not context_text:
        return method
    # 优先使用 extra_user_content_parts，避免每轮改 system_prompt 破坏缓存。
    try:
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is not None and hasattr(parts, "append"):
            parts.append(context_text)
            return "extra_user_content_parts"
    except Exception:
        pass
    try:
        old = getattr(req, "system_prompt", "") or ""
        setattr(req, "system_prompt", str(old) + "\n\n" + context_text)
        return "system_prompt"
    except Exception:
        pass
    try:
        old = getattr(req, "prompt", "") or ""
        setattr(req, "prompt", context_text + "\n\n" + str(old))
        return "prompt"
    except Exception:
        pass
    return "failed"


async def _mb_v060_on_llm_request_impl(self, event: AstrMessageEvent, req: Any):
    if not _mb_v060_memory_enabled(self):
        return
    try:
        origin = self.origin(event)
    except Exception:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
    if not origin:
        return
    try:
        if hasattr(self, "is_origin_allowed") and not self.is_origin_allowed(origin):
            return
    except Exception:
        pass
    query = _mb_v060_message_text(event, req)
    if _mb_v060_is_command_text(query):
        return
    payload = _mb_v060_retrieve_memory(self, origin, query)
    if not payload.get("ok"):
        return
    method = _mb_v060_apply_injection_to_req(req, payload.get("context") or "")
    row = {
        "ok": method not in {"", "failed"},
        "origin": origin,
        "method": method,
        "query_preview": preview(query, 160),
        "imports": payload.get("imports") or [],
        "chunks": payload.get("chunks", 0),
        "chars": payload.get("chars", 0),
        "keywords": payload.get("keywords", [])[:20],
    }
    _mb_v060_append_injection_log(self, row)
    try:
        self.state["last_memory_injection"] = row
        self.save_state()
    except Exception:
        pass


try:
    _mb_v060_on_llm_request = filter.on_llm_request()(_mb_v060_on_llm_request_impl)
except Exception:
    _mb_v060_on_llm_request = _mb_v060_on_llm_request_impl
MemoryBridgePlugin.on_llm_request_memory_bridge = _mb_v060_on_llm_request


def _mb_v060_memory_status_payload(self, origin: str | None = None) -> dict[str, Any]:
    origin = origin or ""
    active = _mb_v060_active_import_records(self, origin or None)
    return {
        "ok": True,
        "enabled": _mb_v060_memory_enabled(self),
        "origin": origin,
        "active_imports_count": len(active),
        "active_imports": [
            {
                "import_id": r.get("import_id"),
                "mode": r.get("mode"),
                "source_file": r.get("source_file"),
                "source_origin": r.get("source_origin"),
                "target_origin": r.get("target_origin"),
                "chunks": ((r.get("plugin_memory") or {}).get("chunks", 0)),
                "created_at": r.get("created_at"),
            }
            for r in active[:20]
        ],
        "recent_injections": _mb_v060_recent_injections(self, 20),
        "limits": {
            "max_imports": _mb_v060_cfg_int(self, "memory_injection.max_imports", 3),
            "max_chunks": _mb_v060_cfg_int(self, "memory_injection.max_chunks", 6),
            "max_card_chars": _mb_v060_cfg_int(self, "memory_injection.max_card_chars", 1600),
            "max_total_chars": _mb_v060_cfg_int(self, "memory_injection.max_total_chars", 3600),
        },
    }
MemoryBridgePlugin.memory_status_payload = _mb_v060_memory_status_payload


_old_mb_v060_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v060_web_status_payload(self):
    payload = _old_mb_v060_web_status_payload(self) if _old_mb_v060_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    try:
        payload["memory_injection"] = _mb_v060_memory_status_payload(self)
    except Exception as error:
        payload["memory_injection"] = {"ok": False, "error": str(error)}
    return payload
MemoryBridgePlugin.web_status_payload = _mb_v060_web_status_payload


_old_mb_v060_web_diagnostics_payload = getattr(MemoryBridgePlugin, "web_diagnostics_payload", None)
def _mb_v060_web_diagnostics_payload(self):
    payload = _old_mb_v060_web_diagnostics_payload(self) if _old_mb_v060_web_diagnostics_payload else {"ok": True, "checks": []}
    try:
        active_count = len(_mb_v060_active_import_records(self, None))
        payload.setdefault("checks", [])
        payload["checks"].append({
            "name": "长期记忆注入",
            "ok": _mb_v060_memory_enabled(self),
            "detail": f"on_llm_request MVP；当前 active plugin_memory/hybrid 导入 {active_count} 条。",
        })
    except Exception:
        pass
    return payload
MemoryBridgePlugin.web_diagnostics_payload = _mb_v060_web_diagnostics_payload


_old_mb_v060_register_routes = getattr(MemoryBridgeWebAdminServer, "_register_routes", None)
def _mb_v060_register_routes(self):
    if _old_mb_v060_register_routes:
        _old_mb_v060_register_routes(self)

    @self.app.get("/api/memory-injection/status")
    async def memory_injection_status(origin: str = ""):
        return self.plugin.memory_status_payload(origin or None)

    @self.app.get("/api/memory-injection/test")
    async def memory_injection_test(origin: str = "", query: str = ""):
        if not origin:
            return JSONResponse({"ok": False, "error": "缺少 origin"}, status_code=400)
        payload = _mb_v060_retrieve_memory(self.plugin, origin, query or "")
        if payload.get("context"):
            payload["context_preview"] = preview(payload.get("context") or "", 1800)
            payload.pop("context", None)
        return JSONResponse(payload, status_code=200 if payload.get("ok") else 404)

MemoryBridgeWebAdminServer._register_routes = _mb_v060_register_routes


try:
    _mb_v060_group = MemoryBridgePlugin.memory_group

    @_mb_v060_group.command("memory_status")
    async def _mb_v060_memory_status_cmd(self, event):
        origin = self.origin(event)
        payload = _mb_v060_memory_status_payload(self, origin)
        lines = [
            f"续忆桥长期记忆注入 v{PLUGIN_VERSION}",
            f"启用：{payload.get('enabled')}",
            f"当前会话：{origin}",
            f"当前会话 active 导入：{payload.get('active_imports_count')}",
            "",
        ]
        for item in payload.get("active_imports") or []:
            lines.append(f"- {item.get('import_id')}  {item.get('mode')}  chunks={item.get('chunks')}  来源={item.get('source_origin')}")
        recent = payload.get("recent_injections") or []
        if recent:
            lines += ["", "最近注入："]
            for row in recent[:5]:
                lines.append(f"- {row.get('time')} {row.get('origin')} imports={len(row.get('imports') or [])} chunks={row.get('chunks')} method={row.get('method')}")
        yield event.plain_result("\n".join(lines)[:3800])

    @_mb_v060_group.command("memory_test")
    async def _mb_v060_memory_test_cmd(self, event, query: str = ""):
        origin = self.origin(event)
        if not query:
            try:
                msg = getattr(event, "message_str", "")
                msg = msg() if callable(msg) else msg
                query = str(msg or "").replace("/续忆 memory_test", "", 1).strip()
            except Exception:
                query = ""
        payload = _mb_v060_retrieve_memory(self, origin, query or "")
        if not payload.get("ok"):
            yield event.plain_result("记忆检索无结果：" + payload.get("reason", "unknown"))
            return
        yield event.plain_result(
            f"记忆检索测试\n会话：{origin}\n关键词：{', '.join(payload.get('keywords') or [])}\n导入：{', '.join(payload.get('imports') or [])}\nchunks：{payload.get('chunks')}\n字符：{payload.get('chars')}\n\n" + preview(payload.get("context") or "", 2800)
        )

    MemoryBridgePlugin.memory_status_cmd = _mb_v060_memory_status_cmd
    MemoryBridgePlugin.memory_test_cmd = _mb_v060_memory_test_cmd
except Exception:
    pass



def _mb_v061_origin_parts(origin: str) -> dict[str, str]:
    text = str(origin or "").strip()
    parts = text.split(":", 2)
    if len(parts) == 3:
        return {"origin": text, "adapter": parts[0], "message_type": parts[1], "tail": parts[2]}
    return {"origin": text, "adapter": "", "message_type": "", "tail": text}


def _mb_v061_origin_key(origin: str) -> str:
    p = _mb_v061_origin_parts(origin)
    mt = p.get("message_type") or ""
    tail = p.get("tail") or ""
    if mt and tail:
        return f"{mt}:{tail}".lower()
    return str(origin or "").strip().lower()


def _mb_v061_same_origin(a: str, b: str) -> bool:
    if str(a or "") == str(b or ""):
        return True
    return bool(a and b and _mb_v061_origin_key(a) == _mb_v061_origin_key(b))


def _mb_v061_known_adapters(self) -> list[str]:
    adapters = []
    try:
        pm = getattr(self.context, "platform_manager", None)
        if pm:
            try:
                platforms = pm.get_insts()
            except Exception:
                platforms = getattr(pm, "platform_insts", []) or []
            for inst in platforms or []:
                try:
                    meta = inst.meta()
                    for value in [getattr(meta, "id", None), getattr(meta, "name", None)]:
                        value = str(value or "").strip()
                        if value and value not in adapters:
                            adapters.append(value)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        sessions = self.load_json(self.sessions_file, {})
        if isinstance(sessions, dict):
            for origin in sessions.keys():
                p = _mb_v061_origin_parts(origin)
                adapter = p.get("adapter") or ""
                if adapter and adapter not in adapters:
                    adapters.append(adapter)
    except Exception:
        pass
    try:
        imports = self.load_json(self.import_records_file, [])
        if isinstance(imports, list):
            for rec in imports:
                if not isinstance(rec, dict):
                    continue
                for origin in [rec.get("target_origin"), rec.get("source_origin")]:
                    p = _mb_v061_origin_parts(origin or "")
                    adapter = p.get("adapter") or ""
                    if adapter and adapter not in adapters:
                        adapters.append(adapter)
    except Exception:
        pass
    if "default" not in adapters:
        adapters.append("default")
    return adapters


def _mb_v061_is_noisy_adapter_origin(origin: str) -> bool:
    text = str(origin or "").strip()
    if not text or text == "unknown":
        return True
    # 注意：不要再把 aiocqhttp / napcat / 其它适配器名当作噪声。
    # 第一段是用户自定义适配器实例名，可能是 default，也可能是任意名称。
    if re.match(r"^\d+$", text):
        return True
    if re.match(r"^\d+_\d+$", text):
        return True
    return False


is_noisy_adapter_origin = _mb_v061_is_noisy_adapter_origin


_old_mb_v061_origin_candidates = getattr(MemoryBridgePlugin, "origin_candidates", None)
def _mb_v061_origin_candidates(self, event: Any) -> list[str]:
    output = []
    # 先保留运行时真实 origin，保证真正适配器名排第一。
    objects = [getattr(event, "message_obj", None), event, getattr(event, "event", None), getattr(event, "raw_event", None)]
    for current_object in objects:
        if current_object is None:
            continue
        for name in ["unified_msg_origin", "get_unified_msg_origin", "session_id", "get_session_id"]:
            try:
                value = getattr(current_object, name, None)
                value = value() if callable(value) else value
                if value and str(value) not in output:
                    output.append(str(value))
            except Exception:
                pass
    if not output and _old_mb_v061_origin_candidates:
        try:
            output = list(_old_mb_v061_origin_candidates(self, event) or [])
        except Exception:
            output = []
    # 再按同 MessageType + tail 生成等价候选，用于兼容旧记录中的 default origin。
    extras = []
    adapters = _mb_v061_known_adapters(self)
    known_origins = []
    try:
        sessions = self.load_json(self.sessions_file, {})
        if isinstance(sessions, dict):
            known_origins += list(sessions.keys())
    except Exception:
        pass
    try:
        imports = self.load_json(self.import_records_file, [])
        if isinstance(imports, list):
            for rec in imports:
                if isinstance(rec, dict):
                    known_origins += [rec.get("target_origin") or "", rec.get("source_origin") or ""]
    except Exception:
        pass
    for origin in list(output):
        p = _mb_v061_origin_parts(origin)
        mt, tail = p.get("message_type"), p.get("tail")
        if mt and tail:
            for adapter in adapters:
                cand = f"{adapter}:{mt}:{tail}"
                if cand not in output and cand not in extras:
                    extras.append(cand)
            key = _mb_v061_origin_key(origin)
            for known in known_origins:
                if known and _mb_v061_origin_key(known) == key and known not in output and known not in extras:
                    extras.append(known)
    output += extras
    return output or ["unknown"]

MemoryBridgePlugin.origin_candidates = _mb_v061_origin_candidates


def _mb_v061_origin(self, event: Any) -> str:
    return self.origin_candidates(event)[0]
MemoryBridgePlugin.origin = _mb_v061_origin


_old_mb_v061_active_import_records = globals().get("_mb_v060_active_import_records")
def _mb_v061_active_import_records(self, origin: str | None = None) -> list[dict[str, Any]]:
    records = self.load_json(self.import_records_file, [])
    if not isinstance(records, list):
        return []
    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("status") not in {"active", "enabled", None, ""}:
            continue
        import_id = str(rec.get("import_id") or "")
        if not import_id:
            continue
        mode = str(rec.get("mode") or "")
        if mode not in {"plugin_memory", "hybrid"}:
            continue
        target = str(rec.get("target_origin") or "")
        if origin and target and not _mb_v061_same_origin(target, origin):
            continue
        import_path = self.import_dir / import_id
        if (import_path / "DISABLED").exists():
            continue
        if not (import_path / "memory_card.md").exists() and not (import_path / "chunks.json").exists():
            continue
        out.append(rec)
    return out

globals()["_mb_v060_active_import_records"] = _mb_v061_active_import_records


def _mb_v061_session_label(self, origin: str) -> str:
    try:
        sessions = self.load_json(self.sessions_file, {})
        if isinstance(sessions, dict):
            item = sessions.get(origin)
            if not item:
                key = _mb_v061_origin_key(origin)
                for known_origin, known_item in sessions.items():
                    if _mb_v061_origin_key(known_origin) == key:
                        item = known_item
                        break
            if isinstance(item, dict):
                return item.get("display_name") or item.get("sender_name") or item.get("group_name") or origin
    except Exception:
        pass
    try:
        return _mb_v051_display_name_from_origin(origin)
    except Exception:
        return str(origin or "未知会话")

MemoryBridgePlugin.session_label = _mb_v061_session_label


def _mb_v061_web_sessions_payload(self):
    self.sessions = self.load_json(self.sessions_file, {})
    sessions = sorted(self.sessions.values(), key=lambda item: item.get("last_seen", ""), reverse=True) if isinstance(self.sessions, dict) else []
    output = []
    seen = set()
    try:
        profiles = _mb_v056_build_user_profiles_from_sessions(self)
    except Exception:
        profiles = {}
    for session in sessions:
        if not isinstance(session, dict):
            continue
        origin = session.get("origin") or ""
        if _mb_v061_is_noisy_adapter_origin(origin):
            continue
        # 精确 origin 保留；不再因 adapter 名不是 default 而隐藏。
        key = origin
        if key in seen:
            continue
        seen.add(key)
        try:
            item = _mb_v056_enrich_session_with_profiles(self, session, profiles)
        except Exception:
            item = dict(session)
        item.setdefault("adapter_id", _mb_v061_origin_parts(origin).get("adapter") or "")
        item.setdefault("origin_key", _mb_v061_origin_key(origin))
        output.append(item)
    return output
MemoryBridgePlugin.web_sessions_payload = _mb_v061_web_sessions_payload


_old_mb_v061_is_origin_allowed = getattr(MemoryBridgePlugin, "is_origin_allowed", None)
def _mb_v061_is_origin_allowed(self, origin: str) -> bool:
    state = self.allowed_users_state() if hasattr(self, "allowed_users_state") else {"enabled": False}
    if not state.get("enabled"):
        return True
    allowed = set(state.get("users") or [])
    if not allowed:
        return True
    try:
        parsed = _mb_v056_parse_origin(origin)
    except Exception:
        parsed = {"tail": _mb_v061_origin_parts(origin).get("tail", ""), "user_id": "", "group_id": ""}
    candidates = {
        str(origin or ""),
        _mb_v061_origin_key(origin),
        str(parsed.get("tail") or ""),
        str(parsed.get("user_id") or ""),
        str(parsed.get("group_id") or ""),
    }
    # 允许白名单里写 default:xxx:yyy，也能匹配 other_adapter:xxx:yyy。
    for item in list(allowed):
        if _mb_v061_origin_key(item) == _mb_v061_origin_key(origin):
            return True
    candidates = {item for item in candidates if item}
    return bool(candidates & allowed)
MemoryBridgePlugin.is_origin_allowed = _mb_v061_is_origin_allowed


_old_mb_v061_memory_status_payload = getattr(MemoryBridgePlugin, "memory_status_payload", None)
def _mb_v061_memory_status_payload(self, origin: str | None = None) -> dict[str, Any]:
    origin = origin or ""
    active = _mb_v061_active_import_records(self, origin or None)
    return {
        "ok": True,
        "enabled": _mb_v060_memory_enabled(self) if "_mb_v060_memory_enabled" in globals() else True,
        "origin": origin,
        "origin_key": _mb_v061_origin_key(origin),
        "adapter_note": "v0.6.1 已按 MessageType + session_tail 兼容不同 adapter_id，不再固定 default。",
        "active_imports_count": len(active),
        "active_imports": [
            {
                "import_id": r.get("import_id"),
                "mode": r.get("mode"),
                "source_file": r.get("source_file"),
                "source_origin": r.get("source_origin"),
                "target_origin": r.get("target_origin"),
                "target_origin_key": _mb_v061_origin_key(r.get("target_origin") or ""),
                "chunks": ((r.get("plugin_memory") or {}).get("chunks", 0)),
                "created_at": r.get("created_at"),
            }
            for r in active[:20]
        ],
        "recent_injections": _mb_v060_recent_injections(self, 20) if "_mb_v060_recent_injections" in globals() else [],
    }
MemoryBridgePlugin.memory_status_payload = _mb_v061_memory_status_payload


_old_mb_v061_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v061_web_status_payload(self):
    payload = _old_mb_v061_web_status_payload(self) if _old_mb_v061_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["adapter_origin_fix"] = {
        "enabled": True,
        "rule": "exact origin first; fallback match by MessageType + session_tail; adapter_id is not assumed to be default",
        "known_adapters": _mb_v061_known_adapters(self),
    }
    return payload
MemoryBridgePlugin.web_status_payload = _mb_v061_web_status_payload


_old_mb_v061_backend = getattr(MemoryBridgePlugin, "backend", None)
# backend 命令原函数不强行覆盖，避免破坏 filter 元数据；状态页已显示修复状态。
PLUGIN_VERSION = "0.6.3"


# ---------------------------------------------------------------------------
# v0.6.4 integrated runtime patch
# 修复：真实会话记录与历史兼容候选拆开，避免同一会话按 napcat/aiocqhttp/webchat/default 显示四份。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.4"


def _mb_v064_collect_real_origins(event: Any) -> list[str]:
    output: list[str] = []
    objects = [getattr(event, "message_obj", None), event, getattr(event, "event", None), getattr(event, "raw_event", None)]
    for current_object in objects:
        if current_object is None:
            continue
        for name in ["unified_msg_origin", "get_unified_msg_origin", "session_id", "get_session_id"]:
            try:
                value = getattr(current_object, name, None)
                value = value() if callable(value) else value
                value = str(value or "").strip()
                if value and value not in output:
                    output.append(value)
            except Exception:
                pass
    return output


def _mb_v064_origin_candidates(self, event: Any) -> list[str]:
    output = _mb_v064_collect_real_origins(event)
    if output:
        return output
    return ["unknown"]


MemoryBridgePlugin.origin_candidates = _mb_v064_origin_candidates


def _mb_v064_origin(self, event: Any) -> str:
    return self.origin_candidates(event)[0]


MemoryBridgePlugin.origin = _mb_v064_origin


def _mb_v064_history_origin_candidates(self, event: Any) -> list[str]:
    output = list(self.origin_candidates(event) or [])
    extras: list[str] = []
    known_origins: list[str] = []
    try:
        sessions = self.load_json(self.sessions_file, {})
        if isinstance(sessions, dict):
            known_origins += list(sessions.keys())
    except Exception:
        pass
    try:
        imports = self.load_json(self.import_records_file, [])
        if isinstance(imports, list):
            for rec in imports:
                if isinstance(rec, dict):
                    known_origins += [rec.get("target_origin") or "", rec.get("source_origin") or ""]
    except Exception:
        pass
    for origin in list(output):
        key = _mb_v061_origin_key(origin)
        for known in known_origins:
            if known and _mb_v061_origin_key(known) == key and known not in output and known not in extras:
                extras.append(known)
    return output + extras if output or extras else ["unknown"]


def _mb_v064_record_session(self, event: AstrMessageEvent):
    try:
        if not isinstance(getattr(self, "sessions", None), dict):
            self.sessions = self.load_json(self.sessions_file, {}) or {}
        for origin in self.origin_candidates(event):
            if not origin or origin == "unknown":
                continue
            old = self.sessions.get(origin, {}) if isinstance(self.sessions, dict) else {}
            try:
                new = _mb_v055_session_meta_from_event(self, event, origin)
            except Exception:
                category = "group" if "group" in origin.lower() else "friend" if "friend" in origin.lower() or "private" in origin.lower() else "unknown"
                new = {"origin": origin, "category": category, "last_seen": now_str()}
            try:
                merged = _mb_v055_merge_non_empty(old, new)
            except Exception:
                merged = dict(old or {})
                merged.update({k: v for k, v in (new or {}).items() if v not in (None, "", [], {})})
            message = getattr(event, "message_str", None)
            message = message() if callable(message) else message
            if message:
                merged["last_message_preview"] = preview(str(message), 120)
            else:
                merged.setdefault("last_message_preview", old.get("last_message_preview", ""))
            merged["last_seen"] = now_str()
            merged["origin"] = origin
            try:
                item = _mb_v056_enrich_session_with_profiles(self, merged, _mb_v056_load_user_profiles(self))
            except Exception:
                item = merged
            self.sessions[origin] = item
        self.save_json(self.sessions_file, self.sessions)
    except Exception:
        pass


MemoryBridgePlugin.record_session = _mb_v064_record_session


async def _mb_v064_history(self, event: Any, limit: int | None = None) -> list[MemoryMessage]:
    limit = int(limit or self.cfg("export.max_messages", 1000) or 1000)
    conversation_manager = getattr(self.context, "conversation_manager", None)
    raw_items = []
    source = "current_message_only"
    all_debug = []
    origins = _mb_v064_history_origin_candidates(self, event)
    if conversation_manager:
        for origin in origins:
            try:
                items, source_name, debug = await self.try_read_for_origin(conversation_manager, origin, limit)
                all_debug += [f"origin={origin}"] + debug[-20:]
                if items:
                    raw_items = items
                    source = source_name
                    break
            except Exception as error:
                all_debug.append(f"origin={origin} err:{type(error).__name__}:{error}")
    if not raw_items:
        try:
            message = getattr(event, "message_str", None)
            message = message() if callable(message) else message
            raw_items = [{"role": "user", "content": str(message or getattr(event, "message", "") or ""), "time": now_str(), "source": "event_fallback"}]
        except Exception:
            raw_items = [{"role": "system", "content": "未能读取会话历史", "time": now_str()}]
    self.state["last_history_source"] = source
    self.state["last_history_count"] = len(raw_items[-limit:])
    self.state["last_history_origins_tried"] = origins
    self.state["last_history_debug"] = all_debug[-80:]
    self.save_state()
    return [self.to_msg(index, item) for index, item in enumerate(raw_items[-limit:])]


MemoryBridgePlugin.history = _mb_v064_history


def _mb_v064_web_sessions_payload(self):
    self.sessions = self.load_json(self.sessions_file, {})
    sessions = sorted(self.sessions.values(), key=lambda item: item.get("last_seen", ""), reverse=True) if isinstance(self.sessions, dict) else []
    grouped: dict[str, dict[str, Any]] = {}
    try:
        profiles = _mb_v056_build_user_profiles_from_sessions(self)
    except Exception:
        profiles = {}
    for session in sessions:
        if not isinstance(session, dict):
            continue
        origin = session.get("origin") or ""
        if _mb_v061_is_noisy_adapter_origin(origin):
            continue
        key = _mb_v061_origin_key(origin)
        try:
            item = _mb_v056_enrich_session_with_profiles(self, session, profiles)
        except Exception:
            item = dict(session)
        item["adapter_id"] = _mb_v061_origin_parts(origin).get("adapter") or ""
        item["origin_key"] = key
        item["dedupe_note"] = "v0.6.4: same MessageType+tail sessions are merged for display"
        if key not in grouped or str(item.get("last_seen") or "") > str(grouped[key].get("last_seen") or ""):
            grouped[key] = item
    return sorted(grouped.values(), key=lambda item: item.get("last_seen", ""), reverse=True)


MemoryBridgePlugin.web_sessions_payload = _mb_v064_web_sessions_payload


_old_mb_v064_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v064_web_status_payload(self):
    payload = _old_mb_v064_web_status_payload(self) if _old_mb_v064_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["session_dedupe_fix"] = {"enabled": True, "version": "0.6.4", "rule": "record only real event origin; history lookup may use same MessageType+tail fallback; WebUI merges duplicate adapter sessions"}
    return payload


MemoryBridgePlugin.web_status_payload = _mb_v064_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.5 integrated runtime patch
# 修复：WebUI 外部上传的 clean/migration 记忆包能进入“记忆迁移”下拉列表，
#      不再只识别本机 /续忆 clean_export 直接生成的 memory_bridge_clean_*.zip。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.5"


def _mb_v065_zip_names(path: Path) -> set[str]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            return set(archive.namelist())
    except Exception:
        return set()


def _mb_v065_is_migration_zip_path(path: Path) -> bool:
    if not path or not path.exists() or not path.is_file() or path.suffix.lower() != ".zip":
        return False
    names = _mb_v065_zip_names(path)
    if not names:
        return False
    if "migration/memory_card.md" in names or "migration/seed_history.json" in names:
        return True
    if "manifest.json" in names and "cleaning/kept_messages.json" in names:
        return True
    return False


def _mb_v065_read_text_from_zip(archive, name, default=""):
    try:
        return archive.read(name).decode("utf-8", "ignore")
    except Exception:
        return default


def _mb_v065_read_manifest_quick(self, path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            return self.read_json_from_zip(archive, "manifest.json", {}) or {}
    except Exception:
        return {}


_old_mb_v065_web_files_payload = getattr(MemoryBridgePlugin, "web_files_payload", None)
def _mb_v065_web_files_payload(self):
    data = _old_mb_v065_web_files_payload(self) if _old_mb_v065_web_files_payload else []
    records = self.load_json(self.records_file, [])
    records = records if isinstance(records, list) else []
    by_name = {r.get("name"): r for r in records if isinstance(r, dict) and r.get("name")}
    output = []
    seen = set()

    def enrich(item: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
        file_item = dict(item or {})
        name = file_item.get("name") or (path.name if path else "")
        rec = by_name.get(name, {}) if name else {}
        if rec.get("external_import"):
            file_item["external_import"] = True
            file_item["original_filename"] = rec.get("original_filename", file_item.get("original_filename", ""))
        if rec.get("migration_available") is not None:
            file_item["migration_available"] = bool(rec.get("migration_available"))
        if path and _mb_v065_is_migration_zip_path(path):
            file_item["migration_available"] = True
            file_item.setdefault("kind", rec.get("kind") or file_item.get("kind") or "external_import")
            manifest = _mb_v065_read_manifest_quick(self, path)
            if manifest:
                file_item.setdefault("manifest", manifest)
                file_item.setdefault("origin", manifest.get("origin") or rec.get("origin") or "")
                if manifest.get("stats") and not file_item.get("stats"):
                    file_item["stats"] = manifest.get("stats")
        if rec:
            file_item.setdefault("origin", rec.get("origin") or "")
            file_item.setdefault("source_label", rec.get("source_label") or rec.get("origin_label") or rec.get("origin") or "")
            file_item.setdefault("origin_label", rec.get("origin_label") or rec.get("source_label") or rec.get("origin") or "")
            if rec.get("stats") and not file_item.get("stats"):
                file_item["stats"] = rec.get("stats")
            if rec.get("warnings") and not file_item.get("warnings"):
                file_item["warnings"] = rec.get("warnings")
        file_item["migration_detect_rule"] = "v0.6.5: by zip structure, not filename prefix"
        return file_item

    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        path = self.resolve_export_file(name) if name else None
        file_item = enrich(item, path)
        output.append(file_item)
        if name:
            seen.add(name)
    try:
        for path in sorted(self.export_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.name in seen:
                continue
            if not _mb_v065_is_migration_zip_path(path):
                continue
            stat = path.stat()
            item = {"name": path.name, "kind": by_name.get(path.name, {}).get("kind") or "external_import", "size": stat.st_size, "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"), "external_import": path.name.startswith("external_"), "migration_available": True, "download_url": f"/api/files/{path.name}/download"}
            output.append(enrich(item, path))
            seen.add(path.name)
    except Exception:
        pass
    return output

MemoryBridgePlugin.web_files_payload = _mb_v065_web_files_payload


_old_mb_v065_load_migration_package = getattr(MemoryBridgePlugin, "load_migration_package", None)
def _mb_v065_load_migration_package(self, filename=None):
    if filename:
        path = self.resolve_export_file(str(filename))
        if not path:
            return None, {"ok": False, "error": "迁移包不存在或不允许访问", "file": filename}
        if not _mb_v065_is_migration_zip_path(path):
            return None, {"ok": False, "error": "该 zip 不是可迁移的续忆桥 clean/migration 包", "file": path.name}
        try:
            with zipfile.ZipFile(path, "r") as archive:
                names = set(archive.namelist())
                manifest = self.read_json_from_zip(archive, "manifest.json", {}) or {}
                clean_report = self.read_json_from_zip(archive, "cleaning/clean_report.json", {}) or {}
                memory_card_md = _mb_v065_read_text_from_zip(archive, "migration/memory_card.md", "")
                memory_card_json = self.read_json_from_zip(archive, "migration/memory_card.json", {}) or {}
                seed_history = self.read_json_from_zip(archive, "migration/seed_history.json", []) or []
                import_plan = self.read_json_from_zip(archive, "migration/import_plan.json", {}) or {}
                stats = manifest.get("stats") or clean_report.get("stats") or {}
                chunks = []
                if "migration/chunks.jsonl" in names:
                    for raw in _mb_v065_read_text_from_zip(archive, "migration/chunks.jsonl", "").splitlines():
                        raw = raw.strip()
                        if raw:
                            try:
                                chunks.append(json.loads(raw))
                            except Exception:
                                pass
                if not chunks:
                    chunks = self.read_json_from_zip(archive, "migration/chunks.json", []) or []
                if not memory_card_md and "cleaning/kept_messages.json" in names:
                    kept = self.read_json_from_zip(archive, "cleaning/kept_messages.json", []) or []
                    memory_card_md = "# 续忆桥旧版 clean 包兼容导入\n\n" + "\n".join([f"- {m.get('role','unknown')}：{preview(m.get('content',''), 260)}" for m in kept[-40:] if isinstance(m, dict)])
                    seed_history = [{"role": "user", "content": "以下是从旧版 clean 包迁移来的压缩记忆：\n\n" + memory_card_md}, {"role": "assistant", "content": "已接收迁移记忆。"}]
                return path, {"ok": True, "file": path.name, "size": path.stat().st_size, "manifest": manifest, "stats": stats, "memory_card_md": memory_card_md, "memory_card_json": memory_card_json, "seed_history": seed_history, "chunks": chunks, "import_plan": import_plan, "warnings": []}
        except Exception as error:
            return None, {"ok": False, "error": str(error), "traceback": traceback.format_exc()[-2000:], "file": path.name}
    path, package = _old_mb_v065_load_migration_package(self, filename) if _old_mb_v065_load_migration_package else (None, {"ok": False, "error": "load_migration_package unavailable"})
    if package.get("ok"):
        return path, package
    try:
        candidates = [p for p in self.export_dir.glob("*.zip") if _mb_v065_is_migration_zip_path(p)]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            return _mb_v065_load_migration_package(self, candidates[0].name)
    except Exception:
        pass
    return path, package

MemoryBridgePlugin.load_migration_package = _mb_v065_load_migration_package


_old_mb_v065_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v065_web_status_payload(self):
    payload = _old_mb_v065_web_status_payload(self) if _old_mb_v065_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["external_migration_fix"] = {"enabled": True, "version": "0.6.5", "rule": "migration selectable by zip internal structure, not memory_bridge_clean_ filename"}
    return payload

MemoryBridgePlugin.web_status_payload = _mb_v065_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.6 runtime patch
# MVP 注入 Step 2.2：memory_card fallback + 更稳健请求注入。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.7"


def _mb_v066_cfg_bool(self, path: str, default: bool = False) -> bool:
    try:
        return truthy(self.cfg(path, default), default)
    except Exception:
        return default


def _mb_v066_card_fallback_enabled(self) -> bool:
    return _mb_v066_cfg_bool(self, "memory_injection.card_fallback_enabled", True)


def _mb_v066_card_fallback_chars(self) -> int:
    return _mb_v060_cfg_int(self, "memory_injection.card_fallback_chars", 1200) if "_mb_v060_cfg_int" in globals() else 1200


def _mb_v066_line_score(line: str, keywords: list[str]) -> float:
    text = str(line or "").strip()
    if not text:
        return -999.0
    low = text.lower()
    score = 0.0
    for kw in keywords or []:
        if kw and kw in low:
            score += 3.0 if len(kw) >= 4 else 1.5
    if re.match(r"^#{1,4}\s+", text):
        score += 0.3
    if re.search(r"用户偏好|项目状态|技术环境|已解决问题|待办|下一步|最近保留上下文", text):
        score += 0.8
    if len(text) > 420:
        score -= 0.4
    return score


def _mb_v066_select_card_excerpt(memory_card: str, keywords: list[str], limit_chars: int) -> str:
    card = norm(memory_card or "")
    if not card:
        return ""
    limit_chars = max(200, int(limit_chars or 1200))
    lines = [line.strip() for line in card.splitlines() if line.strip()]
    if not lines:
        return card[:limit_chars]
    selected: list[str] = []
    if keywords:
        scored = []
        current_heading = ""
        for idx, line in enumerate(lines):
            if re.match(r"^#{1,4}\s+", line):
                current_heading = line
            score = _mb_v066_line_score(line, keywords)
            if score > 0:
                scored.append((score, idx, line, current_heading))
        scored.sort(key=lambda item: (-item[0], item[1]))
        seen = set()
        for _score, _idx, line, heading in scored[:24]:
            if heading and heading not in seen:
                selected.append(heading)
                seen.add(heading)
            if line not in seen:
                selected.append(line)
                seen.add(line)
            if len("\n".join(selected)) >= limit_chars:
                break
    if not selected:
        preferred = []
        capture = True
        for line in lines:
            if re.match(r"^#{1,4}\s+", line):
                capture = bool(re.search(r"迁移来源|用户偏好|项目状态|技术环境|已解决问题|待办|下一步|最近保留上下文", line))
                if capture:
                    preferred.append(line)
                continue
            if capture:
                preferred.append(line)
            if len("\n".join(preferred)) >= limit_chars:
                break
        selected = preferred or lines[:40]
    excerpt = norm("\n".join(selected))
    if len(excerpt) > limit_chars:
        excerpt = excerpt[:limit_chars] + "\n...[续忆桥：memory_card fallback 已截断]"
    return excerpt


def _mb_v066_retrieve_memory(self, origin: str, query: str) -> dict[str, Any]:
    max_imports = _mb_v060_cfg_int(self, "memory_injection.max_imports", 3) if "_mb_v060_cfg_int" in globals() else 3
    max_chunks = _mb_v060_cfg_int(self, "memory_injection.max_chunks", 6) if "_mb_v060_cfg_int" in globals() else 6
    max_total_chars = _mb_v060_cfg_int(self, "memory_injection.max_total_chars", 3600) if "_mb_v060_cfg_int" in globals() else 3600
    fallback_chars = _mb_v066_card_fallback_chars(self)
    keywords = _mb_v060_tokenize(query) if "_mb_v060_tokenize" in globals() else []
    active_fn = globals().get("_mb_v060_active_import_records")
    records = active_fn(self, origin)[:max_imports] if callable(active_fn) else []
    memories: list[dict[str, Any]] = []
    selected_chunks: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    chunks_total = 0
    for rec in records:
        mem = _mb_v060_load_import_memory(self, rec) if "_mb_v060_load_import_memory" in globals() else {"record": rec, "chunks": [], "memory_card": "", "import_id": rec.get("import_id")}
        memories.append(mem)
        scored = []
        chunks = mem.get("chunks") or []
        chunks_total += len(chunks) if isinstance(chunks, list) else 0
        for ch in chunks if isinstance(chunks, list) else []:
            if not isinstance(ch, dict):
                continue
            score = _mb_v060_score_chunk(ch, keywords) if "_mb_v060_score_chunk" in globals() else 0.0
            if score > 0 or not keywords:
                scored.append((score, ch, mem))
        scored.sort(key=lambda item: item[0], reverse=True)
        selected_chunks.extend(scored[:max_chunks])
    selected_chunks.sort(key=lambda item: item[0], reverse=True)
    selected_chunks = selected_chunks[:max_chunks]
    selected_import_ids = {str(mem.get("import_id") or "") for _score, _ch, mem in selected_chunks}
    if not memories:
        return {"ok": False, "reason": "no_active_memory", "origin": origin, "keywords": keywords, "chunks_total": 0, "fallback_used": False}

    fallback_cards = []
    if _mb_v066_card_fallback_enabled(self):
        for mem in memories:
            import_id = str(mem.get("import_id") or "")
            chunks = mem.get("chunks") or []
            chunks_len = len(chunks) if isinstance(chunks, list) else 0
            if chunks_len > 0 and import_id in selected_import_ids:
                continue
            excerpt = _mb_v066_select_card_excerpt(mem.get("memory_card") or "", keywords, fallback_chars)
            if excerpt:
                fallback_cards.append({
                    "import_id": import_id,
                    "reason": "chunks_empty" if chunks_len == 0 else "no_chunk_hit",
                    "excerpt": excerpt,
                    "chars": len(excerpt),
                })

    if not selected_chunks and not fallback_cards:
        return {
            "ok": False,
            "reason": "no_relevant_chunk_or_card_fallback",
            "origin": origin,
            "keywords": keywords,
            "imports": [m.get("import_id") for m in memories],
            "chunks_total": chunks_total,
            "fallback_used": False,
        }

    parts = [
        "[续忆桥长期记忆注入]",
        "以下内容来自用户主动导入/迁移的旧会话记忆。请只在与当前问题相关时参考；不要主动完整复述；若与当前用户新指令冲突，以当前指令为准。",
        "",
    ]
    if selected_chunks:
        parts.append("## 与当前问题匹配的记忆片段")
        for score, ch, mem in selected_chunks:
            rec = mem.get("record") or {}
            source = rec.get("source_origin") or (mem.get("manifest") or {}).get("origin") or "未知来源"
            text = preview(ch.get("text") or ch.get("content") or "", 520)
            parts.append(f"- [{mem.get('import_id')} / {ch.get('category') or 'fact'} / {ch.get('id') or 'chunk'} / score={score:.1f} / 来源={source}] {text}")
        parts.append("")
    if fallback_cards:
        parts.append("## memory_card fallback 摘要")
        parts.append("说明：以下摘要仅在 chunks 为空或本轮未命中 chunks 时补充，用于保证迁移记忆能被 MVP 注入链路读取。")
        for item in fallback_cards:
            parts.append(f"### {item.get('import_id')}｜{item.get('reason')}")
            parts.append(item.get("excerpt") or "")
            parts.append("")
    context = norm("\n".join(parts))
    if len(context) > max_total_chars:
        context = context[:max_total_chars] + "\n...[续忆桥：已按注入字符上限截断]"
    return {
        "ok": True,
        "origin": origin,
        "keywords": keywords,
        "imports": [m.get("import_id") for m in memories],
        "chunks": len(selected_chunks),
        "chunks_total": chunks_total,
        "fallback_used": bool(fallback_cards),
        "fallback_cards_count": len(fallback_cards),
        "fallback_reasons": [item.get("reason") for item in fallback_cards],
        "card_fallback_enabled": _mb_v066_card_fallback_enabled(self),
        "chars": len(context),
        "context": context,
    }


def _mb_v066_apply_injection_to_req(req: Any, context_text: str) -> str:
    if req is None or not context_text:
        return ""
    try:
        if isinstance(req, dict):
            messages = req.get("messages")
            if isinstance(messages, list):
                messages.insert(max(0, len(messages) - 1), {"role": "system", "content": context_text})
                return "dict.messages.system_insert"
            req["system_prompt"] = str(req.get("system_prompt") or "") + "\n\n" + context_text
            return "dict.system_prompt"
    except Exception:
        pass
    try:
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is not None and hasattr(parts, "append"):
            parts.append(context_text)
            return "extra_user_content_parts"
    except Exception:
        pass
    try:
        messages = getattr(req, "messages", None)
        if isinstance(messages, list):
            messages.insert(max(0, len(messages) - 1), {"role": "system", "content": context_text})
            return "messages.system_insert"
    except Exception:
        pass
    try:
        contexts = getattr(req, "contexts", None)
        if isinstance(contexts, list):
            contexts.append({"role": "system", "content": context_text})
            return "contexts.append"
    except Exception:
        pass
    try:
        old = getattr(req, "system_prompt", "") or ""
        setattr(req, "system_prompt", str(old) + "\n\n" + context_text)
        return "system_prompt"
    except Exception:
        pass
    try:
        old = getattr(req, "prompt", "") or ""
        setattr(req, "prompt", context_text + "\n\n" + str(old))
        return "prompt"
    except Exception:
        pass
    return "failed"


globals()["_mb_v060_retrieve_memory"] = _mb_v066_retrieve_memory
globals()["_mb_v060_apply_injection_to_req"] = _mb_v066_apply_injection_to_req
MemoryBridgePlugin.retrieve_memory = _mb_v066_retrieve_memory


_old_mb_v066_memory_status_payload = getattr(MemoryBridgePlugin, "memory_status_payload", None)
def _mb_v066_memory_status_payload(self, origin: str | None = None) -> dict[str, Any]:
    origin = origin or ""
    try:
        payload = _old_mb_v066_memory_status_payload(self, origin) if _old_mb_v066_memory_status_payload else {}
        payload = payload if isinstance(payload, dict) else {}
    except Exception as error:
        payload = {"ok": False, "error": str(error)}
    payload.update({
        "ok": True,
        "version": PLUGIN_VERSION,
        "enabled": _mb_v060_memory_enabled(self) if "_mb_v060_memory_enabled" in globals() else True,
        "origin": origin,
        "origin_key": _mb_v061_origin_key(origin) if "_mb_v061_origin_key" in globals() else origin,
        "step22_note": "MVP 注入增强：chunks 为空或无命中时启用 memory_card.md 轻量 fallback。",
        "card_fallback_enabled": _mb_v066_card_fallback_enabled(self),
        "recent_injections": _mb_v060_recent_injections(self, 20) if "_mb_v060_recent_injections" in globals() else [],
        "limits": {
            "max_imports": _mb_v060_cfg_int(self, "memory_injection.max_imports", 3) if "_mb_v060_cfg_int" in globals() else 3,
            "max_chunks": _mb_v060_cfg_int(self, "memory_injection.max_chunks", 6) if "_mb_v060_cfg_int" in globals() else 6,
            "card_fallback_chars": _mb_v066_card_fallback_chars(self),
            "max_total_chars": _mb_v060_cfg_int(self, "memory_injection.max_total_chars", 3600) if "_mb_v060_cfg_int" in globals() else 3600,
        },
    })
    return payload

MemoryBridgePlugin.memory_status_payload = _mb_v066_memory_status_payload
globals()["_mb_v060_memory_status_payload"] = _mb_v066_memory_status_payload


_old_mb_v066_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v066_web_status_payload(self):
    payload = _old_mb_v066_web_status_payload(self) if _old_mb_v066_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    try:
        payload["memory_injection"] = self.memory_status_payload(None)
    except Exception as error:
        payload["memory_injection"] = {"ok": False, "error": str(error)}
    payload["mvp_injection_patch"] = {"enabled": True, "version": "0.6.6", "card_fallback": True}
    return payload

MemoryBridgePlugin.web_status_payload = _mb_v066_web_status_payload


async def _mb_v066_on_llm_request_impl(self, event: AstrMessageEvent, req: Any):
    if not _mb_v060_memory_enabled(self):
        return
    try:
        origin = self.origin(event)
    except Exception:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
    if not origin:
        return
    try:
        if hasattr(self, "is_origin_allowed") and not self.is_origin_allowed(origin):
            return
    except Exception:
        pass
    query = _mb_v060_message_text(event, req) if "_mb_v060_message_text" in globals() else ""
    if "_mb_v060_is_command_text" in globals() and _mb_v060_is_command_text(query):
        return
    payload = _mb_v066_retrieve_memory(self, origin, query)
    if not payload.get("ok"):
        try:
            self.state["last_memory_injection_skip"] = {"time": now_str(), "origin": origin, "reason": payload.get("reason"), "query_preview": preview(query, 160), "keywords": payload.get("keywords", [])[:20], "chunks_total": payload.get("chunks_total", 0)}
            self.save_state()
        except Exception:
            pass
        return
    method = _mb_v066_apply_injection_to_req(req, payload.get("context") or "")
    row = {"ok": method not in {"", "failed"}, "origin": origin, "method": method, "query_preview": preview(query, 160), "imports": payload.get("imports") or [], "chunks": payload.get("chunks", 0), "chunks_total": payload.get("chunks_total", 0), "fallback_used": payload.get("fallback_used", False), "fallback_cards_count": payload.get("fallback_cards_count", 0), "fallback_reasons": payload.get("fallback_reasons", []), "chars": payload.get("chars", 0), "keywords": payload.get("keywords", [])[:20], "version": PLUGIN_VERSION}
    _mb_v060_append_injection_log(self, row)
    try:
        self.state["last_memory_injection"] = row
        self.save_state()
    except Exception:
        pass

try:
    _mb_v066_on_llm_request = filter.on_llm_request()(_mb_v066_on_llm_request_impl)
except Exception:
    _mb_v066_on_llm_request = _mb_v066_on_llm_request_impl
MemoryBridgePlugin.on_llm_request_memory_bridge = _mb_v066_on_llm_request


try:
    _mb_v066_group = MemoryBridgePlugin.memory_group

    @_mb_v066_group.command("memory_test2")
    async def _mb_v066_memory_test2_cmd(self, event, query: str = ""):
        origin = self.origin(event)
        if not query:
            try:
                msg = getattr(event, "message_str", "")
                msg = msg() if callable(msg) else msg
                query = str(msg or "").replace("/续忆 memory_test2", "", 1).strip()
            except Exception:
                query = ""
        payload = _mb_v066_retrieve_memory(self, origin, query or "")
        if not payload.get("ok"):
            yield event.plain_result("MVP 记忆检索无结果：" + payload.get("reason", "unknown") + "\n" + dumps({k: v for k, v in payload.items() if k != "context"})[:1600])
            return
        meta = {k: v for k, v in payload.items() if k != "context"}
        yield event.plain_result("MVP 记忆检索测试\n" + dumps(meta)[:1200] + "\n\n" + preview(payload.get("context") or "", 2600))

    MemoryBridgePlugin.memory_test2_cmd = _mb_v066_memory_test2_cmd
except Exception:
    pass


# ---------------------------------------------------------------------------
# v0.6.8 runtime patch
# 修复部分 AstrBot 适配器把当前会话 origin 暴露为纯 QQ/群号，导致导入记录
# target_origin 为完整 adapter origin 时长期记忆被误判为 no_active_memory。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.8"


def _mb_v068_origin_match_keys(origin: str) -> set[str]:
    text = str(origin or "").strip()
    if not text:
        return set()
    keys = {text.lower()}
    try:
        keys.add(_mb_v061_origin_key(text))
    except Exception:
        pass
    try:
        parsed = _mb_v056_parse_origin(text) if "_mb_v056_parse_origin" in globals() else {}
    except Exception:
        parsed = {}
    parts = _mb_v061_origin_parts(text) if "_mb_v061_origin_parts" in globals() else {"tail": text, "message_type": ""}
    tail = str(parsed.get("tail") or parts.get("tail") or text).strip()
    message_type = str(parts.get("message_type") or "").strip()
    user_id = str(parsed.get("user_id") or "").strip()
    group_id = str(parsed.get("group_id") or "").strip()
    is_group = bool(parsed.get("is_group") or message_type == "GroupMessage")
    if tail:
        keys.add(tail.lower())
    if user_id and not is_group:
        keys.add(f"friendmessage:{user_id}".lower())
    if group_id:
        keys.add(f"groupmessage:{group_id}".lower())
    if re.match(r"^\d+$", text):
        keys.add(f"friendmessage:{text}".lower())
    if re.match(r"^\d+_\d+$", text):
        user_part, group_part = text.split("_", 1)
        keys.update({
            f"groupmessage:{text}".lower(),
            group_part.lower(),
            f"groupmessage:{group_part}".lower(),
        })
    if message_type and tail:
        keys.add(f"{message_type}:{tail}".lower())
    return {key for key in keys if key}


def _mb_v068_same_origin(a: str, b: str) -> bool:
    if str(a or "") == str(b or ""):
        return True
    return bool(_mb_v068_origin_match_keys(a) & _mb_v068_origin_match_keys(b))


def _mb_v068_import_record_usable(self, rec: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(rec, dict):
        return False, "bad_record"
    if rec.get("status") not in {"active", "enabled", None, ""}:
        return False, f"status={rec.get('status')}"
    import_id = str(rec.get("import_id") or "")
    if not import_id:
        return False, "missing_import_id"
    mode = str(rec.get("mode") or "")
    if mode not in {"plugin_memory", "hybrid"}:
        return False, f"mode={mode or 'empty'}"
    import_path = self.import_dir / import_id
    if (import_path / "DISABLED").exists():
        return False, "disabled_marker"
    if not (import_path / "memory_card.md").exists() and not (import_path / "chunks.json").exists():
        return False, "missing_memory_files"
    return True, "ok"


def _mb_v068_active_import_records(self, origin: str | None = None) -> list[dict[str, Any]]:
    records = self.load_json(self.import_records_file, [])
    if not isinstance(records, list):
        return []
    out: list[dict[str, Any]] = []
    for rec in records:
        ok, _reason = _mb_v068_import_record_usable(self, rec)
        if not ok:
            continue
        target = str(rec.get("target_origin") or "")
        if origin and target and not _mb_v068_same_origin(target, origin):
            continue
        out.append(rec)
    return out


def _mb_v068_import_match_diagnostics(self, origin: str | None = None, limit: int = 12) -> dict[str, Any]:
    records = self.load_json(self.import_records_file, [])
    records = records if isinstance(records, list) else []
    origin = str(origin or "")
    rows = []
    for rec in records[:limit]:
        if not isinstance(rec, dict):
            continue
        usable, reason = _mb_v068_import_record_usable(self, rec)
        target = str(rec.get("target_origin") or "")
        rows.append({
            "import_id": rec.get("import_id"),
            "mode": rec.get("mode"),
            "status": rec.get("status"),
            "target_origin": target,
            "target_keys": sorted(_mb_v068_origin_match_keys(target))[:8],
            "match_current_origin": bool(origin and target and _mb_v068_same_origin(target, origin)),
            "usable": usable,
            "skip_reason": reason,
            "chunks": ((rec.get("plugin_memory") or {}).get("chunks", 0)),
        })
    return {
        "records_total": len(records),
        "current_origin_keys": sorted(_mb_v068_origin_match_keys(origin))[:10],
        "sample": rows,
    }


globals()["_mb_v060_active_import_records"] = _mb_v068_active_import_records
globals()["_mb_v061_active_import_records"] = _mb_v068_active_import_records


_old_mb_v068_memory_status_payload = getattr(MemoryBridgePlugin, "memory_status_payload", None)
def _mb_v068_memory_status_payload(self, origin: str | None = None) -> dict[str, Any]:
    origin = origin or ""
    try:
        payload = _old_mb_v068_memory_status_payload(self, origin) if _old_mb_v068_memory_status_payload else {}
        payload = payload if isinstance(payload, dict) else {}
    except Exception as error:
        payload = {"ok": False, "error": str(error)}
    active = _mb_v068_active_import_records(self, origin or None)
    payload.update({
        "ok": True,
        "version": PLUGIN_VERSION,
        "origin": origin,
        "origin_key": _mb_v061_origin_key(origin) if "_mb_v061_origin_key" in globals() else origin,
        "origin_match_keys": sorted(_mb_v068_origin_match_keys(origin))[:10],
        "active_imports_count": len(active),
        "active_imports": [
            {
                "import_id": r.get("import_id"),
                "mode": r.get("mode"),
                "source_file": r.get("source_file"),
                "source_origin": r.get("source_origin"),
                "target_origin": r.get("target_origin"),
                "target_origin_key": _mb_v061_origin_key(r.get("target_origin") or "") if "_mb_v061_origin_key" in globals() else r.get("target_origin"),
                "target_match_keys": sorted(_mb_v068_origin_match_keys(r.get("target_origin") or ""))[:8],
                "chunks": ((r.get("plugin_memory") or {}).get("chunks", 0)),
                "created_at": r.get("created_at"),
            }
            for r in active[:20]
        ],
        "import_match_diagnostics": _mb_v068_import_match_diagnostics(self, origin or None),
        "step23_note": "v0.6.8: 当前 origin 为纯 QQ/群号时，也能匹配完整 adapter:MessageType:tail 导入记录。",
    })
    return payload


MemoryBridgePlugin.memory_status_payload = _mb_v068_memory_status_payload
globals()["_mb_v060_memory_status_payload"] = _mb_v068_memory_status_payload
globals()["_mb_v061_memory_status_payload"] = _mb_v068_memory_status_payload


_old_mb_v068_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v068_web_status_payload(self):
    payload = _old_mb_v068_web_status_payload(self) if _old_mb_v068_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    try:
        payload["memory_injection"] = self.memory_status_payload(None)
    except Exception as error:
        payload["memory_injection"] = {"ok": False, "error": str(error)}
    payload["origin_match_patch"] = {"enabled": True, "version": "0.6.8"}
    return payload


MemoryBridgePlugin.web_status_payload = _mb_v068_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.9 runtime patch
# WebUI 长期记忆注入管理：按导入记录与 chunk/card 条目启用或禁用。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.9"


def _mb_v069_memory_item_state_path(self, import_id: str) -> Path:
    return self.import_dir / str(import_id or "") / "memory_items_state.json"


def _mb_v069_load_memory_item_state(self, import_id: str) -> dict[str, Any]:
    data = self.load_json(_mb_v069_memory_item_state_path(self, import_id), {})
    if not isinstance(data, dict):
        data = {}
    disabled = data.get("disabled_chunks") or []
    if not isinstance(disabled, list):
        disabled = []
    return {
        "card_enabled": bool(data.get("card_enabled", True)),
        "disabled_chunks": [str(item) for item in disabled if str(item or "").strip()],
        "updated_at": data.get("updated_at", ""),
    }


def _mb_v069_save_memory_item_state(self, import_id: str, state: dict[str, Any]) -> dict[str, Any]:
    clean = {
        "card_enabled": bool(state.get("card_enabled", True)),
        "disabled_chunks": sorted({str(item) for item in (state.get("disabled_chunks") or []) if str(item or "").strip()}),
        "updated_at": now_str(),
    }
    self.save_json(_mb_v069_memory_item_state_path(self, import_id), clean)
    return clean


def _mb_v069_set_import_enabled(self, import_id: str, enabled: bool) -> dict[str, Any]:
    import_id = str(import_id or "").strip()
    records = self.load_json(self.import_records_file, [])
    records = records if isinstance(records, list) else []
    target = None
    for rec in records:
        if isinstance(rec, dict) and rec.get("import_id") == import_id:
            target = rec
            break
    if not target:
        return {"ok": False, "error": f"未找到导入记录：{import_id}"}
    if enabled and target.get("status") in {"rolled_back", "failed"}:
        return {"ok": False, "error": f"该导入记录状态为 {target.get('status')}，不能重新启用。"}
    import_path = self.import_dir / import_id
    if not import_path.exists():
        return {"ok": False, "error": f"导入目录不存在：{import_id}"}
    marker = import_path / "DISABLED"
    if enabled:
        try:
            if marker.exists():
                marker.unlink()
        except Exception as error:
            return {"ok": False, "error": f"启用失败：{error}"}
        target["status"] = "active"
        target["enabled"] = True
        target["enabled_at"] = now_str()
    else:
        try:
            marker.write_text(now_str(), "utf-8")
        except Exception as error:
            return {"ok": False, "error": f"禁用失败：{error}"}
        target["status"] = "disabled"
        target["enabled"] = False
        target["disabled_at"] = now_str()
    self.save_json(self.import_records_file, records)
    self.append_action_log({"kind": "memory_import_enabled", "ok": True, "import_id": import_id, "enabled": bool(enabled)})
    return {"ok": True, "import_id": import_id, "enabled": bool(enabled), "status": target.get("status")}


def _mb_v069_set_memory_item_enabled(self, import_id: str, item_id: str, enabled: bool) -> dict[str, Any]:
    import_id = str(import_id or "").strip()
    item_id = str(item_id or "").strip()
    if not import_id or not item_id:
        return {"ok": False, "error": "缺少 import_id 或 item_id"}
    import_path = self.import_dir / import_id
    if not import_path.exists():
        return {"ok": False, "error": f"导入目录不存在：{import_id}"}
    state = _mb_v069_load_memory_item_state(self, import_id)
    disabled = set(state.get("disabled_chunks") or [])
    if item_id in {"__card__", "memory_card"}:
        state["card_enabled"] = bool(enabled)
        item_kind = "memory_card"
    else:
        chunks = self.load_json(import_path / "chunks.json", [])
        chunks = chunks if isinstance(chunks, list) else []
        known_ids = {str((ch or {}).get("id") or f"chunk_{idx:04d}") for idx, ch in enumerate(chunks) if isinstance(ch, dict)}
        if item_id not in known_ids:
            return {"ok": False, "error": f"未找到记忆条目：{item_id}"}
        if enabled:
            disabled.discard(item_id)
        else:
            disabled.add(item_id)
        state["disabled_chunks"] = sorted(disabled)
        item_kind = "chunk"
    state = _mb_v069_save_memory_item_state(self, import_id, state)
    self.append_action_log({"kind": "memory_item_enabled", "ok": True, "import_id": import_id, "item_id": item_id, "item_kind": item_kind, "enabled": bool(enabled)})
    return {"ok": True, "import_id": import_id, "item_id": item_id, "item_kind": item_kind, "enabled": bool(enabled), "state": state}


_old_mb_v069_load_import_memory = globals().get("_mb_v060_load_import_memory")
def _mb_v069_load_import_memory(self, record: dict[str, Any]) -> dict[str, Any]:
    mem = _old_mb_v069_load_import_memory(self, record) if callable(_old_mb_v069_load_import_memory) else {"record": record, "chunks": [], "memory_card": "", "import_id": record.get("import_id")}
    import_id = str(mem.get("import_id") or record.get("import_id") or "")
    state = _mb_v069_load_memory_item_state(self, import_id)
    disabled = set(state.get("disabled_chunks") or [])
    chunks = []
    for idx, ch in enumerate(mem.get("chunks") or []):
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or f"chunk_{idx:04d}")
        if cid in disabled:
            continue
        item = dict(ch)
        item.setdefault("id", cid)
        chunks.append(item)
    mem["chunks"] = chunks
    mem["memory_item_state"] = state
    if not state.get("card_enabled", True):
        mem["memory_card"] = ""
    return mem


globals()["_mb_v060_load_import_memory"] = _mb_v069_load_import_memory


def _mb_v069_memory_imports_payload(self) -> dict[str, Any]:
    records = self.load_json(self.import_records_file, [])
    records = records if isinstance(records, list) else []
    rows = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        import_id = str(rec.get("import_id") or "")
        if not import_id:
            continue
        import_path = self.import_dir / import_id
        state = _mb_v069_load_memory_item_state(self, import_id)
        marker_disabled = (import_path / "DISABLED").exists()
        chunks = self.load_json(import_path / "chunks.json", [])
        chunks = chunks if isinstance(chunks, list) else []
        memory_card = ""
        try:
            card_path = import_path / "memory_card.md"
            if card_path.exists():
                memory_card = card_path.read_text("utf-8", errors="ignore")
        except Exception:
            memory_card = ""
        disabled = set(state.get("disabled_chunks") or [])
        chunk_items = []
        for idx, ch in enumerate(chunks):
            if not isinstance(ch, dict):
                continue
            cid = str(ch.get("id") or f"chunk_{idx:04d}")
            text = str(ch.get("text") or ch.get("content") or "")
            chunk_items.append({
                "id": cid,
                "kind": "chunk",
                "enabled": cid not in disabled,
                "category": ch.get("category") or "fact",
                "tags": ch.get("tags") or [],
                "chars": len(text),
                "preview": preview(text, 260),
            })
        items = []
        if memory_card:
            items.append({
                "id": "__card__",
                "kind": "memory_card",
                "enabled": bool(state.get("card_enabled", True)),
                "category": "summary",
                "chars": len(memory_card),
                "preview": preview(memory_card, 420),
            })
        items.extend(chunk_items)
        usable, reason = _mb_v068_import_record_usable(self, rec) if "_mb_v068_import_record_usable" in globals() else (not marker_disabled, "ok")
        rows.append({
            "import_id": import_id,
            "mode": rec.get("mode"),
            "status": rec.get("status"),
            "enabled": bool(usable and not marker_disabled and rec.get("status") not in {"disabled", "rolled_back", "failed"}),
            "usable": bool(usable),
            "skip_reason": reason,
            "source_file": rec.get("source_file"),
            "source_origin": rec.get("source_origin"),
            "target_origin": rec.get("target_origin"),
            "target_label": rec.get("target_label"),
            "source_label": rec.get("source_label"),
            "created_at": rec.get("created_at"),
            "updated_at": state.get("updated_at"),
            "chunks_total": len(chunk_items),
            "chunks_enabled": sum(1 for item in chunk_items if item.get("enabled")),
            "card_enabled": bool(state.get("card_enabled", True)),
            "items": items,
        })
    return {"ok": True, "version": PLUGIN_VERSION, "imports": rows, "recent_injections": _mb_v060_recent_injections(self, 20) if "_mb_v060_recent_injections" in globals() else []}


_old_mb_v069_register_routes = getattr(MemoryBridgeWebAdminServer, "_register_routes", None)
def _mb_v069_register_routes(self):
    if _old_mb_v069_register_routes:
        _old_mb_v069_register_routes(self)

    @self.app.get("/api/memory-injection/imports")
    async def memory_injection_imports():
        return self.plugin.memory_imports_payload()

    @self.app.post("/api/memory-injection/imports/{import_id}/enabled")
    async def memory_injection_import_enabled(import_id: str, payload: dict[str, Any]):
        result = _mb_v069_set_import_enabled(self.plugin, import_id, bool(payload.get("enabled", True)))
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @self.app.post("/api/memory-injection/imports/{import_id}/items/{item_id}/enabled")
    async def memory_injection_item_enabled(import_id: str, item_id: str, payload: dict[str, Any]):
        result = _mb_v069_set_memory_item_enabled(self.plugin, import_id, item_id, bool(payload.get("enabled", True)))
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)


MemoryBridgeWebAdminServer._register_routes = _mb_v069_register_routes
MemoryBridgePlugin.memory_imports_payload = _mb_v069_memory_imports_payload


_old_mb_v069_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v069_web_status_payload(self):
    payload = _old_mb_v069_web_status_payload(self) if _old_mb_v069_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["memory_management_patch"] = {"enabled": True, "version": "0.6.9"}
    return payload


MemoryBridgePlugin.web_status_payload = _mb_v069_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.10 runtime patch
# 当导入包没有 chunks.json/jsonl 时，从 memory_card.md 自动生成可管理的虚拟 chunks。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.10"


def _mb_v610_synthetic_chunks_from_card(memory_card: str, limit: int = 80) -> list[dict[str, Any]]:
    text = norm(memory_card or "")
    if not text:
        return []
    chunks: list[dict[str, Any]] = []
    current_title = "memory_card"
    current_lines: list[str] = []

    def flush():
        nonlocal current_lines
        body = norm("\n".join(current_lines))
        current_lines = []
        if not body:
            return
        while len(body) > 900:
            part = body[:900]
            body = body[900:]
            chunks.append({
                "id": f"card_chunk_{len(chunks) + 1:04d}",
                "category": "memory_card",
                "text": part,
                "tags": ["memory_card", current_title],
                "synthetic": True,
                "source": "memory_card.md",
            })
        chunks.append({
            "id": f"card_chunk_{len(chunks) + 1:04d}",
            "category": "memory_card",
            "text": body,
            "tags": ["memory_card", current_title],
            "synthetic": True,
            "source": "memory_card.md",
        })

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^#{1,4}\s+", line):
            flush()
            current_title = re.sub(r"^#{1,4}\s+", "", line).strip() or "memory_card"
            continue
        current_lines.append(line)
    flush()

    if not chunks:
        chunks.append({
            "id": "card_chunk_0001",
            "category": "memory_card",
            "text": preview(text, 900),
            "tags": ["memory_card"],
            "synthetic": True,
            "source": "memory_card.md",
        })
    return chunks[:max(1, int(limit or 80))]


_old_mb_v610_load_import_memory = globals().get("_mb_v060_load_import_memory")
def _mb_v610_load_import_memory(self, record: dict[str, Any]) -> dict[str, Any]:
    mem = _old_mb_v610_load_import_memory(self, record) if callable(_old_mb_v610_load_import_memory) else {"record": record, "chunks": [], "memory_card": "", "import_id": record.get("import_id")}
    if not mem.get("chunks"):
        mem["chunks"] = _mb_v610_synthetic_chunks_from_card(mem.get("memory_card") or "")
        if mem["chunks"]:
            mem["chunks_synthetic"] = True
    return mem


globals()["_mb_v060_load_import_memory"] = _mb_v610_load_import_memory


def _mb_v610_import_chunks_for_management(self, import_path: Path, memory_card: str) -> tuple[list[dict[str, Any]], bool]:
    chunks = self.load_json(import_path / "chunks.json", [])
    chunks = chunks if isinstance(chunks, list) else []
    if chunks:
        return chunks, False
    return _mb_v610_synthetic_chunks_from_card(memory_card), True


def _mb_v610_set_memory_item_enabled(self, import_id: str, item_id: str, enabled: bool) -> dict[str, Any]:
    import_id = str(import_id or "").strip()
    item_id = str(item_id or "").strip()
    if not import_id or not item_id:
        return {"ok": False, "error": "缺少 import_id 或 item_id"}
    import_path = self.import_dir / import_id
    if not import_path.exists():
        return {"ok": False, "error": f"导入目录不存在：{import_id}"}
    state = _mb_v069_load_memory_item_state(self, import_id)
    disabled = set(state.get("disabled_chunks") or [])
    if item_id in {"__card__", "memory_card"}:
        state["card_enabled"] = bool(enabled)
        item_kind = "memory_card"
    else:
        memory_card = ""
        try:
            card_path = import_path / "memory_card.md"
            if card_path.exists():
                memory_card = card_path.read_text("utf-8", errors="ignore")
        except Exception:
            memory_card = ""
        chunks, _synthetic = _mb_v610_import_chunks_for_management(self, import_path, memory_card)
        known_ids = {str((ch or {}).get("id") or f"chunk_{idx:04d}") for idx, ch in enumerate(chunks) if isinstance(ch, dict)}
        if item_id not in known_ids:
            return {"ok": False, "error": f"未找到记忆条目：{item_id}"}
        if enabled:
            disabled.discard(item_id)
        else:
            disabled.add(item_id)
        state["disabled_chunks"] = sorted(disabled)
        item_kind = "chunk"
    state = _mb_v069_save_memory_item_state(self, import_id, state)
    self.append_action_log({"kind": "memory_item_enabled", "ok": True, "import_id": import_id, "item_id": item_id, "item_kind": item_kind, "enabled": bool(enabled)})
    return {"ok": True, "import_id": import_id, "item_id": item_id, "item_kind": item_kind, "enabled": bool(enabled), "state": state}


globals()["_mb_v069_set_memory_item_enabled"] = _mb_v610_set_memory_item_enabled


def _mb_v610_memory_imports_payload(self) -> dict[str, Any]:
    records = self.load_json(self.import_records_file, [])
    records = records if isinstance(records, list) else []
    rows = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        import_id = str(rec.get("import_id") or "")
        if not import_id:
            continue
        import_path = self.import_dir / import_id
        state = _mb_v069_load_memory_item_state(self, import_id)
        marker_disabled = (import_path / "DISABLED").exists()
        memory_card = ""
        try:
            card_path = import_path / "memory_card.md"
            if card_path.exists():
                memory_card = card_path.read_text("utf-8", errors="ignore")
        except Exception:
            memory_card = ""
        chunks, synthetic = _mb_v610_import_chunks_for_management(self, import_path, memory_card)
        disabled = set(state.get("disabled_chunks") or [])
        chunk_items = []
        for idx, ch in enumerate(chunks):
            if not isinstance(ch, dict):
                continue
            cid = str(ch.get("id") or f"chunk_{idx:04d}")
            text = str(ch.get("text") or ch.get("content") or "")
            chunk_items.append({
                "id": cid,
                "kind": "chunk",
                "enabled": cid not in disabled,
                "category": ch.get("category") or "fact",
                "tags": ch.get("tags") or [],
                "chars": len(text),
                "preview": preview(text, 260),
                "synthetic": bool(ch.get("synthetic") or synthetic),
            })
        items = []
        if memory_card:
            items.append({
                "id": "__card__",
                "kind": "memory_card",
                "enabled": bool(state.get("card_enabled", True)),
                "category": "summary",
                "chars": len(memory_card),
                "preview": preview(memory_card, 420),
            })
        items.extend(chunk_items)
        usable, reason = _mb_v068_import_record_usable(self, rec) if "_mb_v068_import_record_usable" in globals() else (not marker_disabled, "ok")
        rows.append({
            "import_id": import_id,
            "mode": rec.get("mode"),
            "status": rec.get("status"),
            "enabled": bool(usable and not marker_disabled and rec.get("status") not in {"disabled", "rolled_back", "failed"}),
            "usable": bool(usable),
            "skip_reason": reason,
            "source_file": rec.get("source_file"),
            "source_origin": rec.get("source_origin"),
            "target_origin": rec.get("target_origin"),
            "target_label": rec.get("target_label"),
            "source_label": rec.get("source_label"),
            "created_at": rec.get("created_at"),
            "updated_at": state.get("updated_at"),
            "chunks_total": len(chunk_items),
            "chunks_enabled": sum(1 for item in chunk_items if item.get("enabled")),
            "chunks_synthetic": bool(synthetic),
            "card_enabled": bool(state.get("card_enabled", True)),
            "items": items,
        })
    return {"ok": True, "version": PLUGIN_VERSION, "imports": rows, "recent_injections": _mb_v060_recent_injections(self, 20) if "_mb_v060_recent_injections" in globals() else []}


MemoryBridgePlugin.memory_imports_payload = _mb_v610_memory_imports_payload


_old_mb_v610_register_routes = getattr(MemoryBridgeWebAdminServer, "_register_routes", None)
def _mb_v610_register_routes(self):
    if _old_mb_v610_register_routes:
        _old_mb_v610_register_routes(self)

    @self.app.post("/api/memory-injection/imports/{import_id}/items/{item_id}/enabled")
    async def memory_injection_item_enabled_v610(import_id: str, item_id: str, payload: dict[str, Any]):
        result = _mb_v610_set_memory_item_enabled(self.plugin, import_id, item_id, bool(payload.get("enabled", True)))
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)


MemoryBridgeWebAdminServer._register_routes = _mb_v610_register_routes


_old_mb_v610_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v610_web_status_payload(self):
    payload = _old_mb_v610_web_status_payload(self) if _old_mb_v610_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["memory_card_chunk_patch"] = {"enabled": True, "version": "0.6.10"}
    return payload


MemoryBridgePlugin.web_status_payload = _mb_v610_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.11 runtime patch
# 更细粒度拆分 memory_card：按标题、列表项、句段和长度阈值拆成便于 WebUI 管理的小 chunks。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.15"


def _mb_v611_split_text_units(text: str) -> list[str]:
    text = norm(text or "")
    if not text:
        return []
    # 优先按中文/英文句末标点切分；没有标点的长句再按长度切。
    pieces = re.split(r"(?<=[。！？!?；;])\s+", text)
    if len(pieces) <= 1:
        pieces = re.split(r"(?<=[。！？!?；;])", text)
    out: list[str] = []
    for piece in pieces:
        piece = norm(piece)
        if not piece:
            continue
        while len(piece) > 360:
            cut_at = max(piece.rfind("，", 0, 360), piece.rfind(",", 0, 360), piece.rfind("、", 0, 360), piece.rfind(" ", 0, 360))
            if cut_at < 160:
                cut_at = 360
            out.append(norm(piece[:cut_at]))
            piece = norm(piece[cut_at:])
        if piece:
            out.append(piece)
    return out


def _mb_v611_synthetic_chunks_from_card(memory_card: str, limit: int = 240) -> list[dict[str, Any]]:
    text = norm(memory_card or "")
    if not text:
        return []
    chunks: list[dict[str, Any]] = []
    current_title = "memory_card"
    buffer: list[str] = []

    def add_chunk(fragment: str, category: str = "memory_card"):
        fragment = norm(fragment)
        if not fragment:
            return
        while len(fragment) > 520:
            part = fragment[:520]
            fragment = fragment[520:]
            add_chunk(part, category)
        digest = hashlib.sha1(f"{current_title}\n{fragment}".encode("utf-8", "ignore")).hexdigest()[:8]
        chunks.append({
            "id": f"card_chunk_{len(chunks) + 1:04d}_{digest}",
            "category": category,
            "text": fragment,
            "tags": ["memory_card", current_title],
            "synthetic": True,
            "source": "memory_card.md",
        })

    def flush_buffer():
        nonlocal buffer
        if not buffer:
            return
        joined = norm(" ".join(buffer))
        buffer = []
        if joined:
            add_chunk(joined)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_buffer()
            continue
        header = re.match(r"^#{1,4}\s+(.+)$", line)
        if header:
            flush_buffer()
            current_title = norm(header.group(1)) or "memory_card"
            continue
        bullet = re.match(r"^(?:[-*+]\s+|\d+[.)、]\s*)(.+)$", line)
        if bullet:
            flush_buffer()
            body = norm(bullet.group(1))
            for unit in _mb_v611_split_text_units(body):
                add_chunk(unit, "memory_card_item")
            continue
        units = _mb_v611_split_text_units(line)
        for unit in units:
            # 短碎句合并一点，避免过度切成无上下文的词条；长句直接成 chunk。
            if len(unit) < 80:
                buffer.append(unit)
                if len(" ".join(buffer)) >= 180:
                    flush_buffer()
            else:
                flush_buffer()
                add_chunk(unit)
    flush_buffer()

    if not chunks:
        add_chunk(preview(text, 520))
    return chunks[:max(1, int(limit or 240))]


def _mb_v611_load_import_memory(self, record: dict[str, Any]) -> dict[str, Any]:
    mem = _old_mb_v610_load_import_memory(self, record) if callable(_old_mb_v610_load_import_memory) else {"record": record, "chunks": [], "memory_card": "", "import_id": record.get("import_id")}
    if not mem.get("chunks"):
        import_id = str(mem.get("import_id") or record.get("import_id") or "")
        state = _mb_v069_load_memory_item_state(self, import_id)
        disabled = set(state.get("disabled_chunks") or [])
        chunks = []
        for ch in _mb_v611_synthetic_chunks_from_card(mem.get("memory_card") or ""):
            cid = str(ch.get("id") or "")
            if cid and cid not in disabled:
                chunks.append(ch)
        mem["chunks"] = chunks
        if mem["chunks"]:
            mem["chunks_synthetic"] = True
    return mem


def _mb_v611_import_chunks_for_management(self, import_path: Path, memory_card: str) -> tuple[list[dict[str, Any]], bool]:
    chunks = self.load_json(import_path / "chunks.json", [])
    chunks = chunks if isinstance(chunks, list) else []
    if chunks:
        return chunks, False
    return _mb_v611_synthetic_chunks_from_card(memory_card), True


globals()["_mb_v060_load_import_memory"] = _mb_v611_load_import_memory
globals()["_mb_v610_synthetic_chunks_from_card"] = _mb_v611_synthetic_chunks_from_card
globals()["_mb_v610_import_chunks_for_management"] = _mb_v611_import_chunks_for_management


def _mb_v611_memory_imports_payload(self) -> dict[str, Any]:
    records = self.load_json(self.import_records_file, [])
    records = records if isinstance(records, list) else []
    rows = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        import_id = str(rec.get("import_id") or "")
        if not import_id:
            continue
        import_path = self.import_dir / import_id
        state = _mb_v069_load_memory_item_state(self, import_id)
        marker_disabled = (import_path / "DISABLED").exists()
        memory_card = ""
        try:
            card_path = import_path / "memory_card.md"
            if card_path.exists():
                memory_card = card_path.read_text("utf-8", errors="ignore")
        except Exception:
            memory_card = ""
        chunks, synthetic = _mb_v611_import_chunks_for_management(self, import_path, memory_card)
        disabled = set(state.get("disabled_chunks") or [])
        chunk_items = []
        for idx, ch in enumerate(chunks):
            if not isinstance(ch, dict):
                continue
            cid = str(ch.get("id") or f"chunk_{idx:04d}")
            text_value = str(ch.get("text") or ch.get("content") or "")
            chunk_items.append({
                "id": cid,
                "kind": "chunk",
                "enabled": cid not in disabled,
                "category": ch.get("category") or "fact",
                "tags": ch.get("tags") or [],
                "chars": len(text_value),
                "preview": preview(text_value, 320),
                "synthetic": bool(ch.get("synthetic") or synthetic),
            })
        items = []
        if memory_card:
            items.append({
                "id": "__card__",
                "kind": "memory_card",
                "enabled": bool(state.get("card_enabled", True)),
                "category": "summary",
                "chars": len(memory_card),
                "preview": preview(memory_card, 420),
            })
        items.extend(chunk_items)
        usable, reason = _mb_v068_import_record_usable(self, rec) if "_mb_v068_import_record_usable" in globals() else (not marker_disabled, "ok")
        rows.append({
            "import_id": import_id,
            "mode": rec.get("mode"),
            "status": rec.get("status"),
            "enabled": bool(usable and not marker_disabled and rec.get("status") not in {"disabled", "rolled_back", "failed"}),
            "usable": bool(usable),
            "skip_reason": reason,
            "source_file": rec.get("source_file"),
            "source_origin": rec.get("source_origin"),
            "target_origin": rec.get("target_origin"),
            "target_label": rec.get("target_label"),
            "source_label": rec.get("source_label"),
            "created_at": rec.get("created_at"),
            "updated_at": state.get("updated_at"),
            "chunks_total": len(chunk_items),
            "chunks_enabled": sum(1 for item in chunk_items if item.get("enabled")),
            "chunks_synthetic": bool(synthetic),
            "card_enabled": bool(state.get("card_enabled", True)),
            "items": items,
        })
    return {"ok": True, "version": PLUGIN_VERSION, "imports": rows, "recent_injections": _mb_v060_recent_injections(self, 20) if "_mb_v060_recent_injections" in globals() else []}


MemoryBridgePlugin.memory_imports_payload = _mb_v611_memory_imports_payload


_old_mb_v611_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v611_web_status_payload(self):
    payload = _old_mb_v611_web_status_payload(self) if _old_mb_v611_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["memory_card_chunk_patch"] = {"enabled": True, "version": "0.6.11", "granularity": "fine"}
    payload["webui_chunk_search_patch"] = {"enabled": True, "version": "0.6.12", "ime_safe": True}
    payload["webui_group_avatar_patch"] = {"enabled": True, "version": "0.6.13"}
    payload["webui_sessions_layout_patch"] = {"enabled": True, "version": "0.6.14"}
    payload["webui_memory_test_panel"] = {"enabled": True, "version": "0.6.15"}
    return payload


MemoryBridgePlugin.web_status_payload = _mb_v611_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.16 runtime patch
# 导入包质量诊断：显式展示 memory_card / 原生 chunks / card 拆分 chunks / facts / seed 质量。
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.17"


def _mb_v616_zip_quality_payload(self, path: Path | None, package: dict[str, Any] | None = None) -> dict[str, Any]:
    package = package if isinstance(package, dict) else {}
    quality: dict[str, Any] = {
        "ok": bool(package.get("ok", True)),
        "file": path.name if path else package.get("file", ""),
        "has_memory_card": bool(package.get("memory_card_md")),
        "memory_card_chars": len(package.get("memory_card_md") or ""),
        "native_chunks_count": 0,
        "loaded_chunks_count": len(package.get("chunks") or []),
        "synthetic_chunks_count": 0,
        "seed_count": len(package.get("seed_history") or []),
        "extracted_facts_count": 0,
        "kept_messages_count": 0,
        "warnings": [],
        "notes": [],
    }
    names: set[str] = set()
    try:
        if path and path.exists():
            with zipfile.ZipFile(path, "r") as archive:
                names = set(archive.namelist())
                if "migration/chunks.jsonl" in names:
                    quality["native_chunks_count"] = sum(1 for line in archive.read("migration/chunks.jsonl").decode("utf-8", "ignore").splitlines() if line.strip())
                elif "migration/chunks.json" in names:
                    chunks_json = self.read_json_from_zip(archive, "migration/chunks.json", []) or []
                    quality["native_chunks_count"] = len(chunks_json) if isinstance(chunks_json, list) else 0
                if not quality["has_memory_card"] and "migration/memory_card.md" in names:
                    card_text = archive.read("migration/memory_card.md").decode("utf-8", "ignore")
                    quality["has_memory_card"] = bool(card_text.strip())
                    quality["memory_card_chars"] = len(card_text)
                if quality["seed_count"] <= 0 and "migration/seed_history.json" in names:
                    seed_history = self.read_json_from_zip(archive, "migration/seed_history.json", []) or []
                    quality["seed_count"] = len(seed_history) if isinstance(seed_history, list) else 0
                facts = self.read_json_from_zip(archive, "cleaning/extracted_facts.json", []) or []
                kept = self.read_json_from_zip(archive, "cleaning/kept_messages.json", []) or []
                quality["extracted_facts_count"] = len(facts) if isinstance(facts, list) else 0
                quality["kept_messages_count"] = len(kept) if isinstance(kept, list) else 0
                quality["has_migration_dir"] = any(name.startswith("migration/") for name in names)
                quality["has_chunks_jsonl"] = "migration/chunks.jsonl" in names
                quality["has_chunks_json"] = "migration/chunks.json" in names
    except Exception as error:
        quality["warnings"].append(f"质量诊断读取 zip 失败：{error}")
    if quality["native_chunks_count"] <= 0 and quality["memory_card_chars"] > 0:
        card_source = package.get("memory_card_md") or ""
        if not card_source and path and path.exists():
            try:
                with zipfile.ZipFile(path, "r") as archive:
                    if "migration/memory_card.md" in set(archive.namelist()):
                        card_source = archive.read("migration/memory_card.md").decode("utf-8", "ignore")
            except Exception:
                card_source = ""
        synthetic = _mb_v611_synthetic_chunks_from_card(card_source) if "_mb_v611_synthetic_chunks_from_card" in globals() else []
        quality["synthetic_chunks_count"] = len(synthetic)
        quality["notes"].append("原包没有原生 chunks，将从 memory_card 自动拆分。")
    if quality["memory_card_chars"] <= 0:
        quality["warnings"].append("缺少 memory_card.md，fallback 摘要不可用。")
    if quality["native_chunks_count"] <= 0 and quality["synthetic_chunks_count"] <= 0:
        quality["warnings"].append("没有可用 chunks，长期记忆检索效果会很弱。")
    if quality["extracted_facts_count"] <= 0:
        quality["notes"].append("cleaning/extracted_facts.json 为空或不存在，说明清洗阶段没有抽出结构化事实。")
    if quality["loaded_chunks_count"] != quality["native_chunks_count"] and quality["native_chunks_count"] > 0:
        quality["notes"].append("加载 chunks 数与原生 chunks 数不一致，请检查包结构兼容。")
    score = 0
    if quality["memory_card_chars"] > 0:
        score += 1
    if quality["native_chunks_count"] > 0:
        score += 2
    elif quality["synthetic_chunks_count"] > 0:
        score += 1
    if quality["seed_count"] > 0:
        score += 1
    if quality["extracted_facts_count"] > 0:
        score += 1
    quality["quality_level"] = "good" if score >= 4 else "usable" if score >= 2 else "weak"
    return quality


_old_mb_v616_import_preview_payload = getattr(MemoryBridgePlugin, "import_preview_payload", None)
def _mb_v616_import_preview_payload(self, filename=None):
    payload = _old_mb_v616_import_preview_payload(self, filename) if _old_mb_v616_import_preview_payload else {"ok": False, "error": "import_preview_payload unavailable"}
    if isinstance(payload, dict) and payload.get("ok"):
        path, package = self.load_migration_package(filename)
        payload["quality"] = _mb_v616_zip_quality_payload(self, path, package)
    return payload


MemoryBridgePlugin.import_preview_payload = _mb_v616_import_preview_payload


_old_mb_v616_validate_memory_zip = getattr(MemoryBridgePlugin, "validate_memory_zip", None)
def _mb_v616_validate_memory_zip(self, path: Path) -> dict[str, Any]:
    result = _old_mb_v616_validate_memory_zip(self, path) if _old_mb_v616_validate_memory_zip else {"ok": False, "errors": ["validate_memory_zip unavailable"]}
    if isinstance(result, dict) and result.get("ok"):
        package = {}
        try:
            resolved = self.resolve_export_file(path.name)
            if resolved and resolved.resolve() == Path(path).resolve():
                _p, package = self.load_migration_package(path.name)
        except Exception:
            package = {}
        result["quality"] = _mb_v616_zip_quality_payload(self, path, package)
    return result


MemoryBridgePlugin.validate_memory_zip = _mb_v616_validate_memory_zip


_old_mb_v616_memory_imports_payload = getattr(MemoryBridgePlugin, "memory_imports_payload", None)
def _mb_v616_memory_imports_payload(self) -> dict[str, Any]:
    payload = _old_mb_v616_memory_imports_payload(self) if _old_mb_v616_memory_imports_payload else {"ok": True, "imports": []}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "memory_imports_payload returned non-dict"}
    for item in payload.get("imports") or []:
        if not isinstance(item, dict):
            continue
        import_id = str(item.get("import_id") or "")
        import_path = self.import_dir / import_id
        card_path = import_path / "memory_card.md"
        chunks_path = import_path / "chunks.json"
        memory_card = ""
        try:
            if card_path.exists():
                memory_card = card_path.read_text("utf-8", errors="ignore")
        except Exception:
            memory_card = ""
        chunks = self.load_json(chunks_path, []) if chunks_path.exists() else []
        chunks = chunks if isinstance(chunks, list) else []
        synthetic_count = len([entry for entry in (item.get("items") or []) if isinstance(entry, dict) and entry.get("kind") == "chunk" and entry.get("synthetic")])
        quality = {
            "has_memory_card": bool(memory_card),
            "memory_card_chars": len(memory_card),
            "native_chunks_count": len(chunks),
            "synthetic_chunks_count": synthetic_count,
            "managed_items_count": len(item.get("items") or []),
            "quality_level": "good" if len(chunks) > 0 else "usable" if synthetic_count > 0 or memory_card else "weak",
            "notes": [],
            "warnings": [],
        }
        if len(chunks) <= 0 and synthetic_count > 0:
            quality["notes"].append("该导入使用 memory_card 自动拆分 chunks。")
        if not memory_card:
            quality["warnings"].append("缺少 memory_card。")
        item["quality"] = quality
    payload["version"] = PLUGIN_VERSION
    return payload


MemoryBridgePlugin.memory_imports_payload = _mb_v616_memory_imports_payload


_old_mb_v616_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v616_web_status_payload(self):
    payload = _old_mb_v616_web_status_payload(self) if _old_mb_v616_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["memory_package_quality_patch"] = {"enabled": True, "version": "0.6.16"}
    payload["webui_unified_coordinator"] = {"enabled": True, "version": "0.6.17"}
    return payload


MemoryBridgePlugin.web_status_payload = _mb_v616_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.18 runtime patch
# Manual user aliases for WebUI session cards.
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.18"


def _mb_v618_user_alias_file(self) -> Path:
    return self.export_dir / "user_aliases.json"


def _mb_v618_clean_alias(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ""
    return text[:80]


def _mb_v618_load_user_aliases(self) -> dict[str, Any]:
    data = self.load_json(_mb_v618_user_alias_file(self), {})
    if not isinstance(data, dict):
        data = {}
    aliases = {
        "users": data.get("users") if isinstance(data.get("users"), dict) else {},
        "group_members": data.get("group_members") if isinstance(data.get("group_members"), dict) else {},
        "origins": data.get("origins") if isinstance(data.get("origins"), dict) else {},
        "updated_at": data.get("updated_at", ""),
    }
    return aliases


def _mb_v618_save_user_aliases(self, aliases: dict[str, Any]) -> dict[str, Any]:
    data = aliases if isinstance(aliases, dict) else {}
    data.setdefault("users", {})
    data.setdefault("group_members", {})
    data.setdefault("origins", {})
    data["updated_at"] = now_str()
    self.save_json(_mb_v618_user_alias_file(self), data)
    return data


def _mb_v618_alias_entry_alias(entry: Any) -> str:
    if isinstance(entry, dict):
        return _mb_v618_clean_alias(entry.get("alias") or entry.get("name") or entry.get("nickname"))
    return _mb_v618_clean_alias(entry)


def _mb_v618_find_alias_for_session(self, session: dict[str, Any], aliases: dict[str, Any] | None = None) -> tuple[str, str, str]:
    item = session if isinstance(session, dict) else {}
    aliases = aliases if isinstance(aliases, dict) else _mb_v618_load_user_aliases(self)
    user_id, group_id = _mb_v056_extract_ids_from_session(item) if "_mb_v056_extract_ids_from_session" in globals() else ("", "")
    origin = str(item.get("origin") or "")
    origins = aliases.get("origins") if isinstance(aliases.get("origins"), dict) else {}
    group_members = aliases.get("group_members") if isinstance(aliases.get("group_members"), dict) else {}
    users = aliases.get("users") if isinstance(aliases.get("users"), dict) else {}
    if origin:
        alias = _mb_v618_alias_entry_alias(origins.get(origin))
        if alias:
            return alias, "origin", origin
    if group_id and user_id:
        key = f"{group_id}:{user_id}"
        alias = _mb_v618_alias_entry_alias(group_members.get(key))
        if alias:
            return alias, "group_member", key
    if user_id:
        alias = _mb_v618_alias_entry_alias(users.get(user_id))
        if alias:
            return alias, "user", user_id
    return "", "", ""


def _mb_v618_apply_alias_to_session(self, session: dict[str, Any], aliases: dict[str, Any] | None = None) -> dict[str, Any]:
    item = dict(session or {})
    alias, scope, key = _mb_v618_find_alias_for_session(self, item, aliases)
    if not alias:
        if item.get("original_sender_name"):
            item["sender_name"] = item.get("original_sender_name")
        if item.get("original_display_name"):
            item["display_name"] = item.get("original_display_name")
        for field in ("manual_alias", "alias", "alias_scope", "alias_key"):
            item.pop(field, None)
        return item
    user_id, group_id = _mb_v056_extract_ids_from_session(item) if "_mb_v056_extract_ids_from_session" in globals() else ("", "")
    if item.get("sender_name") and item.get("sender_name") != alias:
        item.setdefault("original_sender_name", item.get("sender_name"))
    if item.get("display_name") and item.get("display_name") != alias:
        item.setdefault("original_display_name", item.get("display_name"))
    item["manual_alias"] = alias
    item["alias"] = alias
    item["alias_scope"] = scope
    item["alias_key"] = key
    item["sender_name"] = alias
    item["nickname"] = alias
    if item.get("category") == "group" or "GroupMessage" in str(item.get("origin")) or group_id:
        group_name = item.get("group_name") or (f"群聊 {group_id}" if group_id else "群聊")
        item["display_name"] = f"{group_name} / {alias}"
    else:
        item["display_name"] = alias
    return item


def _mb_v618_set_user_alias(self, payload: dict[str, Any]) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    origin = str(payload.get("origin") or "").strip()
    parsed = _mb_v056_parse_origin(origin) if "_mb_v056_parse_origin" in globals() else {}
    user_id = str(payload.get("user_id") or parsed.get("user_id") or "").strip()
    group_id = str(payload.get("group_id") or parsed.get("group_id") or "").strip()
    alias = _mb_v618_clean_alias(payload.get("alias"))
    aliases = _mb_v618_load_user_aliases(self)
    users = dict(aliases.get("users") or {})
    group_members = dict(aliases.get("group_members") or {})
    origins = dict(aliases.get("origins") or {})
    target = ""
    if group_id and user_id:
        key = f"{group_id}:{user_id}"
        target = f"group_member:{key}"
        if alias:
            group_members[key] = {"alias": alias, "user_id": user_id, "group_id": group_id, "origin": origin, "updated_at": now_str()}
        else:
            group_members.pop(key, None)
    elif user_id:
        target = f"user:{user_id}"
        if alias:
            users[user_id] = {"alias": alias, "user_id": user_id, "origin": origin, "updated_at": now_str()}
        else:
            users.pop(user_id, None)
    elif origin:
        target = f"origin:{origin}"
        if alias:
            origins[origin] = {"alias": alias, "origin": origin, "updated_at": now_str()}
        else:
            origins.pop(origin, None)
    else:
        return {"ok": False, "error": "missing user_id/group_id/origin"}
    aliases.update({"users": users, "group_members": group_members, "origins": origins})
    aliases = _mb_v618_save_user_aliases(self, aliases)
    try:
        self.append_action_log({"kind": "user_alias_update", "ok": True, "target": target, "alias": alias})
    except Exception:
        pass
    return {"ok": True, "target": target, "alias": alias, "aliases": aliases}


_old_mb_v618_web_sessions_payload = getattr(MemoryBridgePlugin, "web_sessions_payload", None)
def _mb_v618_web_sessions_payload(self):
    data = _old_mb_v618_web_sessions_payload(self) if _old_mb_v618_web_sessions_payload else []
    aliases = _mb_v618_load_user_aliases(self)
    output = []
    for session in data if isinstance(data, list) else []:
        output.append(_mb_v618_apply_alias_to_session(self, session, aliases) if isinstance(session, dict) else session)
    return output


MemoryBridgePlugin.web_sessions_payload = _mb_v618_web_sessions_payload


_old_mb_v618_session_label = getattr(MemoryBridgePlugin, "session_label", None)
def _mb_v618_session_label(self, origin: str) -> str:
    try:
        session = {"origin": origin}
        aliases = _mb_v618_load_user_aliases(self)
        alias, _scope, _key = _mb_v618_find_alias_for_session(self, session, aliases)
        if alias:
            return alias
    except Exception:
        pass
    return _old_mb_v618_session_label(self, origin) if _old_mb_v618_session_label else str(origin or "unknown")


MemoryBridgePlugin.session_label = _mb_v618_session_label


_old_mb_v618_record_session = getattr(MemoryBridgePlugin, "record_session", None)
def _mb_v618_record_session(self, event: AstrMessageEvent):
    if _old_mb_v618_record_session:
        _old_mb_v618_record_session(self, event)
    try:
        aliases = _mb_v618_load_user_aliases(self)
        self.sessions = self.load_json(self.sessions_file, {}) or {}
        if isinstance(self.sessions, dict):
            changed = False
            for origin, session in list(self.sessions.items()):
                if isinstance(session, dict):
                    enriched = _mb_v618_apply_alias_to_session(self, session, aliases)
                    if enriched != session:
                        self.sessions[origin] = enriched
                        changed = True
            if changed:
                self.save_json(self.sessions_file, self.sessions)
    except Exception:
        pass


MemoryBridgePlugin.record_session = _mb_v618_record_session


_old_mb_v618_register_routes = getattr(MemoryBridgeWebAdminServer, "_register_routes", None)
def _mb_v618_register_routes(self):
    if _old_mb_v618_register_routes:
        _old_mb_v618_register_routes(self)

    @self.app.get("/api/user-aliases")
    async def user_aliases_get():
        aliases = _mb_v618_load_user_aliases(self.plugin)
        return {
            "ok": True,
            "aliases": aliases,
            "counts": {
                "users": len(aliases.get("users") or {}),
                "group_members": len(aliases.get("group_members") or {}),
                "origins": len(aliases.get("origins") or {}),
            },
            "updated_at": aliases.get("updated_at", ""),
        }

    @self.app.post("/api/user-aliases")
    async def user_aliases_post(payload: dict[str, Any]):
        result = _mb_v618_set_user_alias(self.plugin, payload)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)


MemoryBridgeWebAdminServer._register_routes = _mb_v618_register_routes


_old_mb_v618_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v618_web_status_payload(self):
    payload = _old_mb_v618_web_status_payload(self) if _old_mb_v618_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["webui_user_alias_patch"] = {"enabled": True, "version": "0.6.18"}
    return payload


MemoryBridgePlugin.web_status_payload = _mb_v618_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.19 runtime patch
# Better synthetic chunk balancing for memory_card fallback.
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.19"


def _mb_v619_sentence_units(text: str, soft_max: int = 220, hard_max: int = 360) -> list[str]:
    text = norm(text or "")
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;])\s*|[\r\n]+", text)
    parts = [norm(part) for part in parts if norm(part)]
    if not parts:
        parts = [text]
    units: list[str] = []
    for part in parts:
        queue = [part]
        while queue:
            piece = norm(queue.pop(0))
            if not piece:
                continue
            if len(piece) <= hard_max:
                units.append(piece)
                continue
            cut_at = -1
            for mark in ["，", ",", "、", " "]:
                pos = piece.rfind(mark, 0, soft_max)
                if pos > cut_at:
                    cut_at = pos
            if cut_at < 80:
                cut_at = soft_max
            left = norm(piece[:cut_at + 1])
            right = norm(piece[cut_at + 1:])
            if left:
                units.append(left)
            if right:
                queue.insert(0, right)
    return units


def _mb_v619_synthetic_chunks_from_card(memory_card: str, limit: int = 240) -> list[dict[str, Any]]:
    text = norm(memory_card or "")
    if not text:
        return []
    chunks: list[dict[str, Any]] = []
    min_chars = 32
    target_chars = 120
    max_chars = 180
    current_title = "memory_card"
    buffer: list[str] = []
    buffer_category = "memory_card"
    buffer_title = current_title

    def emit(fragment: str, category: str, title: str):
        fragment = norm(fragment)
        if not fragment:
            return
        if len(fragment) > max_chars:
            for unit in _mb_v619_sentence_units(fragment, target_chars, max_chars):
                emit(unit, category, title)
            return
        digest = hashlib.sha1(f"{title}\n{fragment}".encode("utf-8", "ignore")).hexdigest()[:8]
        chunks.append({
            "id": f"card_chunk_{len(chunks) + 1:04d}_{digest}",
            "category": category,
            "text": fragment,
            "tags": ["memory_card", title],
            "synthetic": True,
            "source": "memory_card.md",
            "split_policy": "balanced_v0.6.19",
        })

    def flush(force: bool = True):
        nonlocal buffer, buffer_category, buffer_title
        if not buffer:
            return
        joined = norm(" ".join(buffer))
        if force or len(joined) >= min_chars:
            emit(joined, buffer_category, buffer_title)
            buffer = []

    def push(unit: str, category: str = "memory_card"):
        nonlocal buffer, buffer_category, buffer_title
        unit = norm(unit)
        if not unit:
            return
        if buffer and (buffer_title != current_title or buffer_category != category):
            flush(True)
        buffer_category = category
        buffer_title = current_title
        if len(unit) >= max_chars:
            flush(True)
            emit(unit, category, current_title)
            return
        candidate = norm(" ".join(buffer + [unit]))
        if buffer and len(candidate) > max_chars:
            flush(True)
        buffer.append(unit)
        candidate = norm(" ".join(buffer))
        if len(candidate) >= target_chars:
            flush(True)

    for raw_line in text.splitlines():
        line = norm(raw_line.strip())
        if not line:
            flush(False)
            continue
        header = re.match(r"^#{1,4}\s+(.+)$", line)
        if header:
            flush(True)
            current_title = norm(header.group(1)) or "memory_card"
            continue
        bullet = re.match(r"^(?:[-*+]\s+|\d+[.)、]\s*)(.+)$", line)
        body = norm(bullet.group(1)) if bullet else line
        category = "memory_card_item" if bullet else "memory_card"
        for unit in _mb_v619_sentence_units(body, target_chars, max_chars):
            push(unit, category)
    flush(True)

    if len(chunks) >= 2 and len(str(chunks[-1].get("text") or "")) < min_chars:
        last = chunks.pop()
        prev = chunks[-1]
        merged = norm(f"{prev.get('text') or ''} {last.get('text') or ''}")
        if prev.get("tags") == last.get("tags") and prev.get("category") == last.get("category") and len(merged) <= max_chars:
            prev["text"] = merged
            prev["id"] = f"card_chunk_{len(chunks):04d}_{hashlib.sha1((str(prev.get('tags')) + merged).encode('utf-8', 'ignore')).hexdigest()[:8]}"
            prev["split_policy"] = "balanced_v0.6.19"
        else:
            chunks.append(last)

    if not chunks:
        emit(preview(text, max_chars), "memory_card", "memory_card")
    return chunks[:max(1, int(limit or 240))]


_mb_v611_split_text_units = _mb_v619_sentence_units
_mb_v611_synthetic_chunks_from_card = _mb_v619_synthetic_chunks_from_card
globals()["_mb_v611_split_text_units"] = _mb_v619_sentence_units
globals()["_mb_v611_synthetic_chunks_from_card"] = _mb_v619_synthetic_chunks_from_card
globals()["_mb_v610_synthetic_chunks_from_card"] = _mb_v619_synthetic_chunks_from_card


_old_mb_v619_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v619_web_status_payload(self):
    payload = _old_mb_v619_web_status_payload(self) if _old_mb_v619_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["memory_card_chunk_patch"] = {"enabled": True, "version": "0.6.19", "granularity": "balanced", "target_chars": 120, "min_chars": 32, "max_chars": 180}
    return payload


MemoryBridgePlugin.web_status_payload = _mb_v619_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.20 runtime patch
# More sensitive chunk retrieval for short Chinese keywords and phrase variants.
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.20"


def _mb_v620_compact_text(value: Any) -> str:
    text = str(value or "").lower()
    return re.sub(r"[\s，。！？；：、“”‘’（）()\[\]{}<>《》,.!?;:'\"`~|/\\]+", "", text)


def _mb_v620_tokenize(text: str) -> list[str]:
    raw_text = str(text or "").lower()
    stop = {
        "这个", "那个", "然后", "就是", "还是", "如果", "但是", "因为", "所以", "可以", "一个", "一些", "我们", "你们", "他们",
        "请问", "帮我", "继续", "问题", "记忆", "续忆", "导入", "导出", "什么", "怎么", "如何", "一下", "the", "and", "for",
        "with", "from", "this", "that", "you", "are", "was",
    }
    tokens: list[str] = []

    def add(token: str):
        token = str(token or "").strip().lower()
        if not token or token in stop:
            return
        if len(token) < 2 and not re.match(r"[\u4e00-\u9fff]", token):
            return
        tokens.append(token)

    for token in re.findall(r"[a-z0-9_#./\-]{2,}", raw_text, re.I):
        add(token)

    for seq in re.findall(r"[\u4e00-\u9fff]+", raw_text):
        if len(seq) <= 12:
            add(seq)
        max_n = min(6, len(seq))
        for n in range(max_n, 1, -1):
            for i in range(0, len(seq) - n + 1):
                add(seq[i:i + n])
        if len(seq) <= 3:
            for ch in seq:
                add(ch)

    compact = _mb_v620_compact_text(raw_text)
    if 2 <= len(compact) <= 24:
        add(compact)

    seen, dedup = set(), []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            dedup.append(token)
    return dedup[:120]


def _mb_v620_cjk_overlap_score(keyword: str, compact_hay: str) -> float:
    if not keyword or not compact_hay or not re.search(r"[\u4e00-\u9fff]", keyword):
        return 0.0
    chars = [ch for ch in keyword if "\u4e00" <= ch <= "\u9fff"]
    if len(chars) < 2:
        return 0.0
    hit = sum(1 for ch in set(chars) if ch in compact_hay)
    ratio = hit / max(1, len(set(chars)))
    if ratio >= 0.8 and len(chars) >= 3:
        return 1.2
    if ratio >= 0.66 and len(chars) >= 4:
        return 0.6
    return 0.0


def _mb_v620_score_chunk(chunk: dict[str, Any], keywords: list[str]) -> float:
    text_parts = [
        chunk.get("text") or chunk.get("content") or "",
        chunk.get("category") or "",
        " ".join([str(t) for t in (chunk.get("tags") or [])]) if isinstance(chunk.get("tags"), list) else str(chunk.get("tags") or ""),
        chunk.get("id") or "",
    ]
    text = "\n".join(str(part or "") for part in text_parts)
    if not str(chunk.get("text") or chunk.get("content") or "").strip():
        return -999.0
    hay = text.lower()
    compact_hay = _mb_v620_compact_text(text)
    score = 0.0
    matched = 0
    for kw in keywords or []:
        kw = str(kw or "").strip().lower()
        if not kw:
            continue
        compact_kw = _mb_v620_compact_text(kw)
        if kw in hay or (compact_kw and compact_kw in compact_hay):
            matched += 1
            if len(compact_kw) >= 6:
                score += 4.0
            elif len(compact_kw) >= 3:
                score += 2.4
            elif len(compact_kw) == 2:
                score += 1.2
            else:
                score += 0.35
        else:
            overlap = _mb_v620_cjk_overlap_score(compact_kw, compact_hay)
            if overlap > 0:
                matched += 1
                score += overlap
    if (keywords or []) and matched <= 0:
        return 0.0
    if matched >= 2:
        score += min(2.5, matched * 0.25)
    category = str(chunk.get("category") or "")
    if category in {"user_preference", "project_status", "open_task", "memory_card_item"}:
        score += 0.8
    elif category in {"technical_context", "resolved_issue", "memory_card"}:
        score += 0.5
    body_len = len(str(chunk.get("text") or chunk.get("content") or ""))
    if body_len > 900:
        score -= 0.4
    elif body_len <= 220 and score > 0:
        score += 0.2
    return score


_mb_v060_tokenize = _mb_v620_tokenize
_mb_v060_score_chunk = _mb_v620_score_chunk
globals()["_mb_v060_tokenize"] = _mb_v620_tokenize
globals()["_mb_v060_score_chunk"] = _mb_v620_score_chunk


_old_mb_v620_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v620_web_status_payload(self):
    payload = _old_mb_v620_web_status_payload(self) if _old_mb_v620_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["memory_chunk_match_patch"] = {"enabled": True, "version": "0.6.20", "tokenizer": "cjk_ngram_compact", "short_keyword_sensitive": True}
    return payload


MemoryBridgePlugin.web_status_payload = _mb_v620_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.21 runtime patch
# User->assistant dialogue chunks and more forgiving short keyword matching.
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.21"


def _mb_v621_role_kind(role: Any) -> str:
    text = str(role or "").strip().lower()
    if text in {"user", "human", "member", "sender", "用户", "提问者"} or "user" in text:
        return "user"
    if text in {"assistant", "ai", "bot", "robot", "助手", "机器人"} or "assistant" in text or "bot" in text:
        return "assistant"
    return ""


def _mb_v621_msg_content(message: dict[str, Any]) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or message.get("message") or message.get("text") or "").strip()


def _mb_v621_pair_text(user_text: str, assistant_text: str, user_limit: int = 260, assistant_limit: int = 380) -> str:
    return f"user: {preview(user_text, user_limit)}\nassistant: {preview(assistant_text, assistant_limit)}"


def _mb_v621_dialogue_chunks_from_kept(kept: list[dict[str, Any]], limit: int = 220) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    pending_users: list[dict[str, Any]] = []
    for message in kept if isinstance(kept, list) else []:
        if not isinstance(message, dict):
            continue
        role = _mb_v621_role_kind(message.get("role"))
        content = _mb_v621_msg_content(message)
        if not content:
            continue
        if role == "user":
            pending_users.append(message)
            pending_users = pending_users[-3:]
            continue
        if role != "assistant" or not pending_users:
            continue
        user_text = "\n".join(_mb_v621_msg_content(item) for item in pending_users if _mb_v621_msg_content(item))
        assistant_text = content
        source_indices = [item.get("index") for item in pending_users if item.get("index") is not None]
        if message.get("index") is not None:
            source_indices.append(message.get("index"))
        text = _mb_v621_pair_text(user_text, assistant_text)
        digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:8]
        chunks.append({
            "id": f"dialogue_chunk_{len(chunks) + 1:04d}_{digest}",
            "category": "dialogue_pair",
            "text": text,
            "tags": ["dialogue_pair", "user_assistant"],
            "source_indices": source_indices,
            "source": "cleaning/kept_messages.json",
            "chunk_policy": "user_to_assistant_v0.6.21",
        })
        pending_users = []
        if len(chunks) >= limit:
            break
    return chunks


def _mb_v621_fact_chunks_from_facts(facts: list[dict[str, Any]], start: int = 1, limit: int = 120) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for fact in facts[:limit] if isinstance(facts, list) else []:
        if not isinstance(fact, dict):
            continue
        content = str(fact.get("content") or "").strip()
        if not content:
            continue
        category = str(fact.get("type") or "fact")
        prompt = f"关于 {category} 的旧记忆是什么？"
        text = _mb_v621_pair_text(prompt, content, 160, 420)
        digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:8]
        chunks.append({
            "id": f"fact_chunk_{start + len(chunks):04d}_{digest}",
            "category": category,
            "text": text,
            "tags": [category, "fact", "user_assistant_wrapped"],
            "source_indices": fact.get("source_indices") or [],
            "source": "cleaning/extracted_facts.json",
            "chunk_policy": "user_to_assistant_v0.6.21",
        })
    return chunks


def _mb_v621_wrap_synthetic_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    item = dict(chunk or {})
    text = str(item.get("text") or item.get("content") or "").strip()
    if not text or text.lower().lstrip().startswith("user:"):
        item["chunk_policy"] = item.get("chunk_policy") or "user_to_assistant_v0.6.21"
        return item
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    title = str(tags[-1] if tags else item.get("category") or "memory_card")
    item["text"] = _mb_v621_pair_text(f"关于 {title} 的旧记忆是什么？", text, 180, 420)
    item["tags"] = list(tags) + ["user_assistant_wrapped"]
    item["chunk_policy"] = "user_to_assistant_v0.6.21"
    digest = hashlib.sha1(item["text"].encode("utf-8", "ignore")).hexdigest()[:8]
    base = str(item.get("id") or f"card_chunk_{digest}")
    item["id"] = f"{base}_{digest}" if not base.endswith(digest) else base
    return item


def _mb_v621_normalize_user_assistant_chunks(chunks: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks if isinstance(chunks, list) else [], 1):
        if not isinstance(chunk, dict):
            continue
        item = dict(chunk)
        text = str(item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        if text.lower().lstrip().startswith("user:") and "\nassistant:" in text.lower():
            item["chunk_policy"] = item.get("chunk_policy") or "user_to_assistant_v0.6.21"
            output.append(item)
            continue
        category = str(item.get("category") or "memory")
        item["text"] = _mb_v621_pair_text(f"关于 {category} 的旧记忆是什么？", text, 180, 420)
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        item["tags"] = list(tags) + ["user_assistant_wrapped"]
        item["chunk_policy"] = "user_to_assistant_v0.6.21"
        digest = hashlib.sha1(item["text"].encode("utf-8", "ignore")).hexdigest()[:8]
        item["id"] = str(item.get("id") or f"wrapped_chunk_{idx:04d}") + f"_{digest}"
        output.append(item)
    return output


_old_mb_v621_build_migration_payload = getattr(MemoryBridgePlugin, "build_migration_payload", None)
def _mb_v621_build_migration_payload(self, clean_result, manifest):
    payload = _old_mb_v621_build_migration_payload(self, clean_result, manifest) if _old_mb_v621_build_migration_payload else {}
    clean_result = clean_result if isinstance(clean_result, dict) else {}
    kept = clean_result.get("kept_messages") or []
    facts = clean_result.get("extracted_facts") or []
    dialogue_chunks = _mb_v621_dialogue_chunks_from_kept(kept)
    fact_chunks = _mb_v621_fact_chunks_from_facts(facts, len(dialogue_chunks) + 1)
    chunks = dialogue_chunks + fact_chunks
    if chunks:
        payload["chunks"] = chunks
        plan = dict(payload.get("import_plan") or {})
        plan["chunks"] = len(chunks)
        plan["chunk_policy"] = "user_to_assistant_v0.6.21"
        plan["dialogue_chunks"] = len(dialogue_chunks)
        plan["fact_chunks"] = len(fact_chunks)
        payload["import_plan"] = plan
        card_json = dict(payload.get("memory_card_json") or {})
        card_json["chunk_policy"] = plan["chunk_policy"]
        card_json["dialogue_chunks"] = len(dialogue_chunks)
        card_json["fact_chunks"] = len(fact_chunks)
        payload["memory_card_json"] = card_json
    return payload


MemoryBridgePlugin.build_migration_payload = _mb_v621_build_migration_payload


_old_mb_v621_load_migration_package = getattr(MemoryBridgePlugin, "load_migration_package", None)
def _mb_v621_load_migration_package(self, filename=None):
    path, package = _old_mb_v621_load_migration_package(self, filename) if _old_mb_v621_load_migration_package else (None, {"ok": False, "error": "load_migration_package unavailable"})
    if isinstance(package, dict) and package.get("chunks"):
        package = dict(package)
        package["chunks"] = _mb_v621_normalize_user_assistant_chunks(package.get("chunks") or [])
        plan = dict(package.get("import_plan") or {})
        if plan:
            plan["chunks"] = len(package["chunks"])
            plan["chunk_policy"] = "user_to_assistant_v0.6.21"
            package["import_plan"] = plan
    return path, package


MemoryBridgePlugin.load_migration_package = _mb_v621_load_migration_package


def _mb_v621_synthetic_chunks_from_card(memory_card: str, limit: int = 240) -> list[dict[str, Any]]:
    base_fn = globals().get("_mb_v619_synthetic_chunks_from_card")
    chunks = base_fn(memory_card, limit) if callable(base_fn) else []
    return [_mb_v621_wrap_synthetic_chunk(ch) for ch in chunks if isinstance(ch, dict)]


_mb_v611_synthetic_chunks_from_card = _mb_v621_synthetic_chunks_from_card
globals()["_mb_v611_synthetic_chunks_from_card"] = _mb_v621_synthetic_chunks_from_card
globals()["_mb_v610_synthetic_chunks_from_card"] = _mb_v621_synthetic_chunks_from_card


def _mb_v621_relaxed_cjk_score(keyword: str, compact_hay: str) -> float:
    chars = [ch for ch in str(keyword or "") if "\u4e00" <= ch <= "\u9fff"]
    if not chars or not compact_hay:
        return 0.0
    unique = list(dict.fromkeys(chars))
    hit = sum(1 for ch in unique if ch in compact_hay)
    if len(unique) == 1 and hit == 1:
        return 0.55
    if len(unique) == 2 and hit == 2:
        return 0.85
    ratio = hit / max(1, len(unique))
    if ratio >= 0.66:
        return 0.65
    return 0.0


def _mb_v621_score_chunk(chunk: dict[str, Any], keywords: list[str]) -> float:
    base = _mb_v620_score_chunk(chunk, keywords) if "_mb_v620_score_chunk" in globals() else 0.0
    if base > 0 or not keywords:
        return base
    text = "\n".join([
        str(chunk.get("text") or chunk.get("content") or ""),
        str(chunk.get("category") or ""),
        " ".join([str(t) for t in (chunk.get("tags") or [])]) if isinstance(chunk.get("tags"), list) else str(chunk.get("tags") or ""),
    ])
    compact_hay = _mb_v620_compact_text(text) if "_mb_v620_compact_text" in globals() else re.sub(r"\s+", "", text.lower())
    score = 0.0
    for kw in keywords or []:
        compact_kw = _mb_v620_compact_text(kw) if "_mb_v620_compact_text" in globals() else str(kw or "").lower()
        score += _mb_v621_relaxed_cjk_score(compact_kw, compact_hay)
    if score > 0:
        if str(chunk.get("category") or "") in {"dialogue_pair", "memory_card_item"}:
            score += 0.35
        return min(score, 3.0)
    return 0.0


_mb_v060_score_chunk = _mb_v621_score_chunk
globals()["_mb_v060_score_chunk"] = _mb_v621_score_chunk


_old_mb_v621_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v621_web_status_payload(self):
    payload = _old_mb_v621_web_status_payload(self) if _old_mb_v621_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["memory_dialogue_chunk_patch"] = {"enabled": True, "version": "0.6.21", "policy": "chunks start with user and end with assistant"}
    payload["memory_chunk_match_patch"] = {"enabled": True, "version": "0.6.21", "short_keyword_sensitive": True, "relaxed_cjk_overlap": True}
    return payload


MemoryBridgePlugin.web_status_payload = _mb_v621_web_status_payload


# ---------------------------------------------------------------------------
# v0.6.22 runtime patch
# Full chunk preview API for WebUI memory injection management.
# ---------------------------------------------------------------------------
PLUGIN_VERSION = "0.6.22"


def _mb_v622_import_path(self, import_id: str) -> Path | None:
    import_id = str(import_id or "").strip()
    if not import_id or not re.match(r"^[A-Za-z0-9_.-]+$", import_id):
        return None
    path = self.import_dir / import_id
    try:
        root = self.import_dir.resolve()
        resolved = path.resolve()
        if not str(resolved).startswith(str(root)):
            return None
    except Exception:
        return None
    return path if path.exists() and path.is_dir() else None


def _mb_v622_memory_item_detail_payload(self, import_id: str, item_id: str) -> dict[str, Any]:
    import_id = str(import_id or "").strip()
    item_id = str(item_id or "").strip()
    import_path = _mb_v622_import_path(self, import_id)
    if not import_path:
        return {"ok": False, "error": "import not found"}
    state = _mb_v069_load_memory_item_state(self, import_id) if "_mb_v069_load_memory_item_state" in globals() else {}
    disabled = set(state.get("disabled_chunks") or [])
    if item_id in {"__card__", "memory_card"}:
        card_path = import_path / "memory_card.md"
        text = ""
        try:
            text = card_path.read_text("utf-8", errors="ignore") if card_path.exists() else ""
        except Exception:
            text = ""
        return {
            "ok": True,
            "import_id": import_id,
            "item": {
                "id": "__card__",
                "kind": "memory_card",
                "enabled": bool(state.get("card_enabled", True)),
                "category": "summary",
                "chars": len(text),
                "text": text,
                "preview": preview(text, 420),
            },
        }
    memory_card = ""
    try:
        card_path = import_path / "memory_card.md"
        if card_path.exists():
            memory_card = card_path.read_text("utf-8", errors="ignore")
    except Exception:
        memory_card = ""
    chunks_fn = globals().get("_mb_v611_import_chunks_for_management") or globals().get("_mb_v610_import_chunks_for_management")
    if callable(chunks_fn):
        chunks, synthetic = chunks_fn(self, import_path, memory_card)
    else:
        chunks = self.load_json(import_path / "chunks.json", [])
        chunks = chunks if isinstance(chunks, list) else []
        synthetic = False
    for idx, ch in enumerate(chunks if isinstance(chunks, list) else []):
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or f"chunk_{idx:04d}")
        if cid != item_id:
            continue
        text = str(ch.get("text") or ch.get("content") or "")
        return {
            "ok": True,
            "import_id": import_id,
            "item": {
                "id": cid,
                "kind": "chunk",
                "enabled": cid not in disabled,
                "category": ch.get("category") or "fact",
                "tags": ch.get("tags") or [],
                "chars": len(text),
                "text": text,
                "preview": preview(text, 420),
                "synthetic": bool(ch.get("synthetic") or synthetic),
                "source": ch.get("source") or "",
                "source_indices": ch.get("source_indices") or [],
                "chunk_policy": ch.get("chunk_policy") or ch.get("split_policy") or "",
            },
        }
    return {"ok": False, "error": "item not found"}


_old_mb_v622_register_routes = getattr(MemoryBridgeWebAdminServer, "_register_routes", None)
def _mb_v622_register_routes(self):
    if _old_mb_v622_register_routes:
        _old_mb_v622_register_routes(self)

    @self.app.get("/api/memory-injection/imports/{import_id}/items/{item_id}")
    async def memory_injection_item_detail(import_id: str, item_id: str):
        result = _mb_v622_memory_item_detail_payload(self.plugin, import_id, item_id)
        return JSONResponse(result, status_code=200 if result.get("ok") else 404)


MemoryBridgeWebAdminServer._register_routes = _mb_v622_register_routes


_old_mb_v622_web_status_payload = getattr(MemoryBridgePlugin, "web_status_payload", None)
def _mb_v622_web_status_payload(self):
    payload = _old_mb_v622_web_status_payload(self) if _old_mb_v622_web_status_payload else {"ok": True}
    payload["version"] = PLUGIN_VERSION
    payload["webui_chunk_preview_patch"] = {"enabled": True, "version": "0.6.22"}
    return payload


MemoryBridgePlugin.web_status_payload = _mb_v622_web_status_payload
