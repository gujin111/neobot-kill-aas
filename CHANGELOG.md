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
| **1.5.1** | — | 官方通道上「不知道该填哪个 ID」：未配规则时不跟踪会话，`/proactive` 看不到 ID |
| **1.5.2** | — | 发送时把会话 ID 转 int 没有必要，且会让非数字 ID 走异常分支 |
| **1.6.0** | — | 官方通道下没人能替别人查私聊 ID，`/proactive` 只对超管开放等于堵死私聊规则 |
| **1.7.0** | — | a9 把官方通道的会话 ID 从 13 位合成号改成 openid 字符串 |
| **1.7.1** | — | 设置页提示重写；`history_message_limit` 真正支持到 1000；失败重试改为「失败 N 次后冷却」 |

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

## 1.5.1

### 问题：官方通道上「不知道该填哪个 ID」

**现象**：升级到官方通道后，用户打开 `/proactive` 想查自己的群 ID，结果只看到
「当前没有正在跟踪的会话」——于是不知道该往配置里填什么。

**根因**：这是个鸡生蛋问题。`record()` 的第一步是查规则，查不到就直接返回、**不创建会话状态**；
而 `/proactive` 的会话列表又只列已跟踪的会话。所以「没配规则」→「看不到 ID」→
「不知道该配什么规则」。

**修复**：`/proactive` 末尾增加一段「当前会话」信息，直接回答三个问题：

- 当前会话的类型与**通道 ID**（就是该填进配置的那个值）；
- 有没有映射到真实 QQ 号/群号（以及该用 `/linkqq` 还是 `/linkgroup` 去绑）；
- 是否命中了规则；命中了但两个触发开关都关着时会直接指出来。

这样即使一条规则都没配，也能在群里发一次 `/proactive` 就把 ID 拿到手。

**没新增命令**：复用了 `/proactive`，避免又多一个要记的入口。

---

## 1.5.2

### 改动：发送时不再把会话 ID 转成 int

**原来的写法**：

```python
def _as_id(value):
    try:
        return int(str(value).strip())   # 数字就转 int
    except (TypeError, ValueError):
        return str(value).strip()        # 非数字原样返回
```

**问题**：这层转换没有收益，却有两个副作用——

1. 非数字 ID（某些平台直接给 openid 字符串）会走异常分支，靠的是「抛了再兜」；
2. 语义上自相矛盾：插件内部 ID 全程是字符串（配置、状态、路由键都是），
   只在这一步变成 int。

**改成**：`_as_id()` 一律返回字符串。

**安全性依据**（对着 v2.1.0a8 源码确认过）：

- `switchable.py:407` 用 `str(conversation.id)` 做会话路由键，字符串与整数等价；
- 官方通道解析 `group_openid` 时自己会 `int(synthetic)`（`official/identity.py`）；
- 两个适配器的 `send_group_msg` 只是把 `group_id` 放进 JSON payload，不做类型假设。

**已知影响**：OneBot 的 JSON 里 `group_id` 现在会是一个字符串（v11 规范写的是 `int64`）。
主流实现（NapCat / go-cqhttp 等）都容忍字符串；万一某个严格实现拒收，
把 `_as_id` 改回 `int()` 即可，只有一行。

---

## 1.6.0

### 问题：官方通道下，私聊规则的 ID 没人查得到

**现象**：想在官方通道上给某个人配私聊规则，结果发现无从下手——
bot 看不到对方的真实 QQ 号，`/proactive` 又只对超级管理员开放。

**根因**：三条限制叠在一起就成了死结：

1. 官方通道只给 `openid`，映射成 13 位合成 ID，**真实 QQ 号对 bot 不可见**；
2. `/linkqq` 需要次级管理员权限，而管理员**不在那个私聊里**、也不知道对方 QQ；
3. `/proactive` 要求超级管理员，普通用户连自己那串 ID 都查不到。

群聊没这个问题：管理员在群里发一次 `/linkgroup <群号>` 就行，一条命令覆盖全群。

**修复**：把 `/proactive` 改成**分档输出**。

- 命令权限从超级管理员降到**所有人**；
- 普通用户只看到自己那一档：
  `你在主动发言里的 QQ：1234567890123` + 「把这串 ID 发给管理员」；
- 全局状态（总开关、规则数、各会话阈值）仍要求 `permissions.can(uid, PERM_SUPER_ADMIN)`，
  由插件内部判定，普通用户拿不到。

这样任何人在私聊里发一次 `/proactive` 就能拿到自己的 ID，转给管理员填进
`privates[].user_id` 即可；不需要额外命令，也不再依赖 `/linkqq` 的权限。

**顺带修的问题**：新写的 `_is_super_admin` 最初调用了 1.4.0 里已删除的 `_ctx_user_id`，
直接 `NameError`。改成就地 `int()` 转换（带异常兜底）。

**权限模型的取舍**：`/proactive` 不再算「核心指令」。但它对普通用户只吐一行自己的 ID，
既不是管理动作也不泄露全局状态——这是官方通道上唯一可行的自助发现方式。

---

## 1.7.0

### 适配 NeoBot v2.1.0a9：官方通道的会话 ID 从「合成号」变成「openid」

**背景**：a9 把官方事件里的 `group_id` / `user_id` 直接改成 `group_openid` /
`member_openid`（`official/events.py`），13 位合成号降级为**兼容遗留**
（`official/identity.py` 注释：「旧数据库、队列、定时任务里残留的 13 位合成号仍可反查成 openid」）。

也就是说，官方通道上的会话 ID 从 **13 位纯数字** 变成了 **非数字的 openid 字符串**。

**结论：插件代码不需要改，但这次是运气。**

1.5.2 把发送时的 `int()` 转换去掉了，ID 全程按字符串透传——正好接住这次变化。
如果当时保留 `int()`，虽然异常分支会兜住（`int("A1B2…")` 抛错后回落原文），
但语义上就成了「靠异常工作」。

**逐项核对结果**：

| 依赖点 | a9 状态 |
|---|---|
| `notification_hub.publish` 签名 | 未变，`_deliver` 直接可用 |
| `CommandContext` 字段 | 未变 |
| `user_identity.qq_for_user` / `group_number_for` | 保留；内部 `_local_candidates` 同时接受 openid 与旧合成号 |
| 官方发送 `send_group_msg(group_id)` | 签名从 `int` 放宽到 `int \| str`，内部 `_openid()` 反查 |
| modloader（`Plugin` / `RuntimePluginContext`） | 42 / 47 个成员，插件用到的 12 项全在 |
| `user_identity.synthetic_for_qq` | **已删除** —— 但插件从未使用它 |

**审计工具的修正**：`check_host_api.py` 原来把 `synthetic_for_qq` 列进必需函数，
在 a9 上报了假阳性。改成只列插件真正调用的 `qq_for_user` / `group_number_for`。

**新增行为（宿主侧，插件自动受益）**：

- **双通道去重**：已 `/linkgroup` 且两通道都在线的群，事件交给 OneBot、发送按群号切通道。
  这类群的会话 ID 是真实群号，规则写真实群号直接生效。
- **后台主动消息显式走 official**：`notification_hub.publish` 不再依赖被动回退，
  插件的主动发言因此能真正送达。
- 新增 `/get openid`、`/get groupid`（所有人）与 `/unlinkqq`、`/unlinkgroup`（超管）。

**新增 3 条用例**：openid 风格的会话 ID 能命中真实群号规则、发送时 openid 原样透传、
openid 含非数字字符时不被数值转换弄坏。

---

## 1.7.1

这一版没有新功能，主要是把设置页里说得含糊的提示重写了一遍；行为上只动了两处：
失败重试按字面改成「失败 N 次后冷却」，以及 `history_message_limit` 不再被 64 条缓存上限截断。

### `work_hours`：格式说明补齐

**现象**：群聊规则原来有一句「如 1,3-5,8-19，支持 22-2 跨零点」，私聊规则只有
「工作时间段，留空表示全天」。用户看不出只支持整点、区间含不含端点、跨零点怎么算。
更麻烦的是写错不会报错：`9:30-18:30`、中文逗号、`24` 这类写法会按「全天」处理。

**根因**：`parse_work_hours()` 只认整点、英文逗号和 `起-止` 区间；无法识别的片段
被静默忽略，全部无效时返回 `None`，等于不限时段。原提示没有把这些边界写出来。

**修复**：群聊/私聊共用 `_WORK_HOURS_HELP`，写清 0-23 整点、英文逗号、区间含两端、
`22-2` 跨零点、留空全天，以及「无法识别即忽略，全部无效按全天」。面板输入框补了
`如 8-19,22-2` 的 placeholder。解析行为没改，避免旧配置因为格式错误突然失效。

### 其他容易误读的设置项

| 设置项 | 原来的问题 | 现在的提示 |
|---|---|---|
| `activity_lower_limit` / `activity_upper_limit` | 叫「灵敏度」「下限/上限」，实际是初始阈值和阈值趋向值；`lower > upper` 时阈值向下走 | 统一为「初始触发阈值」「阈值趋向值」，写明每次成功触发后移动差值的 10% |
| `guild_id` / `user_id` / `apply_default_*` | 只写「群号/用户 ID」，没说明官方通道要填 openid/会话 ID；默认模板的前置条件也没写 | 改成「群号或会话 ID」「QQ 号或会话 ID」，补 `/proactive`、`/linkgroup`、`/linkqq`，说明 id=`default` 规则和精确匹配优先 |
| `idle_interval_minutes` | 「空闲触发间隔」听起来像固定周期 | 写明按「最近消息或上次主动发言（取较晚者）」计时；新私聊规则要先收到一条消息 |
| `enabled` | 看起来是总开关，实际加载时会被 `state.json` 覆盖 | 写明只是首次运行的初始值，之后以 `/proactiveon`、`/proactiveoff` 的运行状态为准 |
| `verbose_log` | 说「完整判断快照」，实际只是多打一行记录日志 | 改成实际行为；`debug_log` 也补了「warning 始终输出」 |
| 提示词模板 / `tick_seconds` / `cooldown_seconds` | 模板没列变量；扫描间隔容易被当成发言间隔；冷却没区分成功/失败 | 列了变量和空值规则；写明 `tick_seconds` 是检查频率、`cooldown_seconds` 只管成功触发 |

`guaranteed_trigger_minutes` 和 `activity_message_interval` 也补了前置条件：都只在
`enable_activity_trigger` 打开时生效；保底按「最近消息或上次主动发言」计时，所以群里
一直有新消息时不会触发。

### `history_message_limit` 实际只缓存 64 条

**现象**：把 `history_message_limit` 从 20 调到 100，`{history}` 里最多还是 64 条。

**根因**：`SessionState.messages` 是 `deque(maxlen=64)`；`record()` 虽然按配置裁剪，
但 deque 本身先丢掉了超过 64 条的部分。`_load()` 恢复状态时也写死了 64。

**修复**：两处都改成 `maxlen=1000`，与字段的 5–1000 范围一致。默认 20 不变；
只有主动调大才会多占内存和 `state.json` 空间。新增用例：上限 100、写 120 条，保留 100 条。

### `max_retry_attempts` 现在真的是「失败 N 次后冷却」

**现象**：默认 `max_retry_attempts=3`，但第一次投递失败后，下一轮就不再尝试，
直接进入 `failure_cooldown_seconds`；这个配置看起来完全没起作用。

**根因**：`_evaluate()` 里无条件检查 `now < failure_cooldown_until`，而每次失败都会写
这个时间戳。`tick()` 里虽然有一个 `failures >= max_retry_attempts` 的跳过条件，
但 `_evaluate()` 自己会提前返回，所以两条路径结果一样——第一次失败就冷却。

**修复**：抽出 `_failure_cooldown_active()`，同时要求
`failures >= max(1, max_retry_attempts)` 且仍在冷却期。现在前 N-1 次失败会在
触发条件仍满足时继续尝试，第 N 次失败后才暂停 `failure_cooldown_seconds`；成功一次清零，
`0` 按 `1` 处理。新增用例覆盖「前两次失败继续重试、第三次后暂停、冷却结束后继续」。

### 验证

- `python tests/test_engine.py` → 19/19
- `python tests/test_integration.py` → 63/63（新增 2 条：失败重试、历史消息上限）
- `python -m compileall __init__.py config.py engine.py` → 通过
- 用 pydantic 2.14 实导入 `config.py`，确认两个 `work_hours` 的 `description` 和
  `placeholder` 都进入 schema

`README.md` 与 `plugin.toml` 的注释同步到了这一版。

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
