# Codex 与 DSH 工作流

公开版把可执行文件统一放在仓库根目录的 `.agents/skills/codex-tiered-workflow/scripts`。技能入口见 [SKILL.md](../../.agents/skills/codex-tiered-workflow/SKILL.md)，安装器为 `scripts/install.py`，支持项目和全局部署。

Sol 负责需求、边界、任务分配、设计和关键决策。模态判断优先于速度和复杂度；多模态任务不交给 DSH。普通有界任务由 DSH、Luna 或可选 Terra 执行，最终由未参与实现的 Sol 审查。非多模态且用户明确要求速度优先的任务，由另一个 DSH 会话只读验收。

派发和等待规则见 [部署说明](../../docs/deployment.md)。只有收到当前轮的真实结束状态并读取结果和必要产物后，才推进依赖步骤。权限、业务含义和模型可用性均以实际环境为准。

运行 `python3 scripts/check.py` 验证派发规则和安装行为。DSH 原生 Bridge 与 Luna app-server 的离线测试在 `tests/workflow`，真实运行还需本机的相应服务和权限。
