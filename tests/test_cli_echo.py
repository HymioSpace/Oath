"""`extensions/cli/` 里那条命令。

框架那一侧（分发、校验、身份）在 `test_cli_server.py`。这里要验的是这条命令自己的边界：
**它拿得到什么、它交出什么** —— 它是 demo，不假装是业务能力，所以它的行为就该薄到一眼看得完。
"""

from __future__ import annotations

from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject
from extensions.cli.echo import echo

SCOPE = Scope("u1", "s1")


def test_it_hands_the_text_back() -> None:
    assert echo("工作区", scope=SCOPE, user_context={}) == {"text": "工作区"}


def test_it_keeps_the_text_verbatim() -> None:
    """回显就是回显 —— 不裁、不改、不解释，空串也是原样。"""
    assert echo("", scope=SCOPE, user_context={}) == {"text": ""}


def test_it_does_not_use_the_user_context() -> None:
    """`user_context` 是给要鉴权的命令用的。这条不用，就该证明用了也没有影响。"""
    noisy: JsonObject = {"token": "synthetic", "tenant": {"id": 7}, "过期": None}

    assert echo("工作区", scope=SCOPE, user_context=noisy) == {"text": "工作区"}
