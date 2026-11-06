# Codex 与 DSH 工作流

公开版把可执行文件统一放在仓库根目录的 `.agents/skills/codex-tiered-workflow/scripts`。技能入口见 [SKILL.md](../../.agents/skills/codex-tiered-workflow/SKILL.md)，安装器为 `scripts/install.py`，支持项目和全局部署。

工作流按模型档位分工，模型名只是参考实现。旗舰档（`gpt-6.1-sol` 等）负责需求、边界、任务分配、设计和关键决策。模态判断优先于速度和复杂度；多模态任务不交给 DSH 或低端档。中等复杂度、边界明确、要求快速处理的非多模态任务交给 DSH；简单、重复、可独立核验的快速任务交给低端档（`gpt-6-luna` 等）；原生中坚子代理（`gpt-6.1-terra`）可选。普通任务成品由未参与实现的旗舰子代理审查；非多模态且用户明确要求速度优先的任务，由未参与实现的 DSH 会话或独立 DSH 子代理只读验收。

任务已经在 DSH 会话里发起时，省去 `scripts/dispatch.py` 的跨进程派发，直接用当前会话的原生子代理分派有界子任务；原本给低端档的简单快速任务合并到 DeepSeek 处理，其他宿主没有可用低端档位时同样合并。需要跨进程回执、状态查询或给其他宿主留验收记录时仍用派发脚本。

派发和等待规则见 [部署说明](../../docs/deployment.md)。只有收到当前轮的真实结束状态并读取结果和必要产物后，才推进依赖步骤。权限、业务含义和模型可用性均以实际环境为准。

运行 `python3 scripts/check.py` 验证派发规则和安装行为。DSH 原生 Bridge 与低端档 app-server 的离线测试在 `tests/workflow`，真实运行还需本机的相应服务和权限。
