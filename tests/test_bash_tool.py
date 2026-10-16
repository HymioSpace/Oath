"""bash 工具与宿主机执行环境。

这里真的起子进程 —— 测的就是这层包装：cwd 落在哪、超时收不收得住、输出留多少、
环境里有什么。命令全是短命令，跑在 tmp_path 下。
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from bootstrap.common.config import from_dict
from core.protocol.datatype.scope import Scope
from core.protocol.datatype.tool import ToolInvocation, ToolResult
from core.protocol.error import ToolExecutionError
from core.shell import BashTool, HostEnvironment
from core.shell.bash_tool import workspace_note

SCOPE = Scope("u1", "s1")


def tool(root: Path, seen: list[Scope] | None = None) -> BashTool:
    """一个 bash 工具，以及（可选）记录每次执行属于哪个 scope 的清单。

    **cli_env 按调用现算** —— 身份不能写死在环境变量里，否则所有 session 共用一个。
    """

    def cli_env(scope: Scope) -> Mapping[str, str]:
        if seen is not None:
            seen.append(scope)
        return {"OATH_CLI_TOKEN": f"token-{scope.session_id}"}

    return BashTool(HostEnvironment(root), cli_env, root=root)


def run(bash: BashTool, **arguments: Any) -> dict[str, Any]:
    result: ToolResult = bash.execute(ToolInvocation(SCOPE, "inv-1", arguments, deadline=None))
    return cast(dict[str, Any], json.loads(cast(str, result.content)))


def test_the_definition_names_the_workspace_it_defaults_to(tmp_path: Path) -> None:
    definition = tool(tmp_path).definition

    assert definition.name == "bash"
    assert definition.input_schema["required"] == ["command"]
    assert str(tmp_path) in definition.description


def test_a_command_runs_in_the_workspace_by_default(tmp_path: Path) -> None:
    root = tmp_path / "workspace"

    outcome = run(tool(root), command="pwd")

    assert outcome["exit_code"] == 0
    # root 还没有被建出来 —— 第一次执行把它建出来再进。
    assert outcome["stdout"].strip() == str(root)
    assert root.is_dir()


def test_cwd_overrides_the_workspace_when_the_user_asks_for_it(tmp_path: Path) -> None:
    """workspace 是默认不是围栏。用户点名要去别处，模型给 cwd 就让它去。"""
    elsewhere = tmp_path / "elsewhere"

    outcome = run(tool(tmp_path / "workspace"), command="pwd", cwd=str(elsewhere))

    assert outcome["stdout"].strip() == str(elsewhere)
    assert outcome["cwd"] == str(elsewhere)


def test_the_result_carries_what_happened_and_how_long_it_took(tmp_path: Path) -> None:
    outcome = run(tool(tmp_path), command="printf out; printf err 1>&2")

    assert (outcome["exit_code"], outcome["stdout"], outcome["stderr"]) == (0, "out", "err")
    assert outcome["timed_out"] is False
    assert outcome["truncated"] is False
    assert outcome["duration_ms"] >= 0


def test_a_failing_command_is_a_result_not_a_tool_failure(tmp_path: Path) -> None:
    """非零退出码是模型的反馈，抛成失败就把最该读到的东西从结果里拿走了。"""
    outcome = run(tool(tmp_path), command="exit 3")

    assert outcome["exit_code"] == 3


def test_the_environment_stops_a_command_that_never_ends(tmp_path: Path) -> None:
    """收手的是执行环境，不是工具层 —— 工具的 deadline 到点只是 core 不再等那个线程，
    杀不掉它。命令必须在这里有界。"""
    result = HostEnvironment(tmp_path).run(
        "printf started; sleep 30", cwd=tmp_path, timeout=0.3, env={}
    )

    assert result.timed_out is True
    assert result.exit_code is None
    # 已经打出来的内容留着 —— 模型据此知道它跑到哪一步。
    assert result.stdout == "started"


def test_the_deadline_caps_how_long_the_command_may_run(tmp_path: Path) -> None:
    """工具层的 deadline 只是提示，但命令用它收紧自己的超时，别占着槽位不放。"""
    started = time.monotonic()
    result = tool(tmp_path).execute(
        ToolInvocation(SCOPE, "inv-1", {"command": "sleep 30"}, deadline=time.monotonic() + 0.2)
    )

    assert json.loads(cast(str, result.content))["timed_out"] is True
    assert time.monotonic() - started < 5


def test_output_longer_than_the_cap_keeps_its_ends(tmp_path: Path) -> None:
    """只留上限那么多：不设限的话一条 `yes` 就能把内存吃满，早于任何超时。"""
    outcome = run(tool(tmp_path), command="head -c 200000 /dev/zero | tr '\\0' 'a'")

    assert outcome["truncated"] is True
    assert "已省略" in outcome["stdout"]
    assert len(outcome["stdout"]) < 200_000


def test_the_cli_environment_is_built_per_invocation(tmp_path: Path) -> None:
    """身份跟着这次调用走，不跟着工具走。"""
    seen: list[Scope] = []
    bash = tool(tmp_path, seen)

    bash.execute(ToolInvocation(Scope("u1", "s9"), "inv-1", {"command": "true"}, None))

    assert seen == [Scope("u1", "s9")]


def test_the_environment_variables_reach_the_command(tmp_path: Path) -> None:
    outcome = run(tool(tmp_path), command='printf %s "$OATH_CLI_TOKEN"')

    assert outcome["stdout"] == "token-s1"


def test_the_oath_cli_directory_is_prepended_to_the_path(tmp_path: Path) -> None:
    """`oath-cli` 就靠这个被找到 —— bin_dirs 不写死成某一个名字。"""
    root = tmp_path / "workspace"
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "my-cli").write_text("#!/bin/sh\nprintf mine\n")
    (tmp_path / "bin" / "my-cli").chmod(0o755)
    bash = BashTool(
        HostEnvironment(root, bin_dirs=(tmp_path / "bin",)), lambda _scope: {}, root=root
    )

    outcome = run(bash, command="my-cli")

    assert outcome["stdout"] == "mine"


def test_an_empty_command_is_refused_before_any_process_starts(tmp_path: Path) -> None:
    with pytest.raises(ToolExecutionError):
        run(tool(tmp_path), command="   ")


def test_output_far_past_the_cap_stays_within_it(tmp_path: Path) -> None:
    """输出边读边丢，内存占用与命令写了多少无关 —— 读完再截的话，5MB 只是开头。"""
    outcome = run(tool(tmp_path), command="head -c 5000000 /dev/zero | tr '\\0' 'a'")

    assert outcome["truncated"] is True
    assert len(outcome["stdout"]) < 100_000


def test_the_runtime_s_own_environment_does_not_reach_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单继承：`dict(os.environ)` 会把模型 API key 交给任意一条 bash 命令。"""
    monkeypatch.setenv("OATH_MODEL_API_KEY", "server-side-secret")

    outcome = run(tool(tmp_path), command='printf %s "${OATH_MODEL_API_KEY:-}"')

    assert outcome["stdout"] == ""


def test_the_environment_variable_the_caller_passes_does_reach_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单只挡继承 —— 调用方明确给的照样要盖得住同名值。"""
    bash = BashTool(HostEnvironment(tmp_path), lambda _s: {"HOME": "/tmp/somewhere"}, root=tmp_path)

    outcome = run(bash, command='printf %s "$HOME"')

    assert outcome["stdout"] == "/tmp/somewhere"


@pytest.mark.skipif(shutil.which("bash") is None, reason="这台机器上没有 bash")
def test_the_command_really_runs_in_bash(tmp_path: Path) -> None:
    """工具叫 bash，`[[ ]]` 就该能用 —— `/bin/sh` 在不少系统上是 dash。"""
    outcome = run(tool(tmp_path), command="[[ 1 == 1 ]] && printf yes")

    assert outcome["exit_code"] == 0
    assert outcome["stdout"] == "yes"


def test_a_relative_cli_directory_is_resolved_before_it_reaches_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相对路径按 runtime 进程的 cwd 解析。

    命令默认落在 workspace 下 —— `cli_bin_dir` 若照原样进 `PATH`，命令就会去找
    `workspace/scripts/my-cli`，默认配置下 `oath-cli` 直接找不到。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scripts").mkdir()
    shim = tmp_path / "scripts" / "my-cli"
    shim.write_text("#!/bin/sh\nprintf mine\n")
    shim.chmod(0o755)
    config = from_dict({"agent": {"workspace_root": "./ws", "cli_bin_dir": "./scripts"}})
    bash = BashTool(
        HostEnvironment(config.agent.workspace_root, bin_dirs=(config.agent.cli_bin_dir,)),
        lambda _s: {},
        root=config.agent.workspace_root,
    )

    outcome = run(bash, command="command -v my-cli")

    assert outcome["exit_code"] == 0
    assert outcome["stdout"].strip() == str(shim)


def test_an_unusable_environment_is_a_tool_failure_not_a_command_result(tmp_path: Path) -> None:
    """环境起不来与命令跑失败是两件事：前者是工具失败，后者是结果。"""

    class Broken(HostEnvironment):
        def run(self, command: str, **kwargs: Any) -> Any:
            raise OSError("no such directory")

    bash = BashTool(Broken(tmp_path), lambda _scope: {}, root=tmp_path)

    with pytest.raises(ToolExecutionError, match="执行环境不可用"):
        bash.execute(ToolInvocation(SCOPE, "inv-1", {"command": "true"}, None))


def test_the_workspace_note_states_where_commands_land(tmp_path: Path) -> None:
    note = workspace_note(tmp_path)

    assert str(tmp_path) in note
    assert "cwd" in note
