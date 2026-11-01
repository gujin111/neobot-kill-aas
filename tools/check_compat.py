#!/usr/bin/env python3
"""兼容性审计：把插件用到的宿主 API，逐个对 modloader 源码验证存在性。

起因：插件 1.0.0 用了 ``@plugin.event(...)``，而它只在 modloader 2.0.2+ 存在，
在 PyPI 稳定版 2.0.1 上直接以 ``'Plugin' object has no attribute 'event'`` 启动失败。
本脚本用 AST 解析宿主源码取真实成员表，避免再凭文档或记忆写 API。

用法::

    # 对着已安装的 neobot_modloader 检查（能 import 就行）
    python tools/check_compat.py

    # 或指定一份 modloader 源码目录（含 plugin.py / context.py）
    python tools/check_compat.py --ref /path/to/neobot_modloader
    python tools/check_compat.py --ref ./_modloader-audit/2.0.1/neobot_modloader

    # 或指定要检查的插件文件
    python tools/check_compat.py --target __init__.py

退出码 0 表示全部通过，1 表示存在不兼容用法。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

#: 已知仅高版本才有的接口（名字 -> 说明）。命中即判不兼容。
KNOWN_NEWER_ONLY = {
    "event": "Plugin.event（modloader 2.0.2+）",
    "action": "Plugin.action（modloader 2.0.2+）",
    "resource": "Plugin.resource（modloader 2.0.2+）",
    "guide": "Plugin.guide（modloader 2.0.2+）",
    "view": "Plugin.view（modloader 2.0.2+）",
}

CTX_LIKE_NAMES = {"ctx", "context"}
CTX_LIKE_ATTRS = {"_ctx", "_context"}


def class_members(path: Path, class_name: str) -> set[str]:
    """类的方法名、@property 名，以及 __init__ 里 self.x = ... 的属性。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    members: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                members.add(item.name)
                if item.name != "__init__":
                    continue
                for sub in ast.walk(item):
                    if (
                        isinstance(sub, ast.Assign)
                        and isinstance(sub.targets[0], ast.Attribute)
                        and isinstance(sub.targets[0].value, ast.Name)
                        and sub.targets[0].value.id == "self"
                    ):
                        members.add(sub.targets[0].attr)
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                members.add(item.target.id)
    return members


def _is_ctx_like(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in CTX_LIKE_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in CTX_LIKE_ATTRS
    return False


def collect_uses(path: Path) -> dict[str, set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {"plugin": set(), "ctx": set(), "getattr": set()}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "plugin":
                out["plugin"].add(node.attr)
            if _is_ctx_like(node.value):
                out["ctx"].add(node.attr)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
                key = node.args[1]
                if _is_ctx_like(node.args[0]) and isinstance(key, ast.Constant) and isinstance(key.value, str):
                    out["getattr"].add(key.value)
    return out


def resolve_ref(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.is_dir() else None
    try:
        import neobot_modloader  # type: ignore

        return Path(neobot_modloader.__file__).parent
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="NeoBot 插件 API 兼容性审计")
    parser.add_argument("--target", default="__init__.py", help="要检查的插件文件")
    parser.add_argument("--ref", default=None, help="modloader 源码目录（默认用已安装的）")
    args = parser.parse_args()

    target = Path(args.target)
    if not target.is_file():
        print(f"找不到目标文件：{target}")
        return 2

    ref = resolve_ref(args.ref)
    if ref is None or not (ref / "plugin.py").is_file():
        print("找不到 modloader 源码。请用 --ref 指定含 plugin.py / context.py 的目录。")
        return 2

    plugin_members = class_members(ref / "plugin.py", "Plugin")
    ctx_members = class_members(ref / "context.py", "RuntimePluginContext")

    try:
        from neobot_modloader.version import __version__ as ref_version  # type: ignore
    except Exception:
        ref_version = "未知"
    print(f"参照 modloader：{ref}（版本 {ref_version}）")
    print(f"  Plugin 成员 {len(plugin_members)} 个 · RuntimePluginContext 成员 {len(ctx_members)} 个\n")

    uses = collect_uses(target)
    failures = 0

    print(f"===== plugin.<attr>（{len(uses['plugin'])} 个）=====")
    for attr in sorted(uses["plugin"]):
        ok = attr in plugin_members and attr not in KNOWN_NEWER_ONLY
        note = f"  ← {KNOWN_NEWER_ONLY[attr]}" if attr in KNOWN_NEWER_ONLY else ""
        print(f"  {'OK  ' if ok else 'MISS'} plugin.{attr}{note}")
        failures += 0 if ok else 1

    print(f"\n===== ctx.<attr>（{len(uses['ctx'])} 个）=====")
    for attr in sorted(uses["ctx"]):
        ok = attr in ctx_members
        print(f"  {'OK  ' if ok else 'MISS'} ctx.{attr}")
        failures += 0 if ok else 1

    print(f'\n===== getattr(ctx, "..." )（{len(uses["getattr"])} 个）=====')
    for name in sorted(uses["getattr"]):
        ok = name in ctx_members
        print(f"  {'OK  ' if ok else 'MISS'} getattr(ctx, {name!r})")
        failures += 0 if ok else 1

    print(f"\n结论：{'全部通过' if failures == 0 else f'{failures} 处不兼容'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
