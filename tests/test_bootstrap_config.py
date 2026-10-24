"""配置怎么从 TOML 读出来 —— 以及各段之间别读串。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bootstrap.common.config import docker_profile, from_dict, load_config
from core.protocol.datatype.config import SupervisorConfig

REPO_ROOT = Path(__file__).resolve().parent.parent

MINIMAL = """
[agent.provider]
model = "m"
api_base = "http://x"
credential = "provider/model"
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


def test_an_unknown_section_is_refused(tmp_path: Path) -> None:
    """写错段名要在这里就停。静默忽略的话，服务会拿着默认地址起起来，然后连不上别人。"""
    with pytest.raises(ValueError, match="Unsupported configuration sections"):
        load_config(_write(tmp_path, '[servce]\nbroker_url = "amqp://x"\n' + MINIMAL))


def test_an_unknown_profile_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown deployment profile"):
        load_config(_write(tmp_path, MINIMAL), profile="k8s")


def test_the_nested_provider_is_read(tmp_path: Path) -> None:
    """provider 是 `[agent.provider]` 下的一块，不是平级的。"""
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.agent.provider.model == "m"
    assert config.agent.provider.credential == "provider/model"


# --------------------------------------------------------------------------
# agent / services / supervisor / secrets 四段各自独立
# --------------------------------------------------------------------------


def test_the_services_block_is_read(tmp_path: Path) -> None:
    """服务之间的地址全在这一段。读错就是服务起来了却互相找不到。"""
    config = load_config(
        _write(
            tmp_path,
            "[services]\n"
            'storage_url = "http://s:8770"\n'
            'manager_url = "http://m:8772"\n'
            "workers = 8\n"
            'internal_token = "shared-secret"\n' + MINIMAL,
        )
    )

    assert config.services.storage_url == "http://s:8770"
    assert config.services.manager_url == "http://m:8772"
    assert config.services.workers == 8


def test_the_services_block_keeps_its_defaults_when_absent(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.services.storage_url == "http://127.0.0.1:8770"
    assert config.services.gateway_port == 8765
    assert config.services.workers == 4


def test_the_supervisor_block_is_read(tmp_path: Path) -> None:
    config = load_config(
        _write(tmp_path, "[supervisor]\ninvoke_interval_seconds = 12.5\n" + MINIMAL)
    )

    assert config.supervisor.invoke_interval_seconds == 12.5


def test_the_supervisor_keeps_its_defaults_when_absent(tmp_path: Path) -> None:
    assert load_config(_write(tmp_path, MINIMAL)).supervisor.invoke_interval_seconds == 30.0


def test_an_infinite_invoke_interval_is_refused() -> None:
    """这个值要拿来当周期。`inf` 会让这个任务永远等不到下一轮。"""
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
    config = load_config(_write(tmp_path, MINIMAL + "extra_params = { top_p = 0.9 }\n"))

    assert config.agent.provider.extra_params == {"top_p": 0.9}


def test_context_provider_can_be_injected_for_worker_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "context-token"
    token.write_text("token")
    monkeypatch.setenv(
        "OATH_CONTEXT_PROVIDER_URL", "http://host.docker.internal:8080/internal/agent/context"
    )
    monkeypatch.setenv("OATH_CONTEXT_PROVIDER_TOKEN_FILE", str(token))

    config = load_config(_write(tmp_path, MINIMAL))

    assert config.agent.context_provider_url.endswith("/internal/agent/context")
    assert config.agent.context_provider_token_file == str(token)


def test_an_absent_temperature_stays_none(tmp_path: Path) -> None:
    """给 `temperature: 0` 和「没设」是一回事，但没设不能变成 0 —— 那会让模型变确定。"""
    assert load_config(_write(tmp_path, MINIMAL)).agent.temperature is None


def test_the_secrets_block_is_read(tmp_path: Path) -> None:
    """模型密钥只写 Vault 别名。这一段放的是 Vault 在哪、身份文件在哪。"""
    config = load_config(
        _write(
            tmp_path,
            '[secrets]\nvault_url = "http://vault:8200"\nrole_id_file = "./id"\n' + MINIMAL,
        )
    )

    assert config.secrets.vault_url == "http://vault:8200"
    assert config.secrets.role_id_file.is_absolute()


def test_the_model_key_never_lands_in_the_config(tmp_path: Path) -> None:
    """配置里只有别名。key 由 worker 从 Vault 取，不经过这条路。"""
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.agent.provider.credential == "provider/model"
    assert config.agent.provider.import_env == ""


# --------------------------------------------------------------------------
# docker profile：宿主机写法 → Compose 网络
# --------------------------------------------------------------------------


def test_the_docker_profile_rewrites_the_service_addresses() -> None:
    """容器里不能用 127.0.0.1 找别的服务 —— 那指向自己。"""
    services = from_dict(docker_profile({"services": {}})).services

    assert services.storage_url == "http://storage:8770"
    assert services.queue_url == "http://queue:8771"
    assert services.manager_url == "http://manager:8772"
    assert services.tools_url == "http://tools:8773"
    assert services.gateway_host == "0.0.0.0"


def test_the_docker_profile_builds_the_broker_url_with_escaped_credentials() -> None:
    """密码里的 `@` / `:` 会把 URL 拆错，进 broker 之前先转义。"""
    services = from_dict(
        docker_profile({"services": {"rabbitmq_user": "o@th", "rabbitmq_password": "p:ss"}})
    ).services

    assert services.broker_url == "amqp://o%40th:p%3Ass@rabbitmq:5672/%2F"


def test_the_docker_profile_moves_the_paths_onto_the_volumes() -> None:
    config = from_dict(docker_profile({"agent": {"workspace_root": "./workspace"}}))

    assert config.data_root == Path("/data")
    assert config.agent.workspace_root == Path("/workspace")
    assert config.secrets.vault_url == "http://vault:8200"


def test_the_docker_profile_keeps_the_values_it_does_not_own() -> None:
    config = from_dict(docker_profile({"agent": {"system_prompt": "你是助手"}}))

    assert config.agent.system_prompt == "你是助手"


# --------------------------------------------------------------------------
# 目录字段：相对路径按进程 cwd 解析
# --------------------------------------------------------------------------


def test_the_directories_are_made_absolute_against_the_process(tmp_path: Path) -> None:
    """相对路径按 runtime 进程的 cwd 解析，不是按命令的 cwd。

    命令默认落在 workspace 下；`cli_bin_dir` 若照原样进 `PATH`，命令就会去找
    `workspace/scripts/oath-cli`。
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        config = from_dict({"agent": {"workspace_root": "./workspace", "cli_bin_dir": "./scripts"}})

    assert config.agent.workspace_root == tmp_path / "workspace"
    assert config.agent.cli_bin_dir == tmp_path / "scripts"


def test_reading_a_file_resolves_its_directories_against_that_file(tmp_path: Path) -> None:
    """`oath.toml` 在哪，目录字段就以哪为底 —— 与命令从哪儿被调用无关。"""
    nested = tmp_path / "deploy"
    nested.mkdir()
    config = load_config(_write(nested, MINIMAL + '\n[agent]\nworkspace_root = "./ws"\n'))

    assert config.agent.workspace_root == nested / "ws"
    assert config.data_root == nested / "data"


def test_a_directory_given_as_absolute_is_left_alone() -> None:
    assert from_dict({"agent": {"cli_bin_dir": "/opt/oath/bin"}}).agent.cli_bin_dir == Path(
        "/opt/oath/bin"
    )


def test_the_extension_directories_default_to_oath_extensions(
    tmp_path: Path,
) -> None:
    """外部扩展通过 oath start 显式追加。"""
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.agent.extension_dirs == (tmp_path / "extensions",)


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
# 技能目录
# --------------------------------------------------------------------------


def test_the_agent_skill_directories_are_discovered(tmp_path: Path) -> None:
    """默认就要扫 coding agent 已经装好的技能，配置里的排在后面。"""
    config = load_config(_write(tmp_path, MINIMAL + '\n[agent]\nskill_scan_dirs = ["./extra"]\n'))

    dirs = config.agent.skill_scan_dirs
    assert Path.cwd() / ".claude" / "skills" in dirs
    assert Path.home() / ".codex" / "skills" in dirs
    assert dirs[-1] == Path("./extra")


def test_the_skill_directory_falls_back_to_the_extensions_layout(tmp_path: Path) -> None:
    default = load_config(_write(tmp_path, MINIMAL))

    assert default.agent.skill_dir == tmp_path / "extensions" / "skills"

    given = load_config(_write(tmp_path, MINIMAL + '\n[agent]\nskill_dir = "./skills"\n'))

    assert given.agent.skill_dir == tmp_path / "skills"


# --------------------------------------------------------------------------
# from_dict 直接调
# --------------------------------------------------------------------------


def test_an_empty_dict_still_gives_a_usable_shape() -> None:
    """装配层拿到的是配置对象，不是 None —— 缺省值在这里定死。"""
    config = from_dict({})

    assert config.services.biz_port == 8766
    assert config.agent.system_prompt == ""
    assert config.database_path == Path.cwd() / "data" / "oath.sqlite3"


# --------------------------------------------------------------------------
# 随仓库发布的那份示例
# --------------------------------------------------------------------------


def test_the_shipped_example_parses() -> None:
    """示例配置写错了要在这里发现 —— 没人会先跑一遍再看报什么错。"""
    config = load_config(REPO_ROOT / "oath.example.toml")

    assert config.agent.provider.model
    assert config.agent.provider.credential, "示例里得有模型密钥的 Vault 别名"
    assert config.agent.system_prompt
