"""canonical JSON 与哈希 —— 全项目唯一的序列化约定。

幂等重试依赖「同样的内容算出同样的字节」。只要有地方绕开这里直接
`json.dumps`，字典键顺序就会让 checksum 变化，重试被误判成内容冲突。

约定：
- `sort_keys=True`：键顺序不影响字节
- `ensure_ascii=False`：中文不转义
- `separators=(",", ":")`：无多余空白
- 不接受 float，见 `assert_json_safe`
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: payload 里允许出现的类型。刻意保守：bytes / datetime / float 都要求先转成
#: 明确的表示，因为它们在 JSON 里的编码不是无损且跨实现稳定的。
_ENCODABLE = (str, int, bool, type(None))


def assert_json_safe(value: Any, *, path: str = "$") -> None:
    """校验值能无损编码成 canonical JSON，否则抛 TypeError。

    不接受 float：同一个数值在不同实现里 `repr` 可能不同，算出的字节就不一样，
    重试会被误判成内容冲突。
    """
    if isinstance(value, _ENCODABLE):
        return
    if isinstance(value, float):
        raise TypeError(
            f"{path}: float 不能参与 checksum —— 跨实现字节不稳定，请转成字符串或整数"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path}: JSON 对象的键必须是 str，实际是 {type(key).__name__}")
            assert_json_safe(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_json_safe(item, path=f"{path}[{index}]")
        return
    raise TypeError(f"{path}: {type(value).__name__} 不是 JSON 可编码类型")


def canonical_json(value: Any) -> str:
    """规范化的 JSON 文本。键有序、无空白、非 ASCII 不转义。"""
    assert_json_safe(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(value: Any) -> bytes:
    """`canonical_json` 的 UTF-8 字节形式。哈希直接对这个做。"""
    return canonical_json(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """内容的十六进制 SHA-256。用于 payload_hash、blob hash、checksum。"""
    return hashlib.sha256(data).hexdigest()


def hash_json(value: Any) -> str:
    """对 JSON 值求 sha256。幂等键、去重比较、commit checksum 都走这里。"""
    return sha256_hex(canonical_bytes(value))


def checksum_of(parts: dict[str, Any]) -> str:
    """一次 commit 的内容 checksum。

    `parts` 里不能有 `seq` 和 `committed_at`。生产者丢了 receipt 重试时算不出这两个值，
    一旦算进 checksum，重试必然被判成「同键不同内容」。
    """
    return hash_json(parts)