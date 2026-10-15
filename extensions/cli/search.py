"""一条查资料的命令 —— `oath-cli search <关键词>` 打到这里。

**签名的后两个参数是框架统一给的**：`scope` 是这次调用的身份，`user_context` 是客户端
随消息带来的那份 kv（已裁掉过期项）。用不上的命令也照收 —— 签名一致，要鉴权的命令才
能自己写在那儿，不必改框架。
"""

from __future__ import annotations

from typing import Final

from core.protocol.datatype.scope import Scope
from core.utils.encoding import JsonObject, JsonValue

#: 示例语料。搜索逻辑是真的，数据是合成的 —— 接上真实资料源时换掉这一张表。
_CORPUS: Final[tuple[dict[str, str], ...]] = (
    {"title": "工作区", "body": "命令默认在 workspace 下执行，要去别处就给 bash 传 cwd。"},
    {"title": "凭证", "body": "client credential 交给 TokenAuthenticator 查表换身份，认不出就拒。"},
    {"title": "恢复", "body": "journal 是唯一状态来源，broker 与快照都是可恢复的派生设施。"},
    {"title": "租户隔离", "body": "每个 session 一条消费线程，事件按 scope 维度投递。"},
)


def search(query: str, *, limit: int = 3, scope: Scope, user_context: JsonObject) -> JsonValue:
    """按关键词查资料。

    打分规则很简单：标题里命中算两分，正文里命中算一分；一个都没命中就不收录。
    同分按语料里的先后排 —— 真实资料源换成什么排序，由那条源自己定。
    """
    terms = query.lower().split()
    if not terms:
        return {"query": query, "hits": []}

    scored: list[tuple[int, int, JsonObject]] = []
    for index, entry in enumerate(_CORPUS):
        title, body = entry["title"].lower(), entry["body"].lower()
        score = sum((2 if term in title else 0) + (1 if term in body else 0) for term in terms)
        if score:
            scored.append((-score, index, dict(entry)))

    scored.sort()
    return {
        "query": query,
        "hits": [entry for _, _, entry in scored[: max(limit, 0)]],
    }


__all__ = ["search"]
