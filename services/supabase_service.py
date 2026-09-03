"""Supabase Auth 与云端数据访问层（任务 12B）。

本模块只负责两件事：

* 用 Supabase Auth 建立/恢复/注销用户会话；
* 通过 publishable key + 当前用户 JWT 访问 RLS 保护的数据，或在服务端
  明确配置 ``SUPABASE_SERVICE_ROLE_KEY`` 时执行管理员导入。

Streamlit 页面不应直接拼接 REST 请求，也不应把 service-role key 放入页面、
浏览器或客户端代码。所有字段都经过白名单过滤，避免把原始工作簿中的任意
列名变成数据库列名。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse


class SupabaseConfigurationError(RuntimeError):
    """Supabase 运行时配置缺失或不安全。"""


class SupabaseAuthError(RuntimeError):
    """认证失败。"""


class SupabaseDataError(RuntimeError):
    """云端数据读写失败。"""


@dataclass(frozen=True)
class SupabaseConfig:
    """客户端配置。

    ``publishable_key`` 可用于用户端 Data API；``service_role_key`` 只在
    Streamlit 服务端管理员导入路径使用，绝不会返回给页面代码。
    """

    url: str
    publishable_key: str
    service_role_key: str | None = None

    @property
    def has_service_role(self) -> bool:
        return bool(self.service_role_key)

    @classmethod
    def from_sources(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        secrets: Mapping[str, Any] | None = None,
    ) -> "SupabaseConfig | None":
        """从环境变量或 Streamlit secrets 读取配置。

        支持顶层键以及 ``[supabase]`` 嵌套键。未配置任何 Supabase 值时
        返回 ``None``，使旧版本地 SQLite 模式继续可用；只配置一半时抛出
        明确错误，避免应用悄悄回退到错误的数据源。
        """

        env = dict(os.environ if environ is None else environ)
        candidates: list[Mapping[str, Any]] = []
        if secrets is not None:
            candidates.append(secrets)
            nested = _mapping_get(secrets, "supabase")
            if isinstance(nested, Mapping):
                candidates.insert(0, nested)
        candidates.append(env)

        url = _first_value(candidates, "SUPABASE_URL", "SUPABASE_PROJECT_URL")
        publishable = _first_value(
            candidates,
            "SUPABASE_PUBLISHABLE_KEY",
            "SUPABASE_ANON_KEY",
            "SUPABASE_KEY",
        )
        service_role = _first_value(
            candidates,
            "SUPABASE_SERVICE_ROLE_KEY",
            "SUPABASE_SECRET_KEY",
        )

        if not url and not publishable and not service_role:
            return None
        if not url or not publishable:
            raise SupabaseConfigurationError(
                "Supabase 配置不完整：需要 SUPABASE_URL 与 "
                "SUPABASE_PUBLISHABLE_KEY（或兼容的 SUPABASE_ANON_KEY）。"
            )

        url = str(url).strip().rstrip("/")
        if not _is_http_url(url):
            raise SupabaseConfigurationError(
                "SUPABASE_URL 必须是 http/https 地址。"
            )
        publishable = str(publishable).strip()
        if _jwt_role(publishable) == "service_role" or publishable.startswith(
            ("sb_secret_", "service_role")
        ):
            raise SupabaseConfigurationError(
                "SUPABASE_PUBLISHABLE_KEY 不能填写 service-role/secret key。"
            )
        if service_role:
            service_role = str(service_role).strip()
            if _jwt_role(service_role) not in (None, "service_role") and not service_role.startswith(
                ("sb_secret_", "service_role")
            ):
                # 新旧密钥格式都允许；只有明确识别为普通 publishable JWT
                # 时才拒绝，避免误判新格式。
                raise SupabaseConfigurationError(
                    "SUPABASE_SERVICE_ROLE_KEY 格式看起来不是服务端密钥。"
                )
        return cls(url=url, publishable_key=publishable, service_role_key=service_role)


@dataclass(frozen=True)
class AuthUser:
    """页面需要的最小用户身份，不保存 user_metadata 作为权限依据。"""

    id: str
    email: str | None = None


@dataclass(frozen=True)
class AuthSession:
    """可放入 Streamlit session_state 的认证会话。"""

    access_token: str
    refresh_token: str
    expires_at: int | None
    user: AuthUser

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "user": {"id": self.user.id, "email": self.user.email},
        }


def _mapping_get(value: Mapping[str, Any], key: str) -> Any:
    try:
        return value.get(key)
    except AttributeError:
        try:
            return value[key]
        except (KeyError, TypeError):
            return None


def _first_value(sources: Sequence[Mapping[str, Any]], *keys: str) -> Any:
    for source in sources:
        for key in keys:
            value = _mapping_get(source, key)
            if value is not None and str(value).strip():
                return value
    return None


def _is_http_url(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _jwt_role(value: str) -> str | None:
    """读取 JWT role 仅用于阻止把 service key 当 publishable key。"""

    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    role = payload.get("role")
    return str(role) if role is not None else None


def create_supabase_client(
    config: SupabaseConfig,
    *,
    service_role: bool = False,
    factory: Callable[[str, str], Any] | None = None,
) -> Any:
    """创建 Supabase client；依赖延迟导入，便于本地 SQLite 测试。"""

    if service_role and not config.service_role_key:
        raise SupabaseConfigurationError(
            "云端 Excel 导入需要服务端 SUPABASE_SERVICE_ROLE_KEY；"
            "该密钥只能放在 Streamlit Secrets，不能放入代码或浏览器。"
        )
    key = config.service_role_key if service_role else config.publishable_key
    if factory is not None:
        return factory(config.url, str(key))
    try:
        from supabase import create_client
    except ImportError as exc:  # pragma: no cover - 依赖安装错误才会触发
        raise SupabaseConfigurationError(
            "未安装 supabase Python 客户端，请运行 pip install -r requirements.txt。"
        ) from exc
    return create_client(config.url, str(key))


def _attr(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _normalize_user(value: Any) -> AuthUser:
    user_id = _attr(value, "id")
    if not user_id:
        raise SupabaseAuthError("Supabase 返回的用户身份无效。")
    return AuthUser(id=str(user_id), email=_attr(value, "email"))


def _normalize_session(response: Any) -> AuthSession:
    session = _attr(response, "session")
    if session is None and isinstance(response, Mapping):
        session = response.get("data", {}).get("session")
    user = _attr(response, "user") or _attr(session, "user")
    if user is None and isinstance(response, Mapping):
        user = response.get("data", {}).get("user")
    if session is None or user is None:
        raise SupabaseAuthError(
            "登录成功但没有可用会话。请检查 Supabase Auth 的邮箱确认设置。"
        )
    access = _attr(session, "access_token")
    refresh = _attr(session, "refresh_token")
    if not access or not refresh:
        raise SupabaseAuthError("Supabase 返回的会话令牌不完整。")
    expires_at = _attr(session, "expires_at")
    return AuthSession(
        access_token=str(access),
        refresh_token=str(refresh),
        expires_at=int(expires_at) if expires_at is not None else None,
        user=_normalize_user(user),
    )


def sign_in(client: Any, email: str, password: str) -> AuthSession:
    """使用邮箱密码登录，不把底层异常原文展示给用户。"""

    email = str(email or "").strip()
    if not email or "@" not in email:
        raise SupabaseAuthError("请输入有效的邮箱地址。")
    if not password:
        raise SupabaseAuthError("请输入密码。")
    try:
        response = client.auth.sign_in_with_password(
            {"email": email, "password": str(password)}
        )
        return _normalize_session(response)
    except SupabaseAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - 第三方异常统一转为安全提示
        raise SupabaseAuthError("登录失败，请检查邮箱、密码或邮箱确认状态。") from exc


def sign_up(client: Any, email: str, password: str) -> AuthSession | None:
    """注册用户；邮箱确认开启时 Supabase 会返回空 session。"""

    email = str(email or "").strip()
    if not email or "@" not in email:
        raise SupabaseAuthError("请输入有效的邮箱地址。")
    if len(str(password or "")) < 8:
        raise SupabaseAuthError("密码至少需要 8 个字符。")
    try:
        response = client.auth.sign_up(
            {"email": email, "password": str(password)}
        )
        if _attr(response, "session") is None and _attr(response, "user") is None:
            data = _attr(response, "data", {})
            if isinstance(data, Mapping) and not data.get("session"):
                return None
        try:
            return _normalize_session(response)
        except SupabaseAuthError:
            return None
    except SupabaseAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SupabaseAuthError("注册失败，请检查邮箱是否已注册或密码是否符合要求。") from exc


def restore_session(client: Any, state: Mapping[str, Any] | None) -> AuthSession | None:
    """在 Streamlit rerun 中恢复会话；失效令牌会安全地清空。"""

    if not isinstance(state, Mapping):
        return None
    access = state.get("access_token")
    refresh = state.get("refresh_token")
    if not access or not refresh:
        return None
    try:
        response = client.auth.set_session(str(access), str(refresh))
        return _normalize_session(response)
    except Exception:  # noqa: BLE001 - 失效会话按未登录处理
        return None


def sign_out(client: Any) -> None:
    try:
        client.auth.sign_out()
    except Exception as exc:  # noqa: BLE001
        raise SupabaseAuthError("退出登录失败，请稍后重试。") from exc


def _execute(builder: Any, operation: str) -> list[dict[str, Any]]:
    try:
        response = builder.execute()
    except Exception as exc:  # noqa: BLE001
        raise SupabaseDataError(f"云端{operation}失败，请稍后重试。") from exc
    data = _attr(response, "data", response)
    if data is None:
        return []
    if isinstance(data, Mapping):
        return [dict(data)]
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        return [dict(item) if isinstance(item, Mapping) else item for item in data]
    return []


def _chain(builder: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    fn = getattr(builder, method, None)
    return fn(*args, **kwargs) if callable(fn) else builder


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: object, *, max_length: int = 5000) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:max_length] if text else None


STATUS_VALUES = frozenset(
    {
        "discovered",
        "shortlisted",
        "opened",
        "applying",
        "applied",
        "assessment",
        "interview",
        "offer",
        "rejected",
        "withdrawn",
    }
)
PRIORITY_VALUES = frozenset({"high", "medium", "low"})
EVENT_TYPES = frozenset(
    {
        "created",
        "stage_changed",
        "note",
        "assessment",
        "interview",
        "offer",
        "rejected",
        "withdrawn",
        "other",
    }
)
_OPENABLE = frozenset({"discovered", "shortlisted"})
_NON_DOWNGRADE = frozenset(
    {"assessment", "interview", "offer", "rejected", "withdrawn"}
)


class SupabaseDataService:
    """对 12A 五张表提供最小、可测试的云端访问接口。"""

    def __init__(self, client: Any, *, admin_client: Any | None = None):
        self.client = client
        self.admin_client = admin_client

    def _table(self, name: str, *, admin: bool = False) -> Any:
        client = self.admin_client if admin else self.client
        if client is None:
            raise SupabaseConfigurationError(
                "此操作需要服务端管理员客户端，但未配置 service-role key。"
            )
        try:
            return client.table(name)
        except Exception as exc:  # noqa: BLE001
            raise SupabaseDataError(f"无法访问云端表 {name}。") from exc

    def upsert_profile(self, user_id: str, display_name: str | None = None) -> dict[str, Any]:
        """创建或更新当前用户资料；不接受任意 app_metadata 权限字段。"""

        if not user_id:
            raise SupabaseDataError("缺少当前用户 ID。")
        payload: dict[str, Any] = {"user_id": str(user_id)}
        name = _clean_text(display_name, max_length=100)
        if name is not None:
            payload["display_name"] = name
        rows = _execute(
            self._table("profiles").upsert(payload, on_conflict="user_id"),
            "用户资料保存",
        )
        return rows[0] if rows else payload

    def list_opportunities(self) -> list[dict[str, Any]]:
        query = self._table("opportunities").select("*")
        query = _chain(query, "order", "company_name", desc=False)
        query = _chain(query, "order", "display_title", desc=False)
        return _execute(query, "机会读取")

    def list_dedupe_keys(self) -> set[str]:
        rows = _execute(self._table("opportunities").select("dedupe_key"), "去重键读取")
        return {str(row.get("dedupe_key")) for row in rows if row.get("dedupe_key")}

    def list_applications(self) -> list[dict[str, Any]]:
        query = self._table("applications").select("*")
        query = _chain(query, "order", "updated_at", desc=True)
        return _execute(query, "申请记录读取")

    def list_application_events(self, application_id: str | None = None) -> list[dict[str, Any]]:
        query = self._table("application_events").select("*")
        if application_id:
            query = _chain(query, "eq", "application_id", str(application_id))
        query = _chain(query, "order", "occurred_at", desc=True)
        return _execute(query, "申请时间线读取")

    def create_application(
        self,
        *,
        company_name: str,
        job_title: str,
        opportunity_id: str | None = None,
        application_url: str | None = None,
        status: str = "discovered",
        current_stage: str = "saved",
        priority: str = "medium",
        notes: str | None = None,
        next_action: str | None = None,
        next_action_at: str | None = None,
    ) -> dict[str, Any]:
        company = _clean_text(company_name, max_length=300)
        title = _clean_text(job_title, max_length=500)
        if not company or not title:
            raise SupabaseDataError("公司名称和岗位名称不能为空。")
        if status not in STATUS_VALUES:
            raise SupabaseDataError("无效的申请状态。")
        if priority not in PRIORITY_VALUES:
            raise SupabaseDataError("无效的申请优先级。")
        stage = _clean_text(current_stage, max_length=120)
        if not stage:
            raise SupabaseDataError("当前流程步骤不能为空。")
        payload: dict[str, Any] = {
            "company_name": company,
            "job_title": title,
            "status": status,
            "current_stage": stage,
            "priority": priority,
        }
        if opportunity_id:
            payload["opportunity_id"] = str(opportunity_id)
        if application_url is not None:
            if application_url and not _is_http_url(application_url):
                raise SupabaseDataError("申请链接必须是 http/https 地址。")
            payload["application_url"] = _clean_text(application_url, max_length=2000)
        for key, value, limit in (
            ("notes", notes, 5000),
            ("next_action", next_action, 500),
            ("next_action_at", next_action_at, 100),
        ):
            clean = _clean_text(value, max_length=limit)
            if clean is not None:
                payload[key] = clean
        rows = _execute(self._table("applications").insert(payload), "申请记录创建")
        if not rows:
            raise SupabaseDataError("云端没有返回新建申请记录。")
        app = rows[0]
        app_id = app.get("id")
        if app_id:
            try:
                self.append_application_event(
                    str(app_id), event_type="created", to_stage=stage
                )
            except SupabaseDataError:
                # 申请主记录已经成功；时间线失败应提示但不能伪造成功。
                app["timeline_warning"] = "申请已保存，但创建时间线失败。"
        return app

    def update_application(self, application_id: str, **updates: Any) -> dict[str, Any]:
        """仅允许更新申请业务字段，禁止修改 user_id / 审计字段。"""

        allowed = {
            "company_name",
            "job_title",
            "application_url",
            "status",
            "current_stage",
            "priority",
            "notes",
            "next_action",
            "next_action_at",
            "applied_at",
        }
        payload = {key: value for key, value in updates.items() if key in allowed}
        if "status" in payload and payload["status"] not in STATUS_VALUES:
            raise SupabaseDataError("无效的申请状态。")
        if "priority" in payload and payload["priority"] not in PRIORITY_VALUES:
            raise SupabaseDataError("无效的申请优先级。")
        if "application_url" in payload and payload["application_url"]:
            if not _is_http_url(payload["application_url"]):
                raise SupabaseDataError("申请链接必须是 http/https 地址。")
        if "current_stage" in payload:
            payload["current_stage"] = _clean_text(payload["current_stage"], max_length=120)
            if not payload["current_stage"]:
                raise SupabaseDataError("当前流程步骤不能为空。")
        for key, limit in (
            ("company_name", 300),
            ("job_title", 500),
            ("notes", 5000),
            ("next_action", 500),
            ("next_action_at", 100),
        ):
            if key in payload:
                cleaned = _clean_text(payload[key], max_length=limit)
                if key in {"company_name", "job_title"} and cleaned is None:
                    raise SupabaseDataError(f"{key} 不能为空。")
                payload[key] = cleaned
        if "application_url" in payload:
            payload["application_url"] = _clean_text(
                payload["application_url"], max_length=2000
            )
        if not payload:
            raise SupabaseDataError("没有可更新的申请字段。")
        if payload.get("status") == "applied" and "applied_at" not in payload:
            payload["applied_at"] = _iso_now()
        rows = _execute(
            self._table("applications").update(payload).eq("id", str(application_id)),
            "申请记录更新",
        )
        return rows[0] if rows else {"id": str(application_id), **payload}

    def mark_application_opened(self, application_id: str) -> dict[str, Any]:
        rows = _execute(
            self._table("applications").select("*").eq("id", str(application_id)),
            "申请记录读取",
        )
        if not rows:
            return {"action": "not_found", "should_open": False, "url": None}
        app = rows[0]
        url = app.get("application_url")
        if not _is_http_url(url):
            return {
                "action": "no_link",
                "should_open": False,
                "url": None,
                "status": app.get("status", ""),
            }
        current = str(app.get("status") or "discovered")
        if current not in _OPENABLE:
            return {
                "action": "opened_without_status_change",
                "should_open": True,
                "url": url,
                "status": current,
            }
        updated = self.update_application(str(application_id), status="opened")
        return {
            "action": "opened",
            "should_open": True,
            "url": url,
            "status": updated.get("status", "opened"),
        }

    def confirm_application_applied(self, application_id: str) -> dict[str, Any]:
        rows = _execute(
            self._table("applications").select("status").eq("id", str(application_id)),
            "申请状态读取",
        )
        if not rows:
            return {"action": "not_found", "status": ""}
        current = str(rows[0].get("status") or "discovered")
        if current == "applied" or current in _NON_DOWNGRADE:
            return {"action": "no_change", "status": current}
        updated = self.update_application(
            str(application_id), status="applied", applied_at=_iso_now()
        )
        return {"action": "applied", "status": updated.get("status", "applied")}

    def append_application_event(
        self,
        application_id: str,
        *,
        event_type: str = "stage_changed",
        from_stage: str | None = None,
        to_stage: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            raise SupabaseDataError("无效的申请事件类型。")
        payload: dict[str, Any] = {
            "application_id": str(application_id),
            "event_type": event_type,
        }
        for key, value, limit in (
            ("from_stage", from_stage, 120),
            ("to_stage", to_stage, 120),
            ("note", note, 5000),
        ):
            clean = _clean_text(value, max_length=limit)
            if clean is not None:
                payload[key] = clean
        rows = _execute(
            self._table("application_events").insert(payload), "申请时间线追加"
        )
        if not rows:
            raise SupabaseDataError("云端没有返回新建时间线事件。")
        return rows[0]

    def delete_application(self, application_id: str) -> None:
        """删除当前用户的申请及其时间线（RLS 只允许本人）。"""

        _execute(
            self._table("applications").delete().eq("id", str(application_id)),
            "申请记录删除",
        )

    def change_application_stage(
        self, application: Mapping[str, Any], new_stage: str, *, note: str | None = None
    ) -> dict[str, Any]:
        application_id = str(application.get("id") or "")
        stage = _clean_text(new_stage, max_length=120)
        if not application_id or not stage:
            raise SupabaseDataError("申请 ID 和流程步骤不能为空。")
        old_stage = _clean_text(application.get("current_stage"), max_length=120)
        updated = self.update_application(application_id, current_stage=stage)
        try:
            self.append_application_event(
                application_id,
                event_type="stage_changed",
                from_stage=old_stage,
                to_stage=stage,
                note=note,
            )
        except SupabaseDataError:
            updated["timeline_warning"] = "步骤已更新，但时间线追加失败。"
        return updated

    def import_records_as_admin(
        self,
        records: Sequence[Mapping[str, Any]],
        classification: Mapping[str, Any],
        *,
        source_filename: str,
        created_by: str,
        file_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        """以管理员服务端路径写入 import_batches + opportunities。

        只接受分类结果中的 ``new`` 项，unknown / pending / invalid 永远不写入。
        原始文件本身不上传 Storage；只保存 SHA-256 元数据，避免把真实工作簿
        永久存进云端。若将来需要文件归档，应另建私有 Storage bucket 与 RLS。
        """

        if self.admin_client is None:
            raise SupabaseConfigurationError(
                "云端 Excel 导入需要 SUPABASE_SERVICE_ROLE_KEY（只放在服务端 Secrets）。"
            )
        filename = Path(str(source_filename or "upload.xlsx")).name[:255]
        if not filename:
            filename = "upload.xlsx"
        counts = dict(classification.get("counts") or {})
        new_items = list((classification.get("items") or {}).get("new") or [])
        payloads = [
            cloud_opportunity_payload(item.get("prepared") or item.get("record") or {})
            for item in new_items
        ]
        payloads = [item for item in payloads if item is not None]
        batch_payload: dict[str, Any] = {
            "created_by": str(created_by),
            "source_filename": filename,
            "file_sha256": hashlib.sha256(file_bytes).hexdigest() if file_bytes is not None else None,
            "status": "processing",
            "total_rows": int(classification.get("total") or len(records)),
            "imported_rows": 0,
            "duplicate_rows": int(counts.get("duplicate", 0)),
            "invalid_rows": int(counts.get("invalid", 0)),
            "pending_rows": int(counts.get("pending", 0)),
        }
        batch_payload = {k: v for k, v in batch_payload.items() if v is not None}
        batch_rows = _execute(
            self._table("import_batches", admin=True).insert(batch_payload),
            "导入批次创建",
        )
        if not batch_rows or not batch_rows[0].get("id"):
            raise SupabaseDataError("云端没有返回导入批次 ID。")
        batch_id = str(batch_rows[0]["id"])
        for row in payloads:
            row["import_batch_id"] = batch_id
        try:
            inserted = 0
            # 小批次写入，避免一次请求超过 Data API 负载限制。
            for start in range(0, len(payloads), 100):
                chunk = payloads[start : start + 100]
                if chunk:
                    inserted += len(
                        _execute(
                            self._table("opportunities", admin=True).insert(chunk),
                            "机会导入",
                        )
                    )
            completed = {
                "status": "completed",
                "imported_rows": inserted,
                "completed_at": _iso_now(),
            }
            _execute(
                self._table("import_batches", admin=True)
                .update(completed)
                .eq("id", batch_id),
                "导入批次完成",
            )
        except Exception:
            # 尝试写入失败状态；原始异常继续向上抛出，页面不会显示“导入成功”。
            try:
                _execute(
                    self._table("import_batches", admin=True)
                    .update({"status": "failed", "completed_at": _iso_now()})
                    .eq("id", batch_id),
                    "导入批次失败标记",
                )
            except Exception:
                pass
            raise
        return {
            "batch_id": batch_id,
            "inserted": inserted,
            "total": int(classification.get("total") or len(records)),
            "items": classification.get("items") or {},
            "counts": {
                "new": inserted,
                "duplicate": int(counts.get("duplicate", 0)),
                "invalid": int(counts.get("invalid", 0)),
                "pending": int(counts.get("pending", 0)),
            },
        }


_CLOUD_OPPORTUNITY_FIELDS = frozenset(
    {
        "record_type",
        "display_title",
        "job_title",
        "job_categories",
        "company_name",
        "industry",
        "recruitment_type",
        "target_cohort",
        "education_requirement",
        "location",
        "deadline",
        "announcement_title",
        "announcement_url",
        "application_url",
        "source_sheet",
        "source_row",
        "import_batch_id",
        "dedupe_key",
        "raw_data",
    }
)


def cloud_opportunity_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """把本地标准记录转换为 12A cloud opportunities 白名单字段。"""

    if str(record.get("record_type") or "") not in {"campaign", "job"}:
        return None
    company = _clean_text(record.get("company_name"), max_length=300)
    title = _clean_text(record.get("display_title"), max_length=500)
    if not company or not title:
        return None
    payload = {
        key: record.get(key)
        for key in _CLOUD_OPPORTUNITY_FIELDS
        if key in record and key not in {"raw_data"}
    }
    payload["company_name"] = company
    payload["display_title"] = title
    payload["raw_data"] = (
        dict(record.get("raw_data"))
        if isinstance(record.get("raw_data"), Mapping)
        else {}
    )
    for key in ("announcement_url", "application_url"):
        if payload.get(key) and not _is_http_url(payload[key]):
            payload[key] = None
    if payload.get("source_row") is not None:
        try:
            payload["source_row"] = int(payload["source_row"])
        except (TypeError, ValueError):
            return None
    return payload


def overlay_user_applications(
    opportunities: Sequence[Mapping[str, Any]],
    applications: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """把当前用户的申请状态覆盖到全局机会目录，不修改全局 opportunity。"""

    by_opp: dict[str, Mapping[str, Any]] = {}
    for app in applications:
        opp_id = app.get("opportunity_id")
        if opp_id is not None:
            by_opp[str(opp_id)] = app
    result: list[dict[str, Any]] = []
    for opportunity in opportunities:
        row = dict(opportunity)
        app = by_opp.get(str(opportunity.get("id")))
        if app:
            row.update(
                {
                    "status": app.get("status", "discovered"),
                    "priority": app.get("priority", "low"),
                    "notes": app.get("notes"),
                    "application_id": app.get("id"),
                    "current_stage": app.get("current_stage", "saved"),
                    "next_action": app.get("next_action"),
                    "next_action_at": app.get("next_action_at"),
                    "applied_at": app.get("applied_at"),
                }
            )
        else:
            row.setdefault("status", "discovered")
            row.setdefault("priority", "low")
        result.append(row)
    return result


__all__ = [
    "AuthSession",
    "AuthUser",
    "EVENT_TYPES",
    "PRIORITY_VALUES",
    "STATUS_VALUES",
    "SupabaseAuthError",
    "SupabaseConfig",
    "SupabaseConfigurationError",
    "SupabaseDataError",
    "SupabaseDataService",
    "cloud_opportunity_payload",
    "create_supabase_client",
    "overlay_user_applications",
    "restore_session",
    "sign_in",
    "sign_out",
    "sign_up",
]
