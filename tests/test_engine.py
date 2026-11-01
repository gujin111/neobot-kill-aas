"""纯逻辑层测试（不依赖 NeoBot、不依赖 pydantic）。

可直接 `python tests/test_engine.py` 运行，也可由 pytest 收集。
"""

from __future__ import annotations

import importlib.util
import random
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# 直接按文件路径加载 engine 模块：绕开包的 __init__.py（那里需要 NeoBot SDK）
_spec = importlib.util.spec_from_file_location("proactive_engine", _ROOT / "proactive" / "engine.py")
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)
sys.modules["proactive_engine"] = engine


def test_parse_work_hours_empty_means_all_day():
    assert engine.parse_work_hours("") is None
    assert engine.parse_work_hours(None) is None
    assert engine.parse_work_hours("   ") is None


def test_parse_work_hours_list_and_range():
    assert engine.parse_work_hours("1,3-5") == frozenset({1, 3, 4, 5})
    assert engine.parse_work_hours("8-19") == frozenset(range(8, 20))


def test_parse_work_hours_wraps_over_midnight():
    assert engine.parse_work_hours("22-2") == frozenset({22, 23, 0, 1, 2})


def test_parse_work_hours_rejects_out_of_range():
    assert engine.parse_work_hours("99-100") is None
    assert engine.parse_work_hours("24") is None


def test_in_work_hours_boundaries():
    assert engine.in_work_hours("", datetime(2026, 1, 1, 3, 0)) is True
    assert engine.in_work_hours("22-2", datetime(2026, 1, 1, 23, 0)) is True
    assert engine.in_work_hours("22-2", datetime(2026, 1, 1, 1, 0)) is True
    assert engine.in_work_hours("22-2", datetime(2026, 1, 1, 3, 0)) is False
    assert engine.in_work_hours("9-18", datetime(2026, 1, 1, 9, 0)) is True
    assert engine.in_work_hours("9-18", datetime(2026, 1, 1, 18, 59)) is True
    assert engine.in_work_hours("9-18", datetime(2026, 1, 1, 19, 0)) is False


def test_activity_score_empty_is_zero():
    scorer = engine.ActivityScorer()
    assert scorer.calculate_score([], 1000.0) == 0.0


def test_activity_score_busy_group_is_maximal():
    scorer = engine.ActivityScorer()
    now = 10_000.0
    timestamps = [now - i * 0.5 for i in range(30)]  # 15 秒内 30 条
    assert scorer.calculate_score(timestamps, now) > 0.99


def test_activity_score_single_message_is_low():
    scorer = engine.ActivityScorer()
    now = 10_000.0
    assert scorer.calculate_score([now], now) < 0.10


def test_activity_score_decays_with_staleness():
    scorer = engine.ActivityScorer()
    now = 10_000.0
    fresh = scorer.calculate_score([now - 5 for _ in range(5)], now)
    stale = scorer.calculate_score([now - 600 for _ in range(5)], now)
    assert stale < fresh
    assert stale < 0.05


def test_threshold_moves_toward_upper_limit_when_upper_is_higher():
    scorer = engine.ActivityScorer()
    # 下限 < 上限：越聊越克制（阈值升高）
    assert scorer.adjust_threshold(0.20, 0.20, 0.90) > 0.20


def test_threshold_moves_toward_lower_limit_when_upper_is_lower():
    scorer = engine.ActivityScorer()
    # 下限 > 上限：越聊越积极（阈值降低）
    assert scorer.adjust_threshold(0.90, 0.90, 0.20) < 0.90


def test_threshold_stays_when_limits_equal():
    scorer = engine.ActivityScorer()
    assert scorer.adjust_threshold(0.85, 0.85, 0.85) == 0.85


def test_threshold_never_leaves_bounds():
    scorer = engine.ActivityScorer()
    value = 0.89
    for _ in range(50):
        value = scorer.adjust_threshold(value, 0.20, 0.90)
    assert 0.20 <= value <= 0.90


def test_idle_wait_without_jitter_is_exact():
    assert engine.idle_wait_seconds(180, False) == 180 * 60


def test_idle_wait_with_jitter_stays_within_ten_percent():
    rng = random.Random(20261101)
    base = 60 * 60
    for _ in range(200):
        value = engine.idle_wait_seconds(60, True, rng)
        assert 0.89 * base <= value <= 1.11 * base


def test_render_template_replaces_known_variables():
    out = engine.render_template("群：{group_name} 空闲：{idle_minutes}", {"group_name": "测试群", "idle_minutes": "12"})
    assert out == "群：测试群 空闲：12"


def test_render_template_blanks_unknown_variables():
    assert engine.render_template("a{unknown}b", {}) == "ab"


def test_format_history_limits_and_handles_empty():
    assert engine.format_history([], 10) == "（本轮没有新消息）"
    assert engine.format_history(["a", "b", "c"], 2) == "b\nc"
    assert engine.format_history(["a", "b"], 0) == "a\nb"


def test_default_templates_carry_expected_variables():
    for template in (engine.DEFAULT_ACTIVITY_PROMPT, engine.DEFAULT_IDLE_PROMPT):
        assert "{history}" in template
        assert "{date}" in template
    assert "{idle_minutes}" in engine.DEFAULT_IDLE_PROMPT


def _run_all() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, func in tests:
        try:
            func()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
