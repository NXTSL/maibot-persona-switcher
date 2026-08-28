# 麦麦人格一键切换（nxtsl.persona-switcher）

通过 `/persona ...` 命令在多套预置人格 / 回复风格之间一键切换，仅修改
`bot_config.toml` 的 `[personality]` 段，写完即触发宿主 reload（若可用）。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/persona list` | 列出全部可用人格（编号 + id + 中文显示名） |
| `/persona use <id\|序号>` | 切换到指定人格 |
| `/persona current` | 查看当前 bot_config 中的人格 / 回复风格前 60 字 |
| `/persona reset` | 切回 `id="default"` 的人格 |
| `/persona reload` | 重新读取 `personas.toml`（热添加人格后使用） |

## 目录结构

```text
nxtsl_persona-switcher/
├── _manifest.json
├── __init__.py
├── config.py            # 插件配置模型（SDK 自动生成 config.toml + WebUI 表单）
├── plugin.py            # 主逻辑
├── personas.toml.example
└── personas.toml.example
```

`personas.toml` 格式：

```toml
[[persona]]
id = "default"
display = "默认（佛系女大）"
personality = "..."
reply_style = "..."
```

复制 `[[persona]]` 块即可继续追加，`id` 字段必须唯一且只含字母 / 数字 / `_` / `-`。

## 权限

`config.toml` 的 `[access]` 段控制谁能用命令：

- `mode = "none"` —— 所有人可用
- `mode = "blacklist"` —— 黑名单外的所有人可用（默认）
- `mode = "whitelist"` —— 仅白名单用户可用

## 切换流程

1. 把 `bot_config.toml` 备份到同目录的 `backups/bot_config.toml.YYYYMMDD-HHMMSS.bak`，
   最多保留最近 10 份。
2. 用正则只改 `[personality]` 段内的 `personality=` 与 `reply_style=` 两行，
   其它段与所有注释原样保留。
3. 原子写回（先写 `.tmp` 再 `replace`）。
4. 按 `hot_reload_apis` 顺序尝试宿主 reload API；任一成功即视为热更新成功，
   否则给"需在 WebUI 手动重载 / 重启 core"的提示。

## 注意事项

- 插件**不会**改 `[personality]` 段以外的任何字段，但请务必先用 WebUI 备份一次
  `bot_config.toml`。
- `bot_config.toml` 在容器内路径 `/MaiMBot/config/bot_config.toml`，
  如果你的部署路径不同，请在插件配置中修改 `switcher.target_config`。
- 如果热更新 API 列表里没有一个可用，最终命令会返回"需手动重载"，不算错误。
- 默认的 `hot_reload_apis` 是按 MaiBot 命名约定猜的，如果一个都没命中，请
  在 WebUI 的插件配置页把它清空，并在 MaiBot WebUI 重载插件后手动重启 core
  容器。
