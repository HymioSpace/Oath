"""扩展包根。

`extensions/` 可缺省 —— 判据是删掉它 runtime 仍能启动。没有 manifest，也没有插件
加载器：core 不反向查找或下载插件，注册由 `bootstrap/` 显式完成。
"""

from __future__ import annotations
