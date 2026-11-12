# 变更与问题记录

按 **现象 → 根因 → 修复** 记录每个版本踩到的问题。报错原文保留，方便按错误信息搜到这里。

## 总览

| 版本 | 提交 | 这个版本的主要问题 |
|---|---|---|
| **1.0.0** | `c948e52` `abd9ddb` | 装不上（缺 `plugin.toml`）；装上后加载失败（用了高版本才有的 API） |
| **1.1.0** | `548e734` | 换成跨版本都有的接口，能在 modloader 2.0.1 上加载 |
| **1.2.0** | `3f3915b` | 一条写错的规则就把整份配置打回默认值 |
| **1.2.1** | `e9acbe7` | TOML 里号码不带引号（int）时规则被丢弃 |
| **1.3.0** | `d3aabea` | 适配 modloader 2.1.0a3，并补上可复跑的双版本审计工具 |
| **1.3.1** | `0cf3f10` | 容错只修在加载期，面板保存仍被拒 → 「添加不了私聊规则」 |
| **1.4.0** | `e2caedd` | `/helpimg` 与本体 `/help` 语义重叠，删除并统一入口 |
| **1.5.0** | — | 适配 2.1.0a8 官方机器人通道：会话 ID 变成 13 位合成 ID，按真实 QQ 号配的规则失效 |

---

## 1.0.0

初次发布：把 `koishi-plugin-chatluna-proactive-trigger` 的能力重写成 NeoBot 插件。

### 问题一：面板安装时报「压缩包内未找到插件目录（缺少 plugin.toml）」

**现象**：面板「插件 → 安装」填仓库后直接失败。

**根因**：NeoBot 的 `installer._extract()` 解压 zipball 后**只在第一层子目录里找
`plugin.toml`**，不递归。GitHub 的 zipball 天然套了一层 `<repo>-main/`，
而当时 `plugin.toml` 被我放在了 `proactive/` 子目录里，等于多了一层。

```python
roots = [child for child in destination.iterdir() if child.is_dir()]
candidates = [root for root in roots if (root / "plugin.toml").is_file()]
...
raise PluginInstallError("压缩包内未找到插件目录（缺少 plugin.toml）")
```

**修复**（`abd9ddb`）：把 `plugin.toml` / `__init__.py` / `config.py` / `engine.py`
提到**仓库根目录**，与 NeoBot SDK `init` 生成的工程布局一致。

### 问题二：加载失败 `'Plugin' object has no attribute 'event'`

**现象**：安装成功但启用时报上面这行。

**根因**：我按仓库 `main` 分支（modloader **2.0.2a14**）写了代码，
而用户装的是 PyPI 稳定版 **2.0.1**。有三个 API 是 2.0.2 才加的：

| 用了（2.0.2+） | 2.0.1 上的表现 |
|---|---|
| `@plugin.event(post_type="message")` | `'Plugin' object has no attribute 'event'` |
| `@plugin.command(..., permission=...)` | `command() got an unexpected keyword argument 'permission'` |
| `ctx.create_task(...)` | `'RuntimePluginContext' object has no attribute 'create_task'` |

**这也是这一版最大的教训**：不能拿"仓库里有什么"当"用户的环境有什么"。

---

## 1.1.0

### 换成跨版本都存在的接口

| 用途 | 改前（2.0.2+） | 改后（2.0.1 就有） |
|---|---|---|
| 注册命令 | `@plugin.command(..., permission=...)` | `ctx.app_commands.register(name, description=, usage=, permission=)` |
| 监听消息 | `@plugin.event(post_type="message")` | `@plugin.message(priority=50)` |
| 后台循环 | `ctx.create_task(...)` | `asyncio.create_task(...)` + `on_shutdown` 里取消 |

发现方式：把 **2.0.1 的 wheel 从 PyPI 下下来解包**，用 AST 解析 `plugin.py` / `context.py`
取真实成员表，再逐项对比——从此变成 `tools/check_compat.py`。

### 附带的坑：版本号不一致会被拒绝加载

`plugin.toml` 的 `version` 必须与 `Plugin(version=...)` **完全一致**：

```python
raise ValueError(f"plugin.toml version 与 Plugin(...) version 不一致: {manifest_version!r} != {plugin.version!r}")
```

1.1.0 改版本号时只改了一处，差点又炸一次。现在有测试守着
（`test_plugin_toml_version_matches_plugin_object`）。

---

## 1.2.0

### 问题：一条写错的规则，整份配置回退默认值

**现象**：配置里 `privates` 写错后，群聊规则也一起失效了。

**根因**：加载逻辑是

```python
try:
    config = ProactiveConfig(**raw_config)
except Exception:
    config = ProactiveConfig()      # ← 整个配置清零，只留一行日志
```

而报错长这样：

```
privates.0 Input should be a valid dictionary or instance of PrivateRule
```

即 `privates = ["123456"]` —— 元素是字符串，不是表。TOML 里正确写法是 `[[privates]]`。

**修复**：

- 容忍「只写 id」的简写（`["123456"]` → `{user_id: "123456"}`）
- 坏条目单独跳过并记提示，其余规则照常生效
- 整体校验失败时改为**逐项回退**，不再一键清零
- 问题回显到 `/proactive` 的「配置告警」

---

## 1.2.1

### 问题：号码不带引号就丢规则

**现象**：`user_id = 123456789`（TOML 里裸数字）的规则不生效。

**根因**：TOML 把裸数字解析成 `int`，而 `user_id` 声明是 `str`，
**pydantic v2 不会把 int 自动转成 str**，该规则直接校验失败被丢弃。

**修复**：装配规则前先把 id 字段转成字符串；`apply_default_group_configs` /
`apply_default_private_configs` 同样接受 int 列表。

---

## 1.3.0

### 适配 modloader 2.1.0a3（不改实现）

把 2.1.0a3 的 `neobot-modloader` 与 `neobot-app` 两个 wheel 都下下来解包对比，结论是**不用改**：

- `Plugin` 成员 36 → 42，**只增不减**（新增 `event` / `action` / `resource` / `guide` / `view` / `surface`）
- `RuntimePluginContext` 成员数 47 → 47，**一个没变**
- 依赖注入约定名、消息匹配逻辑未变；`_injected_value` 只是**新增**两种注入
- `plugin.contract.json` / `[sdk]` **不在加载路径里**，loader 仍只读 `plugin.toml` 的三项
- app 侧 `CommandContext` 十字段、`CommandService.send_image_bytes`、`render_card_*`、宿主服务名全部未变
- 唯一变动：`SpeechBlocked` 从 `neobot_adapter.speaking` 挪到 `neobot_contracts.ports.speaking`，
  本插件按**类名**捕获，不受影响

### 附带的工具修复

`tools/check_compat.py` 读版本号时读错了文件——`version.py` 里只有版本**比较函数**，
没有 `__version__` 常量，之前会显示成包名。改从 `dist-info/METADATA` 读。

同时支持重复传 `--ref`，一次对比多个 modloader 版本。

---

## 1.3.1

### 问题：面板里「添加不了私聊规则」

**现象**：面板里新增私聊规则后保存不上去。

**根因**：1.2.0/1.2.1 的容错**只写在插件加载期**（`_coerce_rules`），
而面板保存走的是宿主自己的校验：

```python
validated = model.model_validate({**defaults, **values})   # config.py:80
```

也就是 NeoBot 用**同一个 pydantic 模型**先校验了。我的容错根本没参与，
所以在面板/TOML 里写简写依然被拒。

**修复**：把规范化挪进 **`mode="before"` 的 `field_validator`**，
面板保存与插件加载因此共用同一份逻辑：

```python
@field_validator("privates", mode="before")
@classmethod
def _loose_privates(cls, value):
    items, _ = normalize_rule_list(value, "user_id")
    return items
```

顺带收紧垃圾值判定：`null` / 空串 / `0` 一律丢弃（QQ 号、群号不会 ≤ 0）。

**教训**：容错要修在校验发生的那一层，修在"读配置"那一层是自我安慰。

---

## 1.4.0

### 问题：`/helpimg` 与本体 `/help` 语义重叠

**现象**：两个入口都在回答「有哪些指令」。

**根因**：本体 `/help` 本来就会遍历 `registry.commands()`、按调用者权限过滤，
并给插件命令标 `[来源: <插件名>]`：

```python
visible = [
    command for command in ctx.service.registry.commands()
    if ctx.service.permissions.can(ctx.user_id, command.permission)
]
```

插件命令注册进同一张表，所以**本来就在 `/help` 里**。另造一个入口等于维护两份判定，
权限/来源标记迟早不一致。

**修复**：删掉 `/helpimg` 及其全部支撑代码（约 167 行），并删除配置里的 6 个
`help_image_*` 字段。指令总览统一交给 `/help`。

副作用是好的：不再依赖 `html_card` / `ctx.screenshots`，少了一条
「没有 Chromium 就降级」的路径。

---

## 1.5.0

### 问题：官方机器人通道上线后，按真实 QQ 号配的规则全部失效

**现象**：NeoBot 2.1.0a8 把新部署的默认通道改成官方机器人（`adapter.mode=official`），
升级后主动发言不再触发，`/proactive` 里「跟踪中的会话」是空的。

**根因**：官方通道的 `group_id` / `user_id` 不是真实 QQ 号，而是
**13 位合成 ID**（由 `openid` 派生）。而规则里写的是真实群号/QQ 号，
`rule_for()` 拿合成 ID 去比，永远不相等 → 连消息都不记录。

官方文档也说明了：`group_openid` 没有到 QQ 群号的映射，二者只能**人工绑定**
（NeoBot 提供 `/linkqq`、`/linkgroup`，落在 `data/user_qq_map.json`）。

**修复**：

- 新增 `_resolve_aliases()`，通过 `neobot_app.user_identity` 的
  `qq_for_user` / `group_number_for` 把合成 ID 映射回真实号；
- `rule_for()` 改成对**候选 ID 集合**匹配（合成 ID + 真实号），
  两种写法都能命中，`apply_default_*` 也一并按候选集判断；
- 会话状态记录 `aliases`，`/proactive` 显示成
  `group:1234567890123（真实群号 987654321）`，方便直接复制；
- 发送时不再无脑 `int(conversation_id)`，非数字 ID 原样透传（`_as_id`）；
- `tools/check_host_api.py` 增加对身份映射函数的检查。

**没改的部分**：官方通道的事件本来就是 OneBot 形状
（`post_type` / `message_type` / `group_id` / `user_id` / `message` / `sender` 都在），
消息钩子无需改动；发送侧由宿主的 `SwitchableAdapter` 按「会话出现在哪条通道」路由，
插件调 `ctx.send_group()` 即可。

**遗留**：映射是人工的。没绑定时只能用合成 ID 配规则，这点在 README 里写清楚了。

---

## 尚未验证 / 已知残留

- **没有官方验收报告**：`scripts/plugin_ci.py` 硬性要求 Python 3.13/3.14，
  开发机是 3.12 且没有 `uv`，跑不了；也没有 `plugin.contract.json`。
- **没有真机装载**：`neobot_modloader` 依赖 pydantic / sqlalchemy / httpx 一整条链，
  装不起来。所有兼容性结论来自**静态契约比对**（`tools/check_compat.py`、
  `tools/check_host_api.py`），不是真实运行。
- **`/help` 里插件命令的实际展示未在真机确认**（普通用户看不到超管命令是预期行为）。
- **面板旧配置残留**：1.4.0 之前的 `plugins_data/proactive/config.toml` 里可能还留着
  `help_image_*`，pydantic 默认忽略未知字段，不影响加载，但建议手动删掉。
