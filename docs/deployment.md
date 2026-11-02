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

Luna 使用 `--backend luna`，运行入口会核实服务端接受指定模型、`max` 和 Fast 后才开始任务。Terra 和 Astra 走宿主的原生子代理入口，派发器没有这两个 backend。

DSH 技能共享是独立的可选步骤，按 [适配器说明](../tools/dsh-codex-import/README.md) 生成本机清单并安装。清单含本机绝对路径，保持在本地。

## 交付和更新

更新前先拉取目标版本，再运行 `python3 scripts/check.py`。安装器支持重复运行；检查预览后再安装，保留备份和回执。脚本不会删除目标额外文件。

独立审查应只收到成品、具体要求及必要原始问题证据。审查者需核对实际文件和运行结果；有视觉成品时还需检查实际渲染。修复后复验受影响部分，最后分别报告本地完成、Git 提交、推送和发布状态。
