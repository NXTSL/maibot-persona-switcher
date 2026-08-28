"""人格切换插件配置模型。

策略：插件配置（`config.toml`）只放行为开关与权限，**所有可切换的人格/回复风格放
到独立的 `personas.toml` 中**。这样新增人格不需要改 `config.toml`，直接编辑
`personas.toml` 即可，避免每加一个就触发一次配置热更。
"""
from __future__ import annotations

from typing import List

from maibot_sdk import Field, PluginConfigBase


class BasicSection(PluginConfigBase):
    __ui_label__ = "基础设置"

    config_version: str = Field(default="0.1.0", description="配置版本号")
    enabled: bool = Field(default=True, description="是否启用本插件")


class SwitcherSection(PluginConfigBase):
    __ui_label__ = "切换设置"

    personas_file: str = Field(
        default="personas.toml",
        description="人格库文件路径（相对插件目录或绝对路径）",
    )
    target_config: str = Field(
        default="/MaiMBot/config/bot_config.toml",
        description="被修改的 bot_config.toml 路径（容器内绝对路径）",
    )
    backup_keep: int = Field(
        default=10,
        description="保留最近多少份 bot_config.toml 备份；超过会被删除",
        ge=1,
        le=200,
    )
    try_hot_reload: bool = Field(
        default=True,
        description="写完文件后是否尝试调用宿主 reload API 热更新；失败不影响后续",
    )
    hot_reload_apis: List[str] = Field(
        default_factory=lambda: [
            "host.system.reload_config",
            "host.config.reload",
            "host.system.reload",
            "host.config.reload_global",
        ],
        description="按顺序尝试的热更新 API 名列表；第一个成功的就停",
    )


class AccessSection(PluginConfigBase):
    __ui_label__ = "权限"

    mode: str = Field(
        default="blacklist",
        description="权限模式：blacklist 拒绝名单中的人；whitelist 仅允许名单中的人；none 不限制",
        json_schema_extra={"enum": ["blacklist", "whitelist", "none"]},
    )
    user_list: List[str] = Field(
        default_factory=list,
        description="权限名单（QQ 号字符串列表，按 mode 解释）",
    )


class PersonaSwitcherConfig(PluginConfigBase):
    __ui_label__ = "人格切换配置"

    plugin: BasicSection = Field(default_factory=BasicSection)
    switcher: SwitcherSection = Field(default_factory=SwitcherSection)
    access: AccessSection = Field(default_factory=AccessSection)
