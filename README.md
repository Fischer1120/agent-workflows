# Agent Workflows

把当前 Codex 与 DSH 协作规则整理成可部署的技能包。包含分层派发、独立验收、纠正记录、中文和英文文风技能，以及全局和项目 `AGENTS.md` 模板。

## 给 agent 的部署入口

克隆仓库后，先读 [部署说明](docs/deployment.md)。默认先预览，确认输出路径后再加 `--apply`：

```sh
git clone https://github.com/Fischer1120/agent-workflows.git
cd agent-workflows
python3 scripts/install.py --project "/absolute/project"
python3 scripts/install.py --project "/absolute/project" --apply
```

安装器保留目标项目已有指令和纠正记录，对变更文件先做备份。安装到新项目后，重新加载该项目，让 agent 发现 `.agents/skills`。也可以全局安装：

```sh
python3 scripts/install.py --global
python3 scripts/install.py --global --apply
```

全局技能默认放在 `~/.agents/skills`，全局指令放在 `~/.codex/AGENTS.md`，纠正记录放在 `~/.codex/agent-workflows/docs/corrections`。`--codex-home`、`--skills-home` 和 `--records-root` 可调整位置。

## 包含什么

| 内容 | 位置 | 用途 |
| --- | --- | --- |
| 分层协作技能及派发入口 | [.agents/skills/codex-tiered-workflow](.agents/skills/codex-tiered-workflow/SKILL.md) | 模态判断、Sol/DSH/Luna/Terra 分工、等待和验收 |
| 纠正记录技能 | [.agents/skills/project-corrections](.agents/skills/project-corrections/SKILL.md) | 查询旧方案、记录实际问题、同步索引 |
| 中文和英文文风技能 | [.agents/skills/humanizer-zh](.agents/skills/humanizer-zh/SKILL.md)、[humanizer](.agents/skills/humanizer/SKILL.md) | 交付前规范自然语言文档 |
| 项目指令 | [AGENTS.md](AGENTS.md) | 本仓库的协作和成品要求 |
| 指令模板 | [全局模板](templates/global.AGENTS.md)、[项目模板](templates/project.AGENTS.md) | 部署到其他机器或项目 |
| DSH 技能共享适配器 | [tools/dsh-codex-import](tools/dsh-codex-import/README.md) | 让 DSH 读取原技能正文和资源 |
| 维护规则及模板 | [docs/corrections](docs/corrections/index.md) | 初始化目标项目自己的纠正记录 |
| 工作流案例 | [docs/workflow-cases.md](docs/workflow-cases.md) | 可复用的脱敏排错经验 |
| Superpowers 7.0.1 | [optional](optional/README.md) | 15 个开发流程技能及完整资源，可选启用 |

## 运行前确认

安装和离线验证使用 Python 3.10+。DSH 共享适配器还需要 Node.js 和已安装的 DSH Desktop。执行 Luna 任务需要已登录且具备相应模型权限的 Codex CLI；执行 DSH 任务需要已运行的 Desktop Bridge。

工作流保留源配置中的 `gpt-6.1-sol`、`gpt-6-luna`、`gpt-6.1-terra` 和 `gpt-6-astra` 角色要求。这些是该工作流的运行时标识，部署前应在自己的环境核对可用性。配置文件无法授予模型、Fast、工具或账户权限；模型不可用时应明确回报。

DSH 默认模型留空，安装时从本机实时 catalog 选择：

```sh
python3 .agents/skills/codex-tiered-workflow/scripts/dispatch.py discover
python3 scripts/install.py --project "/absolute/project" --dsh-model "<selectionId>" --apply
```

模态限制优先于速度和复杂度分流。图片、音频、视频、截图及实际渲染验收不交给 DSH。普通任务最终由独立 Sol 审查；非多模态且用户明确要求速度优先的任务，由另一个 DSH 会话只读验收。

## 验证

```sh
python3 scripts/check.py
```

该命令运行离线派发与安装回归，并检查公开文件。DSH 适配器的验证命令见其说明。真实模型任务会使用本机账户，需另行执行和确认实际结果，离线检查不能替代它。

本次具体检查和修复范围见 [验证说明](docs/validation.md)。可由 agent 读取的技能清单见 [bundle.json](bundle.json)。

## 导出边界

本仓库导出当前定制工作流、直接引用的文风技能，以及可选的 Superpowers 开发流程技能。其他领域技能和插件不打包，见 [依赖与来源](docs/dependencies.md)。已移除本机账户、认证配置、Bridge endpoint、个人模型目录快照、运行简报及无关私人项目记录；保留规则、安装代码和可复用案例。

本项目代码使用 [MIT License](LICENSE)。文风技能保留上游许可证及署名，见 [第三方说明](THIRD_PARTY_NOTICES.md)。
