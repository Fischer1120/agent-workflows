# Codex 与 DSH 共享技能

DSH 直接读取 Codex 的 `SKILL.md` 和技能资源目录，正文、脚本和参考资料都不复制。适配器只维护名称与文件路径的清单，处理 DSH 不接受的大写、命名空间和重名技能。

全局指令通过 `~/.dsh/AGENTS.md` 链接到 `~/.codex/AGENTS.md`。项目里的 `AGENTS.md` 和 `.agents/skills` 由 DSH 按工作目录原生加载，保留各自的适用范围。

Codex 插件提供的专属工具、登录状态和应用连接不会随技能文件导入。模型、工具和账户也不随清单迁移。DSH 能读到技能说明，实际执行仍要看对应能力是否存在；缺失时如实回报，不要声称已经执行。

## 前置条件

- macOS，已安装 DSH Desktop。默认依赖根目录是 `/Applications/DSH Desktop.app/Contents/Resources/app`。安装在其他位置时设置 `DSH_APP_ROOT`（应用资源目录）或 `DSH_NODE_MODULES`（该目录下的 `node_modules`）。
- Node.js 和 Python 3.9+。可以用 DSH 自带运行时，也可以用系统版本。
- `codex` 命令可用，并且 `~/.codex/AGENTS.md` 已存在。Codex 主目录不在默认位置时，用 `--codex-home` 指定。
- 不需要安装 npm 或 pip 依赖。读取 Desktop 插件技能和使用 provider 解析技能前置数据时，都用 DSH 自带的 YAML 解析器。需要精确指定解析器位置时，设置 `DSH_YAML_PACKAGE_JSON`。

## 部署步骤

以下命令都在仓库根目录运行，路径按实际位置替换。生成的清单包含本机绝对路径，已被 `tools/dsh-codex-import/.gitignore` 忽略，不要提交。

### 1. 读取 Codex 技能清单

```sh
python3 tools/dsh-codex-import/read_codex_catalog.py \
  --cwd "/绝对路径/项目" \
  --output tools/dsh-codex-import/.verification/codex-catalog.json
```

脚本只启动 `codex app-server --stdio`，完成 initialize 握手后调用 `skills/list`，不会启动推理轮。输出含本机路径，权限为 0600。

### 2. 生成 DSH 清单

只使用 Codex 全局技能：

```sh
node tools/dsh-codex-import/build_manifest.mjs \
  --catalog tools/dsh-codex-import/.verification/codex-catalog.json \
  --output tools/dsh-codex-import/manifest.json
```

如果要导入桌面插件技能，先手动确认当前实际加载的插件版本目录，再重复传入它的技能根目录。脚本不会自动遍历缓存版本。下面的版本号只是示例，按本机实际目录替换：

```sh
node tools/dsh-codex-import/build_manifest.mjs \
  --catalog tools/dsh-codex-import/.verification/codex-catalog.json \
  --output tools/dsh-codex-import/manifest.json \
  --desktop-skills "$HOME/.codex/plugins/cache/openai-curated-remote/data-analytics/1.0.11/skills" \
  --desktop-skills "sites=$HOME/.codex/plugins/cache/openai-curated-remote/sites/1.0.2/skills"
```

默认命名空间取 `<插件>/<版本>/skills` 中的插件目录名，也可以用 `命名空间=路径` 覆盖。已禁用的技能只写入 `disabled`；`repo` 范围的技能留在 `project_skills`，由 DSH 原生发现。清单条目只有 `name`、`codex_name`、`path` 三个字段。

### 3. 预演并安装

```sh
python3 tools/dsh-codex-import/install.py \
  --manifest tools/dsh-codex-import/manifest.json \
  --dsh-home "$HOME/.dsh" \
  --codex-home "$HOME/.codex"
```

默认只打印计划，不写文件。确认后加 `--apply`：

```sh
python3 tools/dsh-codex-import/install.py \
  --manifest tools/dsh-codex-import/manifest.json \
  --dsh-home "$HOME/.dsh" \
  --codex-home "$HOME/.codex" \
  --apply
```

安装器先检查清单、技能源文件、DSH profile 和指令链接，全部通过后才写入。写入前备份 profile 和旧的插件文件。已有 `AGENTS.md` 不是指向 Codex 指令的链接时拒绝覆盖；profile 中已有非托管同名 id 或标记不完整时也拒绝。其他 profile 配置保持不变，包括原有换行字节。目标文件、目标目录、profile、临时文件和备份路径及其父级不得含符号链接；已有临时文件也会被拒绝。唯一允许的目标链接是已指向所选 Codex 指令文件的 DSH `AGENTS.md`。安装回执写在备份目录内。

### 4. 验证

已安装：

```sh
node tools/dsh-codex-import/verify_import.mjs --cwd "/绝对路径/项目" --installed --dsh-home "$HOME/.dsh"
```

安装前或临时目录：

```sh
node tools/dsh-codex-import/verify_import.mjs \
  --cwd "/绝对路径/项目" \
  --manifest tools/dsh-codex-import/manifest.json
```

使用自定义 DSH 目录安装时，将 `--dsh-home` 设为同一目录。验证脚本只检查当前清单声明的条目：共享技能正文和资源目录是否仍指向原文件，`project_skills` 是否能由 DSH 原生发现，`disabled` 是否没有被导入。项目原生技能与共享技能同名时，验证器分别检查共享源和清单声明的实际生效项目源，并在 `shadowed_shared_skills` 中报告覆盖；`shared_skills_loaded` 是已核验的共享源数量，`shared_skills_effective` 是当前生效的共享技能数量；未声明或路径不符的覆盖会报错。它不要求项目里必须有某个具体技能。报告写到 `tools/dsh-codex-import/.verification/`。

### 5. 测试

```sh
node --test tools/dsh-codex-import/test_provider.mjs \
  tools/dsh-codex-import/test_build_manifest.mjs \
  tools/dsh-codex-import/test_verify.mjs

python3 tools/dsh-codex-import/test_install.py -v
python3 tools/dsh-codex-import/test_read_catalog.py -v
```

测试只使用系统临时目录和 DSH 的只读包，不读写真实的 `~/.dsh` 或 `~/.codex`。

## 日常维护

Codex 技能增删、启用或禁用后，重新读取清单、生成并安装。技能正文和资源的修改不需要重装，DSH 每次加载都会读取原文件并刷新目录。桌面插件升级后，重新确认插件版本目录并传入新的 `--desktop-skills`，不要假定旧缓存仍然有效。

`manifest.example.json` 是脱敏示例，只说明字段结构。不要把本机 `manifest.json`、`.verification/` 或 Codex 缓存中的个人技能复制进仓库。

## 已知限制

- 默认应用路径只覆盖 macOS。其他平台通过 `DSH_APP_ROOT` 或 `DSH_NODE_MODULES` 指定 DSH 依赖根目录。
- 桌面插件版本需要人工确认，脚本不猜测哪个缓存版本正在使用。
- 验证范围限于清单可判定的事实，不等于真实运行时的模型、工具或账户已经可用。