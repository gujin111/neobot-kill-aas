"""插件配置模型（pydantic v2）。面板据此自动生成配置表单。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from .engine import DEFAULT_ACTIVITY_PROMPT, DEFAULT_IDLE_PROMPT

_LIMIT_HISTORY = "历史消息条数上限（5-1000）：同时作为每会话缓存上限与 {history} 最大注入条数"

_WORK_HOURS_HELP = (
    "工作时间段：按服务器本地时间的小时判断，只能填 0-23 的整点（午夜写 0，不要写 24）。"
    "用英文逗号分隔，可写单个小时或「起-止」区间（含两端，如 8-19 覆盖 8:00–19:59）；"
    "22-2 表示跨零点。例如 1,3-5,8-19。留空即全天。"
    "无法识别的片段会被忽略；全部无法识别时按全天处理。"
)

_IDLE_INTERVAL_HELP = (
    "空闲触发间隔（分钟）：距最近一条消息（或上次主动发言，取较晚者）超过这么久才触发；不是固定周期"
)

_PROMPT_HELP = (
    "可用变量：{history} {time} {date} {group_name} {user_name} {idle_minutes}；"
    "未知变量会被替换为空（{user_name} 目前恒为空）"
)


def normalize_rule_list(value: Any, id_field: str) -> tuple[list[dict[str, Any]], int]:
    """把用户/面板给的规则列表规范成 ``[{...}, ...]``，返回 (列表, 丢弃条数)。

    容忍这些写法：
    * ``[{"user_id": "123"}]`` —— 标准写法
    * ``["123"]`` / ``[123]`` —— 只写 id 的简写
    * 单个值而不是列表 —— 按一条处理
    * 号码不带引号（TOML 里是 int）—— 自动转成字符串

    这样面板保存（``model_validate``）和插件加载（同一模型）都能接受。
    """
    if value is None:
        return [], 0
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return [], 1

    items: list[dict[str, Any]] = []
    dropped = 0
    for raw in value:
        if isinstance(raw, BaseModel):
            items.append(raw.model_dump())
            continue
        if isinstance(raw, dict):
            payload = dict(raw)
            if isinstance(payload.get(id_field), int):
                payload[id_field] = str(payload[id_field])
            items.append(payload)
            continue
        # 只写 id 的简写：字符串或正整数（QQ 号/群号不会 <= 0）
        if isinstance(raw, str) and raw.strip():
            items.append({id_field: raw.strip()})
            continue
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            items.append({id_field: str(raw)})
            continue
        dropped += 1
    return items, dropped


def normalize_id_list(value: Any) -> tuple[list[str], int]:
    """把「默认模板适用群/用户」列表统一成字符串列表。"""
    if value is None:
        return [], 0
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return [], 1
    out: list[str] = []
    dropped = 0
    for raw in value:
        if isinstance(raw, str) and raw.strip():
            out.append(raw.strip())
        elif isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            out.append(str(raw))
        else:
            dropped += 1
    return out, dropped


class GroupRule(BaseModel):
    """单个群聊的主动发言规则。"""

    guild_id: str = Field(
        default="default",
        description=(
            "群号或会话 ID；填 default 表示默认模板，需要在「默认模板适用群」里列出才会生效。"
            "官方通道可用 /proactive 查看当前会话 ID，或用 /linkgroup 绑定真实群号"
        ),
    )
    enable_activity_trigger: bool = Field(default=False, description="启用活跃度触发：群里聊得热时主动接话")
    activity_lower_limit: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="初始触发阈值（0~1）：活跃度分数达到它才触发，数值越小越容易触发；也是阈值自适应的起点",
    )
    activity_upper_limit: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "阈值趋向值：每次成功触发后，当前阈值向它移动两者差值的 10%；"
            "大于初始阈值则越聊越克制，小于则越聊越积极，相等则固定"
        ),
    )
    activity_message_interval: int = Field(
        default=20,
        ge=0,
        description=(
            "消息计数兜底间隔（仅在启用活跃度触发时生效）：自上次成功触发后攒够这么多条消息，"
            "即使活跃度不够也触发一次，0 表示关闭"
        ),
    )
    guaranteed_trigger_minutes: int = Field(
        default=0,
        ge=0,
        description=(
            "保底触发间隔（分钟）：仅在启用活跃度触发时生效；"
            "距最近一条消息或上次主动发言（取较晚者）超过这么久仍未触发时，强制触发一次，0 表示关闭"
        ),
    )
    enable_idle_trigger: bool = Field(default=False, description="启用空闲触发：长时间没人说话时主动开话题")
    idle_interval_minutes: int = Field(default=180, ge=1, description=_IDLE_INTERVAL_HELP)
    idle_enable_jitter: bool = Field(default=True, description="空闲触发抖动：在间隔上下浮动 5%~10%，避免时间过于固定")
    history_message_limit: int = Field(default=20, ge=5, le=1000, description=_LIMIT_HISTORY)
    cooldown_seconds: int = Field(
        default=30,
        ge=0,
        le=3600,
        description="成功触发后的冷却时间（秒）：同一会话在这段时间内不会再次自动触发；失败重试见 failure_cooldown_seconds",
    )
    work_hours: str = Field(
        default="",
        description=_WORK_HOURS_HELP,
        json_schema_extra={"placeholder": "如 8-19,22-2"},
    )
    activity_prompt_template: str = Field(
        default=DEFAULT_ACTIVITY_PROMPT, description="活跃度触发提示词模板。" + _PROMPT_HELP
    )
    idle_prompt_template: str = Field(
        default=DEFAULT_IDLE_PROMPT, description="空闲触发提示词模板。" + _PROMPT_HELP
    )


class PrivateRule(BaseModel):
    """单个私聊的主动发言规则（私聊只支持空闲触发）。"""

    user_id: str = Field(
        default="default",
        description=(
            "QQ 号或会话 ID（官方通道通常是 openid 字符串）；"
            "填 default 表示默认模板，需要在「默认模板适用用户」里列出才会生效。"
            "可用 /proactive 查看当前会话 ID，或用 /linkqq 绑定真实 QQ 号"
        ),
    )
    enable_idle_trigger: bool = Field(
        default=False, description="启用空闲触发：长时间没人说话时主动开话题（私聊只支持这一种触发）"
    )
    idle_interval_minutes: int = Field(
        default=180, ge=1, description=_IDLE_INTERVAL_HELP + "；新私聊规则需先收到一条消息才会开始计时"
    )
    idle_enable_jitter: bool = Field(default=True, description="空闲触发抖动：在间隔上下浮动 5%~10%，避免时间过于固定")
    history_message_limit: int = Field(default=20, ge=5, le=1000, description=_LIMIT_HISTORY)
    cooldown_seconds: int = Field(
        default=30,
        ge=0,
        le=3600,
        description="成功触发后的冷却时间（秒）：同一会话在这段时间内不会再次自动触发；失败重试见 failure_cooldown_seconds",
    )
    work_hours: str = Field(
        default="",
        description=_WORK_HOURS_HELP,
        json_schema_extra={"placeholder": "如 8-19,22-2"},
    )
    idle_prompt_template: str = Field(
        default=DEFAULT_IDLE_PROMPT, description="空闲触发提示词模板。" + _PROMPT_HELP
    )


class ProactiveConfig(BaseModel):
    """主动发言 + 指令图片中心的全部配置。"""

    # --- 总开关 ---
    enabled: bool = Field(
        default=True,
        description=(
            "主动发言总开关的初始值：只在首次运行或没有 state.json 时生效；"
            "之后以 /proactiveon、/proactiveoff 持久化的运行状态为准（可用 /proactive 查看当前状态）"
        ),
    )

    # --- 触发层 ---
    apply_default_group_configs: list[str] = Field(
        default_factory=list,
        description=(
            "默认群聊模板适用的群号/会话 ID 列表；"
            "只有 groups 中存在 guild_id=\"default\" 的规则时才生效；精确群号规则优先于默认模板"
        ),
    )
    apply_default_private_configs: list[str] = Field(
        default_factory=list,
        description=(
            "默认私聊模板适用的 QQ 号/会话 ID 列表；"
            "只有 privates 中存在 user_id=\"default\" 的规则时才生效；精确用户规则优先于默认模板"
        ),
    )
    groups: list[GroupRule] = Field(
        default_factory=list, description="群聊规则列表：每个群一条；没有命中规则的群不会被记录，也不会主动发言"
    )
    privates: list[PrivateRule] = Field(
        default_factory=list,
        description="私聊规则列表：私聊只支持空闲触发；没有命中规则的用户不会被记录，也不会主动发言",
    )
    tick_seconds: int = Field(
        default=20,
        ge=5,
        le=600,
        description="调度器检查触发条件的频率（秒），不是主动发言的间隔；调小更及时，调大更省资源",
    )

    # --- 失败处理 ---
    failure_cooldown_seconds: int = Field(
        default=60, ge=0, description="失败达到 max_retry_attempts 次后，后续触发尝试暂停的时间（秒）"
    )
    max_retry_attempts: int = Field(
        default=3,
        ge=0,
        description=(
            "自上次成功以来失败达到 N 次后，后续尝试在 failure_cooldown_seconds 内暂停；"
            "未达到时下一轮仍会尝试，0 按 1 处理"
        ),
    )

    # --- 日志 ---
    debug_log: bool = Field(default=False, description="输出触发成功/失败、跳过原因等 info 日志（warning 始终输出）")
    verbose_log: bool = Field(
        default=False,
        description=(
            "输出与 debug_log 相同的 info 日志，并额外输出每条消息的「记录消息…共 N 条」；"
            "排查用，平时建议关闭"
        ),
    )

    # --- 宽松输入 -----------------------------------------------------------
    # 面板保存走 model_validate，插件加载也走同一套模型；校验前先把宽松写法
    # 规范化，两处行为才一致（否则面板里填的简写会在保存时被拒）。

    @field_validator("groups", mode="before")
    @classmethod
    def _loose_groups(cls, value: Any) -> Any:
        items, _ = normalize_rule_list(value, "guild_id")
        return items

    @field_validator("privates", mode="before")
    @classmethod
    def _loose_privates(cls, value: Any) -> Any:
        items, _ = normalize_rule_list(value, "user_id")
        return items

    @field_validator("apply_default_group_configs", "apply_default_private_configs", mode="before")
    @classmethod
    def _loose_ids(cls, value: Any) -> Any:
        items, _ = normalize_id_list(value)
        return items
