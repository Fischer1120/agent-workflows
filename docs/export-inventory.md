# 导出清单与对应关系

| 源工作流内容 | 公开版位置或替代入口 |
| --- | --- |
| 已安装的 codex-tiered-workflow 技能、元数据、派发器、Bridge 和 Luna worker | `.agents/skills/codex-tiered-workflow/` |
| project-corrections 技能及元数据 | `.agents/skills/project-corrections/` |
| 全局 AGENTS.md | `templates/global.AGENTS.md` |
| 项目 AGENTS.md | 根 `AGENTS.md` 和 `templates/project.AGENTS.md` |
| 源工作流 README | `tools/codex-dsh-workflow/README.md`，按公开目录修订 |
| 旧工作流安装器 | `scripts/install.py`，支持项目和全局部署，保留备份和既有规则 |
| 固定供应商的默认模型设置脚本 | 安装器 `--dsh-model` 与派发器同名参数，模型从目标机器实时 catalog 选择 |
| 派发、Bridge、Luna 回归测试 | `tests/workflow/` |
| DSH 技能共享适配器、清单生成、目录读取、安装与验证 | `tools/dsh-codex-import/` |
| 本机完整技能清单 | 不发布，提供 `manifest.example.json` 与目标机器重新生成入口 |
| 纠正记录的规则、模板和索引格式 | `docs/corrections/`，初始化为空记录库 |
| 与工作流直接相关的历史案例 | `docs/workflow-cases.md`，脱敏摘要与原编号 |
| humanizer、humanizer-zh 及参考资料 | `.agents/skills/`，保留上游许可证 |
| Superpowers 7.0.1 开发流程插件 | `optional/superpowers/`，15 个技能及完整随附资源 |

此清单说明迁移后的入口。原机器的认证、Bridge endpoint、运行记录和无关私人项目资料不属于部署包。
