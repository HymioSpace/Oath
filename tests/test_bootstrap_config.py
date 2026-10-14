"""配置怎么从 TOML 读出来 —— 以及凭证不进配置文件。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bootstrap.config import from_dict, load_config

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
        _write(tmp_path, '[server]\nhost = "0.0.0.0"\nport = 9000\nmax_connections = 4\n')
    )

    assert (config.server.host, config.server.port, config.server.max_connections) == (
        "0.0.0.0",
        9000,
        4,
    )


def test_the_server_keeps_its_defaults_when_absent(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.server.send_queue_size == 256
    assert config.server.host == "127.0.0.1"


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


def test_a_credential_token_can_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OATH_TEST_TOKEN", "s3cret")
    config = load_config(
        _write(
            tmp_path,
            MINIMAL
            + '\n[[credentials]]\ntoken = "${OATH_TEST_TOKEN}"\nuser_id = "u"\nsession_id = "s"\n',
        )
    )

    assert config.credential_for("s3cret") == ("u", "s")


# --------------------------------------------------------------------------
# 凭证
# --------------------------------------------------------------------------


def test_credentials_map_a_token_to_an_identity(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            MINIMAL + '\n[[credentials]]\ntoken = "t"\nuser_id = "u"\nsession_id = "s"\n',
        )
    )

    assert config.credential_for("t") == ("u", "s")


def test_an_unknown_token_has_no_identity(tmp_path: Path) -> None:
    """返回 None 是「不认」，不是「随便给一个」—— 认证器据此拒绝，不回落匿名。"""
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.credential_for("nobody") is None


def test_a_tokenless_credential_is_dropped(tmp_path: Path) -> None:
    config = load_config(
        _write(tmp_path, MINIMAL + '\n[[credentials]]\nuser_id = "u"\nsession_id = "s"\n')
    )

    assert config.credentials == {}


def test_to_json_never_carries_the_credentials(tmp_path: Path) -> None:
    """`to_json` 会被人打日志、发监控。凭证的值不能出现在里面。"""
    config = load_config(
        _write(
            tmp_path,
            MINIMAL
            + '\n[[credentials]]\ntoken = "a-long-demo-token"\nuser_id = "u"\nsession_id = "s"\n',
        )
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
    assert load_config(_write(tmp_path, MINIMAL)).agent.skill_dir == Path("./extensions/skills")
    assert load_config(
        _write(tmp_path, MINIMAL + '\n[agent]\nskill_dir = "./skills"\n')
    ).agent.skill_dir == Path("./skills")


# --------------------------------------------------------------------------
# from_dict 直接调
# --------------------------------------------------------------------------


def test_an_empty_dict_still_gives_a_usable_shape() -> None:
    """装配层拿到的是配置对象，不是 None —— 缺省值在这里定死。"""
    config = from_dict({})

    assert config.server.port == 8765
    assert config.agent.system_prompt == ""


# --------------------------------------------------------------------------
# 随仓库发布的那份示例
# --------------------------------------------------------------------------


def test_the_shipped_example_parses() -> None:
    """示例配置写错了要在这里发现 —— 没人会先跑一遍再看报什么错。"""
    config = load_config(REPO_ROOT / "oath.example.toml")

    assert config.agent.provider.model
    assert config.agent.system_prompt
