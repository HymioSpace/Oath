# Feature 05 — 扩展

状态：Draft。

## core 是机制，扩展只提供内容和动作

core 自己实现：事件运行时、状态机、日志、工具生命周期与超时、权限判定、context 组装、
skill 加载、模型调用。扩展只往里塞三样：

| 类型 | 提供什么 | 形式 |
| --- | --- | --- |
| 业务 tool | 执行一个动作 | `execute(call)`，由 core 调用 |
| 业务 skill | 给模型的说明 | 一个目录，里面一个 `SKILL.md` |
| 额外 context 来源 | 查数据喂给 core 的组装流程 | `query(request)` / `fetch(ref)` |

控制面一律在 core：谁能执行、允许多久、状态怎么迁移、结果怎么记、投递到哪里，
都不由扩展决定。所以没有「权限扩展」「调度扩展」这种位置。
`extensions/` 整体可缺省 —— 删掉它 runtime 照样启动，只是没有业务能力可用。

MCP 只是工具的一种实现方式，不改变上面这几条。

## Skill 就是目录里的 SKILL.md

skill 没有 Python 接口。core 拿一串 skill 根目录，逐个扫 `<root>/<name>/SKILL.md`
读出来：frontmatter 写 name 和 description，正文是给模型的说明。

所以「扩展贡献 skill」= `extensions/` 下的目录也在被扫的根里，多一个路径而已。

## 待决事项

Nostalgia 是独立 git 仓库，要提供一个 context 来源。装成依赖、走进程边界、还是 HTTP
访问，三种方式的权限隔离和故障模型都不同。选定之后再定这部分代码放哪。
