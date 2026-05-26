"""plugin_manager — /pm 命令处理。

命令列表：
  /pm list                        — 列出所有已加载插件
  /pm check [插件名]               — 检查 GitHub 远程版本
  /pm update <插件名>              — 更新指定插件（更新前自动备份）
  /pm update --all                 — 批量更新所有有更新的插件
  /pm backup <插件名>              — 手动备份插件
  /pm backup list <插件名>         — 查看插件的备份列表
  /pm restore <插件名> [编号]       — 恢复指定备份
  /pm switch <插件名>              — 查看并切换插件 Git 分支
  /pm version <插件名>             — 查看并切换插件版本/标签
  /pm reload [插件名]              — 热加载全部或指定插件
  /pm autoupdate                   — 查看自动更新设置
  /pm autoupdate on/off            — 开启/关闭自动更新
  /pm autoupdate interval <分钟>    — 修改检查间隔
  /pm settings                     — 查看当前配置
  /pm settings <配置项> <值>        — 修改配置（例：auto_update.enabled 开启）
  /pm install <仓库地址>            — 从 GitHub 安装插件（git clone + 热加载）
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from src.app.plugin_system.api import plugin_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.permission_api import (
    generate_person_id,
    get_user_permission_level,
)
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

logger = get_logger("plugin_manager.pm_commands")

# ── 自动更新状态追踪 ──────────────────────────────────────────
_auto_update_task: asyncio.Task | None = None
_auto_update_plugins: dict[str, bool] = {}  # plugin_name -> enabled
_plugin_settings_path: Path | None = None


class PMCommand(BaseCommand):
    """插件管理命令。"""
    command_name: str = "pm"
    command_description: str = "插件管理：list/check/update/backup/restore/switch/version/reload/autoupdate/settings/install"
    permission_level: PermissionLevel = PermissionLevel.USER

    # ── 内部辅助方法 ──────────────────────────────────────────

    async def _reply(self, text: str) -> None:
        """向当前流发送回复。"""
        await send_text(text, stream_id=self.stream_id)

    async def _ensure_admin(self) -> bool:
        """检查当前用户是否为 ADMIN 及以上。返回 False 时已自动发送拒绝消息。"""
        if self._message is None:
            await self._reply("❌ 无法获取消息来源")
            return False
        platform = self._message.platform or ""
        sender_id = str(getattr(self._message, "sender_id", ""))
        if not platform or not sender_id:
            await self._reply("❌ 无法识别用户身份")
            return False
        pid = generate_person_id(platform, sender_id)
        level = await get_user_permission_level(pid)
        if level.value < PermissionLevel.OPERATOR.value:
            await self._reply("❌ 权限不足：此操作需要 ADMIN 或以上权限")
            return False
        return True

    def _get_plugin_dir(self) -> Path:
        """获取本插件所在目录。"""
        path = plugin_api.get_plugin_path("plugin_manager")
        if path:
            return Path(path)
        return Path.cwd() / "plugins" / "plugin_manager"

    def _get_backup_dir(self, plugin_name: str) -> Path:
        """获取指定插件的备份目录。"""
        base = self._get_plugin_dir() / "backups"
        return base / plugin_name

    def _format_timestamp(self, ts: float) -> str:
        """格式化时间戳为可读字符串。"""
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def _get_manifest_field(self, plugin_name: str, field: str) -> str:
        """从插件 manifest.json 中读取字段值。"""
        path = plugin_api.get_plugin_path(plugin_name)
        if not path:
            return ""
        manifest_path = Path(path) / "manifest.json"
        if not manifest_path.exists():
            return ""
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get(field, "")
        except (json.JSONDecodeError, OSError):
            return ""

    async def _resolve_plugin_name(self, name_or_index: str) -> str:
        """将数字编号或插件名解析为插件名。纯数字则在已加载插件列表按序号查找。"""
        if name_or_index.isdigit():
            loaded = sorted(plugin_api.list_loaded_plugins())
            idx = int(name_or_index) - 1
            if 0 <= idx < len(loaded):
                return loaded[idx]
        return name_or_index
    def _get_github_repo(self, plugin_name: str) -> str:
        """获取插件的 GitHub 仓库地址。
        优先级：manifest.json 中的 repo 字段 > git remote
        """
        # 1. 从 manifest.json 读取
        repo = self._get_manifest_field(plugin_name, "repo")
        if repo:
            return repo
        # 2. 从 git remote 获取
        path = plugin_api.get_plugin_path(plugin_name)
        if not path:
            return ""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "-C", path, "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                url = result.stdout.strip()
                # 转换 SSH 格式为 HTTPS
                if url.startswith("git@"):
                    url = url.replace(":", "/").replace("git@", "https://")
                if url.endswith(".git"):
                    url = url[:-4]
                return url
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return ""

    def _parse_github_repo(self, url: str) -> tuple[str, str] | None:
        """从 GitHub URL 解析 owner 和 repo。
        返回 (owner, repo) 或 None。
        """
        url = url.rstrip("/")
        # https://github.com/owner/repo
        if "github.com" not in url:
            return None
        parts = url.split("github.com/")
        if len(parts) < 2:
            return None
        path_parts = parts[1].split("/")
        if len(path_parts) >= 2:
            owner = path_parts[0]
            repo = path_parts[1].replace(".git", "")
            return owner, repo
        return None

    def _github_api_request(
        self,
        owner: str,
        repo: str,
        endpoint: str,
        token: str = "",
        proxy: str = "",
    ) -> Any:
        """调用 GitHub API。返回解析后的 JSON 数据。"""
        url = f"https://api.github.com/repos/{owner}/{repo}/{endpoint}"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "MoFox-PluginManager/1.0",
        }
        if token:
            headers["Authorization"] = f"token {token}"
        req = Request(url, headers=headers)
        try:
            resp = urlopen(req, timeout=15)
            return json.loads(resp.read().decode())
        except HTTPError as e:
            logger.warning(f"GitHub API 请求失败 [{e.code}]: {url}")
            if e.code == 404:
                return None
            if e.code == 403:
                return {"error": "rate_limited", "message": "API 限频，请配置 GitHub Token"}
            return {"error": str(e.code)}
        except URLError as e:
            logger.warning(f"GitHub API 连接失败: {e.reason}")
            return {"error": "connection", "message": str(e.reason)}
        except json.JSONDecodeError:
            return None

    async def _async_github_request(
        self,
        owner: str,
        repo: str,
        endpoint: str,
        token: str = "",
        proxy: str = "",
    ) -> Any:
        """异步版 GitHub API 请求。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._github_api_request,
            owner, repo, endpoint, token, proxy,
        )

    async def _create_backup(self, plugin_name: str) -> str | None:
        """创建插件备份（自动备份）。返回备份文件路径，失败返回 None。"""
        path = plugin_api.get_plugin_path(plugin_name)
        if not path:
            return None
        backup_dir = self._get_backup_dir(plugin_name)
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = backup_dir / f"{timestamp}.zip"
        try:
            # 压缩整个插件目录
            shutil.make_archive(
                str(zip_path.with_suffix("")),
                "zip",
                root_dir=path,
            )
            logger.info(f"已备份 {plugin_name} → {zip_path}")
            # 清理旧备份
            await self._clean_old_backups(plugin_name)
            return str(zip_path)
        except Exception as e:
            logger.error(f"备份失败 {plugin_name}: {e}")
            return None

    async def _clean_old_backups(self, plugin_name: str) -> None:
        """清理超出最大保留数的旧备份。"""
        backup_dir = self._get_backup_dir(plugin_name)
        if not backup_dir.exists():
            return
        # 获取最大备份数配置
        max_backups = 5  # 默认值
        try:
            # 从插件配置读取
            plugin = plugin_api.get_plugin("plugin_manager")
            if plugin and hasattr(plugin, "config"):
                max_backups = plugin.config.backup.max_backups_per_plugin
        except Exception:
            pass
        backups = sorted(backup_dir.glob("*.zip"), key=os.path.getmtime)
        while len(backups) > max_backups:
            oldest = backups.pop(0)
            oldest.unlink()
            logger.info(f"已删除旧备份: {oldest}")

    # ── 自动更新相关 ──────────────────────────────────────────

    def _load_plugin_settings(self) -> dict[str, Any]:
        """加载插件设置（每个插件的 auto_update 开关等）。"""
        global _plugin_settings_path
        settings_path = self._get_plugin_dir() / "plugin_settings.json"
        _plugin_settings_path = settings_path
        if settings_path.exists():
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_plugin_settings(self, data: dict[str, Any]) -> None:
        """保存插件设置。"""
        global _plugin_settings_path
        if _plugin_settings_path is None:
            _plugin_settings_path = self._get_plugin_dir() / "plugin_settings.json"
        try:
            _plugin_settings_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_plugin_settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as e:
            logger.error(f"保存插件设置失败: {e}")

    async def _start_auto_update(self) -> None:
        """启动自动更新后台任务。"""
        global _auto_update_task
        if _auto_update_task and not _auto_update_task.done():
            _auto_update_task.cancel()
        _auto_update_task = asyncio.create_task(self._auto_update_loop())
        logger.info("自动更新后台任务已启动")

    async def _auto_update_loop(self) -> None:
        """自动更新循环。"""
        global _auto_update_plugins
        while True:
            try:
                # 读取配置
                interval = 480  # 默认 8 小时
                enabled = False
                plugin = plugin_api.get_plugin("plugin_manager")
                if plugin and hasattr(plugin, "config"):
                    enabled = plugin.config.auto_update.enabled
                    interval = plugin.config.auto_update.check_interval_minutes
                if not enabled:
                    # 如果总开关关闭，等待较长时间再检查
                    await asyncio.sleep(600)  # 10 分钟
                    continue
                # 加载插件设置
                settings = self._load_plugin_settings()
                _auto_update_plugins = settings.get("auto_update", {})
                # 遍历所有已加载插件
                loaded = sorted(plugin_api.list_loaded_plugins())
                for name in loaded:
                    if _auto_update_plugins.get(name, False):
                        await self._check_and_update(name)
                await asyncio.sleep(interval * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动更新循环异常: {e}")
                await asyncio.sleep(300)  # 5 分钟后重试

    async def _check_and_update(self, plugin_name: str) -> tuple[bool, str]:
        """检查并更新单个插件。返回 (是否成功, 消息)。"""
        repo_url = self._get_github_repo(plugin_name)
        if not repo_url:
            return False, f"{plugin_name}: 无 GitHub 仓库信息，跳过"
        parsed = self._parse_github_repo(repo_url)
        if not parsed:
            return False, f"{plugin_name}: 无法解析仓库地址"
        owner, repo = parsed
        # 获取配置
        token = ""
        plugin = plugin_api.get_plugin("plugin_manager")
        if plugin and hasattr(plugin, "config"):
            token = plugin.config.github.token
        # 查询最新版本
        latest = await self._async_github_request(owner, repo, "releases/latest", token)
        if latest is None or isinstance(latest, dict) and "error" in latest:
            # 尝试从 tags 获取
            tags = await self._async_github_request(owner, repo, "tags", token)
            if tags and isinstance(tags, list) and len(tags) > 0:
                latest_version = tags[0].get("name", "")
            else:
                return False, f"{plugin_name}: 无法获取远程版本"
        else:
            latest_version = latest.get("tag_name", "")
        if not latest_version:
            return False, f"{plugin_name}: 无法解析远程版本号"
        # 获取本地版本
        local_version = self._get_manifest_field(plugin_name, "version")
        if local_version == latest_version:
            return True, f"{plugin_name}: 已是最新版本 ({local_version})"
        # 有更新，执行更新
        return await self._do_update(plugin_name, owner, repo, latest_version, token)

    async def _do_update(
        self,
        plugin_name: str,
        owner: str,
        repo: str,
        version: str,
        token: str = "",
    ) -> tuple[bool, str]:
        """执行插件更新。"""
        # 1. 自动备份
        backup_path = await self._create_backup(plugin_name)
        if backup_path:
            logger.info(f"更新前已备份: {backup_path}")
        # 2. 下载 release zip
        download_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{version}"
        path = plugin_api.get_plugin_path(plugin_name)
        if not path:
            return False, f"{plugin_name}: 无法获取插件路径"
        dest = Path(path)
        try:
            headers = {"User-Agent": "MoFox-PluginManager/1.0"}
            if token:
                headers["Authorization"] = f"token {token}"
            req = Request(download_url, headers=headers)
            loop = asyncio.get_running_loop()
            resp_data = await loop.run_in_executor(
                None, lambda: urlopen(req, timeout=30).read()
            )
            # 解压到临时目录
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                zip_path = Path(tmpdir) / "download.zip"
                zip_path.write_bytes(resp_data)
                extract_dir = Path(tmpdir) / "extracted"
                extract_dir.mkdir()
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)
                # GitHub zip 包含顶层目录 (owner-repo-commit), 找到实际内容
                contents = list(extract_dir.iterdir())
                if len(contents) == 1 and contents[0].is_dir():
                    source = contents[0]
                else:
                    source = extract_dir
                # 覆盖到插件目录（排除 .git、__pycache__ 等）
                for item in source.iterdir():
                    if item.name in (".git", "__pycache__", ".venv"):
                        continue
                    target = dest / item.name
                    if target.exists():
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    if item.is_dir():
                        shutil.copytree(item, target)
                    else:
                        shutil.copy2(item, target)
        except Exception as e:
            logger.error(f"更新下载/解压失败 {plugin_name}: {e}")
            return False, f"{plugin_name}: 下载/解压失败 - {e}"
        # 3. 重载插件
        try:
            success = await plugin_api.reload_plugin(plugin_name)
            if success:
                return True, f"✅ {plugin_name} 已更新到 {version}（已热加载）"
            else:
                return False, f"⚠️ {plugin_name} 已下载到 {version}，但热加载失败，请手动重启 MoFox"
        except Exception as e:
            return False, f"⚠️ {plugin_name} 已下载到 {version}，但热加载异常: {e}"

    # ── 默认入口（无子命令） ──────────────────────────────────

    @cmd_route()
    async def handle_default(self) -> tuple[bool, str]:
        """显示帮助信息。"""
        help_text = (
            "📦 插件管理器\n"
            "━━━━━━━━━━━━━━━━\n"
            "• /pm list — 列出已加载插件\n"
            "• /pm check [插件] — 检查 GitHub 版本\n"
            "• /pm update <插件> — 更新插件\n"
            "• /pm update --all — 批量更新\n"
            "• /pm backup <插件> — 手动备份\n"
            "• /pm backup list <插件> — 查看备份列表\n"
            "• /pm restore <插件> [编号] — 恢复备份\n"
            "• /pm switch <插件> — 切换分支\n"
            "• /pm version <插件> — 切换版本\n"
            "• /pm reload [插件] — 热加载插件\n"
            "• /pm autoupdate — 自动更新管理\n"
            "• /pm settings — 查看/修改配置\n"
            "• /pm install <仓库地址> — 安装插件"
        )
        await self._reply(help_text)
        return True, "shown help"

    # ── list ────────────────────────────────────────────────

    @cmd_route("list")
    async def handle_list(self) -> tuple[bool, str]:
        """列出所有已加载插件。"""
        loaded = sorted(plugin_api.list_loaded_plugins())
        if not loaded:
            await self._reply("📭 当前没有已加载的插件")
            return True, "no plugins"
        # 获取插件实例信息
        all_plugins = plugin_api.get_all_plugins()
        # 加载自动更新设置，标记已开启的插件
        settings = self._load_plugin_settings()
        auto_update_map = settings.get("auto_update", {})
        lines = ["📋 已加载插件:", "───" * 10]
        for i, name in enumerate(sorted(loaded), 1):
            instance = all_plugins.get(name)
            version = getattr(instance, "plugin_version", "?") if instance else "?"
            desc = getattr(instance, "plugin_description", "") if instance else ""
            short_desc = f" — {desc[:40]}" if desc else ""
            has_repo = bool(self._get_github_repo(name))
            no_repo_mark = "" if has_repo else " ⛔不可自动更新"
            auto_mark = " 🔄" if auto_update_map.get(name, False) else ""
            lines.append(f"[{i}] ✅ {name} v{version}{short_desc}{no_repo_mark}{auto_mark}")
        # 检查是否有未加载的插件
        try:
            unloaded = await plugin_api.list_unloaded_plugins()
            if unloaded:
                lines.append("")
                lines.append("❌ 未加载/失败的插件:")
                for name, info in unloaded.items():
                    status = info.get("status", "unknown")
                    reason = info.get("reason", "")
                    suffix = f" ({reason})" if reason else ""
                    lines.append(f"  ✗ {name} [{status}]{suffix}")
        except Exception:
            pass
        await self._reply("\n".join(lines))
        return True, f"listed {len(loaded)} plugins"

    # ── check ───────────────────────────────────────────────

    @cmd_route("check")
    async def handle_check(self, plugin_name: str = "") -> tuple[bool, str]:
        """检查插件 GitHub 版本。留空则检查所有。"""
        plugin_name = await self._resolve_plugin_name(plugin_name)
        plugins_to_check = [plugin_name] if plugin_name else plugin_api.list_loaded_plugins()
        if not plugins_to_check:
            await self._reply("📭 没有已加载的插件可检查")
            return True, "no plugins"
        await self._reply(f"🔍 正在检查 {len(plugins_to_check)} 个插件的版本，请稍候…")
        results = []
        for name in plugins_to_check:
            repo_url = self._get_github_repo(name)
            if not repo_url:
                results.append(f"⏭️ {name}: 无 GitHub 仓库信息")
                continue
            parsed = self._parse_github_repo(repo_url)
            if not parsed:
                results.append(f"⏭️ {name}: 无法解析仓库地址")
                continue
            owner, repo = parsed
            token = ""
            plugin = plugin_api.get_plugin("plugin_manager")
            if plugin and hasattr(plugin, "config"):
                token = plugin.config.github.token
            latest_data = await self._async_github_request(owner, repo, "releases/latest", token)
            if latest_data is None:
                # 尝试 tags
                tags = await self._async_github_request(owner, repo, "tags", token)
                if tags and isinstance(tags, list) and len(tags) > 0:
                    latest_ver = tags[0].get("name", "?")
                else:
                    results.append(f"❓ {name}: 无法获取远程版本 (API 可能不可用)")
                    continue
            elif isinstance(latest_data, dict) and "error" in latest_data:
                results.append(f"❓ {name}: API 错误 - {latest_data.get('message', 'unknown')}")
                continue
            else:
                latest_ver = latest_data.get("tag_name", "?")
            local_ver = self._get_manifest_field(name, "version")
            if local_ver == latest_ver:
                results.append(f"✅ {name}: 已是最新 ({local_ver})")
            elif local_ver and latest_ver:
                results.append(f"🔄 {name}: {local_ver} → {latest_ver}")
            else:
                results.append(f"📋 {name}: 本地={local_ver or '?'} 远程={latest_ver}")
        await self._reply("\n".join(results))
        return True, "check completed"

    # ── update ──────────────────────────────────────────────

    @cmd_route("update")
    async def handle_update(self, *args: str) -> tuple[bool, str]:
        """更新插件。"""
        if not await self._ensure_admin():
            return False, "permission denied"
        if not args:
            await self._reply("❌ 用法: /pm update <插件名> 或 /pm update --all")
            return False, "missing args"
        if args[0] == "--all":
            return await self._handle_update_all()
        plugin_name = await self._resolve_plugin_name(args[0])
        # 检查插件是否存在
        path = plugin_api.get_plugin_path(plugin_name)
        if not path:
            await self._reply(f"❌ 插件 {plugin_name} 不存在或未加载")
            return False, "plugin not found"
        repo_url = self._get_github_repo(plugin_name)
        if not repo_url:
            await self._reply(f"❌ {plugin_name} 无 GitHub 仓库信息，无法更新")
            return False, "no repo info"
        parsed = self._parse_github_repo(repo_url)
        if not parsed:
            await self._reply(f"❌ 无法解析 {plugin_name} 的仓库地址")
            return False, "parse failed"
        owner, repo = parsed
        token = ""
        plugin = plugin_api.get_plugin("plugin_manager")
        if plugin and hasattr(plugin, "config"):
            token = plugin.config.github.token
        await self._reply(f"🔄 正在更新 {plugin_name}…")
        # 获取最新版本
        latest = await self._async_github_request(owner, repo, "releases/latest", token)
        if latest is None or isinstance(latest, dict) and "error" in latest:
            tags = await self._async_github_request(owner, repo, "tags", token)
            if tags and isinstance(tags, list) and len(tags) > 0:
                version = tags[0].get("name", "")
            else:
                await self._reply(f"❌ 无法获取 {plugin_name} 的远程版本")
                return False, "no version"
        else:
            version = latest.get("tag_name", "")
        if not version:
            await self._reply(f"❌ 无法解析 {plugin_name} 的远程版本号")
            return False, "no version"
        success, msg = await self._do_update(plugin_name, owner, repo, version, token)
        await self._reply(msg)
        return success, msg

    async def _handle_update_all(self) -> tuple[bool, str]:
        """批量更新所有有更新的插件。"""
        loaded = sorted(plugin_api.list_loaded_plugins())
        if not loaded:
            await self._reply("📭 没有已加载的插件")
            return True, "no plugins"
        await self._reply(f"🔄 正在批量检查 {len(loaded)} 个插件的更新…")
        results = []
        for name in loaded:
            repo_url = self._get_github_repo(name)
            if not repo_url:
                results.append(f"⏭️ {name}: 无仓库信息")
                continue
            parsed = self._parse_github_repo(repo_url)
            if not parsed:
                continue
            owner, repo = parsed
            token = ""
            plugin = plugin_api.get_plugin("plugin_manager")
            if plugin and hasattr(plugin, "config"):
                token = plugin.config.github.token
            latest = await self._async_github_request(owner, repo, "releases/latest", token)
            if latest is None or isinstance(latest, dict) and "error" in latest:
                tags = await self._async_github_request(owner, repo, "tags", token)
                if tags and isinstance(tags, list) and len(tags) > 0:
                    version = tags[0].get("name", "")
                else:
                    results.append(f"⏭️ {name}: 无法获取远程版本")
                    continue
            else:
                version = latest.get("tag_name", "")
            if not version:
                results.append(f"⏭️ {name}: 无法解析版本")
                continue
            local_ver = self._get_manifest_field(name, "version")
            if local_ver == version:
                results.append(f"✅ {name}: 已是最新")
                continue
            success, msg = await self._do_update(name, owner, repo, version, token)
            results.append(msg)
        await self._reply("\n".join(results))
        return True, "batch update completed"

    # ── backup ──────────────────────────────────────────────

    @cmd_route("backup")
    async def handle_backup(self, plugin_name: str = "") -> tuple[bool, str]:
        """手动备份指定插件。"""
        plugin_name = await self._resolve_plugin_name(plugin_name)
        if not await self._ensure_admin():
            return False, "permission denied"
        plugin_name = await self._resolve_plugin_name(plugin_name)
        if not plugin_name:
            await self._reply("❌ 用法: /pm backup <插件名>")
            return False, "missing args"
        path = plugin_api.get_plugin_path(plugin_name)
        if not path:
            await self._reply(f"❌ 插件 {plugin_name} 不存在或未加载")
            return False, "not found"
        await self._reply(f"💾 正在备份 {plugin_name}…")
        result = await self._create_backup(plugin_name)
        if result:
            await self._reply(f"✅ {plugin_name} 已备份: {result}")
            return True, f"backup saved: {result}"
        else:
            await self._reply(f"❌ {plugin_name} 备份失败")
            return False, "backup failed"

    @cmd_route("backup", "list")
    async def handle_backup_list(self, plugin_name: str = "") -> tuple[bool, str]:
        """查看插件的备份列表。"""
        plugin_name = await self._resolve_plugin_name(plugin_name)
        if not plugin_name:
            await self._reply("❌ 用法: /pm backup list <插件名>")
            return False, "missing args"
        backup_dir = self._get_backup_dir(plugin_name)
        if not backup_dir.exists():
            await self._reply(f"📭 {plugin_name} 没有备份记录")
            return True, "no backups"
        backups = sorted(backup_dir.glob("*.zip"), key=os.path.getmtime, reverse=True)
        if not backups:
            await self._reply(f"📭 {plugin_name} 没有备份记录")
            return True, "no backups"
        lines = [f"📦 {plugin_name} 的备份列表:", "───" * 10]
        for i, bp in enumerate(backups, 1):
            size = bp.stat().st_size
            size_str = f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"
            mtime = os.path.getmtime(bp)
            lines.append(f"  [{i}] {self._format_timestamp(mtime)} ({size_str})")
        await self._reply("\n".join(lines))
        return True, f"listed {len(backups)} backups"

    # ── restore ─────────────────────────────────────────────

    @cmd_route("restore")
    async def handle_restore(self, plugin_name: str = "", backup_index: str = "") -> tuple[bool, str]:
        """恢复插件备份。"""
        plugin_name = await self._resolve_plugin_name(plugin_name)
        if not await self._ensure_admin():
            return False, "permission denied"
        if not plugin_name:
            await self._reply("❌ 用法: /pm restore <插件名> [备份编号]")
            return False, "missing args"
        backup_dir = self._get_backup_dir(plugin_name)
        if not backup_dir.exists():
            await self._reply(f"📭 {plugin_name} 没有备份记录")
            return False, "no backups"
        backups = sorted(backup_dir.glob("*.zip"), key=os.path.getmtime, reverse=True)
        if not backups:
            await self._reply(f"📭 {plugin_name} 没有备份记录")
            return False, "no backups"
        # 选择备份
        index = 1
        if backup_index:
            try:
                index = int(backup_index)
            except ValueError:
                await self._reply("❌ 备份编号必须是数字")
                return False, "invalid index"
        if index < 1 or index > len(backups):
            await self._reply(f"❌ 备份编号无效，范围 1-{len(backups)}")
            return False, "index out of range"
        selected = backups[index - 1]
        path = plugin_api.get_plugin_path(plugin_name)
        if not path:
            await self._reply(f"❌ 插件 {plugin_name} 不存在")
            return False, "not found"
        await self._reply(f"♻️ 正在恢复 {plugin_name} 备份 #{index}…")
        try:
            # 解压备份到插件目录
            with zipfile.ZipFile(selected, "r") as zf:
                # 清空插件目录（保留 .git 等）
                dest = Path(path)
                for item in dest.iterdir():
                    if item.name in (".git", "__pycache__", ".venv", "backups"):
                        continue
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                # 解压
                zf.extractall(dest)
            # 重载
            success = await plugin_api.reload_plugin(plugin_name)
            if success:
                await self._reply(f"✅ {plugin_name} 已从备份 #{index} 恢复（已热加载）")
            else:
                await self._reply(f"⚠️ {plugin_name} 已恢复，但热加载失败，请手动重启 MoFox")
            return True, f"restored from backup #{index}"
        except Exception as e:
            logger.error(f"恢复备份失败 {plugin_name}: {e}")
            await self._reply(f"❌ 恢复失败: {e}")
            return False, f"restore failed: {e}"

    # ── switch ──────────────────────────────────────────────

    @cmd_route("switch")
    async def handle_switch(self, plugin_name: str = "") -> tuple[bool, str]:
        """查看并切换插件 Git 分支。"""
        plugin_name = await self._resolve_plugin_name(plugin_name)
        if not await self._ensure_admin():
            return False, "permission denied"
        if not plugin_name:
            await self._reply("❌ 用法: /pm switch <插件名>  # 查看分支列表并切换")
            return False, "missing args"
        path = plugin_api.get_plugin_path(plugin_name)
        if not path:
            await self._reply(f"❌ 插件 {plugin_name} 不存在或未加载")
            return False, "not found"
        repo_url = self._get_github_repo(plugin_name)
        if not repo_url:
            await self._reply(f"❌ {plugin_name} 无 GitHub 仓库信息")
            return False, "no repo"
        parsed = self._parse_github_repo(repo_url)
        if not parsed:
            await self._reply(f"❌ 无法解析仓库地址")
            return False, "parse failed"
        owner, repo = parsed
        token = ""
        plugin = plugin_api.get_plugin("plugin_manager")
        if plugin and hasattr(plugin, "config"):
            token = plugin.config.github.token
        branches = await self._async_github_request(owner, repo, "branches", token)
        if not branches or not isinstance(branches, list):
            await self._reply(f"❌ 无法获取 {plugin_name} 的分支列表")
            return False, "no branches"
        lines = [f"🌿 {plugin_name} 的远程分支:", "───" * 10]
        for i, b in enumerate(branches, 1):
            name = b.get("name", "?")
            lines.append(f"  [{i}] {name}")
        lines.append("")
        lines.append("请回复分支编号来切换，如: /pm switch_branch <插件名> <编号>")
        await self._reply("\n".join(lines))
        return True, f"listed {len(branches)} branches"

    @cmd_route("switch_branch")
    async def handle_switch_branch(self, plugin_name: str = "", branch_index: str = "") -> tuple[bool, str]:
        """切换到指定分支。内部命令，由 /pm switch 后调用。"""
        plugin_name = await self._resolve_plugin_name(plugin_name)
        if not await self._ensure_admin():
            return False, "permission denied"
        if not plugin_name or not branch_index:
            await self._reply("❌ 用法: /pm switch_branch <插件名> <编号>")
            return False, "missing args"
        path = plugin_api.get_plugin_path(plugin_name)
        if not path:
            await self._reply(f"❌ 插件 {plugin_name} 不存在")
            return False, "not found"
        repo_url = self._get_github_repo(plugin_name)
        if not repo_url:
            await self._reply(f"❌ 无仓库信息")
            return False, "no repo"
        parsed = self._parse_github_repo(repo_url)
        if not parsed:
            await self._reply(f"❌ 无法解析仓库地址")
            return False, "parse failed"
        owner, repo = parsed
        token = ""
        plugin = plugin_api.get_plugin("plugin_manager")
        if plugin and hasattr(plugin, "config"):
            token = plugin.config.github.token
        branches = await self._async_github_request(owner, repo, "branches", token)
        if not branches or not isinstance(branches, list):
            await self._reply(f"❌ 无法获取分支列表")
            return False, "no branches"
        try:
            idx = int(branch_index) - 1
            if idx < 0 or idx >= len(branches):
                await self._reply(f"❌ 编号无效，范围 1-{len(branches)}")
                return False, "invalid index"
            branch_name = branches[idx].get("name", "")
        except (ValueError, IndexError):
            await self._reply(f"❌ 编号无效")
            return False, "invalid index"
        if not branch_name:
            await self._reply("❌ 无法获取分支名")
            return False, "no branch name"
        # 自动备份
        await self._create_backup(plugin_name)
        # 尝试用 git 切换
        dest = Path(path)
        git_dir = dest / ".git"
        try:
            import subprocess
            subprocess.run(["git", "-C", path, "fetch", "--all"], capture_output=True, timeout=15)
            result = subprocess.run(
                ["git", "-C", path, "checkout", branch_name],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                # git 方式失败，尝试下载
                await self._reply(f"⚠️ git checkout 失败，尝试下载分支内容…")
                success, msg = await self._do_update(plugin_name, owner, repo, branch_name, token)
                await self._reply(msg)
                return success, msg
            # 重载
            success = await plugin_api.reload_plugin(plugin_name)
            status = "（已热加载）" if success else "（热加载失败，请手动重启）"
            await self._reply(f"✅ {plugin_name} 已切换到分支 {branch_name}{status}")
            return True, f"switched to {branch_name}"
        except Exception as e:
            await self._reply(f"❌ 切换失败: {e}")
            return False, f"switch failed: {e}"

    # ── version ─────────────────────────────────────────────

    @cmd_route("version")
    async def handle_version(self, plugin_name: str = "") -> tuple[bool, str]:
        """查看并切换插件版本/标签。"""
        plugin_name = await self._resolve_plugin_name(plugin_name)
        if not await self._ensure_admin():
            return False, "permission denied"
        if not plugin_name:
            await self._reply("❌ 用法: /pm version <插件名>  # 查看版本列表并切换")
            return False, "missing args"
        path = plugin_api.get_plugin_path(plugin_name)
        if not path:
            await self._reply(f"❌ 插件 {plugin_name} 不存在或未加载")
            return False, "not found"
        repo_url = self._get_github_repo(plugin_name)
        if not repo_url:
            await self._reply(f"❌ {plugin_name} 无 GitHub 仓库信息")
            return False, "no repo"
        parsed = self._parse_github_repo(repo_url)
        if not parsed:
            await self._reply(f"❌ 无法解析仓库地址")
            return False, "parse failed"
        owner, repo = parsed
        token = ""
        plugin = plugin_api.get_plugin("plugin_manager")
        if plugin and hasattr(plugin, "config"):
            token = plugin.config.github.token
        # 获取 tags
        tags = await self._async_github_request(owner, repo, "tags", token)
        if not tags or not isinstance(tags, list):
            # 尝试 releases
            releases = await self._async_github_request(owner, repo, "releases", token)
            if releases and isinstance(releases, list):
                tags = [{"name": r.get("tag_name", "?")} for r in releases]
            else:
                await self._reply(f"❌ 无法获取 {plugin_name} 的版本列表")
                return False, "no tags"
        if not tags:
            await self._reply(f"📭 {plugin_name} 没有可用的版本标签")
            return True, "no tags"
        # 限制显示数量
        tags = tags[:30]
        lines = [f"🏷️ {plugin_name} 的版本/标签:", "───" * 10]
        local_ver = self._get_manifest_field(plugin_name, "version")
        for i, t in enumerate(tags, 1):
            name = t.get("name", "?")
            marker = " ← 当前" if name == local_ver else ""
            lines.append(f"  [{i}] {name}{marker}")
        lines.append("")
        lines.append("请回复编号来切换，如: /pm switch_version <插件名> <编号>")
        await self._reply("\n".join(lines))
        return True, f"listed {len(tags)} tags"

    @cmd_route("switch_version")
    async def handle_switch_version(self, plugin_name: str = "", version_index: str = "") -> tuple[bool, str]:
        """切换到指定版本。内部命令，由 /pm version 后调用。"""
        plugin_name = await self._resolve_plugin_name(plugin_name)
        if not await self._ensure_admin():
            return False, "permission denied"
        if not plugin_name or not version_index:
            await self._reply("❌ 用法: /pm switch_version <插件名> <编号>")
            return False, "missing args"
        path = plugin_api.get_plugin_path(plugin_name)
        if not path:
            await self._reply(f"❌ 插件 {plugin_name} 不存在")
            return False, "not found"
        repo_url = self._get_github_repo(plugin_name)
        if not repo_url:
            await self._reply(f"❌ 无仓库信息")
            return False, "no repo"
        parsed = self._parse_github_repo(repo_url)
        if not parsed:
            await self._reply(f"❌ 无法解析仓库地址")
            return False, "parse failed"
        owner, repo = parsed
        token = ""
        plugin = plugin_api.get_plugin("plugin_manager")
        if plugin and hasattr(plugin, "config"):
            token = plugin.config.github.token
        tags = await self._async_github_request(owner, repo, "tags", token)
        if not tags or not isinstance(tags, list):
            releases = await self._async_github_request(owner, repo, "releases", token)
            if releases and isinstance(releases, list):
                tags = [{"name": r.get("tag_name", "?")} for r in releases]
            else:
                await self._reply("❌ 无法获取版本列表")
                return False, "no tags"
        try:
            idx = int(version_index) - 1
            if idx < 0 or idx >= len(tags):
                await self._reply(f"❌ 编号无效，范围 1-{len(tags)}")
                return False, "invalid index"
            tag_name = tags[idx].get("name", "")
        except (ValueError, IndexError):
            await self._reply("❌ 编号无效")
            return False, "invalid index"
        if not tag_name:
            await self._reply("❌ 无法获取版本名")
            return False, "no tag name"
        # 自动备份
        await self._create_backup(plugin_name)
        # 尝试用 git 切换
        try:
            import subprocess
            result = subprocess.run(
                ["git", "-C", path, "checkout", "tags/" + tag_name],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                success = await plugin_api.reload_plugin(plugin_name)
                status = "（已热加载）" if success else "（热加载失败，请手动重启）"
                await self._reply(f"✅ {plugin_name} 已切换到 {tag_name}{status}")
                return True, f"switched to {tag_name}"
        except Exception:
            pass
        # git 方式失败，下载版本
        await self._reply(f"⚠️ git 切换失败，尝试下载版本内容…")
        success, msg = await self._do_update(plugin_name, owner, repo, tag_name, token)
        await self._reply(msg)
        return success, msg

    # ── reload ──────────────────────────────────────────────

    @cmd_route("reload")
    async def handle_reload(self, plugin_name: str = "") -> tuple[bool, str]:
        """热加载插件。不指定则热加载全部。"""
        plugin_name = await self._resolve_plugin_name(plugin_name)
        if not await self._ensure_admin():
            return False, "permission denied"
        if plugin_name:
            path = plugin_api.get_plugin_path(plugin_name)
            if not path:
                await self._reply(f"❌ 插件 {plugin_name} 不存在或未加载")
                return False, "not found"
            try:
                success = await plugin_api.reload_plugin(plugin_name)
                if success:
                    await self._reply(f"✅ {plugin_name} 热加载成功")
                else:
                    await self._reply(f"❌ {plugin_name} 热加载失败")
                return success, f"reload {plugin_name}: {success}"
            except Exception as e:
                await self._reply(f"❌ {plugin_name} 热加载异常: {e}")
                return False, f"reload error: {e}"
        # 热加载全部（先加载未加载的，再重载已加载的）
        # 直接扫描 plugins 目录，找未加载的插件
        plugin_base = Path(plugin_api.get_plugin_path("plugin_manager")).parent
        loaded_set = set(plugin_api.list_loaded_plugins())
        new_plugins = 0
        newly_loaded = []
        if plugin_base.exists():
            for item in sorted(plugin_base.iterdir()):
                if not item.is_dir():
                    continue
                name = item.name
                if name.startswith("_") or name.startswith("."):
                    continue
                if name.endswith(("_old", "_bak")):
                    continue
                # 读 manifest.json 获取真实插件名
                manifest_path = item / "manifest.json"
                if not manifest_path.exists():
                    continue
                try:
                    import json
                    with open(manifest_path, "r", encoding="utf-8") as mf:
                        manifest_data = json.load(mf)
                    manifest_name = manifest_data.get("name", "")
                    if not manifest_name:
                        continue
                except Exception:
                    continue
                if manifest_name in loaded_set:
                    continue
                # 尝试加载
                try:
                    ok = await plugin_api.load_plugin(str(item))
                    if ok:
                        new_plugins += 1
                        newly_loaded.append(manifest_name)
                    else:
                        # 获取失败原因
                        reason = ""
                        try:
                            unloaded = await plugin_api.list_unloaded_plugins()
                            if manifest_name in unloaded:
                                reason = unloaded[manifest_name].get("reason", "")
                        except Exception:
                            pass
                        msg = f"⚠️ 加载 {manifest_name} 失败"
                        if reason:
                            msg += f"（{reason}）"
                        await self._reply(msg)
                except Exception as e:
                    await self._reply(f"⚠️ 加载 {manifest_name} 异常: {e}")
        # 合并已加载和新加载的
        loaded = sorted(loaded_set | set(newly_loaded))
        if not loaded:
            await self._reply("📭 没有已加载的插件")
            return True, "no plugins"
        await self._reply(f"🔄 正在热加载 {len(loaded)} 个插件{'（含 ' + str(new_plugins) + ' 个新加载）' if new_plugins else ''}…")
        success_list = []
        fail_list = []
        for name in loaded:
            try:
                ok = await plugin_api.reload_plugin(name)
                if ok:
                    success_list.append(name)
                else:
                    fail_list.append(name)
            except Exception as e:
                fail_list.append(f"{name}({e})")
        lines = []
        if success_list:
            lines.append(f"✅ 成功: {len(success_list)} 个")
            for n in success_list:
                lines.append(f"  • {n}")
        if fail_list:
            lines.append(f"❌ 失败: {len(fail_list)} 个")
            for n in fail_list:
                lines.append(f"  • {n}")
        await self._reply("\n".join(lines))
        return True, f"reloaded {len(success_list)}/{len(loaded)}"

    # ── autoupdate ──────────────────────────────────────────

    @cmd_route("autoupdate")
    async def handle_autoupdate(self, action_args: str = "") -> tuple[bool, str]:
        """管理自动更新。"""
        # 拆分 action_args 为 args 列表
        args = action_args.split() if action_args else []
        # 读取当前配置
        plugin = plugin_api.get_plugin("plugin_manager")
        if not plugin or not hasattr(plugin, "config"):
            await self._reply("❌ 无法读取插件配置")
            return False, "no config"
        cfg = plugin.config.auto_update
        settings = self._load_plugin_settings()
        auto_update_map = settings.get("auto_update", {})
        if not args:
            # 查看状态
            enabled_plugins = [k for k, v in auto_update_map.items() if v]
            lines = [
                "📡 自动更新设置:",
                "───" * 10,
                f"  总开关: {'🟢 开启' if cfg.enabled else '🔴 关闭'}",
                f"  检查间隔: {cfg.check_interval_minutes} 分钟 ({cfg.check_interval_minutes // 60} 小时)",
                f"  已开启的插件: {len(enabled_plugins)}",
            ]
            if enabled_plugins:
                for n in enabled_plugins:
                    lines.append(f"    • {n}")
            lines.append("")
            lines.append("📖 用法:")
            lines.append("  /pm autoupdate              — 查看当前状态")
            lines.append("  /pm autoupdate on           — 开启全局自动更新")
            lines.append("  /pm autoupdate off          — 关闭全局自动更新")
            lines.append("  /pm autoupdate on <插件名>   — 为指定插件开启自动更新")
            lines.append("  /pm autoupdate off <插件名>  — 为指定插件关闭自动更新")
            lines.append("  /pm autoupdate interval <分钟> — 修改检查间隔")
            await self._reply("\n".join(lines))
            return True, "shown autoupdate status"

        if not await self._ensure_admin():
            return False, "permission denied"

        action = args[0]
        if action == "on":
            # 开启全局或单个插件
            if len(args) >= 2:
                plugin_name = args[1]
                auto_update_map[plugin_name] = True
                await self._reply(f"✅ 已为 {plugin_name} 开启自动更新")
            else:
                cfg.enabled = True
                await self._reply("✅ 已开启全局自动更新")
                # 启动任务
                await self._start_auto_update()
        elif action == "off":
            if len(args) >= 2:
                plugin_name = args[1]
                auto_update_map[plugin_name] = False
                await self._reply(f"✅ 已为 {plugin_name} 关闭自动更新")
            else:
                cfg.enabled = False
                await self._reply("✅ 已关闭全局自动更新")
        elif action == "interval":
            if len(args) < 2:
                await self._reply(f"当前间隔: {cfg.check_interval_minutes} 分钟")
                return True, "shown interval"
            try:
                minutes = int(args[1])
                if minutes < 1:
                    await self._reply("❌ 间隔必须 ≥ 1 分钟")
                    return False, "invalid interval"
                cfg.check_interval_minutes = minutes
                await self._reply(f"✅ 检查间隔已设为 {minutes} 分钟 ({minutes // 60} 小时)")
            except ValueError:
                await self._reply("❌ 间隔必须是数字（分钟）")
                return False, "invalid number"
        else:
            await self._reply("❌ 未知操作，支持: on / off / interval <分钟>")
            return False, "unknown action"
        # 保存设置
        settings["auto_update"] = auto_update_map
        self._save_plugin_settings(settings)
        return True, "autoupdate updated"

    # ── settings ────────────────────────────────────────────

    @cmd_route("settings")
    async def handle_settings(self, key_value: str = "") -> tuple[bool, str]:
        """查看/修改配置。"""
        plugin = plugin_api.get_plugin("plugin_manager")
        if not plugin or not hasattr(plugin, "config"):
            await self._reply("❌ 无法读取插件配置")
            return False, "no config"
        cfg = plugin.config
        # 拆分 key_value 为 args 列表（兼容空格分隔）
        args = key_value.split() if key_value else []
        if not args:
            # 显示所有配置（带备注说明）
            lines = [
                "⚙️ 插件管理器配置:",
                "───" * 10,
                "# 备份配置",
                f"[backup]",
                f"  max_backups_per_plugin = {cfg.backup.max_backups_per_plugin}  # 每个插件最大备份保留数 (int)",
                f"  backup_dir = {cfg.backup.backup_dir}  # 备份目录名 (str)",
                f"",
                "# 自动更新配置",
                f"[auto_update]",
                f"  enabled = {cfg.auto_update.enabled}  # 自动更新总开关 (bool)",
                f"  check_interval_minutes = {cfg.auto_update.check_interval_minutes}  # 检查间隔，单位分钟 (int)",
                f"",
                "# 代理配置",
                f"[proxy]",
                f"  http = {cfg.proxy.http or '(未设置)'}  # HTTP 代理地址 (str)",
                f"  https = {cfg.proxy.https or '(未设置)'}  # HTTPS 代理地址 (str)",
                f"",
                "# GitHub 配置",
                f"[github]",
                f"  token = {'***' if cfg.github.token else '(未设置)'}  # GitHub Token，可选，提高 API 限频 (str)",
            ]
            await self._reply("\n".join(lines))
            # 单独发送修改用法说明，避免消息过长被截断
            usage = (
                "📖 修改配置:\n"
                "  /pm settings <配置项路径> <值>\n"
                "  或 /pm settings <配置项路径>=<值>\n"
                "  配置项路径格式: <分组>.<字段名>\n"
                "  示例: /pm settings auto_update.enabled 开启      # 开启自动更新\n"
                "  示例: /pm settings check_interval_minutes 120    # 检查间隔(分钟)\n"
                "  示例: /pm settings backup.max_backups_per_plugin 10  # 最大备份数\n"
                "  示例: /pm settings proxy.http=http://127.0.0.1:7890  # 设置代理(用=避免空格问题)\n"
                "  支持值类型: 布尔(开启/关闭/是/否/1/0)、数字(整数)、文本(直接输入)"
            )
            await self._reply(usage)
            return True, "shown settings"
        if not await self._ensure_admin():
            return False, "permission denied"
        # 修改配置
        if len(args) < 2:
            # 支持 = 分隔：proxy.http=http://127.0.0.1:7890
            if args and "=" in args[0]:
                key, value = args[0].split("=", 1)
            else:
                await self._reply("❌ 用法: /pm settings <配置项路径> <值>\n  例: /pm settings auto_update.enabled 开启\n  例: /pm settings proxy.http=http://127.0.0.1:7890")
                return False, "missing args"
        else:
            key = args[0]
            value = " ".join(args[1:])
        # 支持点号路径: backup.max_backups_per_plugin
        try:
            parts = key.split(".")
            if len(parts) == 2:
                section_name, field_name = parts
                section = getattr(cfg, section_name, None)
                if section and hasattr(section, field_name):
                    # 类型转换
                    current = getattr(section, field_name)
                    if isinstance(current, bool):
                        typed = value.lower() in ("true", "1", "yes", "on")
                    elif isinstance(current, int):
                        typed = int(value)
                    else:
                        typed = value
                    setattr(section, field_name, typed)
                    await self._reply(f"✅ {key} = {typed}")
                    # 重载插件使配置生效
                    try:
                        reload_ok = await plugin_api.reload_plugin("plugin_manager")
                        if reload_ok:
                            await self._reply("♻️ 插件管理器已重载，配置已生效")
                        else:
                            await self._reply("⚠️ 配置已保存，但重载失败，请手动重启 MoFox")
                    except Exception as e:
                        await self._reply(f"⚠️ 配置已保存，但重载异常: {e}")
                else:
                    await self._reply(f"❌ 未知配置项: {key}")
                    return False, "unknown key"
            else:
                await self._reply("❌ 格式错误，使用 section.field 格式，如 backup.max_backups_per_plugin")
                return False, "invalid key format"
        except ValueError as e:
            await self._reply(f"❌ 类型转换失败: {e}")
            return False, f"type error: {e}"
        return True, "setting updated"

    # ── install ─────────────────────────────────────────────

    @cmd_route("install")
    async def handle_install(self, github_url: str = "") -> tuple[bool, str]:
        """从 GitHub 安装插件（git clone + 热加载）。"""
        if not await self._ensure_admin():
            return False, "permission denied"
        if not github_url:
            await self._reply("❌ 用法: /pm install <仓库地址>\n  示例: /pm install https://github.com/用户/仓库名\n  示例: /pm install 用户/仓库名（自动补全）")
            return False, "missing url"
        # 自动补全 github.com 前缀
        url = github_url.strip().rstrip("/")
        # 如果已经是完整 URL（http:// 或 https://），直接使用
        if url.startswith("http://") or url.startswith("https://"):
            pass
        elif "/" in url and "." not in url.split("/")[0]:
            # owner/repo 格式
            url = f"https://github.com/{url}"
        else:
            url = f"https://github.com/{url}"
        # 提取仓库名作为插件目录名
        repo_name = url.rstrip("/").split("/")[-1].replace(".git", "")
        if not repo_name:
            await self._reply("❌ 无法从 URL 解析仓库名")
            return False, "parse failed"
        # 确定安装路径
        plugin_base = Path(plugin_api.get_plugin_path("plugin_manager")).parent
        dest_path = plugin_base / repo_name
        if dest_path.exists():
            await self._reply(f"❌ 插件目录已存在: {dest_path}\n如需重新安装，请先删除旧目录")
            return False, "already exists"
        await self._reply(f"📥 正在从 {url} 克隆插件…")
        # 用异步子进程实现进度提示，并注入代理
        try:
            # 构建 git 环境变量（注入代理）
            git_env = os.environ.copy()
            # 优先从插件配置读取代理，其次系统环境变量，最后默认值
            proxy_http = ""
            proxy_https = ""
            plugin_inst = plugin_api.get_plugin("plugin_manager")
            if plugin_inst and hasattr(plugin_inst, "config"):
                cfg = plugin_inst.config
                proxy_http = getattr(cfg.proxy, "http", "") or ""
                proxy_https = getattr(cfg.proxy, "https", "") or ""
            if not proxy_http:
                proxy_http = os.environ.get("HTTP_PROXY", "") or os.environ.get("http_proxy", "")
            if not proxy_https:
                proxy_https = os.environ.get("HTTPS_PROXY", "") or os.environ.get("https_proxy", "")
            if not proxy_http:
                proxy_http = "http://127.0.0.1:7890"  # 默认代理
            if not proxy_https:
                proxy_https = proxy_http  # 默认与 HTTP 相同
            git_env["HTTP_PROXY"] = proxy_http
            git_env["HTTPS_PROXY"] = proxy_https
            git_env["http_proxy"] = proxy_http
            git_env["https_proxy"] = proxy_https

            process = await asyncio.create_subprocess_exec(
                "git", "clone", "--progress", url, str(dest_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=git_env,
            )
        except FileNotFoundError:
            await self._reply("❌ 系统未安装 git，请先安装 git")
            return False, "git not found"

        # 进度心跳：每 15 秒发一次提醒
        async def _progress_ticker():
            waited = 0
            while True:
                await asyncio.sleep(15)
                waited += 15
                if process.returncode is not None:
                    break
                await self._reply(f"⏳ 正在克隆… 已等待 {waited} 秒（大仓库可能需要数分钟）")

        ticker_task = asyncio.create_task(_progress_ticker())
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=180
            )
        except asyncio.TimeoutError:
            ticker_task.cancel()
            process.kill()
            # 清理残留目录
            if dest_path.exists():
                import shutil
                shutil.rmtree(dest_path, ignore_errors=True)
            await self._reply("⏱️ 克隆超时（超过 3 分钟）\n"
                              "可能原因：仓库地址错误、网络不通或 DNS 解析失败\n"
                              "建议：确认地址后重试")
            return False, "clone timeout"
        finally:
            ticker_task.cancel()

        if process.returncode != 0:
            error_msg = (stderr_bytes.decode() if stderr_bytes else "").strip() or "未知错误"
            # 截取过长错误信息
            if len(error_msg) > 500:
                error_msg = error_msg[:500] + "…"
            await self._reply(f"❌ git clone 失败: {error_msg}")
            # 清理残留目录
            if dest_path.exists():
                import shutil
                shutil.rmtree(dest_path, ignore_errors=True)
            return False, f"clone failed: {error_msg}"
        # 验证克隆结果：检查目录下是否有插件文件
        has_files = False
        if dest_path.exists():
            for f in dest_path.iterdir():
                if f.name != ".git" and not f.name.startswith("."):
                    has_files = True
                    break
        if not has_files:
            # 清理空目录
            if dest_path.exists():
                import shutil
                shutil.rmtree(dest_path, ignore_errors=True)
            await self._reply("❌ 克隆失败：仓库为空或不存在，请检查仓库地址")
            return False, "empty clone"
        # 加载插件
        await self._reply(f"✅ 仓库已下载到 {dest_path}\n🔌 正在加载插件…")
        try:
            success = await plugin_api.load_plugin(str(dest_path))
            if success:
                await self._reply(f"✅ 插件 {repo_name} 安装成功！已加载运行")
                return True, f"installed {repo_name}"
            else:
                # 加载失败，获取详细原因
                reason = ""
                try:
                    unloaded = await plugin_api.list_unloaded_plugins()
                    if repo_name in unloaded:
                        reason = unloaded[repo_name].get("reason", "")
                except Exception:
                    pass
                err_msg = f"⚠️ {repo_name} 已下载，但加载失败"
                if reason:
                    err_msg += f"\n原因: {reason}"
                err_msg += "\n💡 可尝试:\n  1. 检查插件目录结构是否正确（需 manifest.json）\n  2. 执行 /pm reload 重试加载\n  3. 删除目录后重新安装"
                await self._reply(err_msg)
                return False, "load failed"
        except Exception as e:
            await self._reply(f"❌ 加载失败: {e}")
            return False, f"load error: {e}"
