"""配置怎么从 TOML 读出来 —— 以及凭证不进配置文件。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bootstrap.common.config import from_dict, load_config
from core.protocol.datatype.config import SupervisorConfig

REPO_ROOT = Path(__file__).resolve().parent.parent

MINIMAL = """
[agent.provider]
model = "m"
api_base = "http://x"
api_key_env = "OATH_KEY"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "oath.toml"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 读文件
# --------------------------------------------------------------------------


def test_a_missing_file_says_so(tmp_path: Path) -> None:
    """起不来就说清楚为什么，不要落到一个看不懂的解析错上。"""
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_the_nested_provider_is_read(tmp_path: Path) -> None:
    """provider 是 `[agent.provider]` 下的一块，不是平级的。"""
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.agent.provider.model == "m"
    assert config.agent.provider.api_key_env == "OATH_KEY"


def test_the_server_block_is_read(tmp_path: Path) -> None:
    config = load_config(
        _write(tmp_path, '[server]\nhost = "0.0.0.0"\nhttp_port = 9000\nmax_connections = 4\n')
    )

    assert (config.server.host, config.server.http_port, config.server.max_connections) == (
        "0.0.0.0",
        9000,
        4,
    )


def test_the_server_keeps_its_defaults_when_absent(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.server.send_queue_size == 256
    assert config.server.host == "127.0.0.1"


def test_the_supervisor_block_is_read(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            "[supervisor]\ninvoke_interval_seconds = 12.5\ninvoke_timeout_seconds = 2.5\n",
        )
    )

    assert config.supervisor.invoke_interval_seconds == 12.5
    assert config.supervisor.invoke_timeout_seconds == 2.5
    # 没写的那些各自有自己的默认值。
    assert config.supervisor.shutdown_timeout_seconds == 5.0


def test_the_supervisor_keeps_its_defaults_when_absent(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.supervisor.invoke_interval_seconds == 30.0
    assert config.supervisor.invoke_timeout_seconds == 5.0


def test_an_infinite_invoke_interval_is_refused() -> None:
    """这些值是要拿来当定时与超时的。`inf` 会让这个周期任务永远等不到下一轮。"""
    with pytest.raises(ValueError, match="invoke_interval_seconds"):
        SupervisorConfig(invoke_interval_seconds=float("inf"))


def test_the_agent_block_is_read(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            MINIMAL + '\n[agent]\nsystem_prompt = "你是助手"\nmax_output_tokens = 512\n'
            "temperature = 0.3\n",
        )
    )

    assert config.agent.system_prompt == "你是助手"
    assert config.agent.max_output_tokens == 512
    assert config.agent.temperature == 0.3


def test_extra_provider_params_are_carried(tmp_path: Path) -> None:
    """各厂家那些名字各异的参数走 `extra_params`，core 不为谁开字段。"""
    config = load_config(
        _write(
            tmp_path,
            MINIMAL.replace(
                'api_key_env = "OATH_KEY"',
                'api_key_env = "OATH_KEY"\nextra_params = { top_p = 0.9 }',
            ),
        )
    )

    assert config.agent.provider.extra_params == {"top_p": 0.9}


def test_an_absent_temperature_stays_none(tmp_path: Path) -> None:
    """给 `temperature: 0` 和「没设」是一回事，但没设不能变成 0 —— 那会让模型变确定。"""
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.agent.temperature is None


# --------------------------------------------------------------------------
# 环境变量展开
# --------------------------------------------------------------------------


def test_a_placeholder_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """凭证走这条路：配置文件只写变量名。"""
    monkeypatch.setenv("OATH_TEST_BASE", "https://example.invalid/v1")
    config = load_config(
        _write(tmp_path, MINIMAL.replace('api_base = "http://x"', 'api_base = "${OATH_TEST_BASE}"'))
    )

    assert config.agent.provider.api_base == "https://example.invalid/v1"


def test_an_unset_variable_expands_to_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空值比原样留着 `${VAR}` 好：后者会被当成一个真的域名发出去。"""
    monkeypatch.delenv("OATH_TEST_BASE", raising=False)
    config = load_config(
        _write(
            tmp_path, MINIMAL.replace('api_base = "http://x"', 'api_base = "x${OATH_TEST_BASE}y"')
        )
    )

    assert config.agent.provider.api_base == "xy"


def test_a_service_credential_can_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OATH_TEST_TOKEN", "s3cret")
    config = load_config(
        _write(tmp_path, 'service_credentials = ["${OATH_TEST_TOKEN}"]\n' + MINIMAL)
    )

    assert config.service_authorised("s3cret")


# --------------------------------------------------------------------------
# 服务级凭证
# --------------------------------------------------------------------------


def test_a_service_credential_authorises_its_token(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, 'service_credentials = ["t", "u"]\n' + MINIMAL))

    assert config.service_authorised("t")
    assert config.service_authorised("u")


def test_an_unknown_token_is_not_authorised(tmp_path: Path) -> None:
    """不认就是不认 —— 认证器据此拒绝，不回落匿名。"""
    config = load_config(_write(tmp_path, MINIMAL))

    assert not config.service_authorised("nobody")


def test_an_empty_token_is_never_authorised(tmp_path: Path) -> None:
    """`${VAR}` 在变量没设时展开成空串。空串当成一个凭证就是无凭证可过。"""
    config = load_config(_write(tmp_path, 'service_credentials = ["${OATH_NOPE}"]\n' + MINIMAL))

    assert config.service_credentials == ()
    assert not config.service_authorised("")


def test_to_json_never_carries_the_service_credentials(tmp_path: Path) -> None:
    """`to_json` 会被人打日志、发监控。凭证的值不能出现在里面。"""
    config = load_config(
        _write(tmp_path, 'service_credentials = ["a-long-demo-token"]\n' + MINIMAL)
    )

    assert "a-long-demo-token" not in json.dumps(config.to_json())
    assert "credentials" not in config.to_json()


def test_to_json_only_names_the_environment_variable(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.to_json()["agent"]["provider"]["api_key_env"] == "OATH_KEY"


# --------------------------------------------------------------------------
# 技能目录
# --------------------------------------------------------------------------


def test_the_agent_skill_directories_are_discovered(tmp_path: Path) -> None:
    """默认就要扫 coding agent 已经装好的技能，配置里的排在后面。"""
    config = load_config(_write(tmp_path, MINIMAL + '\n[agent]\nskill_scan_dirs = ["./extra"]\n'))

    dirs = config.agent.skill_scan_dirs
    assert Path.cwd() / ".claude" / "skills" in dirs
    assert Path.home() / ".claude" / "skills" in dirs
    assert Path.home() / ".codex" / "skills" in dirs
    assert dirs[-1] == Path("./extra")


def test_the_skill_directory_falls_back_to_the_extensions_layout(tmp_path: Path) -> None:
    assert load_config(_write(tmp_path, MINIMAL)).agent.skill_dir == (
        Path.cwd() / "extensions" / "skills"
    )
    assert (
        load_config(
            _write(tmp_path, MINIMAL + '\n[agent]\nskill_dir = "./skills"\n')
        ).agent.skill_dir
        == Path.cwd() / "skills"
    )


# --------------------------------------------------------------------------
# 扩展目录
# --------------------------------------------------------------------------


def test_the_extension_directories_default_to_the_committed_one_and_the_local_one(
    tmp_path: Path,
) -> None:
    """两个目录用同一套规则；本地那个只是不进版本库。"""
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.agent.extension_dirs == (
        Path.cwd() / "extensions",
        Path.cwd() / "extensions-local",
    )


def test_the_extension_directories_are_made_absolute_against_the_process(
    tmp_path: Path,
) -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        config = from_dict({"agent": {"extension_dirs": ["./ext"]}})

    assert config.agent.extension_dirs == (tmp_path / "ext",)


def test_an_empty_extension_directory_list_turns_the_scan_off() -> None:
    """空数组是有意义的取值 —— 关掉扩展扫描，不等于「没配」。"""
    assert from_dict({"agent": {"extension_dirs": []}}).agent.extension_dirs == ()


# --------------------------------------------------------------------------
# from_dict 直接调
# --------------------------------------------------------------------------


def test_an_empty_dict_still_gives_a_usable_shape() -> None:
    """装配层拿到的是配置对象，不是 None —— 缺省值在这里定死。"""
    config = from_dict({})

    assert config.server.http_port == 8766
    assert config.agent.system_prompt == ""


# --------------------------------------------------------------------------
# 随仓库发布的那份示例
# --------------------------------------------------------------------------


def test_the_shipped_example_parses() -> None:
    """示例配置写错了要在这里发现 —— 没人会先跑一遍再看报什么错。"""
    config = load_config(REPO_ROOT / "oath.example.toml")

    assert config.agent.provider.model
    assert config.agent.system_prompt


def test_the_directories_are_made_absolute_against_the_process(tmp_path: Path) -> None:
    """相对路径按 runtime 进程的 cwd 解析，不是按命令的 cwd。

    命令默认落在 workspace 下；`cli_bin_dir` 若照原样进 `PATH`，命令就会去找
    `workspace/scripts/oath-cli` —— 默认配置下 `oath-cli` 直接找不到。
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        config = from_dict({"agent": {"workspace_root": "./workspace", "cli_bin_dir": "./scripts"}})

    assert config.agent.workspace_root == tmp_path / "workspace"
    assert config.agent.cli_bin_dir == tmp_path / "scripts"


def test_a_directory_given_as_absolute_is_left_alone() -> None:
    config = from_dict({"agent": {"cli_bin_dir": "/opt/oath/bin"}})

    assert config.agent.cli_bin_dir == Path("/opt/oath/bin")
