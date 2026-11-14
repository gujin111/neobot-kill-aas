"""插件集成测试：用桩 SDK 真导入插件包，验证注册结果与调度行为。

可直接 `python tests/test_integration.py` 运行，也可由 pytest 收集。

重点：断言插件只使用 **modloader 2.0.1 也存在** 的接口——
命令走 `ctx.app_commands.register(...)`，消息监听走 `@plugin.message(...)`。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import stubs  # noqa: E402

stubs.install_pydantic_stub()
stubs.install_modloader_stub()


def _load_plugin_module():
    # 仓库根目录就是插件目录（NeoBot 的安装器只在压缩包第一层找 plugin.toml）
    package_dir = ROOT
    spec = importlib.util.spec_from_file_location(
        "proactive",
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["proactive"] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_plugin_module()
PLUGIN = MODULE.plugin

# 沙箱只允许写工作区、且不允许删除目录：每次用唯一目录，避免上一轮遗留的
# state.json 被新一轮引擎读到（那会污染 enabled 等运行期状态）。
TMP_ROOT = ROOT / ".tmp-tests"
TMP_ROOT.mkdir(exist_ok=True)


def _writable(directory) -> bool:
    try:
        probe = Path(directory) / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


@contextlib.contextmanager
def _tmpdir():
    """优先用系统临时目录；受限环境（禁止建目录 / 不可写 / 禁止删除）回落到工程内。"""
    handle = None
    try:
        candidate = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    except Exception:
        candidate = None
    if candidate is not None:
        if _writable(candidate.name):
            handle = candidate
        else:
            try:
                candidate.cleanup()
            except Exception:
                pass
    if handle is not None:
        try:
            yield handle.name
            return
        finally:
            try:
                handle.cleanup()
            except Exception:
                pass
    path = TMP_ROOT / f"case{time.time_ns()}"
    path.mkdir(parents=True, exist_ok=True)
    yield str(path)


# ---------------------------------------------------------------------------
# 假宿主
# ---------------------------------------------------------------------------


class FakeLogger:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _record(self, level):
        def writer(message):
            self.records.append((level, str(message)))

        return writer

    @property
    def info(self):
        return self._record("info")

    @property
    def warning(self):
        return self._record("warning")

    @property
    def error(self):
        return self._record("error")


class FakeNotificationHub:
    def __init__(self, blocked: bool = False):
        self.published: list[dict] = []
        self.blocked = blocked

    async def publish(self, **kwargs):
        if self.blocked:
            raise type("SpeechBlocked", (Exception,), {})("speech blocked by policy")
        self.published.append(kwargs)
        return True


class FakeOrchestrator:
    def __init__(self, active: bool = False):
        self.active = active
        self.started: list[dict] = []

    def is_pipeline_key_active(self, pipeline_key):
        return self.active

    def start_background_reply(self, **kwargs):
        self.started.append(kwargs)
        return object()


class FakeServices:
    def __init__(self, **services):
        self._services = services

    def get(self, name, default=None):
        return self._services.get(name, default)

    def require(self, name):
        if name not in self._services:
            raise KeyError(name)
        return self._services[name]

    def names(self):
        return list(self._services)


class FakeHost:
    def __init__(self, services):
        self.services = services


class FakeRegistrar:
    """模拟 ctx.app_commands（PluginCommandRegistrar）。"""

    def __init__(self, available: bool = True):
        self.available = available
        self.registered: list[dict] = []

    def register(self, name=None, *, prefixed=False, **kwargs):
        requested = str(kwargs.pop("name", "") or name or "")

        def decorate(handler):
            self.registered.append(
                {
                    "name": requested,
                    "description": kwargs.get("description", ""),
                    "usage": kwargs.get("usage", ""),
                    "permission": kwargs.get("permission", 0),
                    "aliases": tuple(kwargs.get("aliases") or ()),
                    "handler": handler,
                }
            )
            return handler

        return decorate


class FakeCtx:
    def __init__(self, data_dir, services, app_commands=None, config=None):
        self.data_dir = Path(data_dir)
        self.logger = FakeLogger()
        self.plugin_host = FakeHost(services)
        self.app_commands = app_commands if app_commands is not None else FakeRegistrar()
        self.screenshots = None
        self.config = dict(config or {})
        self.sent: list[tuple[str, int, object]] = []

    async def send_group(self, group_id, message):
        self.sent.append(("group", group_id, message))
        return {"status": "ok"}

    async def send_private(self, user_id, message):
        self.sent.append(("private", user_id, message))
        return {"status": "ok"}


class FakeCommandContext:
    """模拟宿主 neobot_app.commands.model.CommandContext。"""

    def __init__(self, kind, conv_id, user_id, service, raw_args=""):
        self.kind = kind
        self.conv_id = conv_id
        self.user_id = user_id
        self.service = service
        self.raw_args = raw_args
        self.args = raw_args.split() if raw_args else []
        self.sent: list[str] = []

    async def reply(self, text):
        self.sent.append(text)


class FakePerms:
    def __init__(self, level):
        self.level = level

    def can(self, user_id, level):
        return self.level >= level


class FakeAdminService:
    """只用来喂 permissions.can() 的假服务。"""

    def __init__(self, level=None):
        self.permissions = FakePerms(MODULE.PERM_SUPER_ADMIN if level is None else level)


def _as_super_admin(command_ctx):
    """把命令上下文标成超级管理员调用。"""
    command_ctx.service = FakeAdminService(MODULE.PERM_SUPER_ADMIN)
    return command_ctx


def _make_config(module, **overrides):
    config = module.ProactiveConfig()
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _group_rule(module, **overrides):
    rule = module.GroupRule()
    for key, value in overrides.items():
        setattr(rule, key, value)
    return rule


def _private_rule(module, **overrides):
    rule = module.PrivateRule()
    for key, value in overrides.items():
        setattr(rule, key, value)
    return rule


def _build_engine(module, tmpdir, services, **config_overrides):
    config = _make_config(module, **config_overrides)
    ctx = FakeCtx(tmpdir, services)
    return module.ProactiveEngine(ctx, config), ctx, config


async def _bootstrap_async(tmpdir, services=None, **config_overrides):
    """在已有事件循环里跑插件的 on_load。"""
    ctx = FakeCtx(tmpdir, services or FakeServices(), config=dict(config_overrides))
    await MODULE._on_load(ctx)
    return ctx


def _bootstrap(tmpdir, services=None, **config_overrides):
    """跑一遍插件的 on_load，拿到注册好的命令（同步用例用）。"""
    ctx = FakeCtx(tmpdir, services or FakeServices(), config=dict(config_overrides))
    asyncio.run(MODULE._on_load(ctx))
    return ctx


def _registered(ctx, name):
    for item in ctx.app_commands.registered:
        if item["name"] == name:
            return item
    raise AssertionError(f"未注册命令 {name}，实际: {[i['name'] for i in ctx.app_commands.registered]}")


# ---------------------------------------------------------------------------
# 注册结果
# ---------------------------------------------------------------------------


def test_plugin_identity_and_export():
    assert PLUGIN.name == "proactive"
    assert PLUGIN.version == "1.7.0"
    assert PLUGIN.config_model.__name__ == "ProactiveConfig"


def test_plugin_toml_version_matches_plugin_object():
    """两者不一致时 modloader 会拒绝加载，这里提前拦住。"""
    import re

    text = (ROOT / "plugin.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert match, "plugin.toml 缺少 version"
    assert match.group(1) == PLUGIN.version


def test_does_not_use_newer_than_2_0_1_decorators():
    """@plugin.command / @plugin.event 是 2.0.2 才有的，用了会在 2.0.1 上炸。"""
    assert PLUGIN.commands == [], "不应使用 @plugin.command（2.0.1 没有 permission 参数）"
    assert len(PLUGIN.messages) == 1, "消息监听应且仅应注册一次"


def test_only_letter_slash_commands_are_registered():
    with _tmpdir() as tmp:
        ctx = _bootstrap(tmp)
    names = [item["name"] for item in ctx.app_commands.registered]
    assert names == ["proactive", "proactiveon", "proactiveoff", "proactivetest"], names
    for name in names:
        assert name.isalpha(), f"{name} 不是纯字母"


def test_plugin_does_not_register_its_own_help_entry():
    """指令总览统一走本体 /help，插件不该再注册任何 help* 命令去抢这件事。"""
    with _tmpdir() as tmp:
        ctx = _bootstrap(tmp)
    names = [item["name"] for item in ctx.app_commands.registered]
    assert not [name for name in names if name.startswith("help")], names


def test_admin_commands_are_super_admin_only_and_described():
    """管理类指令仅超管；/proactive 是全员自助查 ID，靠内部判定收窄。"""
    with _tmpdir() as tmp:
        ctx = _bootstrap(tmp)
    perms = {item["name"]: item["permission"] for item in ctx.app_commands.registered}
    for item in ctx.app_commands.registered:
        assert item["description"], item["name"]
    assert perms["proactive"] == MODULE.PERM_EVERYONE
    for name in ("proactiveon", "proactiveoff", "proactivetest"):
        assert perms[name] == MODULE.PERM_SUPER_ADMIN, name


def test_registration_is_skipped_when_registrar_unavailable():
    with _tmpdir() as tmp:
        ctx = FakeCtx(tmp, FakeServices(), app_commands=FakeRegistrar(available=False))
        asyncio.run(MODULE._on_load(ctx))
    assert ctx.app_commands.registered == []
    assert any("宿主命令表不可用" in msg for _, msg in ctx.logger.records)


def test_message_hook_is_registered_without_filters():
    registration = PLUGIN.messages[0]
    assert registration["pattern"] is None
    # 无 text/contains/keywords/regex 等过滤条件 -> 匹配所有消息
    for key in ("text", "contains", "keywords", "regex", "startswith", "endswith", "fullmatch"):
        assert registration.get(key) is None, f"不应设置 {key} 过滤"
    assert registration.get("priority") == 50


def test_lifecycle_hooks_are_registered():
    assert len(PLUGIN.lifecycle["load"]) == 1
    assert len(PLUGIN.lifecycle["startup"]) == 1
    assert len(PLUGIN.lifecycle["shutdown"]) == 1


def test_default_config_values_match_upstream_port():
    rule = MODULE.GroupRule()
    assert rule.idle_interval_minutes == 180
    assert rule.activity_lower_limit == 0.85
    assert rule.activity_upper_limit == 0.85
    assert rule.activity_message_interval == 20
    assert rule.cooldown_seconds == 30
    assert rule.history_message_limit == 20
    assert rule.enable_idle_trigger is False
    assert rule.enable_activity_trigger is False
    private = MODULE.PrivateRule()
    assert private.idle_interval_minutes == 180
    config = MODULE.ProactiveConfig()
    assert config.tick_seconds == 20
    assert config.failure_cooldown_seconds == 60
    assert config.max_retry_attempts == 3


# ---------------------------------------------------------------------------
# 规则解析优先级
# ---------------------------------------------------------------------------


def test_rule_priority_exact_then_default_then_none():
    with _tmpdir() as tmp:
        engine, _, _ = _build_engine(
            MODULE,
            tmp,
            FakeServices(),
            groups=[
                _group_rule(MODULE, guild_id="100", enable_idle_trigger=True),
                _group_rule(MODULE, guild_id="default", enable_idle_trigger=False),
            ],
            apply_default_group_configs=["200"],
        )
        assert engine.rule_for("group", "100").guild_id == "100"
        assert engine.rule_for("group", "200").guild_id == "default"
        assert engine.rule_for("group", "300") is None
        assert engine.rule_for("private", "100") is None


def test_record_ignores_conversations_without_rule():
    with _tmpdir() as tmp:
        engine, _, _ = _build_engine(MODULE, tmp, FakeServices(), groups=[])
        engine.record("group", "1", "hello")
        assert engine._sessions == {}


# ---------------------------------------------------------------------------
# 触发行为
# ---------------------------------------------------------------------------


def test_idle_trigger_publishes_to_notification_hub():
    async def scenario():
        with _tmpdir() as tmp:
            hub = FakeNotificationHub()
            engine, _, _ = _build_engine(
                MODULE,
                tmp,
                FakeServices(notification_hub=hub),
                groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)],
            )
            engine.record("group", "100", "good evening", "Alice")
            state = engine._sessions["group:100"]
            state.last_message_time = time.time() - 200 * 60
            await engine.tick()
            assert len(hub.published) == 1, hub.published
            call = hub.published[0]
            assert call["kind"] == "group"
            assert call["conversation_id"] == "100"
            assert call["source"] == "proactive"
            assert "good evening" in call["content"]
            assert len(state.messages) == 0

    asyncio.run(scenario())


def test_cooldown_blocks_second_trigger():
    async def scenario():
        with _tmpdir() as tmp:
            hub = FakeNotificationHub()
            engine, _, _ = _build_engine(
                MODULE,
                tmp,
                FakeServices(notification_hub=hub),
                groups=[
                    _group_rule(MODULE, guild_id="100", enable_idle_trigger=True, cooldown_seconds=3600)
                ],
            )
            engine.record("group", "100", "anyone here", "Bob")
            state = engine._sessions["group:100"]
            state.last_message_time = time.time() - 200 * 60
            await engine.tick()
            assert len(hub.published) == 1
            state.last_message_time = time.time() - 200 * 60
            await engine.tick()
            assert len(hub.published) == 1, "cooldown should block the second trigger"

    asyncio.run(scenario())


def test_work_hours_block_trigger_outside_window():
    async def scenario():
        with _tmpdir() as tmp:
            hub = FakeNotificationHub()
            blocked_hour = (datetime.now().hour + 1) % 24
            engine, _, _ = _build_engine(
                MODULE,
                tmp,
                FakeServices(notification_hub=hub),
                groups=[
                    _group_rule(
                        MODULE, guild_id="100", enable_idle_trigger=True, work_hours=str(blocked_hour)
                    )
                ],
            )
            engine.record("group", "100", "hello", "Bob")
            state = engine._sessions["group:100"]
            state.last_message_time = time.time() - 200 * 60
            await engine.tick()
            assert hub.published == []

    asyncio.run(scenario())


def test_activity_message_counter_triggers_without_score():
    async def scenario():
        with _tmpdir() as tmp:
            hub = FakeNotificationHub()
            engine, _, _ = _build_engine(
                MODULE,
                tmp,
                FakeServices(notification_hub=hub),
                groups=[
                    _group_rule(
                        MODULE, guild_id="100", enable_activity_trigger=True, activity_message_interval=3
                    )
                ],
            )
            for i in range(3):
                engine.record("group", "100", f"msg{i}", "Carol")
            state = engine._sessions["group:100"]
            state.timestamps.clear()
            await engine.tick()
            assert len(hub.published) == 1
            assert "msg2" in hub.published[0]["content"]

    asyncio.run(scenario())


def test_activity_score_trigger_fires_on_busy_group():
    async def scenario():
        with _tmpdir() as tmp:
            hub = FakeNotificationHub()
            engine, _, _ = _build_engine(
                MODULE,
                tmp,
                FakeServices(notification_hub=hub),
                groups=[
                    _group_rule(
                        MODULE, guild_id="100", enable_activity_trigger=True, activity_message_interval=0
                    )
                ],
            )
            engine.record("group", "100", "lets talk", "Dave")
            state = engine._sessions["group:100"]
            now = time.time()
            state.timestamps.clear()
            for i in range(40):
                state.timestamps.append(now - i * 0.2)
            await engine.tick()
            assert len(hub.published) == 1

    asyncio.run(scenario())


def test_speech_blocked_gives_up_without_starting_pipeline():
    """发言策略拒绝时直接放弃：起管线也会被适配器拦下，白占资源。"""

    async def scenario():
        with _tmpdir() as tmp:
            hub = FakeNotificationHub(blocked=True)
            orchestrator = FakeOrchestrator(active=False)
            services = FakeServices(notification_hub=hub, reply_orchestrator=orchestrator)
            engine, ctx, _ = _build_engine(
                MODULE,
                tmp,
                services,
                groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)],
            )
            engine.record("group", "100", "morning", "Eve")
            state = engine._sessions["group:100"]
            state.last_message_time = time.time() - 200 * 60
            await engine.tick()
            assert orchestrator.started == []
            assert ctx.sent == []
            assert state.failures == 1, (
                f"failures={state.failures} hub={len(hub.published)} "
                f"orch={len(orchestrator.started)} sent={len(ctx.sent)} enabled={engine.enabled}"
            )

    asyncio.run(scenario())


def test_orchestrator_used_when_notification_hub_absent():
    async def scenario():
        with _tmpdir() as tmp:
            orchestrator = FakeOrchestrator(active=False)
            engine, ctx, _ = _build_engine(
                MODULE,
                tmp,
                FakeServices(reply_orchestrator=orchestrator),
                groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)],
            )
            engine.record("group", "100", "morning", "Eve")
            state = engine._sessions["group:100"]
            state.last_message_time = time.time() - 200 * 60
            await engine.tick()
            assert len(orchestrator.started) == 1
            assert orchestrator.started[0]["kind"] == "group"
            assert ctx.sent == []

    asyncio.run(scenario())


def test_active_pipeline_skips_without_sending():
    async def scenario():
        with _tmpdir() as tmp:
            hub = FakeNotificationHub(blocked=True)
            orchestrator = FakeOrchestrator(active=True)
            services = FakeServices(notification_hub=hub, reply_orchestrator=orchestrator)
            engine, ctx, _ = _build_engine(
                MODULE,
                tmp,
                services,
                groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)],
            )
            engine.record("group", "100", "morning", "Eve")
            state = engine._sessions["group:100"]
            state.last_message_time = time.time() - 200 * 60
            await engine.tick()
            assert orchestrator.started == []
            assert ctx.sent == []

    asyncio.run(scenario())


def test_no_services_falls_back_to_plain_send():
    async def scenario():
        with _tmpdir() as tmp:
            engine, ctx, _ = _build_engine(
                MODULE,
                tmp,
                FakeServices(),
                groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)],
            )
            engine.record("group", "100", "hi", "Eve")
            state = engine._sessions["group:100"]
            state.last_message_time = time.time() - 200 * 60
            await engine.tick()
            assert len(ctx.sent) == 1
            assert ctx.sent[0][0] == "group"

    asyncio.run(scenario())


def test_disabled_engine_does_nothing():
    async def scenario():
        with _tmpdir() as tmp:
            hub = FakeNotificationHub()
            engine, _, _ = _build_engine(
                MODULE,
                tmp,
                FakeServices(notification_hub=hub),
                enabled=False,
                groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)],
            )
            engine.record("group", "100", "hi", "Eve")
            state = engine._sessions["group:100"]
            state.last_message_time = time.time() - 200 * 60
            await engine.tick()
            assert hub.published == []
            engine.set_enabled(True)
            await engine.tick()
            assert len(hub.published) == 1

    asyncio.run(scenario())


def test_state_persists_enabled_flag_and_sessions():
    with _tmpdir() as tmp:
        hub = FakeNotificationHub()
        services = FakeServices(notification_hub=hub)
        engine, _, _ = _build_engine(
            MODULE, tmp, services, groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)]
        )
        engine.record("group", "100", "persist-me", "Frank")
        engine.set_enabled(False)
        engine.save(force=True)

        engine2, _, _ = _build_engine(
            MODULE, tmp, services, groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)]
        )
        assert engine2.enabled is False
        assert "group:100" in engine2._sessions
        assert "persist-me" in engine2._sessions["group:100"].messages[0]


# ---------------------------------------------------------------------------
# 指令行为
# ---------------------------------------------------------------------------


def test_proactive_commands_report_status_and_toggle():
    async def scenario():
        with _tmpdir() as tmp:
            ctx = await _bootstrap_async(tmp, FakeServices(), groups=[])
            engine = MODULE._current_engine()
            assert engine is not None

            status = await _registered(ctx, "proactive")["handler"](
                _as_super_admin(FakeCommandContext("group", "1", 1, None))
            )
            assert isinstance(status, str) and "总开关" in status

            await _registered(ctx, "proactiveoff")["handler"](FakeCommandContext("group", "1", 1, None))
            assert engine.enabled is False
            await _registered(ctx, "proactiveon")["handler"](FakeCommandContext("group", "1", 1, None))
            assert engine.enabled is True

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 消息钩子
# ---------------------------------------------------------------------------


def test_message_hook_records_group_message():
    async def scenario():
        with _tmpdir() as tmp:
            ctx = await _bootstrap_async(
                tmp, FakeServices(), groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)]
            )
            handler = PLUGIN.messages[0]["handler"]
            event = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 100,
                "user_id": 200,
                "self_id": 999,
                "sender": {"card": "小明"},
                "message": [{"type": "text", "data": {"text": "大家好啊"}}],
            }
            await handler(ctx=ctx, event=event)
            engine = MODULE._current_engine()
            state = engine._sessions["group:100"]
            assert "小明" in state.messages[0]
            assert "大家好啊" in state.messages[0]

    asyncio.run(scenario())


def test_message_hook_skips_commands_self_and_empty():
    async def scenario():
        with _tmpdir() as tmp:
            ctx = await _bootstrap_async(
                tmp, FakeServices(), groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)]
            )
            handler = PLUGIN.messages[0]["handler"]
            base = {"post_type": "message", "message_type": "group", "group_id": 100, "user_id": 200}

            await handler(
                ctx=ctx,
                event={**base, "self_id": 999, "message": [{"type": "text", "data": {"text": "/help"}}]},
            )
            await handler(
                ctx=ctx, event={**base, "self_id": 200, "message": [{"type": "text", "data": {"text": "自己"}}]}
            )
            engine = MODULE._current_engine()
            assert engine._sessions.get("group:100") is None

    asyncio.run(scenario())


def test_message_hook_counts_image_segments():
    async def scenario():
        with _tmpdir() as tmp:
            ctx = await _bootstrap_async(
                tmp, FakeServices(), groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)]
            )
            handler = PLUGIN.messages[0]["handler"]
            await handler(
                ctx=ctx,
                event={
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 100,
                    "user_id": 200,
                    "self_id": 999,
                    "message": [{"type": "image", "data": {}}],
                },
            )
            state = MODULE._current_engine()._sessions["group:100"]
            assert "[图片]" in state.messages[0]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 配置容错（privates.0 / groups.0 写错时不能整份配置崩掉）
# ---------------------------------------------------------------------------


def test_config_accepts_bare_string_rule():
    """privates = ["123456"] 这种「只写 id」的写法应被救回来。"""
    config, notes = MODULE._build_config({"privates": ["123456"]})
    assert len(config.privates) == 1
    assert config.privates[0].user_id == "123456"
    assert notes == []


def test_config_accepts_bare_string_group():
    config, _ = MODULE._build_config({"groups": ["987654321"]})
    assert len(config.groups) == 1
    assert config.groups[0].guild_id == "987654321"


def test_config_skips_junk_entries_but_keeps_valid_ones():
    config, notes = MODULE._build_config(
        {"privates": [None, 42, "keep-me", {"user_id": "explicit"}]}
    )
    ids = [rule.user_id for rule in config.privates]
    assert "keep-me" in ids and "explicit" in ids
    assert any("无法解析" in note for note in notes)


def test_config_single_string_instead_of_list():
    config, notes = MODULE._build_config({"privates": "123456"})
    assert len(config.privates) == 1
    assert any("应该是列表" in note for note in notes)


def test_config_non_list_is_ignored_with_note():
    config, notes = MODULE._build_config({"privates": {"user_id": "x"}})
    assert config.privates == []
    assert any("不是列表" in note for note in notes)


def test_bad_privates_does_not_discard_groups():
    """一条坏私聊规则不能把群聊配置一起打回默认值。"""
    config, _ = MODULE._build_config(
        {
            "groups": [{"guild_id": "100", "enable_idle_trigger": True}],
            "privates": ["not-a-dict-either-but-string-works"],
            "enabled": True,
        }
    )
    assert len(config.groups) == 1
    assert config.groups[0].guild_id == "100"
    assert config.groups[0].enable_idle_trigger is True


def test_status_lines_surface_config_notes():
    async def scenario():
        with _tmpdir() as tmp:
            ctx = await _bootstrap_async(tmp, FakeServices(), groups=[], privates=["123"])
            engine = MODULE._current_engine()
            text = "\n".join(engine.status_lines())
            assert "群聊规则：0 条" in text
            assert "私聊规则：1 条" in text

    asyncio.run(scenario())


def test_config_accepts_unquoted_qq_number_in_rule():
    """TOML 里 user_id = 123456789（不带引号）是 int，要能自动转成字符串。"""
    config, notes = MODULE._build_config({"privates": [{"user_id": 123456789}]})
    assert len(config.privates) == 1
    assert config.privates[0].user_id == "123456789"
    assert config.privates[0].user_id.strip() == "123456789"


def test_config_accepts_unquoted_qq_number_as_whole_rule():
    config, _ = MODULE._build_config({"groups": [987654321]})
    assert len(config.groups) == 1
    assert config.groups[0].guild_id == "987654321"


def test_apply_default_lists_accept_unquoted_numbers():
    config, notes = MODULE._build_config(
        {"apply_default_group_configs": [123456789], "apply_default_private_configs": [987654321]}
    )
    assert config.apply_default_group_configs == ["123456789"]
    assert config.apply_default_private_configs == ["987654321"]


def test_apply_default_lists_drop_invalid_items():
    config, notes = MODULE._build_config(
        {"apply_default_group_configs": [123456789, None, ""]}
    )
    assert config.apply_default_group_configs == ["123456789"]
    assert any("无效" in note for note in notes)


# ---------------------------------------------------------------------------
# 宽松输入规范化（面板保存与插件加载共用同一份逻辑）
# ---------------------------------------------------------------------------


def test_normalize_rule_list_accepts_string_and_int_shorthand():
    items, dropped = MODULE.normalize_rule_list(["123456"], "user_id")
    assert items == [{"user_id": "123456"}] and dropped == 0

    items, dropped = MODULE.normalize_rule_list([123456], "user_id")
    assert items == [{"user_id": "123456"}] and dropped == 0


def test_normalize_rule_list_coerces_int_id_inside_dict():
    items, _ = MODULE.normalize_rule_list([{"user_id": 123456, "enable_idle_trigger": True}], "user_id")
    assert items[0]["user_id"] == "123456"
    assert items[0]["enable_idle_trigger"] is True


def test_normalize_rule_list_wraps_scalar():
    items, _ = MODULE.normalize_rule_list("123456", "guild_id")
    assert items == [{"guild_id": "123456"}]


def test_normalize_rule_list_drops_junk():
    items, dropped = MODULE.normalize_rule_list([None, "", 0, {"user_id": "ok"}], "user_id")
    assert items == [{"user_id": "ok"}]
    assert dropped == 3


def test_normalize_rule_list_handles_none_and_non_list():
    assert MODULE.normalize_rule_list(None, "user_id") == ([], 0)
    assert MODULE.normalize_rule_list({"a": 1}, "user_id") == ([], 1)


def test_normalize_id_list_coerces_and_drops():
    assert MODULE.normalize_id_list([123456, " 789 "]) == (["123456", "789"], 0)
    items, dropped = MODULE.normalize_id_list([123456, None, ""])
    assert items == ["123456"]
    assert dropped == 2


# ---------------------------------------------------------------------------
# 官方通道（2.1.0a8+）：合成 ID ↔ 真实 QQ 号/群号
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _fake_identity(**mapping):
    """装一个假的 neobot_app.user_identity，模拟「管理员已用 /linkqq 建立映射」。"""
    module = types.ModuleType("neobot_app.user_identity")
    module.qq_for_user = lambda uid, channel=None: mapping.get(str(uid))
    module.group_number_for = lambda gid, channel=None: mapping.get(str(gid))
    module.synthetic_for_qq = lambda qq, channel=None: None
    package = types.ModuleType("neobot_app")
    package.user_identity = module
    saved = {name: sys.modules.get(name) for name in ("neobot_app", "neobot_app.user_identity")}
    sys.modules["neobot_app"] = package
    sys.modules["neobot_app.user_identity"] = module
    try:
        yield module
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def test_resolve_aliases_maps_official_synthetic_id():
    with _fake_identity(**{"1234567890123": 987654321}):
        assert MODULE._resolve_aliases("private", "1234567890123") == ("987654321",)
        assert MODULE._resolve_aliases("group", "1234567890123") == ("987654321",)


def test_resolve_aliases_returns_empty_without_module():
    for name in ("neobot_app", "neobot_app.user_identity"):
        sys.modules.pop(name, None)
    assert MODULE._resolve_aliases("private", "1234567890123") == ()


def test_rule_matches_rule_written_with_real_qq():
    """规则按真实 QQ 号写、事件里是官方合成 ID —— 靠映射命中。"""
    with _tmpdir() as tmp:
        engine, _, _ = _build_engine(
            MODULE,
            tmp,
            FakeServices(),
            privates=[_private_rule(MODULE, user_id="987654321", enable_idle_trigger=True)],
        )
        with _fake_identity(**{"1234567890123": 987654321}):
            engine.record("private", "1234567890123", "在吗", "Someone")
        assert "private:1234567890123" in engine._sessions
        assert engine._sessions["private:1234567890123"].aliases == ("987654321",)


def test_rule_matches_rule_written_with_synthetic_id():
    with _tmpdir() as tmp:
        engine, _, _ = _build_engine(
            MODULE,
            tmp,
            FakeServices(),
            privates=[_private_rule(MODULE, user_id="1234567890123", enable_idle_trigger=True)],
        )
        with _fake_identity(**{"1234567890123": 987654321}):
            engine.record("private", "1234567890123", "在吗", "Someone")
        assert "private:1234567890123" in engine._sessions


def test_group_alias_participates_in_default_template_matching():
    with _tmpdir() as tmp:
        engine, _, _ = _build_engine(
            MODULE,
            tmp,
            FakeServices(),
            groups=[_group_rule(MODULE, guild_id="default", enable_idle_trigger=True)],
            apply_default_group_configs=["555666777"],
        )
        with _fake_identity(**{"1234567890123": 555666777}):
            engine.record("group", "1234567890123", "hi", "A")
        assert "group:1234567890123" in engine._sessions


def test_display_target_shows_real_id_when_mapped():
    state = MODULE.SessionState(
        kind="private", conversation_id="1234567890123", aliases=("987654321",)
    )
    text = MODULE._display_target(state)
    assert "1234567890123" in text and "987654321" in text and "QQ" in text


def test_display_target_falls_back_to_key():
    state = MODULE.SessionState(kind="group", conversation_id="100")
    assert MODULE._display_target(state) == "group:100"


def test_as_id_always_returns_string():
    """发送时不做数值转换：一律字符串（非数字 ID 也就不会抛错）。"""
    assert MODULE._as_id("1234567890123") == "1234567890123"
    assert MODULE._as_id(1234567890123) == "1234567890123"
    assert MODULE._as_id(" 42 ") == "42"
    assert MODULE._as_id("abc") == "abc"
    assert isinstance(MODULE._as_id(42), str)


def test_send_uses_string_conversation_id():
    """兜底发送时传给适配器的必须是字符串。"""
    async def scenario():
        with _tmpdir() as tmp:
            engine, ctx, _ = _build_engine(
                MODULE,
                tmp,
                FakeServices(),
                groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)],
            )
            engine.record("group", "100", "hi", "Eve")
            state = engine._sessions["group:100"]
            state.last_message_time = time.time() - 200 * 60
            await engine.tick()
            assert len(ctx.sent) == 1
            assert ctx.sent[0][1] == "100"
            assert isinstance(ctx.sent[0][1], str)

    asyncio.run(scenario())


def test_proactive_status_shows_current_conversation_id():
    """/proactive 要能回答「我的群 ID 是多少」——配规则之前就得看得到。"""
    async def scenario():
        with _tmpdir() as tmp:
            ctx = await _bootstrap_async(tmp, FakeServices(), groups=[])
            handler = _registered(ctx, "proactive")["handler"]
            command_ctx = _as_super_admin(FakeCommandContext("group", "1234567890123", 1, None))
            result = await handler(command_ctx)
            assert "当前会话" in result
            assert "1234567890123" in result
            assert "命中规则：无" in result
            assert "groups[].guild_id" in result

    asyncio.run(scenario())


def test_proactive_status_reports_matched_rule_and_mapping():
    async def scenario():
        with _tmpdir() as tmp:
            ctx = await _bootstrap_async(
                tmp,
                FakeServices(),
                groups=[_group_rule(MODULE, guild_id="987654321", enable_idle_trigger=True)],
            )
            handler = _registered(ctx, "proactive")["handler"]
            command_ctx = _as_super_admin(FakeCommandContext("group", "1234567890123", 1, None))
            with _fake_identity(**{"1234567890123": 987654321}):
                result = await handler(command_ctx)
            assert "命中规则：987654321" in result
            assert "空闲" in result

    asyncio.run(scenario())


def test_proactive_status_warns_when_triggers_are_off():
    async def scenario():
        with _tmpdir() as tmp:
            ctx = await _bootstrap_async(
                tmp, FakeServices(), groups=[_group_rule(MODULE, guild_id="100")]
            )
            handler = _registered(ctx, "proactive")["handler"]
            command_ctx = _as_super_admin(FakeCommandContext("group", "100", 1, None))
            result = await handler(command_ctx)
            assert "两个触发开关都是关的" in result

    asyncio.run(scenario())


def test_proactive_status_hides_global_state_from_non_admin():
    """普通用户只能拿到自己的会话 ID —— 官方通道上他们得能自己查。"""
    async def scenario():
        with _tmpdir() as tmp:
            ctx = await _bootstrap_async(
                tmp,
                FakeServices(),
                groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True)],
            )
            handler = _registered(ctx, "proactive")["handler"]
            normal = FakeCommandContext("private", "1234567890123", 42, FakeAdminService(MODULE.PERM_EVERYONE))
            result = await handler(normal)
            assert "1234567890123" in result
            assert "privates[].user_id" in result
            # 全局调度状态不能泄露
            assert "总开关" not in result
            assert "跟踪中的会话" not in result

    asyncio.run(scenario())


def test_proactive_status_self_view_mentions_mapping_when_bound():
    async def scenario():
        with _tmpdir() as tmp:
            ctx = await _bootstrap_async(tmp, FakeServices(), groups=[])
            handler = _registered(ctx, "proactive")["handler"]
            normal = FakeCommandContext("private", "1234567890123", 42, FakeAdminService(MODULE.PERM_EVERYONE))
            with _fake_identity(**{"1234567890123": 987654321}):
                result = await handler(normal)
            assert "987654321" in result
            assert "已绑定" in result

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2.1.0a9：官方事件直接给 openid 字符串（不再走 13 位合成号）
# ---------------------------------------------------------------------------


def test_openid_style_conversation_id_matches_real_group_number():
    """非数字的 openid 作为会话 ID 时，映射匹配必须照样成立。"""
    with _tmpdir() as tmp:
        engine, _, _ = _build_engine(
            MODULE,
            tmp,
            FakeServices(),
            groups=[_group_rule(MODULE, guild_id="987654321", enable_idle_trigger=True)],
        )
        with _fake_identity(**{"A1B2C3D4E5F6G7": 987654321}):
            engine.record("group", "A1B2C3D4E5F6G7", "hi", "A")
        assert "group:A1B2C3D4E5F6G7" in engine._sessions
        assert engine._sessions["group:A1B2C3D4E5F6G7"].aliases == ("987654321",)


def test_send_passes_openid_string_through_untouched():
    async def scenario():
        with _tmpdir() as tmp:
            engine, ctx, _ = _build_engine(
                MODULE,
                tmp,
                FakeServices(),
                groups=[_group_rule(MODULE, guild_id="A1B2C3D4E5F6G7", enable_idle_trigger=True)],
            )
            engine.record("group", "A1B2C3D4E5F6G7", "hi", "A")
            state = engine._sessions["group:A1B2C3D4E5F6G7"]
            state.last_message_time = time.time() - 200 * 60
            await engine.tick()
            assert len(ctx.sent) == 1
            assert ctx.sent[0][1] == "A1B2C3D4E5F6G7"
            assert isinstance(ctx.sent[0][1], str)

    asyncio.run(scenario())


def test_openid_id_is_not_mangled_by_int_conversion():
    """openid 含非数字字符时也不能被数值转换弄坏。"""
    assert MODULE._as_id("A1B2-C3D4_E5F6") == "A1B2-C3D4_E5F6"
    assert MODULE._as_id("  A1B2  ") == "A1B2"


def _run_all() -> int:
    tests = [
        (name, obj) for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)
    ]
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
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
