"""Streamlit 端 Supabase 登录控件（任务 12B）。

没有 Supabase 配置时返回 ``None``，页面保持原有本地 SQLite 模式；配置完整
后，所有云端页面都要求登录。会话令牌只存于当前 Streamlit server session，
不写文件、不进入 URL、不显示给用户。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

import streamlit as st

from services.supabase_service import (
    AuthSession,
    AuthUser,
    SupabaseConfig,
    SupabaseAuthError,
    SupabaseConfigurationError,
    SupabaseDataError,
    SupabaseDataService,
    create_supabase_client,
    restore_session,
    sign_in,
    sign_out,
    sign_up,
)


@dataclass(frozen=True)
class AuthContext:
    """页面使用的认证上下文。``configured`` 区分未配置与未登录。"""

    config: SupabaseConfig
    client: Any
    user: AuthUser | None

    @property
    def configured(self) -> bool:
        return True


def _read_streamlit_secrets() -> Mapping[str, Any] | None:
    try:
        value = st.secrets
        # Streamlit 在没有 secrets.toml 时仍返回一个 Mapping-like 对象，
        # 但第一次读取/转 dict 才会抛 StreamlitSecretNotFoundError。
        if isinstance(value, Mapping):
            return dict(value)
    except Exception:
        return None
    return None


def load_runtime_config() -> SupabaseConfig | None:
    """读取环境变量/Streamlit Secrets，不打印密钥。"""

    return SupabaseConfig.from_sources(
        environ=os.environ,
        secrets=_read_streamlit_secrets(),
    )


def _client_for(config: SupabaseConfig) -> Any:
    cached = st.session_state.get("supabase_user_client")
    cached_url = st.session_state.get("supabase_client_url")
    if cached is not None and cached_url == config.url:
        return cached
    client = create_supabase_client(config)
    st.session_state["supabase_user_client"] = client
    st.session_state["supabase_client_url"] = config.url
    return client


def _session_from_state() -> AuthSession | None:
    value = st.session_state.get("supabase_auth_session")
    if not isinstance(value, Mapping):
        return None
    user = value.get("user")
    if not isinstance(user, Mapping) or not user.get("id"):
        return None
    try:
        return AuthSession(
            access_token=str(value["access_token"]),
            refresh_token=str(value["refresh_token"]),
            expires_at=(
                int(value["expires_at"])
                if value.get("expires_at") is not None
                else None
            ),
            user=AuthUser(id=str(user["id"]), email=user.get("email")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _set_session(session: AuthSession | None) -> None:
    if session is None:
        st.session_state.pop("supabase_auth_session", None)
        st.session_state.pop("supabase_auth_user", None)
    else:
        st.session_state["supabase_auth_session"] = session.as_dict()
        st.session_state["supabase_auth_user"] = session.user


def get_auth_context() -> AuthContext | None:
    """获取当前云端上下文；未配置 Supabase 时返回 ``None``。"""

    config = load_runtime_config()
    if config is None:
        return None
    client = _client_for(config)
    session = _session_from_state()
    if session is None:
        # 只在 session_state 中已有令牌时尝试恢复，避免无意义的网络请求。
        persisted = st.session_state.get("supabase_auth_session")
        session = restore_session(client, persisted)
        if session is None and persisted:
            _set_session(None)
        elif session is not None:
            _set_session(session)
    context = AuthContext(config=config, client=client, user=session.user if session else None)
    if session is not None:
        # 12A 已建立 profiles 表；首次成功登录后创建自己的资料行，
        # 不把令牌或 user_metadata 写入业务表。失败不阻断登录/申请记录。
        profile_key = f"supabase_profile_synced:{session.user.id}"
        if not st.session_state.get(profile_key):
            try:
                SupabaseDataService(client).upsert_profile(session.user.id)
            except SupabaseDataError:
                st.session_state[f"{profile_key}:error"] = True
            st.session_state[profile_key] = True
    return context


def render_auth_controls() -> AuthContext | None:
    """渲染登录/注册/退出控件并返回认证上下文。"""

    try:
        context = get_auth_context()
    except SupabaseConfigurationError as exc:
        # 配置只填了一半时不能静默回退本地库，避免用户误以为数据已上云。
        st.sidebar.error(f"Supabase 配置错误：{exc}")
        st.stop()
    if context is None:
        return None

    st.sidebar.markdown("---")
    if context.user is not None:
        st.sidebar.success(
            f"已登录：{context.user.email or context.user.id[:8]}"
        )
        if st.session_state.get(f"supabase_profile_synced:{context.user.id}:error"):
            st.sidebar.caption("账户已登录；云端资料初始化暂未完成，申请记录仍可使用。")
        if st.sidebar.button("退出登录", key="supabase_sign_out"):
            try:
                sign_out(context.client)
            except SupabaseAuthError as exc:
                st.sidebar.error(str(exc))
            else:
                _set_session(None)
                st.rerun()
        return context

    st.sidebar.subheader("云端账户")
    mode = st.sidebar.radio(
        "账户操作", ["登录", "注册"], horizontal=True, key="supabase_auth_mode"
    )
    with st.sidebar.form("supabase_auth_form", clear_on_submit=False):
        email = st.text_input("邮箱", key="supabase_auth_email")
        password = st.text_input(
            "密码", type="password", key="supabase_auth_password"
        )
        submitted = st.form_submit_button("登录" if mode == "登录" else "注册")
    if submitted:
        try:
            session = (
                sign_in(context.client, email, password)
                if mode == "登录"
                else sign_up(context.client, email, password)
            )
        except SupabaseAuthError as exc:
            st.sidebar.error(str(exc))
        else:
            if session is None:
                st.sidebar.success("注册成功。请先完成邮箱确认，再返回登录。")
            else:
                _set_session(session)
                st.rerun()
    return context


__all__ = [
    "AuthContext",
    "get_auth_context",
    "load_runtime_config",
    "render_auth_controls",
]
