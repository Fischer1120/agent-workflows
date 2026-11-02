# 可选开发流程技能

`superpowers/` 保存导出时已安装的 Superpowers 7.0.1 插件快照，包含 15 个技能、参考资料、辅助脚本、插件元数据及所需图标。它覆盖需求讨论、计划、测试、排错、子代理协作、代码审查和交付验证。

默认安装器只部署根目录列出的四个核心技能，不自动启用 Superpowers。需要完整开发流程时，将 `superpowers/` 作为本地插件交给支持该结构的宿主，或使用宿主的技能安装器从 `superpowers/skills/` 安装所需技能。迁移时应保留整个技能目录和相对路径，不能只复制 SKILL.md。

该目录保留上游原始文件与 [MIT License](superpowers/LICENSE)，上游安装说明见 [README](superpowers/README.md)。插件功能和辅助脚本仍受宿主工具及权限约束。用户当次指令、项目授权和当前运行时限制优先于技能流程。
