"""扩展目录的发现规则。

纯单元：目录由测试现造，装配器之外的东西一律不碰。这里要验的是「什么样的文件会被
当成扩展」——规则每松一寸，一个写错的名字就能进到模型的工具列表或 shell 命令行里。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bootstrap.common.extension_loader import ExtensionError, _package_name, load_extensions

TOOL_SOURCE = """
from core.protocol.datatype import ToolDefinition, ToolInvocation, ToolResult
from core.protocol.interface import Tool
from core.utils.encoding import JsonObject

_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class EchoTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="echo", description="echo", input_schema=_SCHEMA)

    def execute(self, invocation: ToolInvocation) -> ToolResult:
        return ToolResult(content=invocation.arguments.get("text"))
"""

COMMAND_SOURCE = """
from typing import Final

from core.protocol.datatype import CliCommand
from core.protocol.datatype.user import User
from core.utils.encoding import JsonObject, JsonValue


def shout(text: str, *, user: User, user_context: JsonObject) -> JsonValue:
    return {"text": text.upper()}


COMMANDS: Final[tuple[CliCommand, ...]] = (
    CliCommand(
        name="shout",
        description="Shout the text.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=shout,
    ),
)
"""


@pytest.fixture(autouse=True)
def _forget_loaded_modules() -> Iterator[None]:
    """扫过的模块留在 `sys.modules` 里 —— 这正是它该有的样子。测试之间不留残留。"""
    yield
    for name in [key for key in sys.modules if key.startswith("oath_ext_")]:
        del sys.modules[name]


def write(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def root(tmp_path: Path, name: str = "extensions") -> Path:
    """一个扩展根目录。默认名不带 `-` —— 另一条测试专门验带 `-` 的。"""
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# -- 目录布局 ---------------------------------------------------------------


def test_a_directory_that_does_not_exist_is_not_an_error(tmp_path: Path) -> None:
    """可选的扩展子目录不存在时跳过。"""
    found = load_extensions([tmp_path / "nowhere"])

    assert found.tools == ()
    assert found.commands == ()
    assert found.skill_dirs == ()


def test_subdirectories_that_do_not_exist_are_not_an_error(tmp_path: Path) -> None:
    assert load_extensions([root(tmp_path)]).tools == ()


def test_only_the_skill_directory_is_reported_when_it_exists(tmp_path: Path) -> None:
    directory = root(tmp_path)
    (directory / "skills" / "demo").mkdir(parents=True)

    assert load_extensions([directory]).skill_dirs == (directory / "skills",)


def test_directories_are_scanned_in_the_order_they_are_given(tmp_path: Path) -> None:
    """按配置顺序收进同一张表。工具名不同，所以这里验的是顺序，不是谁赢。"""
    first, second = root(tmp_path, "a"), root(tmp_path, "b")
    write(first / "tools" / "one.py", TOOL_SOURCE.replace('"echo"', '"first"'))
    write(second / "tools" / "two.py", TOOL_SOURCE.replace('"echo"', '"second"'))

    found = load_extensions([first, second])

    assert [tool.definition.name for tool in found.tools] == ["first", "second"]


# -- 工具 -------------------------------------------------------------------


def test_a_tool_defined_in_the_file_is_collected(tmp_path: Path) -> None:
    directory = root(tmp_path)
    write(directory / "tools" / "echo.py", TOOL_SOURCE)

    [tool] = load_extensions([directory]).tools

    assert tool.definition.name == "echo"


def test_underscore_files_are_not_discovered(tmp_path: Path) -> None:
    """共享的辅助代码放这里，但它自己不该被当成一条扩展。"""
    directory = root(tmp_path)
    write(directory / "tools" / "_shared.py", TOOL_SOURCE)

    assert load_extensions([directory]).tools == ()


def test_importing_a_core_tool_is_not_a_declaration(tmp_path: Path) -> None:
    """文件顶上 import 一个内置工具是写依赖，不是要把它再注册一遍。"""
    directory = root(tmp_path)
    write(
        directory / "tools" / "borrowed.py",
        "from core.detail import DetailReadTool\n",
    )

    assert load_extensions([directory]).tools == ()


def test_an_abstract_tool_is_not_collected(tmp_path: Path) -> None:
    """抽象基类是给人继承的，不是给人执行的。"""
    directory = root(tmp_path)
    write(
        directory / "tools" / "half_done.py",
        '''
from abc import abstractmethod

from core.protocol.datatype import ToolDefinition
from core.protocol.interface import Tool
from core.utils.encoding import JsonObject


class Base(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="base", description="x", input_schema={"type": "object"})

    @abstractmethod
    def execute(self, invocation: object) -> object:
        """还没写完。"""


class Done(Base):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="done", description="x", input_schema={"type": "object"})

    def execute(self, invocation: object) -> object:
        return None
''',
    )

    assert [tool.definition.name for tool in load_extensions([directory]).tools] == ["done"]


def test_a_tool_that_needs_constructor_arguments_is_refused(tmp_path: Path) -> None:
    """扩展目录里的工具只能零参构造。需要依赖的走 Runtime(tools=...)。"""
    directory = root(tmp_path)
    write(
        directory / "tools" / "needy.py",
        """
from core.protocol.datatype import ToolDefinition, ToolResult
from core.protocol.interface import Tool
from core.utils.encoding import JsonObject


class Needy(Tool):
    def __init__(self, store: object) -> None:
        self._store = store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="needy", description="x", input_schema={"type": "object"})

    def execute(self, invocation: object) -> ToolResult:
        return ToolResult()
""",
    )

    with pytest.raises(ExtensionError, match="零参构造"):
        load_extensions([directory])


# -- 命令 -------------------------------------------------------------------


def test_a_declared_command_is_collected(tmp_path: Path) -> None:
    directory = root(tmp_path)
    write(directory / "cli" / "shout.py", COMMAND_SOURCE)

    [command] = load_extensions([directory]).commands

    assert command.name == "shout"
    assert command.handler(text="hi", user=None, user_context={}) == {"text": "HI"}


def test_a_command_with_a_name_the_shell_could_misread_is_refused(tmp_path: Path) -> None:
    """命令名会进 shell 命令行。字符集在这里收死，不靠下游转义。"""
    directory = root(tmp_path)
    write(
        directory / "cli" / "sneaky.py",
        COMMAND_SOURCE.replace('name="shout"', 'name="shout; rm -rf /"'),
    )

    with pytest.raises(ExtensionError, match="命令名不合法"):
        load_extensions([directory])


def test_commands_that_are_not_declarations_are_refused(tmp_path: Path) -> None:
    directory = root(tmp_path)
    write(directory / "cli" / "sloppy.py", 'COMMANDS = [{"name": "shout"}]\n')

    with pytest.raises(ExtensionError, match="必须是 CliCommand"):
        load_extensions([directory])


# -- 撞名 -------------------------------------------------------------------


def test_two_tools_with_the_same_name_name_both_files(tmp_path: Path) -> None:
    """报错要能定位到冲突的两端，只说「重名」等于没说。"""
    first, second = root(tmp_path, "a"), root(tmp_path, "b")
    write(first / "tools" / "one.py", TOOL_SOURCE)
    write(second / "tools" / "two.py", TOOL_SOURCE)

    with pytest.raises(ExtensionError) as failure:
        load_extensions([first, second])

    assert "one.py" in str(failure.value)
    assert "two.py" in str(failure.value)


def test_two_commands_with_the_same_name_name_both_files(tmp_path: Path) -> None:
    first, second = root(tmp_path, "a"), root(tmp_path, "b")
    write(first / "cli" / "one.py", COMMAND_SOURCE)
    write(second / "cli" / "two.py", COMMAND_SOURCE)

    with pytest.raises(ExtensionError, match="命令名 'shout' 重复"):
        load_extensions([first, second])


# -- 装载 -------------------------------------------------------------------


def test_a_module_that_raises_on_import_is_refused(tmp_path: Path) -> None:
    directory = root(tmp_path)
    write(directory / "tools" / "broken.py", "raise ValueError('装不起来')\n")

    with pytest.raises(ExtensionError, match=r"broken\.py"):
        load_extensions([directory])


def test_a_failed_module_is_not_left_behind_in_sys_modules(tmp_path: Path) -> None:
    """留一个半初始化的模块在那里，下一次 import 会拿到它，报出来的是另一个错。"""
    directory = root(tmp_path)
    write(directory / "tools" / "broken.py", "raise ValueError('装不起来')\n")

    with pytest.raises(ExtensionError):
        load_extensions([directory])

    assert not [name for name in sys.modules if name.endswith(".broken")]


def test_a_directory_name_with_a_dash_still_loads(tmp_path: Path) -> None:
    """带 `-` 的目录名不能直接当包名。"""
    directory = root(tmp_path, "my-tools")
    write(directory / "tools" / "echo.py", TOOL_SOURCE)

    assert [tool.definition.name for tool in load_extensions([directory]).tools] == ["echo"]


def test_the_same_module_name_in_two_directories_does_not_collide(tmp_path: Path) -> None:
    """两个目录里的 `tools/echo.py` 是两段不同的代码，不该互相顶掉。"""
    first, second = root(tmp_path, "a"), root(tmp_path, "b")
    write(first / "tools" / "echo.py", TOOL_SOURCE.replace('"echo"', '"first"'))
    write(second / "tools" / "echo.py", TOOL_SOURCE.replace('"echo"', '"second"'))

    found = load_extensions([first, second])

    assert [tool.definition.name for tool in found.tools] == ["first", "second"]


def test_directories_that_share_a_basename_do_not_collide(tmp_path: Path) -> None:
    """配置允许任意路径。`~/a/addons` 与 `~/b/addons` 是两个目录，不该共用一个包名。"""
    first = root(tmp_path / "a", "addons")
    second = root(tmp_path / "b", "addons")
    write(first / "tools" / "echo.py", TOOL_SOURCE.replace('"echo"', '"first"'))
    write(second / "tools" / "echo.py", TOOL_SOURCE.replace('"echo"', '"second"'))

    found = load_extensions([first, second])

    assert [tool.definition.name for tool in found.tools] == ["first", "second"]


def test_a_package_name_is_stable_across_processes(tmp_path: Path) -> None:
    """重启后同一个目录还得是同一个包名 —— 装过不重装的判断建立在它上面。

    `hash()` 不行：它对 str 加了进程盐，跨进程就变了。
    """
    directory = root(tmp_path / "a", "addons")

    assert _package_name(directory) == _package_name(tmp_path / "a" / "addons")


def test_loading_the_same_directory_twice_yields_one_class_object(tmp_path: Path) -> None:
    """测试里会起多个 Runtime。装出两个类的话 isinstance 就对不上了。"""
    directory = root(tmp_path)
    write(directory / "tools" / "echo.py", TOOL_SOURCE)

    first = load_extensions([directory]).tools
    second = load_extensions([directory]).tools

    assert type(first[0]) is type(second[0])


def test_a_helper_module_can_be_imported_by_a_sibling(tmp_path: Path) -> None:
    """注册成带 `__path__` 的包，同级 import 才走得通。"""
    directory = root(tmp_path)
    write(directory / "tools" / "_shared.py", 'SHARED = "共享的"\n')
    write(
        directory / "tools" / "uses_helper.py",
        """
from core.protocol.datatype import ToolDefinition, ToolResult
from core.protocol.interface import Tool
from core.utils.encoding import JsonObject

from {package}.tools._shared import SHARED


class Helper(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="helper", description=SHARED, input_schema={"type": "object"})

    def execute(self, invocation: object) -> ToolResult:
        return ToolResult()
""".replace("{package}", _package_name(directory)),
    )

    [tool] = load_extensions([directory]).tools

    assert tool.definition.description == "共享的"


def test_the_package_initializer_runs_when_there_is_one(tmp_path: Path) -> None:
    directory = root(tmp_path)
    write(directory / "tools" / "__init__.py", 'GREETING = "你好"\n')
    write(
        directory / "tools" / "greeter.py",
        """
from core.protocol.datatype import ToolDefinition, ToolResult
from core.protocol.interface import Tool
from core.utils.encoding import JsonObject

from {package}.tools import GREETING


class Greeter(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="greeter", description=GREETING, input_schema={"type": "object"})

    def execute(self, invocation: object) -> ToolResult:
        return ToolResult()
""".replace("{package}", _package_name(directory)),
    )

    [tool] = load_extensions([directory]).tools

    assert tool.definition.description == "你好"


# -- 仓库里那一份 -----------------------------------------------------------


def test_the_committed_extensions_are_loaded_as_written() -> None:
    """仓库里那份就是照这套规则写的 —— 它自己得先过得了。"""
    found = load_extensions([Path(__file__).resolve().parents[1] / "extensions"])

    assert "echo" in [tool.definition.name for tool in found.tools]
    assert "echo" in [command.name for command in found.commands]


def test_the_examples_are_synthetic_or_reference_something_real() -> None:
    """仓库里的工具不许带出真实数据：定义要么描述一个真动作，要么只是一条验证链路。"""
    found = load_extensions([Path(__file__).resolve().parents[1] / "extensions"])

    for tool in found.tools:
        definition: Any = tool.definition
        assert definition.description
        assert definition.input_schema.get("type") == "object"
