"""plugin_manager — 插件管理插件。

提供查询已加载插件、检查更新、更新插件、备份/恢复、切换版本/分支、
热加载、自动更新等功能，通过 /pm 命令触发。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from .commands.pm_commands import PMCommand
from .config import PluginManagerConfig

if TYPE_CHECKING:
    pass

logger = get_logger("plugin_manager")


@register_plugin
class PluginManagerPlugin(BasePlugin):
    """插件管理器插件。"""
    plugin_name: str = "plugin_manager"
    plugin_description: str = "插件管理器，提供查询、更新、备份、切换版本/分支、热加载等功能"
    plugin_version: str = "1.0.0"
    configs: list[type] = [PluginManagerConfig]
    dependent_components: list[str] = []

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._auto_update_task: asyncio.Task | None = None

    async def on_enable(self) -> None:
        """插件启用时启动自动更新任务。"""
        logger.info("plugin_manager 已启用")
        # Auto-update task will be started by PMCommand

    async def on_disable(self) -> None:
        """插件禁用时取消自动更新任务。"""
        if self._auto_update_task and not self._auto_update_task.done():
            self._auto_update_task.cancel()
            logger.info("自动更新任务已取消")

    def get_components(self) -> list[type]:
        """获取插件内所有组件类。"""
        return [PMCommand]
