# proactive · NeoBot 插件

把 Koishi 插件 **`koishi-plugin-chatluna-proactive-trigger` 0.3.18** 的「主动发言」能力移植到
[NeoBot](https://github.com/SuperQuail/NeoBot)：空闲触发与活跃度触发，用宿主服务把话真正说出去。

- 插件名：`proactive`（与 NeoBot 官方内置插件不重名）
- 类型：**第三方插件**，放进 `plugins/` 目录即可，不改 NeoBot 源码
- 依赖：NeoBot 本体（Python 3.13/3.14）+ `pydantic`（本体已自带）
- 版本：1.5.2
- 历次版本踩到的坑与修法：[CHANGELOG.md](CHANGELOG.md)

### 兼容性（重要）

只使用**跨版本都存在**的接口，因此同一份代码在 2.0.1 与 2.1.0a3 上都能跑：

| 用途 | 本插件用的 | 没有使用的（仅高版本有） |
|---|---|---|
| 注册命令 | `ctx.app_commands.register(name, description=, usage=, permission=)` | `@plugin.command(..., permission=...)` |
| 监听消息 | `@plugin.message(priority=50)` | `@plugin.event(post_type="message")` |
| 后台循环 | `asyncio.create_task(...)` + `on_shutdown` 里取消 | `ctx.create_task(...)` |

> 1.0.0 用了后一列的三个接口，在 modloader 2.0.1 上会以
> `'Plugin' object has no attribute 'event'` 启动失败。1.1.0 起全部替换。

| modloader 版本 | 结论 | 依据 |
|---|---|---|
| 2.0.1（PyPI 稳定版） | ✅ 通过 | `tools/check_compat.py` 12 项 API 全部存在 |
| 2.1.0a3（最新预发布） | ✅ 通过，**无需改代码** | 同上；另加 app 侧契约审计 |
| 2.1.0a3 的 app 侧 | ✅ 通过 | `CommandContext` 字段、`send_image_bytes`、`render_card_*`、宿主服务名均未变 |

2.1.0a3 相对 2.0.1 的差异只影响“能不能用新写法”，不影响本插件：

- `Plugin` 新增 `event / action / resource / guide / view / surface`（只增不减）
- `RuntimePluginContext` 成员数不变
- 依赖注入的约定名、消息匹配逻辑均未变；`_injected_value` 只是**新增**了两种注入
- `plugin.contract.json` / `[sdk]` **不在加载路径里**，loader 仍只读 `plugin.toml` 的
  `enabled` / `priority` / `[config]`

另外注意：`plugin.toml` 里的 `version` 必须与 `Plugin(version=...)` 完全一致，
否则 modloader 会以「plugin.toml version 与 Plugin(...) version 不一致」拒绝加载。

---

## 一、为什么是重写而不是改造

原包是 Koishi 的 **TypeScript/Node** 插件，NeoBot 是 **Python** 框架，两者没有可复用的代码层：
Koishi 的 `Service`/`Context`、ChatLuna 的 room 与历史池，在 NeoBot 里都不存在。

所以这是一次**按语义移植**：触发算法（活跃度打分、空闲调度、阈值自适应）原样搬过来，
而「怎么把话真正说出去」换成 NeoBot 的宿主服务链路。

| 原插件（Koishi + ChatLuna） | 本插件（NeoBot） |
|---|---|
| `session.guildId` / `session.userId` | 入站事件 `message_type` + `group_id`/`user_id` |
| ChatLuna 的 room 与历史池 | 自建会话状态 + `state.json` 持久化 |
| `session.send()` | `notification_hub.publish()` → `reply_orchestrator` → `ctx.send_group` |
| Koishi 命令树 | 宿主命令表 `ctx.app_commands`（`/` 前缀、权限等级 0/1/2） |
| `h.text()` / `h.image()` | `html_card` 卡片 + `Reply.image()` |

原插件里依赖 ChatLuna 才有的部分（每群一个 proactive room、图片本地缓存与 `[图片:hash]` 标记）
在 NeoBot 上没有对应概念，未移植，见文末「与上游的差异」。

---

## 二、指令

**全部是 `/` + 纯字母**，注册进宿主命令表，因此 `/help` 会列出它们、权限由宿主统一判定。

| 指令 | 权限 | 说明 |
|---|---|---|
| `/proactive` | 超级管理员 | 查看调度状态：总开关、规则数、跟踪中的会话、各自阈值与空闲时长 |
| `/proactiveon` | 超级管理员 | 打开主动发言总开关（立刻写盘） |
| `/proactiveoff` | 超级管理员 | 关闭主动发言总开关 |
| `/proactivetest` | 超级管理员 | 在当前会话**立即**试触发一次，用来验证回复通道是否通 |

### 指令总览只有 `/help` 一个入口

本体 `/help` 本来就会遍历 `registry.commands()`、按调用者权限过滤、并给插件命令标上
`[来源: proactive]`。本插件把命令注册进同一张表，所以 **`/help` 里自然能看到它们**。

因此插件**不再自己实现任何帮助入口**（早期版本有个 `/helpimg`，会把指令渲染成图片，
和 `/help` 干的是同一件事，1.4.0 已删除）。这样做的原因：

- 两个入口会各自维护一份"什么是可用指令"的判定，权限/来源标记迟早不一致；
- `/help` 有分页、缓存、图文与纯文本降级，插件版不可能做得更好；
- 删掉之后少一条命令、少一套 `html_card` 依赖，面板保存和配置项也跟着简化了。

`/help` 的过滤规则：普通用户只看得到 `permission=所有人` 的命令，
超级管理员能看到全部（含本插件的 4 条）。

### 官方机器人通道（NeoBot 2.1.0a8+）

2.1.0a8 引入了「官方机器人」通道，并把新部署的默认值改成 `adapter.mode=official`。
它对插件的影响只有一件事：**会话 ID 换了**。

| | OneBot 通道 | 官方通道 |
|---|---|---|
| `group_id` / `user_id` | 真实 QQ 群号 / QQ 号（5~11 位） | **13 位合成 ID**（由 `openid` 映射而来） |
| 与真实号码的关系 | 就是号码本身 | 没有 API 可以互转，只能**人工绑定** |

NeoBot 提供管理员命令 `/linkqq`（@用户 + QQ 号）与 `/linkgroup`（当前群 + 群号）建立映射，
落在 `data/user_qq_map.json`。本插件会读这份映射（`neobot_app.user_identity`）：

- **规则里写真实 QQ 号/群号**：只要会话建立了映射就能命中——OneBot 环境本来就命中，
  官方通道绑定后同样命中；
- **规则里写合成 ID**：直接命中，不需要绑定；
- 两种写法都配了规则时，按配置顺序取第一条精确匹配。

所以官方通道上的推荐做法是：先让管理员用 `/linkqq`、`/linkgroup` 把常用群/人绑好，
规则统一写真实号码——这样 OneBot 和官方两条通道**共用同一份配置**。

没绑定也可以：**在群里发一次 `/proactive`**，它会多出一段「当前会话」信息，
直接告诉你该填什么：

```
当前会话：
  类型：group
  通道 ID：1234567890123
  真实群号：（未绑定）
  命中规则：无（这个会话不会被记录，也就不会主动发言）
  → 把 1234567890123 填进 groups[].guild_id
     或先用 /linkgroup <群号> 绑定真实号码，之后直接填真实号码
```

绑定之后同一段会显示 `真实群号：987654321` 与 `命中规则：987654321（空闲）`；
如果规则命中了但两个触发开关都没开，它也会直接指出来。

> 官方通道另有两条平台限制（与插件无关）：不能 @ 其他用户（@ 由宿主合成）、
> 主动消息有条数频控。本插件发的是**纯文本**，不受 @ 限制；
> 但频控在平台侧，官方通道上建议把 `cooldown_seconds` 调大。

---

## 三、主动发言

### 三个触发来源

| 触发 | 条件 | 适用 |
|---|---|---|
| **空闲触发** | 距最后一条消息（或上次触发）超过 `idle_interval_minutes` | 群聊冷场补位、私聊自然开场 |
| **活跃度触发** | 多时间窗口消息密度打分 ≥ 当前阈值 | 群里聊得热闹时自然接话 |
| **计数兜底** | 攒够 `activity_message_interval` 条消息 | 分数没到也保证不沉默 |

此外还有**保底触发** `guaranteed_trigger_minutes`：超过这么久没触发过就强制来一次。

### 活跃度打分（与上游同一套参数）

三个窗口的消息速率 → 两条 logistic 曲线加权 → 突发加成 → 新鲜度衰减：

```
recent 90s 窗口 × 0.65 + instant 20s 窗口 × 0.35
突发（30s 内 >12 条/分）额外 +0.25
再乘新鲜度系数 0.55 + 0.45 × exp(-距今/60s · ln2)
最终 clamp 到 0~1
```

**阈值自适应**：`activity_lower_limit` 与 `activity_upper_limit`
- 两者相等 → 固定阈值
- 下限 < 上限 → 每次触发后阈值升高（**越聊越克制**）
- 下限 > 上限 → 每次触发后阈值降低（**越聊越积极**）

### 话怎么说出去

三级链路，任意一级失败自动降级：

1. `notification_hub.publish(...)` —— 首选，会先过 NeoBot 的发言策略（发言时段/静默）；
2. `reply_orchestrator.start_background_reply(...)` —— 通知中心不可用或会话已有活跃管线时跳过；
3. `ctx.send_group()/send_private()` —— 最后兜底，直接发一条纯文本。

> **注意**：`ctx.agent_reply()` **不能**用于定时触发。它只在插件消息处理器的执行期内有效，
> 在定时器里调用会静默返回 `False`。这是 NeoBot 的既定行为，不是本插件的限制。

---

## 四、安装

### 方式一：面板从仓库安装（推荐）

面板「插件 → 安装」，仓库填 `gujin111/neobot-kill-aas`，分支 `main`，安装后启用。

> 本仓库遵循 NeoBot 的工程惯例：**仓库根目录就是插件目录**（`plugin.toml` 与 `__init__.py` 在根上）。
> NeoBot 的安装器解压 zipball 后只在**第一层子目录**里找 `plugin.toml`，不递归；
> 所以插件文件不能藏在 `proactive/` 之类的子目录里，否则会报
> 「压缩包内未找到插件目录（缺少 plugin.toml）」。

### 方式二：手动复制

把仓库内容放进 NeoBot 的插件目录（默认 `./plugins`，由 `plugins.dir` 决定），目录名用 `proactive`：

```text
NeoBot/
└─ plugins/
   └─ proactive/
      ├─ plugin.toml
      ├─ __init__.py
      ├─ config.py
      └─ engine.py
```

只需要这四个文件；`tests/`、`pyproject.toml`、`README.md` 是开发用的，可以不拷。

### 之后

1. 重启 NeoBot（或面板「插件 → 重新扫描」）。
2. 面板「插件 → proactive」里**启用**插件，然后到「配置」页填群号与触发参数。
3. 群里发 `/help` 能看到 `[来源: proactive]` 的 4 条命令；用 `/proactivetest` 验证主动发言通道。

### 最小可用配置

```toml
# plugins_data/proactive/config.toml
enabled = true

[[groups]]
guild_id = "123456789"          # 你的群号
enable_idle_trigger = true
idle_interval_minutes = 120
enable_activity_trigger = true
activity_lower_limit = 0.85
activity_upper_limit = 0.85
activity_message_interval = 20
cooldown_seconds = 30
history_message_limit = 20
```

默认模板用法：写一条 `guild_id = "default"` 的规则，再把群号列进
`apply_default_group_configs`。优先级固定为
**精确匹配 > 默认模板命中 > 无规则**。

### 规则列表的写法（注意 TOML 语法）

`groups` / `privates` 是**对象数组**，每个元素必须是一张表：

```toml
# ✅ 正确：[[groups]] 每出现一次就是一条规则
[[groups]]
guild_id = "123456789"
enable_idle_trigger = true

[[privates]]
user_id = "123456789"
enable_idle_trigger = true

# ❌ 错误：元素是字符串，会报
#    privates.0 Input should be a valid dictionary or instance of PrivateRule
privates = ["123456789"]
```

面板里的「规则列表」控件（`model_list`）会给每条规则渲染独立的小表单，用它添加最省心；
手写 TOML 时记住 `[[...]]` 是「追加一张表」，`[...] = [...]` 是「赋一个值」，别混。

**容错**：`normalize_rule_list` 做在 **pydantic 的 `mode="before"` 校验器**里，
所以**面板保存**（`model_validate`）和**插件加载**走的是同一份规范化：

- `["123456789"]` / `[123456789]` → 当成 `user_id` / `guild_id`
- 号码不带引号（TOML 里是 int）→ 自动转字符串
- 单个值不是列表 → 按一条处理
- `null` / 空串 / `0` 等垃圾条目 → 跳过，其余仍然生效

跳过的问题会在日志里提示，也会在 `/proactive` 的「配置告警」里回显。
注意：面板**保存前**走的是宿主自己的 `model_validate`，所以规范化必须放在模型内部才有效——
这也是 1.3.1 把逻辑从加载期挪进校验器的原因。

### 模板变量

`activity_prompt_template` / `idle_prompt_template` 支持：

| 变量 | 含义 |
|---|---|
| `{history}` | 本轮历史消息（受 `history_message_limit` 限制） |
| `{time}` / `{date}` | 当前时间 / 年月日与星期 |
| `{group_name}` | 会话标识（私聊为空） |
| `{user_name}` | 预留，当前为空 |
| `{idle_minutes}` | 已空闲分钟数 |

---

## 五、验证情况

| 项目 | 结果 |
|---|---|
| `tests/test_engine.py`（纯逻辑，19 条） | **19/19 通过** |
| `tests/test_integration.py`（桩 SDK 集成，56 条） | **56/56 通过** |
| modloader 侧审计（2.0.1 / 2.1.0a3 双版本） | **12/12 项 API 两个版本都存在** |
| app 侧审计（2.1.0a8：CommandContext / 身份映射 / 服务名） | **全部通过** |
| `compileall`（全部源文件语法） | 通过 |
| 状态持久化 | 实测：写入 `state.json` 后重启引擎可恢复 `enabled` 与历史池 |

覆盖到的行为：注册的命令名与权限（只注册 4 条、且不含任何 help* 命令）、命令描述、
注册器不可用时跳过、规则优先级、空闲触发投递、冷却拦截、工作时间段拦截、计数兜底触发、
活跃度打分触发、发言策略拒绝时放弃、通知中心缺失时降级到管线、会话已有管线时跳过、
无服务时回落纯文本、总开关、状态持久化、配置宽松输入的规范化、
消息钩子（记录 / 跳过命令与自身消息 / 图片折叠）。

**兼容性审计怎么做的**：两个脚本，都对着宿主源码做 AST 解析，不靠文档也不靠记忆。

`tools/check_compat.py`（modloader 侧）解析 `plugin.py` 与 `context.py`，取出 `Plugin` 和
`RuntimePluginContext` 的真实成员表，再对照本插件用到的每个 `plugin.<attr>` /
`ctx.<attr>` / `getattr(ctx, "...")` 逐个验证存在性。支持一次传多个 `--ref` 做矩阵对比。

`tools/check_host_api.py`（app 侧）固定 `CommandContext` 字段和宿主服务名——这些漂了通常只在运行时才炸。

```bash
# modloader：对着已安装的检查，或对比多个版本
python tools/check_compat.py
python tools/check_compat.py --ref /path/to/modloader-2.0.1 --ref /path/to/modloader-2.1.0a3

# app：命令上下文 / 宿主服务
python tools/check_host_api.py --app /path/to/neobot_app
```

**没有验证到的部分（必须说明）**：

- 测试机是 **Python 3.12**，且没有 `uv`，无法运行 NeoBot 官方的 `scripts/plugin_ci.py`
  （它硬性要求 3.13/3.14），因此**没有官方验收报告**，也没有 `plugin.contract.json`；
- 没有把 `neobot_modloader` 真跑起来（它依赖 pydantic / sqlalchemy / httpx 一整条链），
  所以兼容性结论来自**静态契约比对**，不是真机装载；
- 没有真实 QQ 账号与 NeoBot 完整运行环境，`/help` 里插件命令的实际展示未在真机确认；
- `notification_hub` / `reply_orchestrator` 均按源码签名对接，
  并用同形状的假对象跑通，但未与真实实例联调。

上线前请按第四节走一遍：发 `/help` 确认能看到 4 条插件命令，再用 `/proactivetest` 看通道是否通。

---

## 六、与上游的差异

| 上游能力 | 本插件 |
|---|---|
| 每群一个 proactive room + 触发后清空 ChatLuna 历史 | 用自建会话池代替（清空消息池、保留阈值） |
| 图片本地缓存与 `[图片:hash]` 标记 | 只把图片折叠成 `[图片]` 文本占位 |
| `enableQuoteReplyByMessageId` 注入 `message_id` | 未实现 |
| 多群/多私聊规则与默认模板 | 完整保留 |
| 工作时间段（含 `22-2` 跨零点） | 完整保留 |
| 失败冷却、最大重试次数 | 完整保留 |
| 空活跃度打分、阈值自适应、保底触发 | 完整保留（同一套参数） |

---

## 七、目录结构

仓库根目录**就是插件目录**（与 NeoBot SDK `init` 生成的工程一致）：

```text
neobot-kill-aas/           ← 仓库根 = 插件目录，安装后成为 plugins/proactive/
├─ plugin.toml             ← 插件清单与打包默认配置
├─ __init__.py             ← 插件装配、指令、消息钩子、调度引擎
├─ config.py               ← pydantic 配置模型（面板据此生成表单）
├─ engine.py               ← 纯逻辑：打分/时间窗/模板，不依赖 NeoBot
├─ pyproject.toml          ← pytest 配置（开发用）
├─ README.md
├─ CHANGELOG.md            ← 每个版本踩到的问题与修法
├─ tools/
│  ├─ check_compat.py      ← modloader 侧：对照源码验证用到的 API，支持多版本对比
│  └─ check_host_api.py    ← app 侧：命令上下文 / 宿主服务契约
└─ tests/
   ├─ stubs.py             ← 测试用最小桩（pydantic + modloader）
   ├─ test_engine.py       ← 纯逻辑测试
   └─ test_integration.py  ← 集成测试
```

`engine.py` 不 import 任何 NeoBot 模块，所以可以脱离宿主单独跑测试——这是刻意设计的边界。
