"""模型选择：清单怎么读、超出清单怎么挡、选中的模型怎么送到 provider。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bootstrap.common.config import load_config
from bootstrap.gateway.process import validate_model
from core.llm.openai_provider import OpenAICompatibleProvider
from core.protocol.datatype.config import AgentConfig, ModelProviderConfig, OathConfig
from core.protocol.datatype.inbound import of_command
from core.protocol.datatype.llm import LlmRequest
from core.protocol.datatype.message import Message
from core.protocol.datatype.wire import Command, CommandType

MINIMAL = """
[agent]
selectable_models = ["m2", "m3"]

[agent.provider]
model = "m1"
api_base = "http://x"
credential = "provider/model"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "oath.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _config(*selectable: str) -> OathConfig:
    return OathConfig(
        agent=AgentConfig(
            provider=ModelProviderConfig(model="m1", api_base="http://x"),
            selectable_models=selectable,
        )
    )


def _message(model: object = None) -> Command:
    payload: dict = {"content": "你好"}
    if model is not None:
        payload["model"] = model
    return Command(id="c1", type=CommandType.USER_MESSAGE, payload=payload)


# --------------------------------------------------------------------------
# 清单怎么读
# --------------------------------------------------------------------------


def test_the_allowlist_is_read(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, MINIMAL))

    assert config.agent.selectable_models == ("m2", "m3")


def test_the_allowlist_may_be_empty(tmp_path: Path) -> None:
    text = MINIMAL.replace('selectable_models = ["m2", "m3"]', "selectable_models = []")

    config = load_config(_write(tmp_path, text))

    assert config.agent.selectable_models == ()


@pytest.mark.parametrize("value", ['[""]', '["  "]', "[1]", '["' + "x" * 129 + '"]'])
def test_a_broken_allowlist_stops_the_startup(tmp_path: Path, value: str) -> None:
    """空 ID、非字符串、过长的 ID 都在读配置时停 —— 别等到用户选了才炸。"""
    text = MINIMAL.replace('selectable_models = ["m2", "m3"]', f"selectable_models = {value}")

    with pytest.raises(ValueError, match="selectable_models"):
        load_config(_write(tmp_path, text))


# --------------------------------------------------------------------------
# 超出清单怎么挡
# --------------------------------------------------------------------------


def test_the_default_model_is_always_allowed() -> None:
    validate_model(_config("m2"), _message("m1"))


def test_a_listed_model_is_allowed() -> None:
    validate_model(_config("m2", "m3"), _message("m3"))


def test_an_unlisted_model_is_refused() -> None:
    """客户端不能自选端点和凭据 —— 清单之外的一律在网关挡住。"""
    with pytest.raises(ValueError, match="Model is not available"):
        validate_model(_config("m2"), _message("other"))


def test_a_model_that_is_not_a_string_is_refused() -> None:
    with pytest.raises(ValueError, match="Model is not available"):
        validate_model(_config("m2"), _message(7))


def test_a_message_without_a_model_is_left_alone() -> None:
    validate_model(_config("m2"), _message())


def test_another_command_is_left_alone() -> None:
    command = Command(id="c1", type=CommandType.PING, payload={"model": "other"})

    validate_model(_config("m2"), command)


# --------------------------------------------------------------------------
# 选中的模型怎么往下走
# --------------------------------------------------------------------------


def test_the_model_travels_with_the_user_message() -> None:
    inbound = of_command(_message("m3"))

    assert inbound is not None
    assert inbound.payload["model"] == "m3"


def test_a_message_without_a_model_carries_none() -> None:
    inbound = of_command(_message())

    assert inbound is not None
    assert "model" not in inbound.payload


def test_the_requested_model_reaches_the_provider() -> None:
    """冻结在 step 里的模型才是真正发出去的那个 —— 不能被配置里的默认值顶掉。"""
    seen: dict = {}

    class _Secrets:
        def get(self, name: str) -> str:
            return "key"

    provider = OpenAICompatibleProvider(
        ModelProviderConfig(model="m1", api_base="http://x", credential="provider/model"),
        _Secrets(),
    )

    def _capture(url, payload, headers, timeout):
        seen["payload"] = payload
        return {"choices": [{"message": {"content": "好"}, "finish_reason": "stop"}]}

    provider._complete_once = _capture  # type: ignore[method-assign]
    provider.complete(LlmRequest(messages=[Message(role="user", content="你好")], model="m3"))

    assert seen["payload"]["model"] == "m3"


def test_the_configured_model_is_used_when_none_is_requested() -> None:
    seen: dict = {}

    class _Secrets:
        def get(self, name: str) -> str:
            return "key"

    provider = OpenAICompatibleProvider(
        ModelProviderConfig(model="m1", api_base="http://x", credential="provider/model"),
        _Secrets(),
    )

    def _capture(url, payload, headers, timeout):
        seen["payload"] = payload
        return {"choices": [{"message": {"content": "好"}, "finish_reason": "stop"}]}

    provider._complete_once = _capture  # type: ignore[method-assign]
    provider.complete(LlmRequest(messages=[Message(role="user", content="你好")]))

    assert seen["payload"]["model"] == "m1"
