# Agent Workflows

[![CI](https://github.com/Fischer1120/agent-workflows/actions/workflows/validate.yml/badge.svg)](https://github.com/Fischer1120/agent-workflows/actions/workflows/validate.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](#环境要求)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**面向 Codex 与 DSH 的可部署协作工作流。**

把任务分派、独立验收、文档要求和纠正记录整理成可复用的技能包。在项目或全局安装同一套规则，让 agent 知道该由谁执行、如何检查，以及遇到问题时去哪里查找已验证的方案。

[快速开始](#快速开始) · [工作流](#工作流) · [技能与组件](#技能与组件) · [文档](#文档) · [验证](#验证) · [许可证](#许可证)

## 特性

- 提供项目和全局两种安装方式，默认预览，应用前备份，保留已有指令和纠正记录。
- 用实际脚本派发 DSH 和 Luna 任务，保存会话回执、状态和结果。
- 先判断任务模态，再按速度要求和复杂度分工；成品由未参与实现的执行者独立验收。
- 将纠正记录的查询、复用、补录和索引维护纳入交付流程。
- 随附中文、英文文风技能，以及可选的 Superpowers 开发流程技能。
- 提供机器可读的 [技能清单](bundle.json)、离线测试和 GitHub Actions 检查。

## 快速开始

需要 Python 3.10+ 和 Git。先克隆仓库，再预览目标项目的安装计划：

```sh
git clone https://github.com/Fischer1120/agent-workflows.git
cd agent-workflows

python3 scripts/install.py --project "/absolute/project"
```

核对输出路径后执行安装：

```sh
python3 scripts/install.py --project "/absolute/project" --apply
```

安装器会写入项目的 `.agents/skills`、`AGENTS.md` 和 `docs/corrections`。已有 `AGENTS.md` 只合并工作流与纠正记录区段，其他规则和已有记录保持原样；变更文件的备份位置会写入回执。安装完成后，在宿主中重新加载目标项目。

### 全局安装

```sh
python3 scripts/install.py --global
python3 scripts/install.py --global --apply
```

默认位置：

| 内容 | 路径 |
| --- | --- |
| 核心技能 | `~/.agents/skills` |
| 全局指令 | `~/.codex/AGENTS.md` |
| 纠正记录 | `~/.codex/agent-workflows/docs/corrections` |

使用 `--codex-home`、`--skills-home` 和 `--records-root` 可以调整位置。完整参数与升级方式见 [部署说明](docs/deployment.md)。

### 交给 agent 部署

也可以将下面的任务交给支持本地文件和命令执行的 agent：

```text
将 https://github.com/Fischer1120/agent-workflows 的核心工作流安装到当前项目。
先阅读 README.md、AGENTS.md 和 docs/deployment.md，在当前授权范围内预览并安装。
保留已有指令和纠正记录，检查安装路径、技能资源和备份；完成后报告实际验证结果。
```

## 工作流

Sol 负责需求、验收标准、设计和关键决策。执行前先判断任务需要的模态，再决定是否采用速度优先模式。

| 任务 | 执行 | 最终独立验收 |
| --- | --- | --- |
| 图片、音频、视频、截图或实际渲染相关任务 | 支持相应模态与工具的 Codex 执行者 | Sol |
| 用户明确要求速度优先的非多模态任务 | DSH | 另一个 DSH 会话，只读验收 |
| 普通有界任务 | 按复杂度分配给 DSH、Luna 或可选 Terra | Sol |
| Sol 经针对性分析仍无法解决的十分复杂问题 | Astra 专项求解，再交回 Sol 协调 | Sol |

模态限制优先于速度和复杂度。需要理解或生成多模态内容的工作不派发给 DSH；独立验收只依据成品、具体要求和必要原始证据。

DSH 任务须等待当前轮真正完成，读取 `result.txt` 和必要产物后再推进依赖步骤。等待期间只处理互不依赖、文件不重叠的工作。完整规则见 [工作流技能](.agents/skills/codex-tiered-workflow/SKILL.md)。

### 配置与派发

DSH 默认模型留空。先启动 DSH Desktop，从目标机器的实时 catalog 选择模型：

```sh
python3 .agents/skills/codex-tiered-workflow/scripts/dispatch.py discover
python3 scripts/install.py --project "/absolute/project" \
  --dsh-model "<selectionId>" --apply
```

准备好包含目标、允许修改的文件和验收要求的任务简报后，从已安装的项目根目录派发：

```sh
python3 .agents/skills/codex-tiered-workflow/scripts/dispatch.py submit \
  --backend dsh --cwd "/absolute/project" \
  --task-title "子任务" --brief-file "/absolute/brief.txt" \
  --sandbox workspace-write
```

Luna 使用 `--backend luna`。Terra 和 Astra 通过宿主的原生子代理入口分配。状态查询、速度优先验收和异常处理示例见 [部署说明](docs/deployment.md)。

## 技能与组件

默认安装 **4 个核心技能**；另有 **15 个可选开发流程技能**。

| 组件 | 说明 |
| --- | --- |
| [codex-tiered-workflow](.agents/skills/codex-tiered-workflow/SKILL.md) | 分工、派发、等待、整合与独立验收 |
| [project-corrections](.agents/skills/project-corrections/SKILL.md) | 查询和维护可复用的问题记录 |
| [humanizer-zh](.agents/skills/humanizer-zh/SKILL.md) | 中文文档的语言规范与润色 |
| [humanizer](.agents/skills/humanizer/SKILL.md) | 英文文档的语言规范与润色 |
| [Superpowers 7.0.1](optional/README.md) | 需求讨论、计划、TDD、排错、审查等 15 个技能，默认不启用 |
| [DSH 技能共享适配器](tools/dsh-codex-import/README.md) | 让 DSH 读取原技能正文和资源目录，可选安装 |

全局和项目的 [AGENTS.md 模板](templates) 随包提供。完整导出范围与源入口对应关系见 [导出清单](docs/export-inventory.md)。

## 环境要求

| 能力 | 前置条件 |
| --- | --- |
| 安装技能、运行离线检查 | Python 3.10+ |
| 执行 Luna 任务 | 已登录的 Codex CLI，以及对应模型、推理强度和 Fast 权限 |
| 执行 DSH 任务 | 已运行且可访问的 DSH Desktop Bridge |
| DSH 技能共享与 Node 测试 | Node.js 和 DSH Desktop 随附依赖 |

源工作流使用 `gpt-6.1-sol`、`gpt-6-luna`、`gpt-6.1-terra` 和 `gpt-6-astra` 角色标识。部署时须在自己的运行时核对可用性。文件安装不会授予模型权限，也不会迁移账户、登录状态或插件专属工具。

DSH 适配器的默认应用路径面向 macOS，其他安装位置可通过环境变量配置。其他 agent 可以复用技能和指令；派发脚本、工具入口与权限机制仍需按宿主适配。

## 文档

| 文档 | 内容 |
| --- | --- |
| [部署说明](docs/deployment.md) | 项目、全局安装，模型选择与派发示例 |
| [项目指令](AGENTS.md) · [全局模板](templates/global.AGENTS.md) | 协作、文档与验收要求 |
| [技能清单](bundle.json) · [导出对应关系](docs/export-inventory.md) | 供 agent 读取的入口与迁移范围 |
| [纠正记录](docs/corrections/index.md) · [排错案例](docs/workflow-cases.md) | 维护格式与脱敏经验 |
| [验证说明](docs/validation.md) | 实际测试结果、审查修复与验证边界 |
| [依赖与来源](docs/dependencies.md) · [变更记录](CHANGELOG.md) | 上游组件与版本变化 |

## 验证

```sh
python3 scripts/check.py
```

检查包括公开文件扫描、技能包校验，以及 **80 项 Python 测试**。已安装 DSH 依赖时，还可运行 **26 项 Node 测试**：

```sh
node --test tools/dsh-codex-import/test_provider.mjs \
  tools/dsh-codex-import/test_build_manifest.mjs \
  tools/dsh-codex-import/test_verify.mjs
```

这些测试覆盖安装和派发规则。真实模型任务仍需使用目标机器的账户单独验证；具体检查结果见 [验证说明](docs/validation.md)。

## 隐私与导出范围

公开包保留规则、代码、模板和可复用案例。本机认证、Bridge endpoint、运行简报、个人模型目录快照及无关私人项目历史未随包发布。

生成的 DSH `manifest.json` 含本机绝对路径，日志和回执也可能包含任务材料；它们已加入 `.gitignore`，应保留在本地。其他领域技能与插件的取舍见 [依赖与来源](docs/dependencies.md)。

## 参与贡献

欢迎通过 [Issues](https://github.com/Fischer1120/agent-workflows/issues) 报告问题，或提交 Pull Request。问题报告请附运行环境、复现步骤、脱敏后的错误和预期行为；涉及安装器的改动应附回归测试，并保留现有文件、权限与独立验收规则。

## 许可证

本项目使用 [MIT License](LICENSE)。Humanizer、Humanizer-zh 和 Superpowers 保留各自的上游许可证及署名，见 [第三方说明](THIRD_PARTY_NOTICES.md)。本项目不是 OpenAI 或 DeepSeek 官方发行物。

README 的组织方式参考了 [uv](https://github.com/astral-sh/uv/blob/main/README.md) 的简洁介绍与命令示例，以及 [Anthropic Skills](https://github.com/anthropics/skills/blob/main/README.md) 的技能说明方式。
