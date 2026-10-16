"""装配期扫扩展目录，把三种扩展变成 runtime 要的对象。

**这里是唯一 import 扩展代码的地方。** core 不 import 扩展 —— 扩展是 core 的用户，
不是它的依赖；bootstrap 是装配层，装配层认识用户。

每个目录按同一套规则扫：

```
<扩展目录>/
  tools/*.py    模块里自己定义的 Tool 子类
  cli/*.py      模块级 COMMANDS 声明
  skills/       目录，交给 SkillSystem
```

**目录不存在不是错误** —— 刚 clone 的仓库没有 `extensions-local/`，仓库里也还没有
`extensions/skills/`。反过来，一个坏文件在启动时就炸，不降级成「少了一个工具」：
模型看不到它会以另一种样子回来（编一个不存在的工具名），比启动失败难查得多。
技能是唯一的例外，它本来就是文件，读坏了只丢这一个。
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import inspect
import logging
import re
import sys
import types
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.protocol.datatype import CliCommand, is_valid_command_name
from core.protocol.interface import Tool

_log = logging.getLogger("oath.extensions")

#: 合成包名前缀。**目录名不能直接当包名**：`extensions-local` 里有 `-`，不是合法标识符，
#: 所以每个扩展目录映射成一个合成包名。两个目录下的同名模块才不会互相顶掉 —— 后缀是路径
#: 哈希，所以不同位置下的同名目录也顶不掉。
_PACKAGE_PREFIX: Final = "oath_ext"

#: 参与发现的子目录。缺一个不是错误，没有它也不是。
_TOOLS: Final = "tools"
_CLI: Final = "cli"
_SKILLS: Final = "skills"


class ExtensionError(Exception):
    """扩展目录里的东西装不起来。起不来就说清楚是哪个文件。"""


@dataclass(frozen=True, slots=True)
class Extensions:
    """一次扫描的结果。进程内定型，之后只读。

    `skill_dirs` 只报目录：技能文件本身由 `SkillSystem` 按同一套 frontmatter 规则扫，
    那里已经有读坏就跳过的逻辑，装配器不重复一遍。
    """

    tools: tuple[Tool, ...] = ()
    commands: tuple[CliCommand, ...] = ()
    skill_dirs: tuple[Path, ...] = ()


def load_extensions(
    extension_dirs: Sequence[Path], *, kinds: frozenset[str] = frozenset({_TOOLS, _CLI, _SKILLS})
) -> Extensions:
    """按给定顺序扫每个扩展目录。"""
    tools: list[tuple[Path, Tool]] = []
    commands: list[tuple[Path, CliCommand]] = []
    skill_dirs: list[Path] = []
    for root in extension_dirs:
        if _TOOLS in kinds:
            tools.extend(_tools(root))
        if _CLI in kinds:
            commands.extend(_commands(root))
        if _SKILLS in kinds and (root / _SKILLS).is_dir():
            skill_dirs.append(root / _SKILLS)
    return Extensions(
        tools=_unique_tools(tools),
        commands=_unique_commands(commands),
        skill_dirs=tuple(skill_dirs),
    )


# --------------------------------------------------------------------------
# 模块装载
# --------------------------------------------------------------------------


def _package_name(root: Path) -> str:
    """一个扩展目录对应一个合成包名。

    后缀是**规范化完整路径的哈希**，不是目录名：配置允许任意目录，两个不同位置下
    同名的目录（`addons/cli/x.py` 与别处的 `addons/cli/x.py`）只按目录名区分不开，
    第二个会被当成装过的那一个，报出来的重名是假的。哈希稳定且不依赖进程 —— 重启后
    同一个目录还是同一个包名，装过不重装的判断才成立。
    """
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:8]
    return f"{_PACKAGE_PREFIX}_{re.sub(r'[^0-9a-zA-Z]', '_', root.name)}_{digest}"


def _subpackage(root: Path, kind: str) -> str:
    """把 `<root>/<kind>` 注册成带 `__path__` 的包，返回它的名字。

    这是同级模块能被 import 的前提：模块里写 `from oath_ext_ext.tools.shared import X`
    走的就是这里挂上去的搜索路径。有 `__init__.py` 就执行它 —— 它是包里唯一一处能放
    共享装配代码的地方。没有也照样能用，只是没有共享代码可放。
    """
    directory = root / kind
    name = f"{_package_name(root)}.{kind}"
    if name in sys.modules:
        return name
    init = directory / "__init__.py"
    if init.is_file():
        # `_exec` 已经把它注册进去了。
        _exec(_spec(name, init, [str(directory)]), name, init)
        return name
    sys.modules[name] = _package(directory, name)
    return name


def _package(directory: Path, name: str) -> types.ModuleType:
    """一个只有搜索路径的空包。"""
    module = types.ModuleType(name)
    module.__path__ = [str(directory)]
    module.__package__ = name
    return module


def _modules(root: Path, kind: str) -> Iterator[tuple[Path, types.ModuleType]]:
    """逐个装 `<root>/<kind>` 下的文件，按文件名排序。

    装过的不再装：一个进程里同一段代码只该有一个类对象 —— 测试里起多个 Runtime 就是
    这个场景，第二次装出另一个类，`isinstance` 就对不上了。重新启动进程自然重新装，
    改配置不需要额外的重载机制。
    """
    directory = root / kind
    package = _subpackage(root, kind)
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.py"), key=lambda item: item.name):
        if path.stem.startswith("_"):
            # `_` 前缀不参与发现（`__init__` 也在这一档），但仍能被同级 import ——
            # 共享的辅助代码放这里。
            continue
        dotted = f"{package}.{path.stem}"
        module = sys.modules.get(dotted)
        if module is None:
            module = _exec(_spec(dotted, path), dotted, path)
        yield path, module


def _spec(name: str, path: Path, search: list[str] | None = None) -> importlib.machinery.ModuleSpec:
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=search)
    if spec is None or spec.loader is None:
        raise ExtensionError(f"读不了扩展模块: {path}")
    return spec


def _exec(spec: importlib.machinery.ModuleSpec, name: str, path: Path) -> types.ModuleType:
    """装一个模块。**先注册再执行** —— 模块里的循环 import 与自 import 才找得到同一个对象。

    执行失败就从 `sys.modules` 撤掉：留一个半初始化的模块在那里，下一次 import 会拿到它，
    报出来的是另一个错。
    """
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as error:
        del sys.modules[name]
        raise ExtensionError(f"扩展模块装不起来: {path}") from error
    return module


# --------------------------------------------------------------------------
# 发现
# --------------------------------------------------------------------------


def _tools(root: Path) -> list[tuple[Path, Tool]]:
    found: list[tuple[Path, Tool]] = []
    for path, module in _modules(root, _TOOLS):
        for cls in _declared_tools(module):
            try:
                found.append((path, cls()))
            except TypeError as error:
                raise ExtensionError(
                    f"{path} 里的 {cls.__name__} 构造要参数。扩展目录里的工具只能零参构造 —— "
                    f"需要依赖的工具由装配层用 Runtime(tools=...) 显式注册。"
                ) from error
    return found


def _declared_tools(module: types.ModuleType) -> list[type[Tool]]:
    """模块里自己定义的 `Tool` 子类。

    `__module__` 那一道筛是必需的：文件顶上 `from core.detail import DetailReadTool`
    是写依赖，不是声明工具 —— 少了它，import 一个基类就把 core 内置工具再注册一遍。
    抽象基类跳过：它是给人继承的，不是给人执行的。
    """
    return [
        obj
        for _, obj in sorted(vars(module).items())
        if inspect.isclass(obj)
        and issubclass(obj, Tool)
        and obj is not Tool
        and obj.__module__ == module.__name__
        and not inspect.isabstract(obj)
    ]


def _commands(root: Path) -> list[tuple[Path, CliCommand]]:
    found: list[tuple[Path, CliCommand]] = []
    for path, module in _modules(root, _CLI):
        for command in getattr(module, "COMMANDS", ()):
            if not isinstance(command, CliCommand):
                raise ExtensionError(f"{path} 的 COMMANDS 元素必须是 CliCommand: {type(command)!r}")
            found.append((path, command))
    return found


# --------------------------------------------------------------------------
# 撞名
# --------------------------------------------------------------------------


def _unique_tools(found: Sequence[tuple[Path, Tool]]) -> tuple[Tool, ...]:
    """工具重名是错，不是覆盖。

    注册表只知道自己已经收了什么，不知道它从哪个文件来的，所以这一步在装配器里做，
    才报得出**两个**文件。
    """
    seen: dict[str, Path] = {}
    tools: list[Tool] = []
    for path, tool in found:
        name = tool.definition.name
        if name in seen:
            raise ExtensionError(f"工具名 {name!r} 重复: {seen[name]} 与 {path}")
        seen[name] = path
        tools.append(tool)
    return tuple(tools)


def _unique_commands(found: Sequence[tuple[Path, CliCommand]]) -> tuple[CliCommand, ...]:
    seen: dict[str, Path] = {}
    commands: list[CliCommand] = []
    for path, command in found:
        if not is_valid_command_name(command.name):
            raise ExtensionError(f"{path} 的命令名不合法: {command.name!r}")
        if command.name in seen:
            raise ExtensionError(f"命令名 {command.name!r} 重复: {seen[command.name]} 与 {path}")
        seen[command.name] = path
        commands.append(command)
    return tuple(commands)


__all__ = ["ExtensionError", "Extensions", "load_extensions"]
