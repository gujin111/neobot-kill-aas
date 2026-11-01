"""测试用的最小桩实现：neobot_modloader 与 pydantic。

只用于在**没有 NeoBot 运行环境**的机器上验证插件本身的逻辑，
不会随插件分发，也不参与 NeoBot 加载。
"""

from __future__ import annotations

import sys
import types


# ---------------------------------------------------------------------------
# 极简 pydantic 桩（只支持本插件用到的 Field 默认值 / default_factory / 形状）
# ---------------------------------------------------------------------------


class _FieldInfo:
    def __init__(self, default=None, *, default_factory=None, **meta):
        self.default = default
        self.default_factory = default_factory
        self.meta = meta


def Field(default=None, *, default_factory=None, **meta):  # noqa: N802 - 对齐 pydantic 命名
    return _FieldInfo(default, default_factory=default_factory, **meta)


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
    sys.modules.setdefault("pydantic", module)


# ---------------------------------------------------------------------------
# neobot_modloader 桩
# ---------------------------------------------------------------------------


class CommandRegistration:
    def __init__(self, pattern, kwargs):
        self.pattern = pattern
        self.kwargs = kwargs
        head = pattern.split()[0] if pattern.split() else pattern
        self.name = head
        self.permission = kwargs.get("permission")
        self.description = kwargs.get("description", "")
        self.aliases = tuple(kwargs.get("aliases") or ())


class Plugin:
    """记录装饰器注册结果，供断言使用。"""

    instances: list["Plugin"] = []

    def __init__(self, name, *, version="0.1.0", description="", usage="", author="",
                 config=None, dependencies=(), priority=0, hot_reload=True,
                 config_hot_reload=True, **kwargs):
        self.name = name
        self.version = version
        self.description = description
        self.usage = usage
        self.config_model = config
        self.dependencies = tuple(dependencies)
        self.commands: list[CommandRegistration] = []
        self.events: list[dict] = []
        self.lifecycle: dict[str, list] = {"load": [], "startup": [], "shutdown": []}
        Plugin.instances.append(self)

    # -- 装饰器 -------------------------------------------------------------
    def command(self, pattern, *, permission=None, description="", aliases=None, **kwargs):
        def decorator(func):
            self.commands.append(
                CommandRegistration(
                    pattern,
                    {"permission": permission, "description": description, "aliases": aliases, **kwargs},
                )
            )
            return func

        return decorator

    def event(self, *, post_type=None, **kwargs):
        def decorator(func):
            self.events.append({"post_type": post_type, "handler": func, **kwargs})
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

    def message(self, *args, **kwargs):  # 未使用，保留形状
        return self.event(post_type="message")

    def tool(self, *args, **kwargs):
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
