"""plugin_manager 插件配置。"""
from __future__ import annotations

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class PluginManagerConfig(BaseConfig):
    """插件管理器配置。"""
    config_name = "config"
    config_description = "插件管理器配置"

    @config_section("backup")
    class BackupSection(SectionBase):
        """备份配置。"""
        max_backups_per_plugin: int = Field(default=5, description="每个插件最大备份保留数")
        backup_dir: str = Field(default="backups", description="备份目录名（相对本插件目录）")

    @config_section("auto_update")
    class AutoUpdateSection(SectionBase):
        """自动更新配置。"""
        enabled: bool = Field(default=False, description="自动更新总开关")
        check_interval_minutes: int = Field(default=480, description="检查间隔（分钟），默认8小时")

    @config_section("proxy")
    class ProxySection(SectionBase):
        """代理配置。"""
        http: str = Field(default="", description="HTTP 代理地址")
        https: str = Field(default="", description="HTTPS 代理地址")

    @config_section("github")
    class GitHubSection(SectionBase):
        """GitHub 配置。"""
        token: str = Field(default="", description="GitHub Token（可选，提高 API 限频）")

    backup: BackupSection = Field(default_factory=BackupSection)
    auto_update: AutoUpdateSection = Field(default_factory=AutoUpdateSection)
    proxy: ProxySection = Field(default_factory=ProxySection)
    github: GitHubSection = Field(default_factory=GitHubSection)
