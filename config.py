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

        max_backups_per_plugin: int = Field(
            default=5, description="每个插件最大备份保留数"
        )
        backup_dir: str = Field(
            default="backups", description="备份目录名（相对本插件目录）"
        )

    @config_section("auto_update")
    class AutoUpdateSection(SectionBase):
        """自动更新配置。"""
        enabled: bool = Field(default=False, description="自动更新总开关")
        check_interval_minutes: int = Field(
            default=480, description="检查间隔（分钟），默认8小时"
        )
        passive_check: bool = Field(
            default=False, description="是否被动触发（消息经过时顺带检测）"
        )
        passive_cooldown_minutes: int = Field(
            default=60, description="被动触发冷却时间（分钟）"
        )
        extend_eventbus_timeout: bool = Field(
            default=False, description="增大EventBus超时（/pm check等长时间命令不被打断）"
        )

    @config_section("market")
    class MarketSection(SectionBase):
        """市场插件配置。"""
        check_interval_minutes: int = Field(
            default=480, description="市场订阅缓存有效期（分钟）"
        )
        default_update_policy: str = Field(
            default="silent", description="市场插件默认更新策略：silent/notify/prompt"
        )

    @config_section("notification")
    class NotificationSection(SectionBase):
        """通知配置。"""
        notify_on_update: bool = Field(
            default=True, description="检测到更新时是否发送通知"
        )
        private_chat_ids: str = Field(
            default="",
            description="通知目标私聊QQ号，多个用逗号分隔（留空=发到当前对话）",
        )
        group_chat_ids: str = Field(
            default="", description="通知目标群聊QQ号，多个用逗号分隔"
        )
        prefer_private: bool = Field(
            default=True, description="私聊通知优先（否则群聊优先）"
        )

    @config_section("permission")
    class PermissionSection(SectionBase):
        """权限配置。"""
        admin_qq_ids: str = Field(
            default="",
            description="额外管理员QQ号，多个用逗号分隔（默认仅bot主人可操作）",
        )
        allow_all_users: bool = Field(
            default=False, description="允许所有用户使用全部指令（覆盖以上设置）"
        )

    @config_section("github")
    class GitHubSection(SectionBase):
        """GitHub 配置。"""
        token: str = Field(
            default="", description="GitHub Token（可选，提高 API 限频）"
        )
        default_update_policy: str = Field(
            default="notify", description="GitHub 插件默认更新策略：silent/notify/prompt"
        )

    @config_section("proxy")
    class ProxySection(SectionBase):
        """代理配置。"""

        http: str = Field(default="", description="HTTP 代理地址")
        https: str = Field(default="", description="HTTPS 代理地址")

    backup: BackupSection = Field(default_factory=BackupSection)
    auto_update: AutoUpdateSection = Field(default_factory=AutoUpdateSection)
    market: MarketSection = Field(default_factory=MarketSection)
    notification: NotificationSection = Field(default_factory=NotificationSection)
    permission: PermissionSection = Field(default_factory=PermissionSection)
    github: GitHubSection = Field(default_factory=GitHubSection)
    proxy: ProxySection = Field(default_factory=ProxySection)
