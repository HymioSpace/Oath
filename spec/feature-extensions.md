# Feature — 扩展目录

状态：Draft

## 1 背景与目标

业务能力有三种形态：工具、CLI 命令、技能。它们都是**同一个开发者**写给 runtime 的东西，
却曾经用三种完全不同的方式接进来 —— 工具要在装配代码里 import 一次再传进去，命令要写进
一张源码里的常量表，技能有自己一套目录配置。加一条能力要先想「这一种该怎么接」。

这套机制让三种形态走同一条路：**目录里有，启动时就生效。** 扩展目录是配置项，加一个目录
就是加一组能力；规则对所有目录、所有形态都一样。

其中一个目录默认是 `extensions-local/`，进 `.gitignore`：它只在本机存在，放开发者自己的
工具、命令和技能，不进版本库。它不是特例 —— 与仓库里那份走完全相同的规则，只是来源不同。

不包含：扩展的热加载（改了要重启）；扩展之间的依赖声明；扩展自己注册权限或调度。

## 2 前置依赖

- [工具系统](feature-tool-system.md)：工具的契约与注册表在这里，目录扫描只负责把对象交出去。
- [bash 与 CLI 代理](feature-bash-cli.md)：命令的执行路径。命令在扩展里声明，路由不在。
- [技能](feature-skill.md)：技能文件的格式与索引预算。

## 3 总览

### 3.1 一句话

扩展目录是一组 Python 包目录，每个目录下按 `tools/` `cli/` `skills/` 三个固定位置
收三种扩展，配置里按顺序列出这些目录，启动时扫一遍装好。

### 3.2 流程

```mermaid flowchart TD
    C[agent.extension_dirs] -->|按顺序| L[bootstrap.extension_loader]
    L --> T[tools/*.py → Tool 子类]
    L --> M[cli/*.py → COMMANDS]
    L --> S[skills/ → 目录]
    T --> R[ToolRegistry]
    M --> RT[Runtime.commands]
    S --> SK[SkillSystem]
    RT --> B[biz server /cli/*]
```

贯穿全文的四条规则：

1. **目录不存在不是错误。** 刚 clone 的仓库没有 `extensions-local/`。少一个子目录同理。
2. **装不起来就是没装起来。** 导入报错、名字非法、撞名都在启动时失败，不降级。
3. **撞名不覆盖。** 工具与命令撞名在启动时报错，消息里给出两个文件；技能例外，
   先扫到的赢，后来的一条记 warning。目录顺序不决定谁赢，只决定报错时谁是先到的那一个。
4. **core 不 import 扩展。** 扫描只发生在 `bootstrap` —— 扩展是 core 的用户，不是依赖。

### 3.3 一个完整的例子

开发者在自己机器上建三个文件：

```
extensions-local/
  tools/notes.py         一个 Tool 子类，工具名 note
  cli/now.py             COMMANDS 里声明一条命令 now
  skills/standup/SKILL.md
```

重启 runtime，三件事同时成立：

- 模型看到的工具列表里多了 `note`，与 `bash` `skill` `detail_read` 并列；
- `GET /cli_commands` 里多了 `now`，`POST /cli/now` 能调通，credential 与 `user_context`
  的传递规则与仓库里那条命令完全一样；
- system prompt 末尾的技能索引里多了 `standup`。

把 `notes.py` 里的工具名改成 `echo`（仓库里已有），重启：

```
2026-10-15 21:46:42 oath.runtime 工具名 'echo' 重复:
  /…/extensions/tools/demo.py 与 /…/extensions-local/tools/notes.py
```

进程以退出码 1 结束，不监听任何端口。`git status` 看不到 `extensions-local/`。

## 4 接口与数据

### 4.1 配置

`agent.extension_dirs` 是一串目录。相对路径按 runtime 进程的 cwd 解析，与
`workspace_root` / `cli_bin_dir` 同一条规则；写 `[]` 就是关掉扫描，那是一个有意义的取值，
不等于「没配」。

```toml
extension_dirs = ["./extensions", "./extensions-local"]
```

### 4.2 目录布局

| 位置 | 收什么 | 形状 |
| --- | --- | --- |
| `<目录>/tools/*.py` | 工具 | 模块里定义的 `Tool` 子类，**零参构造** |
| `<目录>/cli/*.py` | 命令 | 模块级 `COMMANDS`，元素是 `CliCommand` |
| `<目录>/skills/` | 技能 | `<名字>/SKILL.md`，与仓库那份同一套格式 |

`_` 开头的文件不参与发现，但仍能被同级 import —— 共享的辅助代码放这里。
子目录里的 `__init__.py` 会被执行，但**不该在里面 import 子模块**：那会让每个模块在包
初始化时就被导一遍，产生第二份类对象。

### 4.3 工具的发现

一个模块里的工具是「模块自己定义的 `Tool` 子类」，不是「模块能看到的 `Tool` 子类」。
判据是 `__module__` 等于这个模块的名字：文件顶上 `from core.detail import DetailReadTool`
是写依赖，不是要把它再注册一遍。抽象基类跳过 —— 它是给人继承的，不是给人执行的。

工具**只能零参构造**。需要依赖的工具不进扩展目录，由装配层用 `Runtime(tools=...)`
显式注入：那里给的是一个已经配好依赖的实例。扫出来的排在注入的之前，两者撞名由注册表
报出来。

### 4.4 命令的声明

`CliCommand` 在 `core/protocol/datatype/cli.py`，与 `ToolDefinition` 同类：带 JSON Schema
的纯数据声明。它在 core 而不在 `biz`，因为扩展要声明命令就得能点名这个类型，而
`biz` 本来就 import 扩展，反过来就是环。

```python
COMMANDS: Final[tuple[CliCommand, ...]] = (
    CliCommand(
        name="echo",
        description="Echo the text back.",
        parameters={"type": "object", "properties": {...}, "required": ["text"]},
        handler=echo,
    ),
)
```

**命令名要收字符集**（`^[a-z][a-z0-9_-]{0,63}$`）。命令名会进 shell 命令行：模型把
`oath-cli <名字>` 原样打进 bash 执行环境的命令。名字来自扩展目录，而扩展目录可以是不进
git 的本地目录 —— 仓库作者不再是唯一的把关人。`properties` 的声明顺序同时是
`oath-cli` 的位置参数顺序。

### 4.5 模块怎么被装进来

每个扩展目录映射成一个**合成包名** `oath_ext_<目录名>_<路径哈希>`：目录名做前缀（非标识符
字符换成 `_`），后缀是规范化完整路径的哈希。目录名不能直接当包名 —— `extensions-local` 里有
`-`；只按目录名区分也不够 —— 配置允许任意路径，`a/addons` 与 `b/addons` 是两个不同的目录，
共用一个包名会让第二个被当成装过的那个，报出一次假重名。哈希必须稳定跨进程（不用 `hash()`），
否则「装过不重装」在下一次启动就失效。

`<目录>` / `<目录>/tools` / `<目录>/cli` 注册成带 `__path__` 的包，叶模块用
`spec_from_file_location` 装，先注册进 `sys.modules` 再执行。装过的不再装：一个进程里
同一段代码只该有一个类对象。重载留给重启 —— 改配置本来就要重启。

## 5 实现方案

`bootstrap/extension_loader.py` 是**唯一 import 扩展代码的地方**，产出
`Extensions(tools, commands, skill_dirs)`。它只报技能目录：技能文件由 `SkillSystem`
按自己的 frontmatter 规则扫，那里已经有读坏就跳过的逻辑。

`Runtime.__init__` 装好扩展后才继续：命令表存进 `runtime.commands`，工具注册进
`ToolRegistry`，技能目录排在 `skill_dir` 之后交给 `SkillSystem`。装配期失败意味着
进程不启动，而不是「模型看不到它」。

`biz/cli/cli_server.py` 只管路由与鉴权，命令表从 `runtime.commands` 读 —— 它不 import
任何一个扩展。

## 6 失败与恢复

| 情况 | 结果 |
| --- | --- |
| 目录或子目录不存在 | 扫到空，不报错 |
| 模块导入抛异常 | `ExtensionError`，报出文件；模块从 `sys.modules` 撤掉 |
| 工具构造要参数 | `ExtensionError`，说明零参构造这条约定 |
| 工具名重复 | `ExtensionError`，报出**两个**文件 |
| 命令名不合法 | `ExtensionError`，报出文件与那个名字 |
| 命令名重复 | `ExtensionError`，报出两个文件 |
| 技能文件读坏 | 跳过该文件并告警（`SkillSystem` 的既有行为） |
| 改了扩展没重启 | 不生效。索引与命令表只在启动时构建 |

**已知的缺口**：扩展没有热加载。改了一个本地工具要重启 runtime 才看得见，这对开发体验
是真实的损失，先记在这里而不是假装没有。

## 7 跨仓库影响

Hymio / Nostalgia 把自己的工具、命令、技能放进 `extension_dirs` 里的任意一个目录即可，
不需要改 Oath 的代码。跨仓库的 wire 契约（`ToolDefinition` 的字段与 schema）不变。

## 8 待决事项

- 扩展之间的依赖声明：一个扩展要用另一个扩展的东西，现在靠 import 同级模块。
- 扩展目录可以配到仓库之外（当前允许，但没有对应的路径约定与文档）。
- 目录扫描要不要给一个上限，防止误把巨大的目录树当扩展目录。
