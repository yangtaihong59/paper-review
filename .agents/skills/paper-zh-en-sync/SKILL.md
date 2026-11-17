---
name: paper-zh-en-sync
description: 同步以中文为主稿的 LaTeX 中英文论文，检查段落漂移并完成审阅。
---

# 中英论文同步

中文 `zh/` 是内容源；中英文使用相同的 `%%¶ <id>` 稳定段落编号，英文编号行的 `zh=<sha>` 记录已核对中文块的指纹。共享宏与数据放在 `shared/`，术语放在 `glossary.tsv`。不要重用删除的编号。数学式、引用和术语仍需人工复核。

1. 确定稿件路径和上次同步点，运行 `python <本仓库>/scripts/zh_en_sync.py changes --root <稿件目录> --since <tag或commit> --output <任务目录>/changes`。
2. 只翻译和调整变化的块；英文的内容变化先回写中文。核对译义后更新对应英文编号行的中文指纹。
3. 运行 `python <本仓库>/scripts/zh_en_sync.py check --root <稿件目录> --json` 并处理告警。数字和引用集合检查不能替代人工审核。
4. 使用稿件自身的构建说明编译中英文正文 PDF。执行 `report --root <稿件目录> --since <同步点> --zh-pdf <中文PDF> --en-pdf <英文PDF> --output <报告路径>`，确认无告警且页数不超过 `sync-config.json` 的 `max_pages`（默认 18）。
5. 门禁通过后用 `mark-synced --root <稿件目录> --zh-pdf <中文PDF> --en-pdf <英文PDF>` 记录同步点。该命令创建 Git 快照与 tag，不 push；检查并遵守稿件仓库的 Git 授权。
6. 使用 paper-review 网页审阅、提交批注轮次；用 `export` 导出批注、修改后用 `reply` 写回回复，再用 `rebuild` 刷新。

退出码 0 表示通过，1 表示检查或 PDF 页数告警，2 表示参数、文件或运行错误。首次使用、英文单边润色豁免以及 PDF 编译的具体约束见项目 README 和工具 `--help`。不得通过批量改指纹掩盖未核对的翻译。
