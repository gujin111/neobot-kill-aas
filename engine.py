"""主动发言的纯逻辑层：活跃度打分、空闲调度、工作时间段、提示词模板。

本模块**不导入任何 NeoBot 模块**，因此可以脱离宿主单独跑单元测试。
算法移植自 koishi-plugin-chatluna-proactive-trigger 0.3.18，参数与权重保持一致。
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Iterable, Sequence
from datetime import datetime

# ---------------------------------------------------------------------------
# 默认提示词模板（变量：{history} {time} {date} {group_name} {user_name} {idle_minutes}）
# ---------------------------------------------------------------------------

DEFAULT_ACTIVITY_PROMPT = "\n".join(
    [
        "你现在需要进行一次“活跃度触发”的主动发言。",
        "当前时间：{date} {time}",
        "群聊名称：{group_name}",
        "",
        "以下是最近群内的消息（可能包含图片）：",
        "{history}",
        "",
        "群里正聊得热闹，请你自然地接一句话，语气友好、简洁，不要机械地总结或罗列。",
    ]
)

DEFAULT_IDLE_PROMPT = "\n".join(
    [
        "你现在需要进行一次“空闲触发”的主动发言。",
        "当前时间：{date} {time}",
        "群聊名称：{group_name}",
        "已空闲分钟数：{idle_minutes}",
        "",
        "以下是自上次你发言以来的群内消息（可能包含图片）：",
        "{history}",
        "",
        "请你主动发起一个自然的话题，语气友好、简洁，不要显得机械。",
    ]
)

_TEMPLATE_VARIABLE_RE = re.compile(r"\{([a-z_]+)\}")


# ---------------------------------------------------------------------------
# 工作时间段
# ---------------------------------------------------------------------------


def parse_work_hours(spec: str | None) -> frozenset[int] | None:
    """解析 ``"1,3-5,8-19"`` 形式的工作时段，返回小时集合。

    支持跨零点区间（``22-2`` 表示 22、23、0、1、2 点）。
    空字符串或全部非法时返回 ``None``，表示不限时段。
    """
    text = str(spec or "").strip()
    if not text:
        return None
    hours: set[int] = set()
    for raw_token in text.split(","):
        token = raw_token.strip()
        if not token:
            continue
        range_match = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", token)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start > 23 or end > 23:
                continue
            if start <= end:
                hours.update(range(start, end + 1))
            else:
                hours.update(range(start, 24))
                hours.update(range(0, end + 1))
            continue
        if token.isdigit():
            hour = int(token)
            if 0 <= hour <= 23:
                hours.add(hour)
    return frozenset(hours) if hours else None


def in_work_hours(spec: str | None, moment: datetime) -> bool:
    """当前小时是否落在工作时段内；未配置时段时恒为 True。"""
    hours = parse_work_hours(spec)
    if hours is None:
        return True
    return moment.hour in hours


# ---------------------------------------------------------------------------
# 活跃度打分
# ---------------------------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _logistic(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


class ActivityScorer:
    """多时间窗口消息密度打分，输出 0~1 的活跃度分数（越大越该插话）。"""

    RECENT_WINDOW_SECONDS = 90.0
    INSTANT_WINDOW_SECONDS = 20.0
    BURST_WINDOW_SECONDS = 30.0

    SUSTAINED_RATE = 10.0  # 条/分钟
    INSTANT_RATE = 9.0
    BURST_RATE = 12.0

    FRESHNESS_HALF_LIFE_SECONDS = 60.0

    def _rate(self, timestamps: Sequence[float], now: float, window: float) -> float:
        cutoff = now - window
        count = sum(1 for t in timestamps if t >= cutoff)
        return count / window * 60.0

    def calculate_score(self, timestamps: Sequence[float], now: float) -> float:
        if not timestamps:
            return 0.0
        recent_rate = self._rate(timestamps, now, self.RECENT_WINDOW_SECONDS)
        instant_rate = self._rate(timestamps, now, self.INSTANT_WINDOW_SECONDS)
        burst_rate = self._rate(timestamps, now, self.BURST_WINDOW_SECONDS)

        sustained = _logistic((recent_rate - self.SUSTAINED_RATE) / 3.0)
        instant = _logistic((instant_rate - self.INSTANT_RATE) / 2.0)
        score = sustained * 0.65 + instant * 0.35

        if burst_rate > self.BURST_RATE:
            score += _clamp((burst_rate - self.BURST_RATE) / 4.0, 0.0, 1.0) * 0.25

        newest = max(timestamps)
        elapsed = max(0.0, now - newest)
        freshness = math.exp(-elapsed / self.FRESHNESS_HALF_LIFE_SECONDS * math.log(2))
        score *= 0.55 + 0.45 * freshness
        return _clamp(score, 0.0, 1.0)

    def should_trigger(self, score: float, threshold: float) -> bool:
        return score >= threshold

    def adjust_threshold(self, current: float, initial: float, target: float) -> float:
        """按初始阈值与趋向值之差的一成步进推进当前阈值。

        初始阈值 < 趋向值 → 每次触发后阈值升高（越聊越克制）；
        初始阈值 > 趋向值 → 每次触发后阈值降低（越聊越积极）。
        """
        step = (target - initial) * 0.1
        return _clamp(current + step, min(initial, target), max(initial, target))


# ---------------------------------------------------------------------------
# 空闲调度
# ---------------------------------------------------------------------------


def idle_wait_seconds(
    idle_interval_minutes: float,
    jitter: bool,
    rng: random.Random | None = None,
) -> float:
    """空闲触发的等待间隔（秒）；开启抖动时上下浮动 5%~10%。"""
    base = max(0.0, float(idle_interval_minutes)) * 60.0
    if not jitter:
        return base
    source = rng or random
    ratio = 0.05 + source.random() * 0.05
    direction = -1.0 if source.random() < 0.5 else 1.0
    return max(1.0, base * (1.0 + direction * ratio))


# ---------------------------------------------------------------------------
# 提示词模板
# ---------------------------------------------------------------------------


def render_template(template: str, variables: dict[str, str]) -> str:
    """替换模板里的 ``{name}`` 变量；未提供的变量替换为空串。"""

    def _replace(match: re.Match[str]) -> str:
        return str(variables.get(match.group(1), ""))

    return _TEMPLATE_VARIABLE_RE.sub(_replace, str(template or ""))


def format_history(messages: Iterable[str], limit: int) -> str:
    """把历史消息剪成模板可用的文本块。"""
    items = list(messages)
    if limit > 0:
        items = items[-limit:]
    if not items:
        return "（本轮没有新消息）"
    return "\n".join(items)
