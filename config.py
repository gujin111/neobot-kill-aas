"""插件配置模型（pydantic v2）。面板据此自动生成配置表单。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .engine import DEFAULT_ACTIVITY_PROMPT, DEFAULT_IDLE_PROMPT

_LIMIT_HISTORY = "历史消息条数上限：同时作为每会话缓存上限与 {history} 最大注入条数"


class GroupRule(BaseModel):
    """单个群聊的主动发言规则。"""

    guild_id: str = Field(
        default="default",
        description="群号；填 default 表示这是一份默认模板，需要在「默认模板适用群」里列出群号才会生效",
    )
    enable_activity_trigger: bool = Field(default=False, description="启用活跃度触发：群里聊得热时主动接话")
    activity_lower_limit: float = Field(
        default=0.85, ge=0.0, le=1.0, description="初始触发灵敏度（0~1，越小越容易触发）"
    )
    activity_upper_limit: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="灵敏度趋向值：大于下限则越聊越克制，小于下限则越聊越积极，相等则固定阈值",
    )
    activity_message_interval: int = Field(
        default=20, ge=0, description="消息计数兜底间隔：攒够这么多条消息，即使活跃度不够也触发一次，0 表示关闭"
    )
    guaranteed_trigger_minutes: int = Field(
        default=0, ge=0, description="保底触发间隔（分钟）：超过这么久没触发过就强制触发一次，0 表示关闭"
    )
    enable_idle_trigger: bool = Field(default=False, description="启用空闲触发：长时间没人说话时主动开话题")
    idle_interval_minutes: int = Field(default=180, ge=1, description="空闲触发间隔（分钟）")
    idle_enable_jitter: bool = Field(default=True, description="空闲触发抖动：在间隔上下浮动 5%~10%，避免时间过于固定")
    history_message_limit: int = Field(default=20, ge=5, le=1000, description=_LIMIT_HISTORY)
    cooldown_seconds: int = Field(default=30, ge=0, le=3600, description="触发后冷却时间（秒）")
    work_hours: str = Field(
        default="",
        description="工作时间段（小时 0-23）：如 1,3-5,8-19，支持 22-2 跨零点；留空表示全天",
    )
    activity_prompt_template: str = Field(default=DEFAULT_ACTIVITY_PROMPT, description="活跃度触发提示词模板")
    idle_prompt_template: str = Field(default=DEFAULT_IDLE_PROMPT, description="空闲触发提示词模板")


class PrivateRule(BaseModel):
    """单个私聊的主动发言规则（私聊只支持空闲触发）。"""

    user_id: str = Field(
        default="default",
        description="用户 ID；填 default 表示这是一份默认模板，需要在「默认模板适用用户」里列出才会生效",
    )
    enable_idle_trigger: bool = Field(default=False, description="启用空闲触发")
    idle_interval_minutes: int = Field(default=180, ge=1, description="空闲触发间隔（分钟）")
    idle_enable_jitter: bool = Field(default=True, description="空闲触发抖动")
    history_message_limit: int = Field(default=20, ge=5, le=1000, description=_LIMIT_HISTORY)
    cooldown_seconds: int = Field(default=30, ge=0, le=3600, description="触发后冷却时间（秒）")
    work_hours: str = Field(default="", description="工作时间段，留空表示全天")
    idle_prompt_template: str = Field(default=DEFAULT_IDLE_PROMPT, description="空闲触发提示词模板")


class ProactiveConfig(BaseModel):
    """主动发言 + 指令图片中心的全部配置。"""

    # --- 总开关 ---
    enabled: bool = Field(default=True, description="主动发言总开关（也可以用 /proactiveon、/proactiveoff 在运行期切换）")

    # --- 触发层 ---
    apply_default_group_configs: list[str] = Field(
        default_factory=list, description="默认群聊模板适用的群号列表"
    )
    apply_default_private_configs: list[str] = Field(
        default_factory=list, description="默认私聊模板适用的用户 ID 列表"
    )
    groups: list[GroupRule] = Field(default_factory=list, description="群聊规则列表")
    privates: list[PrivateRule] = Field(default_factory=list, description="私聊规则列表")
    tick_seconds: int = Field(default=20, ge=5, le=600, description="调度器扫描间隔（秒）")

    # --- 失败处理 ---
    failure_cooldown_seconds: int = Field(default=60, ge=0, description="主动发言失败后的会话冷却时间（秒）")
    max_retry_attempts: int = Field(default=3, ge=0, description="连续失败多少次后进入冷却")

    # --- 日志 ---
    debug_log: bool = Field(default=False, description="输出触发原因等调试日志")
    verbose_log: bool = Field(default=False, description="输出每条消息的完整判断快照（排查用，平时建议关闭）")

    # --- 指令图片中心 ---
    help_image_enabled: bool = Field(default=True, description="启用 /helpimg 指令图片总览")
    help_image_title: str = Field(default="NeoBot 指令总览", description="指令图片的标题")
    help_image_theme: str = Field(
        default="default",
        description="指令图片主题：default / bottle / game / fortune（未知主题自动回落到 default）",
    )
    help_image_width: int = Field(default=900, ge=480, le=1600, description="指令图片宽度（像素）")
    help_image_show_source: bool = Field(default=True, description="在指令图片里显示插件来源标记")
    help_image_max_rows: int = Field(default=200, ge=10, le=500, description="单张图片最多渲染多少条指令")
