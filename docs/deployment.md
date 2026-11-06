# 部署说明

## 项目安装

在克隆目录运行安装器，`--project` 指向需要使用工作流的项目。路径可以含空格。默认预览不写文件，`--apply` 才执行。

```sh
python3 scripts/install.py --project "/absolute/my project"
python3 scripts/install.py --project "/absolute/my project" --apply
```

新项目获得四个技能、项目指令和空的纠正记录。项目已有 `AGENTS.md` 时，安装器只合并带标记的工作流与纠正记录区段，保留其他规则。已有记录不覆盖，已有 DSH 模型选择在升级时保留，显式传入 `--dsh-model` 才替换。备份位置由安装回执给出。

## 全局安装

```sh
python3 scripts/install.py --global
python3 scripts/install.py --global --apply
```

需要自定义目录时使用：

```sh
python3 scripts/install.py --global \
  --codex-home "/absolute/codex home" \
  --skills-home "/absolute/agent skills" \
  --records-root "/absolute/workflow records" --apply
```

安装器不修改 `config.toml`，也不导入认证文件。不会自动改 DSH profile。若现有指令标记损坏或目标路径经过符号链接，安装器会停止，需先核对输出中的具体路径。使用哪个目录，应由部署者根据宿主实际支持的位置决定。

Codex 的全局与项目指令、技能目录约定参考 [官方自定义说明](https://developers.openai.com/codex/customization/overview)。其他 agent 可以读取同一份技能，但它们的工具、子代理入口和运行时协议需要分别适配。

## 配置 DSH 与派发

工作流按模型档位分工，模型名只是参考实现。旗舰档（`gpt-6.1-sol`、Claude Opus、Gemini 4 等）负责需求与验收标准、任务分解、前端设计和关键决策；中等复杂度、边界明确、要求快速处理的非多模态任务交给 DSH；简单、重复、可独立核验的快速任务交给低端档（`gpt-6-luna` 固定 `max` 并启用 Fast、Claude Haiku 等）；原生中坚子代理（`gpt-6.1-terra`）可选；攻坚档用于旗舰档明确无法解决的十分复杂问题，尽量少用。多模态任务不交给 DSH 或低端档。

先启动 DSH Desktop，再运行已安装技能的 `scripts/dispatch.py discover`。从结果中选取实际启用模型的 `selectionId`，通过 `--dsh-model` 保存，或在每次派发时显式传入。

下面示例从已安装项目根目录运行：

```sh
python3 .agents/skills/codex-tiered-workflow/scripts/dispatch.py submit \
  --backend dsh --cwd "/absolute/project" --task-title "有界子任务" \
  --brief-file "/absolute/brief.txt" --sandbox workspace-write --dsh-model "<selectionId>"
python3 .agents/skills/codex-tiered-workflow/scripts/dispatch.py status \
  --job-file "/absolute/project/.workflow-runs/cw-.../job.json"
```

等待同一任务真正完成，读取 `result.txt` 和必要产物后再开展依赖工作。`idle` 不表示完成，`needs_attention` 不表示失败，投递状态不确定时先查同一会话。等待期间只处理无依赖且文件不重叠的工作。

低端档用 `--backend luna` 派发，这是参考实现；运行入口会核实服务端接受指定模型、`max` 和 Fast 后才开始任务。原生中坚子代理和攻坚档走宿主的原生子代理入口，派发器没有这两个 backend。

任务已经在 DSH 会话里发起时，省去 `scripts/dispatch.py` 的跨进程派发，直接用当前会话的原生子代理分派有界子任务。原本给低端档的简单快速任务合并到 DeepSeek 处理；其他宿主没有可用低端档位时同样合并。需要跨进程回执、状态查询或给其他宿主留验收记录时仍用派发脚本。

用户明确要求速度优先的非多模态任务，从外部宿主用 `--speed-priority` 派发，验收阶段加 `--phase review` 并保持只读：

```sh
python3 .agents/skills/codex-tiered-workflow/scripts/dispatch.py submit --speed-priority --phase execute --cwd "/absolute/project" --task-title "速度优先执行" --brief-file "/absolute/execute-brief.txt" --sandbox workspace-write
python3 .agents/skills/codex-tiered-workflow/scripts/dispatch.py submit --speed-priority --phase review --cwd "/absolute/project" --task-title "DSH 独立验收" --brief-file "/absolute/review-brief.txt" --sandbox read-only
```

成品由未参与实现的另一个 DSH 会话或独立 DSH 子代理只读验收。DSH 内发起的同类任务直接在会话内完成，不再绕回派发脚本。

DSH 技能共享是独立的可选步骤，按 [适配器说明](../tools/dsh-codex-import/README.md) 生成本机清单并安装。清单含本机绝对路径，保持在本地。

## 交付和更新

更新前先拉取目标版本，再运行 `python3 scripts/check.py`。安装器支持重复运行；检查预览后再安装，保留备份和回执。脚本不会删除目标额外文件。

独立审查应只收到成品、具体要求及必要原始问题证据。审查者需核对实际文件和运行结果；有视觉成品时还需检查实际渲染。修复后复验受影响部分，最后分别报告本地完成、Git 提交、推送和发布状态。
