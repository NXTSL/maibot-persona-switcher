"""人格一键切换插件主逻辑。

设计要点：
1. 仅修改 ``bot_config.toml`` 的 ``[personality].personality`` 与 ``reply_style``
   两行，其它段一概不动；写之前先备份再原子替换。
2. 人格库 ``personas.toml`` 与插件配置分离，新增/修改人格不会触发 config 热更新。
3. 写完文件后，按配置的顺序尝试宿主 reload API；任一成功即视为热更新成功。
4. 全异步 I/O 通过 ``asyncio.to_thread`` 走线程池，不阻塞事件循环。

命令：
    /persona list              列出全部可用人格（编号 + id + display）
    /persona use <id|序号>     切换到指定人格
    /persona current           查看当前 bot_config 的人格/回复风格前 60 字
    /persona reset             恢复 ``personas.toml`` 中 id="default" 的人格
    /persona reload            重新读取 personas.toml（热添加人格时用）

权限由 ``config.access.mode`` 控制，名单按 user_id（QQ 号字符串）匹配。
"""
from __future__ import annotations

import asyncio
import re
import shutil
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from maibot_sdk import Command, MaiBotPlugin

from .config import PersonaSwitcherConfig

# 容许的 id 字符，避免命令注入与路径穿越
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# 匹配 [personality] 段中两行的弱正则（容许多空格、引号）
_PERSONALITY_LINE_RE = re.compile(
    r"^(\s*personality\s*=\s*)(.*?)(\s*)$",
    re.MULTILINE,
)
_REPLY_STYLE_LINE_RE = re.compile(
    r"^(\s*reply_style\s*=\s*)(.*?)(\s*)$",
    re.MULTILINE,
)


class PersonaSwitcherPlugin(MaiBotPlugin):
    """人格一键切换插件主类。"""

    config_model = PersonaSwitcherConfig

    def __init__(self) -> None:
        super().__init__()
        self._personas: list[dict[str, str]] = []
        self._personas_mtime: float = 0.0
        self._personas_path: Path | None = None
        self._target_path: Path | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        cfg = self.config
        if not cfg.plugin.enabled:
            self.ctx.logger.info("[persona-switcher] 已禁用，跳过初始化")
            return

        self._personas_path = self._resolve_path(cfg.switcher.personas_file)
        self._target_path = self._resolve_path(cfg.switcher.target_config)
        if not self._personas_path or not self._target_path:
            self.ctx.logger.error(
                "[persona-switcher] 路径解析失败 personas=%s target=%s",
                cfg.switcher.personas_file,
                cfg.switcher.target_config,
            )
            return

        # 把示例人格库落到 plugins 目录下，方便用户编辑
        sample = Path(__file__).resolve().parent / "personas.toml.example"
        live = Path(__file__).resolve().parent / "personas.toml"
        if not live.exists() and sample.exists():
            try:
                await asyncio.to_thread(shutil.copyfile, str(sample), str(live))
                self.ctx.logger.info(
                    "[persona-switcher] 已复制示例人格库到 %s，请按需编辑",
                    live,
                )
            except OSError as exc:
                self.ctx.logger.warning(
                    "[persona-switcher] 复制示例人格库失败：%s", exc
                )

        self._personas_path = live if live.exists() else self._personas_path

        await self._reload_personas_log()
        self.ctx.logger.info(
            "[persona-switcher] 已加载 %d 个人格，目标配置：%s",
            len(self._personas),
            self._target_path,
        )

    async def on_unload(self) -> None:
        self.ctx.logger.info("[persona-switcher] 插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict, version: str
    ) -> None:
        """配置变更回调（SDK 要求实现）。

        - 先调 ``set_plugin_config`` 让 ``self.config`` 指向新值；
        - 如果改的是插件自身配置（``scope == "self"``），重新解析路径并刷新人格库。
        """
        try:
            self.set_plugin_config(config_data)
        except Exception as exc:  # noqa: BLE001 - 不同 SDK 版本可能 API 缺失
            self.ctx.logger.warning(
                "[persona-switcher] set_plugin_config 调用失败：%s", exc
            )

        if scope != "self":
            return

        cfg = self.config
        new_personas = self._resolve_path(cfg.switcher.personas_file)
        new_target = self._resolve_path(cfg.switcher.target_config)
        if new_personas and new_target:
            self._personas_path = new_personas
            self._target_path = new_target
        ok, msg = await self._reload_personas_log()
        self.ctx.logger.info(
            "[persona-switcher] 配置已更新（version=%s，personas: %s）",
            version, msg if ok else f"重读失败：{msg}",
        )

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    @Command(
        "persona",
        description="人格切换：list / use <id|序号> / current / reset / reload",
        pattern=r"^/persona(?:\s+(?P<action>list|current|reset|reload|use)(?:\s+(?P<arg>\S+))?)?\s*$",
    )
    async def handle_persona(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = kwargs["stream_id"]
        # MaiBot SDK 把正则命名捕获放在 kwargs["matched_groups"]
        groups: dict[str, str] = kwargs.get("matched_groups") or {}
        user_id = str(kwargs.get("user_id") or "").strip()

        if not self._is_allowed(user_id):
            text = "（你不在本插件的使用名单中）"
            await self.ctx.send.text(text, stream_id)
            return True, text, 1

        action = (groups.get("action") or "list").lower()
        arg = (groups.get("arg") or "").strip()

        if action == "list":
            return await self._cmd_list(stream_id)
        if action == "current":
            return await self._cmd_current(stream_id)
        if action == "reload":
            return await self._cmd_reload(stream_id)
        if action == "reset":
            return await self._cmd_use(stream_id, "default", forced_id="default")
        if action == "use":
            if not arg:
                text = "用法：/persona use <id 或序号>"
                await self.ctx.send.text(text, stream_id)
                return True, text, 1
            return await self._cmd_use(stream_id, arg)
        # 默认 fallback
        return await self._cmd_list(stream_id)

    # ------------------------------------------------------------------
    # 命令实现
    # ------------------------------------------------------------------

    async def _cmd_list(self, stream_id: str) -> tuple[bool, str, int]:
        await self._maybe_reload_personas()
        if not self._personas:
            text = "暂无可用人格，请检查 personas.toml"
            await self.ctx.send.text(text, stream_id)
            return True, text, 1
        lines = ["可用人格（用 /persona use <id 或序号> 切换）："]
        for idx, p in enumerate(self._personas, start=1):
            lines.append(f"  {idx}. {p['id']} — {p['display']}")
        text = "\n".join(lines)
        await self.ctx.send.text(text, stream_id)
        return True, text, 1

    async def _cmd_current(self, stream_id: str) -> tuple[bool, str, int]:
        assert self._target_path is not None
        try:
            data = await asyncio.to_thread(_read_toml, self._target_path)
        except Exception as exc:  # noqa: BLE001
            text = f"读取 bot_config.toml 失败：{exc}"
            await self.ctx.send.text(text, stream_id)
            return True, text, 1
        per = data.get("personality") or {}
        personality = str(per.get("personality") or "")
        reply_style = str(per.get("reply_style") or "")
        text = (
            "当前 [personality] 段：\n"
            f"- personality（前 60 字）：{personality[:60]}{'...' if len(personality) > 60 else ''}\n"
            f"- reply_style（前 60 字）：{reply_style[:60]}{'...' if len(reply_style) > 60 else ''}"
        )
        await self.ctx.send.text(text, stream_id)
        return True, text, 1

    async def _cmd_reload(self, stream_id: str) -> tuple[bool, str, int]:
        ok, msg = await self._reload_personas_log()
        text = ("已重新读取 personas.toml，" if ok else "重读失败：") + msg
        await self.ctx.send.text(text, stream_id)
        return True, text, 1

    async def _cmd_use(
        self, stream_id: str, arg: str, *, forced_id: str | None = None
    ) -> tuple[bool, str, int]:
        await self._maybe_reload_personas()
        if not self._personas:
            text = "暂无可用人格，请检查 personas.toml"
            await self.ctx.send.text(text, stream_id)
            return True, text, 1

        target_id = forced_id
        if target_id is None:
            target_id = self._resolve_persona_id(arg)
        if not target_id or not _ID_PATTERN.match(target_id):
            text = f"非法人格标识：{arg}"
            await self.ctx.send.text(text, stream_id)
            return True, text, 1
        persona = next((p for p in self._personas if p["id"] == target_id), None)
        if persona is None:
            text = f"找不到人格：{target_id}（先用 /persona list 查看）"
            await self.ctx.send.text(text, stream_id)
            return True, text, 1

        try:
            backup = await self._backup_target()
            _ = await self._apply_persona(persona)
        except Exception as exc:  # noqa: BLE001
            text = f"切换失败：{exc}"
            self.ctx.logger.error("[persona-switcher] %s", exc)
            await self.ctx.send.text(text, stream_id)
            return True, text, 1

        reload_msg = ""
        if self.config.switcher.try_hot_reload:
            reload_msg = await self._try_hot_reload()
        text = (
            f"已切换到人格：{persona['display']}（id={persona['id']}）\n"
            f"备份：{backup}\n"
            f"配置已写入：{self._target_path}\n"
            f"{reload_msg}"
        )
        await self.ctx.send.text(text, stream_id)
        return True, text, 1

    # ------------------------------------------------------------------
    # 内部：人格库 / 文件 / 热更新
    # ------------------------------------------------------------------

    def _resolve_path(self, raw: str) -> Path | None:
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            p = (Path(__file__).resolve().parent / p).resolve()
        return p

    async def _maybe_reload_personas(self) -> None:
        assert self._personas_path is not None
        try:
            stat = await asyncio.to_thread(self._personas_path.stat)
        except OSError:
            return
        if stat.st_mtime != self._personas_mtime:
            await self._reload_personas_log()

    async def _reload_personas_log(self) -> tuple[bool, str]:
        assert self._personas_path is not None
        try:
            data = await asyncio.to_thread(_read_toml, self._personas_path)
            stat = await asyncio.to_thread(self._personas_path.stat)
        except Exception as exc:  # noqa: BLE001
            return False, f"读取 {self._personas_path} 失败：{exc}"

        raw_list = data.get("persona")
        if not isinstance(raw_list, list) or not raw_list:
            return False, "personas.toml 缺少 [[persona]] 列表"

        cleaned: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("id") or "").strip()
            if not _ID_PATTERN.match(pid) or pid in seen:
                continue
            seen.add(pid)
            cleaned.append(
                {
                    "id": pid,
                    "display": str(entry.get("display") or pid),
                    "personality": str(entry.get("personality") or "").strip(),
                    "reply_style": str(entry.get("reply_style") or "").strip(),
                }
            )

        if not cleaned:
            return False, "personas.toml 中没有有效 [[persona]]"

        self._personas = cleaned
        self._personas_mtime = stat.st_mtime
        return True, f"载入 {len(cleaned)} 个人格"

    def _resolve_persona_id(self, arg: str) -> str | None:
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(self._personas):
                return self._personas[idx]["id"]
            return None
        if _ID_PATTERN.match(arg):
            return arg
        return None

    def _is_allowed(self, user_id: str) -> bool:
        mode = self.config.access.mode
        if mode == "none":
            return True
        in_list = user_id in {str(x) for x in self.config.access.user_list}
        if mode == "whitelist":
            return in_list
        if mode == "blacklist":
            return not in_list
        return True

    async def _backup_target(self) -> str:
        assert self._target_path is not None
        backup_dir = self._target_path.parent / "backups"
        await asyncio.to_thread(_ensure_dir, backup_dir)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"bot_config.toml.{ts}.bak"
        await asyncio.to_thread(shutil.copyfile, str(self._target_path), str(backup_path))

        # 清理多余备份，保留最近 N 份
        keep = int(self.config.switcher.backup_keep)
        await asyncio.to_thread(_trim_backups, backup_dir, keep)

        return str(backup_path)

    async def _apply_persona(self, persona: dict[str, str]) -> str:
        assert self._target_path is not None

        original = await asyncio.to_thread(self._target_path.read_text, "utf-8")
        updated = _replace_personality_block(
            original,
            personality=persona["personality"],
            reply_style=persona["reply_style"],
        )

        tmp = self._target_path.with_suffix(self._target_path.suffix + ".tmp")
        await asyncio.to_thread(_atomic_write_text, tmp, updated, "utf-8")
        await asyncio.to_thread(tmp.replace, self._target_path)
        return updated

    async def _try_hot_reload(self) -> str:
        apis: Iterable[str] = self.config.switcher.hot_reload_apis or []
        for api_name in apis:
            if not api_name:
                continue
            try:
                result = await self.ctx.api.call(api_name, params={})
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.info(
                    "[persona-switcher] 热更新 %s 抛错：%s", api_name, exc
                )
                continue
            if isinstance(result, dict) and result.get("success") is False:
                self.ctx.logger.info(
                    "[persona-switcher] 热更新 %s 返回失败：%s",
                    api_name,
                    result.get("error"),
                )
                continue
            return f"已触发热更新：{api_name}\n"
        return "热更新 API 均未生效，建议在 MaiBot WebUI 重载插件或重启 core。\n"


# ----------------------------------------------------------------------
# 文件 I/O 帮助函数（同步，全部用 to_thread 调用）
# ----------------------------------------------------------------------


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _atomic_write_text(path: Path, content: str, encoding: str) -> None:
    path.write_text(content, encoding=encoding)


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _trim_backups(directory: Path, keep: int) -> None:
    files = sorted(
        (p for p in directory.glob("bot_config.toml.*.bak") if p.is_file()),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def _replace_personality_block(
    text: str, *, personality: str, reply_style: str
) -> str:
    """仅修改 [personality] 段内的 personality 和 reply_style 两行。

    其它段、原注释、空行、键顺序全部保留。
    """
    section_re = re.compile(
        r"(?ms:^\[personality]\s*$\n(?P<body>.*?))(?=^\[|\Z)"
    )
    match = section_re.search(text)
    if match is None:
        # 找不到则追加新段
        addition = (
            "\n[personality]\n"
            f'personality = "{_escape_toml(personality)}"\n'
            f'reply_style = "{_escape_toml(reply_style)}"\n'
        )
        return text.rstrip() + "\n" + addition

    body = match.group("body")

    def _sub_line(current_body: str, line_re: re.Pattern[str], value: str) -> tuple[str, bool]:
        def _repl(m: re.Match[str]) -> str:
            return f'{m.group(1)}"{_escape_toml(value)}"{m.group(3)}'

        new_body, n = line_re.subn(_repl, current_body, count=1)
        return new_body, n > 0

    new_body, replaced1 = _sub_line(body, _PERSONALITY_LINE_RE, personality)
    new_body, replaced2 = _sub_line(new_body, _REPLY_STYLE_LINE_RE, reply_style)

    if not (replaced1 and replaced2):
        # 段存在但缺字段，则追加在段尾
        new_body = new_body.rstrip("\n") + (
            f'\npersonality = "{_escape_toml(personality)}"\n'
            f'reply_style = "{_escape_toml(reply_style)}"\n'
        )

    return text[: match.start("body")] + new_body + text[match.end("body") :]


def _escape_toml(value: str) -> str:
    """把任意字符串转成 TOML 双引号基本字符串。

    简化策略：去掉控制字符与不可打印字符、转义反斜杠与双引号；多行字符串统一
    压成单行 + 转义换行，避免 TOML 多行三引号的格式复杂度。
    """
    cleaned = "".join(ch for ch in value if ch == "\t" or ch >= " ")
    cleaned = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    cleaned = cleaned.replace("\n", "\\n").replace("\r", "\\r")
    return cleaned


def create_plugin() -> PersonaSwitcherPlugin:
    return PersonaSwitcherPlugin()