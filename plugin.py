"""plugin_manager — 插件管理插件。

提供查询已加载插件、检查更新、更新插件、备份/恢复、切换版本/分支、
热加载、自动更新等功能，通过 /pm 命令触发。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from .commands.pm_commands import PMCommand
from .config import PluginManagerConfig

logger = get_logger("plugin_manager")


@register_plugin
class PluginManagerPlugin(BasePlugin):
    """插件管理器插件。"""

    plugin_name: str = "plugin_manager"
    plugin_description: str = (
        "插件管理器，提供查询、更新、备份、切换版本/分支、热加载等功能"
    )
    plugin_version: str = "1.0.0"
    configs: list[type] = [PluginManagerConfig]
    dependent_components: list[str] = []

    def __init__(self, config=None) -> None:
        super().__init__(config)
        # 修复: 框架 load_plugin_from_manifest 实例化后未将 manifest.version 赋给 plugin_version
        # 从同目录 manifest.json 读取版本覆盖类默认值
        try:
            mpath = Path(__file__).parent / "manifest.json"
            if mpath.exists():
                with open(mpath, encoding="utf-8") as mf:
                    self.plugin_version = json.load(mf).get("version", self.plugin_version)
        except Exception:
            pass

    async def on_plugin_loaded(self) -> None:
        """插件加载后启动自动更新任务。"""
        logger.info("plugin_manager 已加载")
        # 按配置项决定是否增大 EventBus 超时
        try:
            import src.kernel.event.core as _event_core
            if self.config and self.config.auto_update.extend_eventbus_timeout:
                _event_core.EVENT_HANDLER_TIMEOUT_SECONDS = 30.0
                logger.info("EventBus 超时已增大至 30s（配置项 auto_update.extend_eventbus_timeout 开启）")
        except Exception:
            pass
        # 创建 PMCommand 临时实例以启动自动更新（框架在 cmd 执行时才实例化，故此处主动创建）
        cmd = PMCommand(plugin=self, stream_id="")
        await cmd.start_auto_update()

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时取消自动更新任务。"""
        cmd = PMCommand(plugin=self, stream_id="")
        await cmd.stop_auto_update()
        logger.info("自动更新任务已取消")

    def get_components(self) -> list[type]:
        """获取插件内所有组件类。"""
        return [PMCommand]
