> ⚠️ **免责声明**：此插件由 AI 编写，请谨慎使用。

# plugin_manager — Neo-MoFox 插件管理器

提供完整的插件生命周期管理：查询、安装、更新、备份/恢复、热加载、自动更新。

## 实现流程

### 1. 命令路由

使用 `@cmd_route` 装饰器注册子命令，框架自动构建 Trie 树路由：

```
/pm list                        → handle_list()
/pm check <name>                → handle_check()
/pm update <name>               → handle_update()
/pm update --all                → handle_update_all()
/pm backup <name>               → handle_backup()
/pm backup list <name>          → handle_backup_list()
/pm restore <name> [id]         → handle_restore()
/pm switch <name>               → handle_switch_branch()
/pm version <name>              → handle_switch_version()
/pm reload [name]               → handle_reload()
/pm install <url>               → handle_install()
/pm settings [key=value]        → handle_settings()
/pm autoupdate [on|off|interval]→ handle_autoupdate()
```

### 2. 安装插件流程（`/pm install`）

```
用户输入 /pm install <url>
↓
URL 预处理（自动补全 github.com/xxx）
↓
git clone --progress <url> <plugins_dir>/<repo_name>
  ├─ 异步子进程，每15秒发心跳消息
  ├─ 自动注入代理（插件配置 → 环境变量 → 默认 http://127.0.0.1:7890）
  └─ 超时3分钟清理并报错
↓
验证目录结构（排除仅 .git 的空目录）
↓
load_plugin() — MoFox 框架热加载
  ├─ 成功 → ✅ 已加载运行
  └─ 失败 → ⚠️ 显示原因，建议检查插件格式或用 /pm reload 重试
```

### 3. 热加载流程（`/pm reload`）

```
/pm reload（不带参数）
↓
扫描 plugins/ 目录下所有子目录
↓
对不在已加载列表中的目录调用 load_plugin()
  ├─ 成功 → 加入已加载列表
  └─ 失败 → 显示失败原因（如缺少 manifest.json）
↓
对已加载的插件逐一 reload_plugin()
```

### 4. 自动更新流程

```
后台定时任务（默认间隔480分钟）
↓
遍历设置 enabled=true 的插件
↓
检查 GitHub 远程版本
  ├─ 有更新 → git pull + 热重载
  └─ 无更新 → 跳过
```

### 5. 备份/恢复流程

```
/pm backup <name>
↓
读取 manifest.json 获取版本信息
↓
复制插件目录到 backups/<name>/<timestamp>/
↓
记录备份索引到 plugin_backups.json

/pm restore <name> [id]
↓
从备份索引查找备份路径
↓
替换插件目录 → reload_plugin()
```

## 配置项说明

| 配置项 | 类型 | 默认值 | 作用 |
|--------|------|--------|------|
| `proxy.http` | str | `""` | HTTP 代理地址，用于 git clone 时访问 GitHub。为空时自动读取系统环境变量 `HTTP_PROXY`，仍未空则用 `http://127.0.0.1:7890` |
| `proxy.https` | str | `""` | HTTPS 代理地址，同上优先从配置 → 环境变量 → 默认值 |
| `auto_update.enabled` | bool | `false` | 自动更新总开关，关闭后所有插件都不会自动检查更新 |
| `auto_update.check_interval_minutes` | int | `480` | 后台检查更新的间隔（分钟），最小 60 分钟 |
| `backup.max_backups_per_plugin` | int | `5` | 每个插件最多保留的备份数，超限时自动删除最旧的 |
| `backup.backup_dir` | str | `"backups"` | 备份目录名（相对于本插件目录） |
| `github.token` | str | `""` | GitHub 个人访问令牌，可选。用于提高 API 限频（从 60 次/小时 → 5000 次/小时） |

### 配置方式

```bash
/pm settings                              # 查看所有配置
/pm settings proxy.http=http://127.0.0.1:7890  # 设置代理（用 = 避免空格问题）
/pm settings auto_update.enabled 开启     # 开启自动更新
/pm settings check_interval_minutes 120   # 修改检查间隔
/pm settings backup.max_backups_per_plugin 10  # 修改备份上限
/pm settings github.token ghp_xxxxx       # 配置 GitHub Token
```

## 命令详解

| 命令 | 功能 | 说明 |
|------|------|------|
| `/pm list` | 列出插件 | 显示已加载/未加载的插件、版本、描述、自动更新状态 |
| `/pm check [name]` | 检查更新 | 对比本地与 GitHub 远程版本号 |
| `/pm update <name>` | 更新插件 | git pull + 备份旧版 + 热重载，失败自动恢复 |
| `/pm update --all` | 批量更新 | 遍历有更新的插件逐一更新 |
| `/pm backup <name>` | 备份 | 带时间戳的完整目录备份 |
| `/pm backup list <name>` | 备份历史 | 显示该插件所有备份及时间 |
| `/pm restore <name> [id]` | 恢复 | 按编号恢复插件到此前状态 |
| `/pm switch <name>` | 切换分支 | 查看并切换插件的 Git 分支 |
| `/pm version <name>` | 切换版本 | 查看并切换插件的 Git 标签/版本 |
| `/pm reload [name]` | 热加载 | 不指定则先加载新插件，再重载所有已加载插件 |
| `/pm install <url>` | 安装 | 从 GitHub 克隆并热加载，支持 owner/repo 格式 |
| `/pm settings [k=v]` | 配置 | 查看或修改插件配置 |
| `/pm autoupdate [on/off/interval N]` | 自动更新 | 管理自动更新开关和间隔 |

## 项目结构

```
plugin_manager/
├── __init__.py                  # 包标识
├── plugin.py                    # 插件入口，注册 BasePlugin
├── config.py                    # PluginManagerConfig 定义（6个配置项）
├── manifest.json                # 插件清单（含 categories/tags）
├── commands/
│   ├── __init__.py
│   └── pm_commands.py           # PMCommand 实现（~1400行，13个子命令）
└── README.md
```

## 依赖

- **运行环境**：Neo-MoFox >= 1.0.0
- **系统命令**：`git`（`/pm install`、`/pm update` 需要）
- **Python 包**：无额外依赖（仅使用 Neo-MoFox 内置 API）

## 构建

```bash
# 构建为 ZIP 包
mpdt plugin build --format zip

# 构建为 .mfp 格式（推荐用于发布）
mpdt plugin build --format mfp
```

## 许可证

GPL-3.0 © MoFox Team
