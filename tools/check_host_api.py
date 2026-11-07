#!/usr/bin/env python3
"""宿主 app 侧接口审计：插件依赖的 neobot_app 契约是否还在。

`tools/check_compat.py` 只管 modloader（Plugin / RuntimePluginContext）；
但插件还调用 app 层的命令上下文、卡片渲染和宿主服务，这些漂了同样会炸
（而且往往只在运行时才炸）。本脚本把这些契约固定成断言。

用法::

    # 对着已安装的 neobot_app 检查
    python tools/check_host_api.py

    # 或指定一份 app 源码目录（……/site-packages/neobot_app 或 wheel 解包后的同名目录）
    python tools/check_host_api.py --app /path/to/neobot_app

退出码 0 表示契约未变，1 表示有漂移。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

#: 插件读取的 CommandContext 字段
REQUIRED_CONTEXT_FIELDS = {
    "service",
    "kind",
    "conv_id",
    "user_id",
    "raw_args",
    "args",
}
#: 插件调用的 CommandService 方法
REQUIRED_SERVICE_METHODS = {"send_image_bytes", "reply_to"}
#: 插件使用的卡片渲染入口
REQUIRED_CARD_EXPORTS = {"render_card_html", "render_card_image"}
#: 插件从宿主服务注册表里取的键
REQUIRED_HOST_SERVICES = {"command_service", "notification_hub", "reply_orchestrator"}
#: 插件读的 Command 字段
REQUIRED_COMMAND_FIELDS = {"name", "description", "usage", "permission", "source"}


def _parse(path: Path) -> ast.Module | None:
    if not path.is_file():
        return None
    return ast.parse(path.read_text(encoding="utf-8"))


def _class_fields(tree: ast.Module | None, class_name: str) -> set[str]:
    if tree is None:
        return set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            out: set[str] = set()
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    out.add(item.target.id)
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.add(item.name)
            return out
    return set()


def _module_functions(tree: ast.Module | None) -> set[str]:
    if tree is None:
        return set()
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _class_methods(tree: ast.Module | None, class_name: str) -> set[str]:
    return _class_fields(tree, class_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="NeoBot 宿主 app 接口审计")
    parser.add_argument("--app", default=None, help="neobot_app 源码目录（默认用已安装的）")
    args = parser.parse_args()

    if args.app:
        app = Path(args.app)
    else:
        try:
            import neobot_app  # type: ignore

            app = Path(neobot_app.__file__).parent
        except Exception:
            print("找不到 neobot_app。请用 --app 指定含 commands/ 与 runtime/ 的目录。")
            return 2

    if not (app / "commands").is_dir():
        print(f"{app} 看起来不是 neobot_app 目录（缺少 commands/）")
        return 2

    print(f"参照 neobot_app：{app}\n")
    failures = 0

    def report(title: str, missing: set[str], extra_note: str = "") -> None:
        nonlocal failures
        if missing:
            print(f"  MISS {title}: {sorted(missing)}{extra_note}")
            failures += len(missing)
        else:
            print(f"  OK   {title}")

    print("===== CommandContext =====")
    fields = _class_fields(_parse(app / "commands" / "model.py"), "CommandContext")
    report("字段齐全", REQUIRED_CONTEXT_FIELDS - fields)

    print("\n===== CommandService =====")
    methods = _class_methods(_parse(app / "commands" / "service.py"), "CommandService")
    report("方法齐全", REQUIRED_SERVICE_METHODS - methods)

    print("\n===== Command =====")
    command_fields = _class_fields(_parse(app / "commands" / "model.py"), "Command")
    report("字段齐全", REQUIRED_COMMAND_FIELDS - command_fields)

    print("\n===== html_card 渲染入口 =====")
    facade = _parse(app / "runtime" / "html_card.py")
    exported: set[str] = set()
    for node in ast.walk(facade) if facade else []:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                exported.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exported.add(node.name)
    report("render_card_html / render_card_image 可用", REQUIRED_CARD_EXPORTS - exported)

    print("\n===== 宿主服务注册 =====")
    boot = app / "bootstrap" / "parts" / "application.py"
    text = boot.read_text(encoding="utf-8") if boot.is_file() else ""
    report("服务名存在", {name for name in REQUIRED_HOST_SERVICES if name not in text})

    print(f"\n结论：{'全部通过' if failures == 0 else f'{failures} 处不兼容'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
