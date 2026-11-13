"""proactive —— NeoBot 第三方插件。

**主动发言调度**：移植 koishi-plugin-chatluna-proactive-trigger 的空闲触发与活跃度触发，
用 NeoBot 宿主服务（notification_hub / reply_orchestrator）把话真正说出去。

插件只提供管理指令（全部限定超级管理员，命令名为纯字母）：``/proactive``、
``/proactiveon``、``/proactiveoff``、``/proactivetest``。

**指令总览统一走本体 ``/help``**：插件命令注册进宿主命令表，``/help`` 会把它们与本体命令
一起列出、按调用者权限过滤，并标上 ``[来源: proactive]``。本插件**不自己实现**帮助入口，
避免和 ``/help`` 抢同一件事。

兼容性
------
本文件刻意只使用 **跨 modloader 版本都存在** 的接口：

* 命令用 ``ctx.app_commands.register(...)``，而不是 2.0.2+ 才有的
  ``@plugin.command(permission=...)``；
* 消息监听用 ``@plugin.message(...)``，而不是 2.0.2+ 才有的 ``@plugin.event(...)``；
* 后台循环用 ``asyncio.create_task``，而不是 2.0.2+ 才有的 ``ctx.create_task``。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from neobot_modloader import Plugin

from . import engine
from .config import (
    GroupRule,
    PrivateRule,
    ProactiveConfig,
    normalize_id_list,
    normalize_rule_list,
)

PLUGIN_NAME = "proactive"
STATE_FILENAME = "state.json"
STATE_VERSION = 1

#: 权限等级（与 neobot_app.commands.model 保持一致）
PERM_EVERYONE = 0
PERM_SUB_ADMIN = 1
PERM_SUPER_ADMIN = 2


plugin = Plugin(
    "proactive",
    version="1.7.0",
    description="主动发言调度（空闲 / 活跃度触发）+ 指令图片总览",
    usage="提供 /proactive 系列主动发言管理指令；全部指令用本体的 /help 查看",
    config=ProactiveConfig,
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _log_ctx(ctx: Any, level: str, message: str) -> None:
    logger = getattr(ctx, "logger", None)
    writer = getattr(logger, level, None) if logger is not None else None
    text = f"[{PLUGIN_NAME}] {message}"
    if callable(writer):
        writer(text)


def _extract_text(payload: Any) -> str:
    """从 OneBot 消息段里提取可读文本；图片段折叠成 [图片]。"""
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, list):
        return ""
    parts: list[str] = []
    for segment in payload:
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type") or "")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if segment_type == "text":
            parts.append(str(data.get("text") or ""))
        elif segment_type == "image":
            parts.append("[图片]")
        elif segment_type == "at":
            parts.append("@")
    return "".join(parts).strip()


def _format_dt(moment: datetime) -> tuple[str, str]:
    weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    clock = moment.strftime("%H:%M")
    date = f"{moment.strftime('%Y-%m-%d')} {weekdays[moment.weekday()]}"
    return clock, date


def _ctx_field(command_ctx: Any, name: str) -> Any:
    return getattr(command_ctx, name, None) if command_ctx is not None else None


# ---------------------------------------------------------------------------
# 配置装配（容错）
# ---------------------------------------------------------------------------


def _coerce_id_list(value: Any, label: str, notes: list[str]) -> list[str]:
    """把「默认模板适用群 / 适用用户」列表统一成字符串（附告警）。

    规范化逻辑在 ``config.normalize_id_list``，面板保存与插件加载共用同一份。
    """
    if value is not None and not isinstance(value, (list, tuple, str, int)):
        notes.append(f"{label} 不是列表，已忽略")
        return []
    items, dropped = normalize_id_list(value)
    if dropped:
        notes.append(f"{label} 有 {dropped} 项无效，已跳过")
    return items


def _coerce_rules(value: Any, model: Any, id_field: str, label: str, notes: list[str]) -> list[Any]:
    """把用户写的规则列表尽量救回来，并记下跳过了几条。

    规范化逻辑在 ``config.normalize_rule_list``（面板保存也用它），
    这里额外负责构造模型并统计失败条数。
    """
    if value is not None and isinstance(value, (str, int)):
        notes.append(f"{label} 应该是列表，收到单个值，已按一条处理")
    if value is not None and not isinstance(value, (list, tuple, str, int)):
        notes.append(f"{label} 不是列表，已忽略")
        return []

    payloads, dropped = normalize_rule_list(value, id_field)
    items: list[Any] = []
    for payload in payloads:
        try:
            items.append(model(**payload))
        except Exception:
            dropped += 1
    if dropped:
        notes.append(f"{label} 有 {dropped} 条无法解析，已跳过（其余仍然生效）")
    return items


def _build_config(raw: Any) -> tuple[ProactiveConfig, list[str]]:
    """把宿主给的原始 dict 转成配置模型，尽量保住能用的部分。"""
    notes: list[str] = []
    cleaned = dict(raw or {})
    groups = _coerce_rules(
        cleaned.pop("groups", []), GroupRule, "guild_id", "群聊规则（groups）", notes
    )
    privates = _coerce_rules(
        cleaned.pop("privates", []), PrivateRule, "user_id", "私聊规则（privates）", notes
    )
    cleaned["apply_default_group_configs"] = _coerce_id_list(
        cleaned.pop("apply_default_group_configs", []), "默认模板适用群", notes
    )
    cleaned["apply_default_private_configs"] = _coerce_id_list(
        cleaned.pop("apply_default_private_configs", []), "默认模板适用用户", notes
    )
    cleaned["groups"] = groups
    cleaned["privates"] = privates

    try:
        return ProactiveConfig(**cleaned), notes
    except Exception as exc:
        notes.append(f"配置整体校验失败，改为逐项回退：{exc}")

    config = ProactiveConfig()
    for key, value in cleaned.items():
        if key in {"groups", "privates"}:
            continue
        try:
            probe = ProactiveConfig(**{key: value})
            setattr(config, key, getattr(probe, key))
        except Exception as exc:
            notes.append(f"字段 {key} 无效，已用默认值：{exc}")
    config.groups = groups
    config.privates = privates
    return config, notes


# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    kind: str
    conversation_id: str
    timestamps: deque[float] = field(default_factory=lambda: deque(maxlen=400))
    messages: deque[str] = field(default_factory=lambda: deque(maxlen=64))
    last_message_time: float = 0.0
    last_trigger_time: float = 0.0
    current_threshold: float = 0.85
    messages_since_trigger: int = 0
    failures: int = 0
    failure_cooldown_until: float = 0.0
    #: 官方通道的合成 ID 对应的真实 QQ 号/群号（来自 user_identity 映射）
    aliases: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.conversation_id}"


def _identity_module() -> Any:
    """宿主的人工身份映射（官方合成 ID ↔ 真实 QQ 号/群号）。

    NeoBot 2.1.0a8 起官方机器人通道给的是 13 位合成 ID，和真实 QQ 号没有 API 可以互转，
    只能靠管理员命令 `/linkqq`、`/linkgroup` 建立映射。拿不到就算了——插件在纯
    OneBot 环境里不需要它。
    """
    try:
        from neobot_app import user_identity  # noqa: WPS433 - 运行期可选依赖

        return user_identity
    except Exception:
        return None


def _resolve_aliases(kind: str, conversation_id: str) -> tuple[str, ...]:
    """把官方合成 ID 映射回真实 QQ 号/群号；没建立映射时返回空。"""
    module = _identity_module()
    if module is None:
        return ()
    try:
        if kind == "group":
            real = module.group_number_for(conversation_id)
        else:
            real = module.qq_for_user(conversation_id)
    except Exception:
        return ()
    text = str(real or "").strip()
    if not text or text == str(conversation_id):
        return ()
    return (text,)


def _display_target(state: SessionState) -> str:
    if not state.aliases:
        return state.key
    label = "群号" if state.kind == "group" else "QQ"
    return f"{state.key}（真实{label} {'/'.join(state.aliases)}）"


def _is_super_admin(command_ctx: Any) -> bool:
    """按宿主权限判定当前调用者是不是超级管理员。"""
    service = _ctx_field(command_ctx, "service")
    permissions = getattr(service, "permissions", None)
    raw = _ctx_field(command_ctx, "user_id")
    if permissions is None or raw is None:
        return False
    try:
        user_id: Any = int(raw)
    except (TypeError, ValueError):
        user_id = raw
    try:
        return bool(permissions.can(user_id, PERM_SUPER_ADMIN))
    except Exception:
        return False


def _self_id_lines(command_ctx: Any) -> list[str]:
    """只回当前会话自己的 ID —— 给普通用户看的极简版。

    官方通道下真实 QQ 号对 bot 不可见，管理员也没法替别人查会话，所以每个人
    都得能自己拿到「发给管理员的那串 ID」。
    """
    kind = str(_ctx_field(command_ctx, "kind") or "")
    conv_id = str(_ctx_field(command_ctx, "conv_id") or "")
    if kind not in {"group", "private"} or not conv_id:
        return ["无法确定当前会话，请在群聊或私聊里使用。"]

    aliases = _resolve_aliases(kind, conv_id)
    label = "群号" if kind == "group" else "QQ"
    field_name = "groups[].guild_id" if kind == "group" else "privates[].user_id"
    lines = [
        f"你在主动发言里的 {label}：{conv_id}",
    ]
    if aliases:
        lines.append(f"（已绑定真实{'群号' if kind == 'group' else 'QQ'}：{'/'.join(aliases)}）")
    else:
        lines.append(f"把这串 ID 发给管理员，填进配置的 {field_name} 即可生效")
    lines.append("（完整调度状态只有超级管理员能看）")
    return lines


def _current_conversation_lines(engine: Any, command_ctx: Any) -> list[str]:
    """当前会话的 ID / 真实号码 / 是否命中规则。

    解决官方通道上的鸡生蛋问题：没配规则时不会跟踪会话，`/proactive` 的
    「跟踪中的会话」是空的，用户就不知道自己的群 ID 到底是多少。
    """
    kind = str(_ctx_field(command_ctx, "kind") or "")
    conv_id = str(_ctx_field(command_ctx, "conv_id") or "")
    if kind not in {"group", "private"} or not conv_id:
        return []

    aliases = _resolve_aliases(kind, conv_id)
    label = "真实群号" if kind == "group" else "真实 QQ"
    rule = engine.rule_for(kind, conv_id, aliases)

    lines = [
        "当前会话：",
        f"  类型：{kind}",
        f"  通道 ID：{conv_id}",
        f"  {label}：{'/'.join(aliases) if aliases else '（未绑定）'}",
    ]
    if rule is None:
        field_name = "groups[].guild_id" if kind == "group" else "privates[].user_id"
        lines.append("  命中规则：无（这个会话不会被记录，也就不会主动发言）")
        lines.append(f"  → 把 {conv_id} 填进 {field_name}")
        if not aliases:
            hint = "/linkgroup <群号>" if kind == "group" else "/linkqq <QQ号>"
            lines.append(f"     或先用 {hint} 绑定真实号码，之后直接填真实号码")
    else:
        rule_id = getattr(rule, "guild_id", None) or getattr(rule, "user_id", "")
        triggers = []
        if getattr(rule, "enable_activity_trigger", False):
            triggers.append("活跃度")
        if getattr(rule, "enable_idle_trigger", False):
            triggers.append("空闲")
        state_text = "/".join(triggers) if triggers else "两个触发开关都是关的"
        lines.append(f"  命中规则：{rule_id}（{state_text}）")
        if not triggers:
            lines.append("  → 规则命中了但不会发言：请打开 enable_idle_trigger 或 enable_activity_trigger")
    return lines


def _as_id(value: Any) -> str:
    """会话 ID 一律按**字符串**传给适配器，不做数值转换。

    依据（v2.1.0a8 源码）：
    * `switchable.py:407` 用 `str(conversation.id)` 做会话路由键，字符串与整数等价；
    * 官方通道解析 `group_openid` 时自己会 `int(synthetic)`（`official/identity.py`）；
    * 非数字 ID（某些平台直接给 openid 字符串）不会在 `int()` 上抛错。
    """
    return str(value).strip()


# ---------------------------------------------------------------------------
# 主动发言引擎
# ---------------------------------------------------------------------------


class ProactiveEngine:
    """调度器：记录消息、评估触发条件、把提示词交给宿主回复管线。"""

    def __init__(self, ctx: Any, config: ProactiveConfig, notes: list[str] | None = None) -> None:
        self._ctx = ctx
        self._config = config
        self._scorer = engine.ActivityScorer()
        self._sessions: dict[str, SessionState] = {}
        self._runtime_enabled = bool(config.enabled)
        self._state_path: Path = Path(getattr(ctx, "data_dir", Path("."))) / STATE_FILENAME
        self._dirty = False
        #: 配置装配时发现的问题，会在 /proactive 里回显
        self.config_notes: list[str] = list(notes or [])
        self._load()

    @property
    def tick_seconds(self) -> int:
        return max(5, int(self._config.tick_seconds))

    @property
    def enabled(self) -> bool:
        return self._runtime_enabled

    def set_enabled(self, value: bool) -> None:
        self._runtime_enabled = bool(value)
        self._dirty = True
        self.save(force=True)

    def rule_for(
        self, kind: str, conversation_id: str, aliases: tuple[str, ...] = ()
    ) -> GroupRule | PrivateRule | None:
        """精确匹配 > applyDefault 命中的默认模板 > 无规则（与上游优先级一致）。

        ``aliases`` 是官方合成 ID 映射出的真实 QQ 号/群号：两套 ID 都算候选，
        所以规则里写真实号码或写合成 ID 都能命中。
        """
        candidates = {str(conversation_id)}
        for alias in aliases:
            text = str(alias or "").strip()
            if text:
                candidates.add(text)

        if kind == "group":
            exact = [r for r in self._config.groups if r.guild_id in candidates]
            if exact:
                return exact[0]
            if candidates & set(self._config.apply_default_group_configs):
                for rule in self._config.groups:
                    if rule.guild_id == "default":
                        return rule
            return None
        if kind == "private":
            exact = [r for r in self._config.privates if r.user_id in candidates]
            if exact:
                return exact[0]
            if candidates & set(self._config.apply_default_private_configs):
                for rule in self._config.privates:
                    if rule.user_id == "default":
                        return rule
            return None
        return None

    # -- 记录 ---------------------------------------------------------------

    def record(self, kind: str, conversation_id: str, text: str, sender_name: str = "") -> None:
        aliases = _resolve_aliases(kind, conversation_id)
        rule = self.rule_for(kind, conversation_id, aliases)
        if rule is None:
            return
        now = time.time()
        state = self._sessions.get(f"{kind}:{conversation_id}")
        if state is None:
            state = SessionState(
                kind=kind,
                conversation_id=conversation_id,
                current_threshold=float(getattr(rule, "activity_lower_limit", 0.85)),
                aliases=aliases,
            )
            self._sessions[state.key] = state
        elif aliases != state.aliases:
            # 映射可能是后来才用 /linkqq 建立的
            state.aliases = aliases
        state.timestamps.append(now)
        state.last_message_time = now
        state.messages_since_trigger += 1
        stamp = datetime.fromtimestamp(now).strftime("%H:%M")
        who = sender_name or "群友"
        state.messages.append(f"[{stamp}] {who}: {text}" if text else f"[{stamp}] {who}: [图片]")
        limit = max(5, int(getattr(rule, "history_message_limit", 20)))
        while len(state.messages) > limit:
            state.messages.popleft()
        self._dirty = True
        if self._config.verbose_log:
            self._log("info", f"记录消息 {state.key} 共 {len(state.messages)} 条")

    # -- 触发评估 -----------------------------------------------------------

    async def tick(self) -> None:
        if not self._runtime_enabled or not self._sessions:
            return
        now = time.time()
        moment = datetime.fromtimestamp(now)
        for state in list(self._sessions.values()):
            if (
                state.failures >= max(1, self._config.max_retry_attempts)
                and now < state.failure_cooldown_until
            ):
                continue
            try:
                await self._evaluate(state, now, moment)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 单个会话出错不影响整轮
                self._log("warning", f"评估 {state.key} 失败: {exc}")

    async def _evaluate(self, state: SessionState, now: float, moment: datetime) -> None:
        rule = self.rule_for(state.kind, state.conversation_id, state.aliases)
        if rule is None:
            return
        if not engine.in_work_hours(getattr(rule, "work_hours", ""), moment):
            return
        cooldown = float(getattr(rule, "cooldown_seconds", 30))
        if state.last_trigger_time and now - state.last_trigger_time < cooldown:
            return
        if now < state.failure_cooldown_until:
            return

        trigger = await self._pick_trigger(state, rule, now, moment)
        if trigger is None:
            return
        reason, prompt = trigger
        ok = await self._deliver(state, reason, prompt)
        if ok:
            state.last_trigger_time = now
            state.failures = 0
            state.failure_cooldown_until = 0.0
            state.messages.clear()
            state.timestamps.clear()
            state.messages_since_trigger = 0
            self._adjust_threshold(state, rule)
            self._log("info", f"{state.key} 触发成功（{reason}）")
        else:
            state.failures += 1
            state.failure_cooldown_until = now + float(self._config.failure_cooldown_seconds)
            self._log("warning", f"{state.key} 触发失败（{reason}），进入冷却")
        self._dirty = True

    async def _pick_trigger(
        self, state: SessionState, rule: GroupRule | PrivateRule, now: float, moment: datetime
    ) -> tuple[str, str] | None:
        history_limit = max(5, int(getattr(rule, "history_message_limit", 20)))

        # --- 空闲触发 ---
        if getattr(rule, "enable_idle_trigger", False) and state.last_message_time:
            anchor = max(state.last_message_time, state.last_trigger_time or 0.0)
            wait = engine.idle_wait_seconds(
                getattr(rule, "idle_interval_minutes", 180),
                bool(getattr(rule, "idle_enable_jitter", True)),
            )
            if now - anchor >= wait:
                silence = int((now - state.last_message_time) // 60)
                prompt = self._build_prompt(
                    getattr(rule, "idle_prompt_template", engine.DEFAULT_IDLE_PROMPT),
                    state,
                    history_limit,
                    moment,
                    idle_minutes=silence,
                )
                return (f"idle {silence}m", prompt)

        # --- 活跃度触发（仅群聊） ---
        if state.kind == "group" and getattr(rule, "enable_activity_trigger", False):
            interval = int(getattr(rule, "activity_message_interval", 0))
            if interval > 0 and state.messages_since_trigger >= interval:
                prompt = self._build_prompt(
                    getattr(rule, "activity_prompt_template", engine.DEFAULT_ACTIVITY_PROMPT),
                    state,
                    history_limit,
                    moment,
                )
                return (f"count {state.messages_since_trigger}", prompt)

            timestamps = list(state.timestamps)
            if timestamps:
                score = self._scorer.calculate_score(timestamps, now)
                if self._scorer.should_trigger(score, state.current_threshold):
                    prompt = self._build_prompt(
                        getattr(rule, "activity_prompt_template", engine.DEFAULT_ACTIVITY_PROMPT),
                        state,
                        history_limit,
                        moment,
                    )
                    return (f"activity {score:.2f}>={state.current_threshold:.2f}", prompt)

            guaranteed = int(getattr(rule, "guaranteed_trigger_minutes", 0))
            if guaranteed > 0:
                base = max(state.last_message_time, state.last_trigger_time or 0.0)
                if base and now - base >= guaranteed * 60:
                    prompt = self._build_prompt(
                        getattr(rule, "activity_prompt_template", engine.DEFAULT_ACTIVITY_PROMPT),
                        state,
                        history_limit,
                        moment,
                    )
                    return (f"guaranteed {guaranteed}m", prompt)
        return None

    def _adjust_threshold(self, state: SessionState, rule: GroupRule | PrivateRule) -> None:
        lower = float(getattr(rule, "activity_lower_limit", 0.85))
        upper = float(getattr(rule, "activity_upper_limit", 0.85))
        state.current_threshold = self._scorer.adjust_threshold(state.current_threshold, lower, upper)

    def _build_prompt(
        self,
        template: str,
        state: SessionState,
        history_limit: int,
        moment: datetime,
        idle_minutes: int = 0,
    ) -> str:
        clock, date = _format_dt(moment)
        variables = {
            "history": engine.format_history(state.messages, history_limit),
            "time": clock,
            "date": date,
            "group_name": state.conversation_id if state.kind == "group" else "",
            "user_name": "",
            "idle_minutes": str(idle_minutes),
        }
        return engine.render_template(template, variables)

    # -- 发送 ---------------------------------------------------------------

    async def _deliver(self, state: SessionState, reason: str, prompt: str) -> bool:
        services = getattr(getattr(self._ctx, "plugin_host", None), "services", None)
        if services is None:
            return await self._deliver_plain(state, prompt)

        hub = services.get("notification_hub")
        if hub is not None and hasattr(hub, "publish"):
            try:
                await hub.publish(
                    source=PLUGIN_NAME,
                    kind=state.kind,
                    conversation_id=str(state.conversation_id),
                    content=prompt,
                    manager_name=PLUGIN_NAME,
                    reasons=[reason],
                )
                return True
            except Exception as exc:  # 发言策略拒绝（SpeechBlocked）或其它异常
                if exc.__class__.__name__ == "SpeechBlocked":
                    self._log("info", f"{state.key} 被发言策略拒绝: {exc}")
                    return False
                self._log("warning", f"{state.key} notification_hub 投递失败: {exc}")

        orchestrator = services.get("reply_orchestrator")
        if orchestrator is not None and hasattr(orchestrator, "start_background_reply"):
            pipeline_key = f"{state.kind}:{state.conversation_id}"
            is_active = getattr(orchestrator, "is_pipeline_key_active", None)
            if callable(is_active) and is_active(pipeline_key):
                self._log("info", f"{state.key} 已有活跃回复管线，跳过")
                return False
            try:
                started = orchestrator.start_background_reply(
                    kind=state.kind,
                    conversation_id=str(state.conversation_id),
                    content=prompt,
                    manager_name=PLUGIN_NAME,
                    reasons=[reason],
                )
                if started is not None:
                    return True
            except Exception as exc:
                self._log("warning", f"{state.key} start_background_reply 失败: {exc}")

        return await self._deliver_plain(state, prompt)

    async def _deliver_plain(self, state: SessionState, prompt: str) -> bool:
        """最后兜底：直接发一条纯文本（同样会经过适配器的发言策略）。"""
        try:
            if state.kind == "group":
                await self._ctx.send_group(_as_id(state.conversation_id), prompt)
            else:
                await self._ctx.send_private(_as_id(state.conversation_id), prompt)
            return True
        except Exception as exc:
            self._log("warning", f"{state.key} 兜底发送失败: {exc}")
            return False

    async def force_trigger(self, kind: str, conversation_id: str) -> str:
        """管理用：忽略冷却与阈值，立即在当前会话试触发一次。"""
        state = self._sessions.get(f"{kind}:{conversation_id}")
        aliases = _resolve_aliases(kind, conversation_id)
        rule = self.rule_for(kind, conversation_id, aliases)
        if rule is None:
            return "当前会话没有匹配的主动发言规则，请先在插件配置里添加该群/用户"
        if state is None:
            state = SessionState(
                kind=kind,
                conversation_id=conversation_id,
                current_threshold=float(getattr(rule, "activity_lower_limit", 0.85)),
                aliases=aliases,
            )
            self._sessions[state.key] = state
        moment = datetime.now()
        prompt = self._build_prompt(
            getattr(rule, "idle_prompt_template", engine.DEFAULT_IDLE_PROMPT),
            state,
            max(5, int(getattr(rule, "history_message_limit", 20))),
            moment,
            idle_minutes=0,
        )
        ok = await self._deliver(state, "manual", prompt)
        if ok:
            state.last_trigger_time = time.time()
            state.messages.clear()
            state.timestamps.clear()
            state.messages_since_trigger = 0
            self._dirty = True
        return "已触发一次主动发言" if ok else "触发失败，已写入日志（可能是发言策略拒绝或没有可用的回复通道）"

    # -- 状态持久化 ---------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            return
        self._runtime_enabled = bool(raw.get("enabled", self._runtime_enabled))
        for item in raw.get("sessions", []):
            try:
                state = SessionState(
                    kind=str(item["kind"]),
                    conversation_id=str(item["conversation_id"]),
                    timestamps=deque(item.get("timestamps", []), maxlen=400),
                    messages=deque(item.get("messages", []), maxlen=64),
                    last_message_time=float(item.get("last_message_time", 0.0)),
                    last_trigger_time=float(item.get("last_trigger_time", 0.0)),
                    current_threshold=float(item.get("current_threshold", 0.85)),
                    messages_since_trigger=int(item.get("messages_since_trigger", 0)),
                )
            except Exception:
                continue
            self._sessions[state.key] = state

    def save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        payload = {
            "version": STATE_VERSION,
            "enabled": self._runtime_enabled,
            "sessions": [
                {
                    "kind": s.kind,
                    "conversation_id": s.conversation_id,
                    "timestamps": list(s.timestamps)[-200:],
                    "messages": list(s.messages),
                    "last_message_time": s.last_message_time,
                    "last_trigger_time": s.last_trigger_time,
                    "current_threshold": s.current_threshold,
                    "messages_since_trigger": s.messages_since_trigger,
                }
                for s in self._sessions.values()
            ],
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._dirty = False
        except Exception as exc:
            self._log("warning", f"状态保存失败: {exc}")

    # -- 日志 ---------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        if level == "info" and not (self._config.debug_log or self._config.verbose_log):
            return
        _log_ctx(self._ctx, level, message)

    # -- 只读视图，供管理指令使用 -------------------------------------------

    def status_lines(self) -> list[str]:
        lines = [
            f"总开关：{'开启' if self._runtime_enabled else '关闭'}",
            f"群聊规则：{len(self._config.groups)} 条，私聊规则：{len(self._config.privates)} 条",
            f"默认模板适用群：{len(self._config.apply_default_group_configs)} 个，"
            f"默认模板适用用户：{len(self._config.apply_default_private_configs)} 个",
            f"扫描间隔：{self.tick_seconds}s，失败冷却：{self._config.failure_cooldown_seconds}s",
        ]
        if self.config_notes:
            lines.append("")
            lines.append("配置告警：")
            lines.extend(f"  ! {note}" for note in self.config_notes)
        if not self._sessions:
            lines.append("当前没有正在跟踪的会话。")
            return lines
        lines.append(f"跟踪中的会话（{len(self._sessions)} 个）：")
        for state in sorted(
            self._sessions.values(), key=lambda s: s.last_message_time, reverse=True
        )[:20]:
            rule = self.rule_for(state.kind, state.conversation_id, state.aliases)
            if rule is None:
                continue
            active = []
            if getattr(rule, "enable_activity_trigger", False):
                active.append("活跃度")
            if getattr(rule, "enable_idle_trigger", False):
                active.append("空闲")
            state_text = "/".join(active) if active else "未启用触发"
            idle_minutes = (
                int((time.time() - state.last_message_time) // 60) if state.last_message_time else -1
            )
            lines.append(
                f"· {_display_target(state)}  [{state_text}]  消息 {len(state.messages)} 条，"
                f"阈值 {state.current_threshold:.2f}，空闲 {idle_minutes} 分钟"
            )
        return lines


# ---------------------------------------------------------------------------
# 运行时装配
# ---------------------------------------------------------------------------


_engine: ProactiveEngine | None = None
_tick_task: asyncio.Task | None = None


def _current_engine() -> ProactiveEngine | None:
    return _engine


async def _tick_loop(ctx: Any, current: ProactiveEngine) -> None:
    while True:
        await asyncio.sleep(current.tick_seconds)
        try:
            await current.tick()
            current.save()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log_ctx(ctx, "warning", f"调度轮询异常: {exc}")


def _register_commands(ctx: Any, config: ProactiveConfig) -> None:
    """把 5 条指令注册进宿主命令表（modloader 2.0.1 / 2.0.2 通用写法）。"""
    registrar = getattr(ctx, "app_commands", None)
    if registrar is None or not getattr(registrar, "available", True):
        _log_ctx(ctx, "warning", "宿主命令表不可用，跳过 /proactive 系列指令注册")
        return


    @registrar.register(
        "proactive",
        description="查看自己在主动发言里的会话 ID；超级管理员可看完整调度状态",
        permission=PERM_EVERYONE,
    )
    async def _status(command_ctx: Any):
        current = _current_engine()
        if current is None:
            return "调度器尚未初始化。"
        if not _is_super_admin(command_ctx):
            return "\n".join(_self_id_lines(command_ctx))
        lines = current.status_lines()
        section = _current_conversation_lines(current, command_ctx)
        if section:
            lines.append("")
            lines.extend(section)
        return "\n".join(lines)

    @registrar.register(
        "proactiveon",
        description="开启主动发言（超级管理员）",
        permission=PERM_SUPER_ADMIN,
    )
    async def _on(command_ctx: Any):
        current = _current_engine()
        if current is None:
            return "调度器尚未初始化。"
        current.set_enabled(True)
        return "主动发言已开启。"

    @registrar.register(
        "proactiveoff",
        description="关闭主动发言（超级管理员）",
        permission=PERM_SUPER_ADMIN,
    )
    async def _off(command_ctx: Any):
        current = _current_engine()
        if current is None:
            return "调度器尚未初始化。"
        current.set_enabled(False)
        return "主动发言已关闭。"

    @registrar.register(
        "proactivetest",
        description="在当前会话立即试触发一次主动发言（超级管理员）",
        permission=PERM_SUPER_ADMIN,
    )
    async def _test(command_ctx: Any):
        current = _current_engine()
        if current is None:
            return "调度器尚未初始化。"
        kind = str(_ctx_field(command_ctx, "kind") or "")
        conv_id = str(_ctx_field(command_ctx, "conv_id") or "")
        if kind not in {"group", "private"} or not conv_id:
            return "无法确定当前会话，请在群聊或私聊里使用。"
        return await current.force_trigger(kind, conv_id)


@plugin.on_load
async def _on_load(ctx) -> None:
    global _engine
    raw_config = dict(getattr(ctx, "config", None) or {})
    config, notes = _build_config(raw_config)
    for note in notes:
        _log_ctx(ctx, "warning", f"配置：{note}")
    _engine = ProactiveEngine(ctx, config, notes=notes)
    _register_commands(ctx, config)
    _log_ctx(ctx, "info", "主动发言调度已加载")


@plugin.on_startup
async def _on_startup(ctx) -> None:
    global _tick_task
    current = _current_engine()
    if current is None:
        return
    if _tick_task is not None and not _tick_task.done():
        return
    _tick_task = asyncio.create_task(_tick_loop(ctx, current), name="proactive-tick")


@plugin.on_shutdown
async def _on_shutdown(ctx) -> None:
    global _tick_task
    task = _tick_task
    _tick_task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001 - 关停时一律吞掉，避免阻断卸载
            pass
    current = _current_engine()
    if current is not None:
        current.save(force=True)


# ---------------------------------------------------------------------------
# 消息记录钩子
# ---------------------------------------------------------------------------


@plugin.message(priority=50)
async def _on_message(ctx, event: dict) -> None:
    current = _current_engine()
    if current is None:
        return
    raw = event or {}
    message_type = str(raw.get("message_type") or "")
    if message_type not in {"group", "private"}:
        return
    user_id = raw.get("user_id")
    self_id = raw.get("self_id")
    if self_id is not None and user_id is not None and str(self_id) == str(user_id):
        return
    text = _extract_text(raw.get("message"))
    if not text or text.startswith("/"):
        return
    conversation_id = raw.get("group_id") if message_type == "group" else user_id
    if conversation_id is None:
        return
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    nickname = str(sender.get("card") or sender.get("nickname") or "") or str(user_id or "群友")
    current.record(message_type, str(conversation_id), text, nickname)
