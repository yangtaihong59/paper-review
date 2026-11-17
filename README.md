# Paper Review：双语论文修改与审阅

一个面向 LaTeX 稿件的本地工具：以中文为主稿，用稳定段落编号同步英文，并在网页中逐段对照、批注和回复。页面仅监听回环地址，数学公式使用随仓库提供的 KaTeX，无需前端构建或 CDN。

## 快速开始

需要 Python **3.11 或更高版本**、Git；生成 PDF 插图预览可选安装 `pdftoppm`。克隆后：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m paper_review list
python -m paper_review serve --paper example
```

浏览器访问 <http://127.0.0.1:8790/>。示例稿件是虚构文本，仅用于演示。端口被占用时可指定 `--port 8791` 或加 `--next-free`。远程机器使用 SSH 转发，在自己的电脑运行：

```bash
ssh -N -L 8790:127.0.0.1:8790 <服务器别名>
```

## 接入自己的稿件

复制 `config.toml` 为不提交的 `config.local.toml`，设置 `repo` 为论文 Git 仓库目录（相对于配置文件或绝对路径），每篇论文在白名单中添加 `[[papers]]`：

```toml
repo = "/path/to/my-papers"
[server]
host = "127.0.0.1"
port = 8790
[[papers]]
id = "my-paper"
dir = "my-paper"
manuscript = "manuscript-ieee"
title = "我的论文"
# default_base = "last-round"
```

`dir` 相对 `repo`，`manuscript` 相对论文目录；工具拒绝越过这些目录的路径。运行时在子命令之后添加 `--config config.local.toml`，例如：

```bash
python -m paper_review serve --config config.local.toml --paper my-paper
python -m paper_review rebuild --config config.local.toml --paper my-paper
```

默认同步工具是本仓库的 `scripts/zh_en_sync.py`；若配置 `[defaults] sync_tool` 或论文条目的 `sync_tool`，路径相对 `repo`（也可为绝对路径）。每篇稿件需要英文 `main.tex`、中文 `zh/main.tex`，及两边 `\input` 引用的内容；对应段落分别标 `%%¶ intro-01` 和 `%%¶ intro-01 zh=<中文指纹>`。两边用相同编号，编号不重用。工具可读取 `shared/`、`glossary.tsv`、各语言的 `.aux` / `.bbl` 与图片。它是面向此约定的 LaTeX 渲染器，不保证支持全部宏；未知宏会显示出来供检查。

## 修改流程

1. 在中文主稿修改后，执行 `python scripts/zh_en_sync.py changes --root <稿件目录> --since <同步点> --output <输出前缀>`，检查新增、改动、删除和调序。
2. 同步英文并人工核对译义、数学式和引用，逐段更新英文编号行的中文 SHA。指纹算法与 `scripts/zh_en_sync.py` 的 `fingerprint()` 一致；不要批量盖章绕过翻译核对。
3. 执行 `python scripts/zh_en_sync.py check --root <稿件目录> --json`。退出码 0 表示通过，1 表示有告警，2 表示运行错误。检查数字和引用集合、术语及漂移，不能替代人工核对。
4. 按稿件自己的构建说明编译中英文 PDF，再用 `report --root <稿件目录> --since <同步点> --zh-pdf <中文PDF> --en-pdf <英文PDF>` 生成比较报告。页面上限通过 `sync-config.json` 的 `max_pages` 设置，缺省为 18；报告与检查无告警后可用 `mark-synced` 创建快照和 `zh-en-sync/*` 标签。此操作修改 Git 对象和标签，不会 push；首次同步和参数参见 `python scripts/zh_en_sync.py --help` 及各子命令的 `--help`。
5. 在网页选择段落、添加批注并提交一轮；用命令行 `export` 导出，处理后用 `reply` 写回，再用 `rebuild` 刷新。随仓库附有供 AI 使用的 `.agents/skills/paper-zh-en-sync/SKILL.md`。

```bash
python -m paper_review export --config config.local.toml --paper my-paper --round latest
python -m paper_review reply --config config.local.toml --paper my-paper --id 1 --status addressed --reply '已修改'
python -m paper_review rebuild --config config.local.toml --paper my-paper
```

页面支持已读进度、差异对比、批注侧栏、筛选、论文切换和快捷键。同步基准默认取已有同步点或 HEAD，也可配置 `default_base = "last-round"` 使用最近提交批注轮次的同步点。

## 数据与安全

批注、已读状态、缓存和导出保存在 `<论文>/paper-session/review/`；请将 `paper-session/` 加入自己论文仓库的 `.gitignore`。网页只读取稿件，批注操作写入该目录。服务仅接受回环监听和回环 Host，写请求需要专用请求头；它不是面向公网的多用户服务。远程使用 SSH 转发，不要直接暴露到公网。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest
```

测试使用临时状态目录和临时 Git 仓库，不写真实论文。KaTeX 使用 MIT 许可，见 `static/vendor/katex/LICENSE`；项目其余代码尚未声明开源许可。
