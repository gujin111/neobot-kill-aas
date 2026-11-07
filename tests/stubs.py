"""测试用的最小桩实现：neobot_modloader 与 pydantic。

只用于在**没有 NeoBot 运行环境**的机器上验证插件本身的逻辑，
不会随插件分发，也不参与 NeoBot 加载。
"""

from __future__ import annotations

import sys
import types


# ---------------------------------------------------------------------------
# 极简 pydantic 桩（只支持本插件用到的 Field 默认值 / default_factory）
# ---------------------------------------------------------------------------


class _FieldInfo:
    def __init__(self, default=None, *, default_factory=None, **meta):
        self.default = default
        self.default_factory = default_factory
        self.meta = meta


def Field(default=None, *, default_factory=None, **meta):  # noqa: N802 - 对齐 pydantic 命名
    return _FieldInfo(default, default_factory=default_factory, **meta)


def field_validator(*fields, mode="after", **kwargs):  # noqa: N802 - 对齐 pydantic 命名
    """桩：不真的执行校验，原样返回被装饰的对象。

    真实校验（含 mode="before" 的规范化）在 config.py 的 normalize_* 里，
    由 test_config_normalize 系列用例直接覆盖。
    """

    def decorator(func):
        return func

    return decorator


class BaseModel:
    def __init__(self, **data):
        fields: dict[str, _FieldInfo] = {}
        for klass in reversed(type(self).__mro__):
            for name, value in vars(klass).items():
                if isinstance(value, _FieldInfo):
                    fields[name] = value
        for name, info in fields.items():
            if name in data:
                setattr(self, name, data[name])
            elif info.default_factory is not None:
                setattr(self, name, info.default_factory())
            else:
                setattr(self, name, info.default)
        for name, value in data.items():
            if name not in fields:
                setattr(self, name, value)

    def model_dump(self) -> dict:
        return {k: v for k, v in vars(self).items()}


def install_pydantic_stub() -> None:
    module = types.ModuleType("pydantic")
    module.BaseModel = BaseModel
    module.Field = Field
    module.field_validator = field_validator
    sys.modules.setdefault("pydantic", module)


# ---------------------------------------------------------------------------
# neobot_modloader 桩
# ---------------------------------------------------------------------------


class Plugin:
    """记录装饰器注册结果，供断言使用。"""

    instances: list["Plugin"] = []

    def __init__(
        self,
        name,
        *,
        version="0.1.0",
        description="",
        usage="",
        author="",
        config=None,
        command_prefix="/",
        dependencies=(),
        priority=0,
        min_neobot_version=None,
        python_dependencies=(),
        hot_reload=True,
        config_hot_reload=True,
        **kwargs,
    ):
        self.name = name
        self.version = version
        self.description = description
        self.usage = usage
        self.config_model = config
        self.command_prefix = command_prefix
        self.dependencies = tuple(dependencies)
        self.priority = priority
        self.hot_reload = hot_reload
        self.config_hot_reload = config_hot_reload
        #: @plugin.message(...) 注册
        self.messages: list[dict] = []
        #: @plugin.command(...) 注册（本插件未使用，保留以便断言「没有用到」）
        self.commands: list[dict] = []
        self.lifecycle: dict[str, list] = {"load": [], "startup": [], "shutdown": []}
        Plugin.instances.append(self)

    # -- 装饰器 -------------------------------------------------------------
    def message(self, pattern=None, **kwargs):
        def decorator(func):
            self.messages.append({"pattern": pattern, "handler": func, **kwargs})
            return func

        return decorator

    def command(self, pattern, **kwargs):
        def decorator(func):
            self.commands.append({"pattern": pattern, "handler": func, **kwargs})
            return func

        return decorator

    def event(self, **kwargs):  # 2.0.2 才有；本插件不该使用
        def decorator(func):
            return func

        return decorator

    def _lifecycle(self, stage):
        def decorator(func):
            self.lifecycle[stage].append(func)
            return func

        return decorator

    def on_load(self, func):
        return self._lifecycle("load")(func)

    def on_startup(self, func):
        return self._lifecycle("startup")(func)

    def on_shutdown(self, func):
        return self._lifecycle("shutdown")(func)

    def tool(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def capability(self, name):
        def decorator(func):
            return func

        return decorator


class Reply:
    def __init__(self, context=None, event=None):
        self.context = context
        self.event = event
        self.sent: list[object] = []
        self.images: list[dict] = []

    async def send(self, message):
        self.sent.append(message)
        return {"status": "ok"}

    async def image(self, *, url=None, file=None, path=None, data=None, filename=None):
        self.images.append({"data": data, "filename": filename})
        return {"status": "ok"}


def install_modloader_stub() -> None:
    module = types.ModuleType("neobot_modloader")
    module.Plugin = Plugin
    module.Reply = Reply
    sys.modules.setdefault("neobot_modloader", module)
