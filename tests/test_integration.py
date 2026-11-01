"""插件集成测试：用桩 SDK 真导入插件包，验证注册结果与调度行为。

可直接 `python tests/test_integration.py` 运行，也可由 pytest 收集。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
import tempfile
import time
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
    """优先用系统临时目录；受限环境下回落到工程内的一次性目录。

    受限环境可能：禁止在系统临时目录建目录、建了却不可写、或禁止删除目录。
    三种情况都探测/兜底，且清理失败不影响测试结论。
    """
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
    path = ROOT / ".tmp-tests" / f"case{time.time_ns()}"
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


class FakeCtx:
    def __init__(self, data_dir, services):
        self.data_dir = Path(data_dir)
        self.logger = FakeLogger()
        self.plugin_host = FakeHost(services)
        self.sent: list[tuple[str, int, object]] = []
        self.tasks: list = []
        self.config: dict = {}

    async def send_group(self, group_id, message):
        self.sent.append(("group", group_id, message))
        return {"status": "ok"}

    async def send_private(self, user_id, message):
        self.sent.append(("private", user_id, message))
        return {"status": "ok"}

    def create_task(self, coro, name=None):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


class FakeCommand:
    def __init__(self, name, permission, description="", usage="", source=""):
        self.name = name
        self.permission = permission
        self.description = description
        self.usage = usage
        self.source = source


class FakeRegistry:
    def __init__(self, commands):
        self._commands = commands

    def commands(self):
        return list(self._commands)


class FakePermissions:
    def __init__(self, level_of_user):
        self._level = level_of_user

    def can(self, user_id, level):
        return self._level.get(int(user_id), 0) >= level


class FakeCommandService:
    def __init__(self, commands, level_of_user):
        self.registry = FakeRegistry(commands)
        self.permissions = FakePermissions(level_of_user)
        self.images: list[dict] = []

    async def send_image_bytes(self, kind, conv_id, data, *, at_user_id=None, filename=None):
        self.images.append({"kind": kind, "conv_id": conv_id, "data": data, "filename": filename})
        return True


class FakeCommandContext:
    def __init__(self, kind, conv_id, user_id, service):
        self.kind = kind
        self.conv_id = conv_id
        self.user_id = user_id
        self.service = service


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


def _build_engine(module, tmpdir, services, **config_overrides):
    config = _make_config(module, **config_overrides)
    ctx = FakeCtx(tmpdir, services)
    return module.ProactiveEngine(ctx, config), ctx, config


# ---------------------------------------------------------------------------
# 注册结果
# ---------------------------------------------------------------------------


def test_plugin_identity_and_export():
    assert PLUGIN.name == "proactive"
    assert PLUGIN.version == "1.0.0"
    assert PLUGIN.config_model.__name__ == "ProactiveConfig"


def test_only_letter_slash_commands_are_registered():
    names = [c.name for c in PLUGIN.commands]
    assert names == ["helpimg", "proactive", "proactiveon", "proactiveoff", "proactivetest"], names
    for name in names:
        assert name.isalpha(), f"{name} is not pure letters"


def test_help_image_command_is_public_and_core_commands_are_super_admin_only():
    perms = {c.name: c.permission for c in PLUGIN.commands}
    assert perms["helpimg"] == MODULE.PERM_EVERYONE
    for name in ("proactive", "proactiveon", "proactiveoff", "proactivetest"):
        assert perms[name] == MODULE.PERM_SUPER_ADMIN, name


def test_message_recording_hook_is_registered_for_all_messages():
    assert len(PLUGIN.events) == 1
    assert PLUGIN.events[0]["post_type"] == "message"


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
                groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True, cooldown_seconds=3600)],
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
                groups=[_group_rule(MODULE, guild_id="100", enable_idle_trigger=True, work_hours=str(blocked_hour))],
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
                groups=[_group_rule(MODULE, guild_id="100", enable_activity_trigger=True, activity_message_interval=3)],
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


def test_force_trigger_without_rule_returns_message():
    async def scenario():
        with _tmpdir() as tmp:
            engine, _, _ = _build_engine(MODULE, tmp, FakeServices(), groups=[])
            result = await engine.force_trigger("group", "999")
            assert isinstance(result, str) and result

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
# 指令图片
# ---------------------------------------------------------------------------


def _fake_commands():
    return [
        FakeCommand("help", MODULE.PERM_EVERYONE, "show help", "[command]"),
        FakeCommand("mg", MODULE.PERM_EVERYONE, "minigame", "[game]", source="minigame"),
        FakeCommand("reload", MODULE.PERM_SUPER_ADMIN, "reload config", source="dashboard"),
    ]


def test_collect_commands_filters_by_permission():
    service = FakeCommandService(_fake_commands(), {1001: MODULE.PERM_EVERYONE, 2002: MODULE.PERM_SUPER_ADMIN})
    with _tmpdir() as tmp:
        ctx = FakeCtx(tmp, FakeServices(command_service=service))
        normal = MODULE._collect_commands(ctx, 1001)
        admin = MODULE._collect_commands(ctx, 2002)
    assert {row["name"] for row in normal} == {"help", "mg"}
    assert {row["name"] for row in admin} == {"help", "mg", "reload"}


def test_collect_commands_supports_keyword_filter():
    service = FakeCommandService(_fake_commands(), {2002: MODULE.PERM_SUPER_ADMIN})
    with _tmpdir() as tmp:
        ctx = FakeCtx(tmp, FakeServices(command_service=service))
        rows = MODULE._collect_commands(ctx, 2002, "reload")
        assert [row["name"] for row in rows] == ["reload"]


def test_collect_commands_without_service_returns_empty():
    with _tmpdir() as tmp:
        ctx = FakeCtx(tmp, FakeServices())
        assert MODULE._collect_commands(ctx, 1) == []


def test_command_rows_render_expected_labels_and_blocks():
    rows = [
        {"name": "help", "usage": "[command]", "description": "show help",
         "permission": MODULE.PERM_EVERYONE, "source": ""},
    ]
    assert MODULE._command_label(rows[0]) == "/help [command]"
    blocks = MODULE._rows_to_blocks(rows, True)
    assert blocks[0]["kind"] == "rows"
    assert blocks[0]["variant"] == "commands"
    assert blocks[0]["rows"][0][0] == "/help [command]"


def test_fallback_text_lists_every_command():
    rows = [
        {"name": "help", "usage": "", "description": "show help",
         "permission": MODULE.PERM_EVERYONE, "source": ""},
        {"name": "reload", "usage": "", "description": "reload",
         "permission": MODULE.PERM_SUPER_ADMIN, "source": "dashboard"},
    ]
    text = MODULE._fallback_text(rows, "TITLE")
    assert "/help" in text and "/reload" in text


def test_help_image_returns_none_when_renderer_missing():
    """测试机上没有 neobot_app，渲染模块导入失败时必须安全返回 None。"""

    async def scenario():
        with _tmpdir() as tmp:
            ctx = FakeCtx(tmp, FakeServices())
            rows = [{"name": "help", "usage": "", "description": "show help",
                     "permission": 0, "source": ""}]
            assert await MODULE._build_help_image(ctx, rows, MODULE.ProactiveConfig()) is None
            assert await MODULE._build_help_image(ctx, [], MODULE.ProactiveConfig()) is None

    asyncio.run(scenario())


def test_helpimg_command_falls_back_to_text_without_browser():
    async def scenario():
        with _tmpdir() as tmp:
            service = FakeCommandService(_fake_commands(), {1001: MODULE.PERM_EVERYONE})
            ctx = FakeCtx(tmp, FakeServices(command_service=service))
            reply = stubs.Reply()
            func = None
            for candidate in vars(MODULE).values():
                if callable(candidate) and getattr(candidate, "__name__", "") == "_cmd_helpimg":
                    func = candidate
            assert func is not None
            command_context = FakeCommandContext("group", "100", 1001, service)
            result = await func(
                ctx=ctx,
                reply=reply,
                command_context=command_context,
                keyword=None,
                config=MODULE.ProactiveConfig(),
            )
            assert isinstance(result, str)
            assert "/help" in result and "/mg" in result
            assert "reload" not in result

    asyncio.run(scenario())


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
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
