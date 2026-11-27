"""平台适配工具模块"""

import hashlib
import re
from typing import Dict, Iterable, Optional, Tuple

from astrbot.core.platform import AstrMessageEvent


class PlatformUtils:
    """平台适配工具类"""

    _MENTION_MARKUP_RE = re.compile(r"<@!?([^>\s]+)>")

    @staticmethod
    def is_qq_official(event: AstrMessageEvent) -> bool:
        """判断是否为 QQ 官方机器人平台（含 Webhook 变体）。"""
        try:
            platform_name = str(event.get_platform_name() or "").lower()
        except Exception:
            platform_name = ""
        if "qq_official" in platform_name or "qqofficial" in platform_name:
            return True

        try:
            event_type = type(event)
            marker = f"{event_type.__module__}.{event_type.__name__}".lower()
            return "qqofficial" in marker
        except Exception:
            return False

    @staticmethod
    def strip_mention_markup(text: str) -> str:
        """移除 QQ 官方 @ 标记，例如 <@OPENID>。"""
        if not text:
            return ""
        return PlatformUtils._MENTION_MARKUP_RE.sub(" ", text)

    @staticmethod
    def _iter_mentions(event: AstrMessageEvent) -> Iterable[object]:
        """遍历事件 mentions 列表，兼容 dict 与对象两种形态。"""
        try:
            message_obj = getattr(event, "message_obj", None)
            raw_message = getattr(message_obj, "raw_message", None)
        except Exception:
            return []

        if isinstance(raw_message, dict):
            mentions = raw_message.get("mentions")
        else:
            mentions = getattr(raw_message, "mentions", None)
        if not isinstance(mentions, (list, tuple)):
            return []
        return mentions

    @staticmethod
    def _get_mention_field(mention: object, *names: str):
        if isinstance(mention, dict):
            for name in names:
                value = mention.get(name)
                if value not in (None, ""):
                    return value
            return None
        for name in names:
            value = getattr(mention, name, None)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def get_mentions(event: AstrMessageEvent) -> Dict[str, dict]:
        """返回 QQ 官方消息 mentions 的用户资料映射。

        key 同时包含 member_openid 与 id，方便 @ 标记、At 组件或手动输入
        openid 时都能命中同一条资料。
        """
        result: Dict[str, dict] = {}
        for mention in PlatformUtils._iter_mentions(event):
            member_openid = str(
                PlatformUtils._get_mention_field(mention, "member_openid") or ""
            ).strip()
            user_id = str(
                PlatformUtils._get_mention_field(mention, "id", "user_openid") or ""
            ).strip()
            nickname = str(
                PlatformUtils._get_mention_field(
                    mention, "member_nickname", "username"
                )
                or ""
            ).strip()
            avatar = str(
                PlatformUtils._get_mention_field(mention, "avatar") or ""
            ).strip()
            is_you = bool(
                PlatformUtils._get_mention_field(mention, "is_you") or False
            )

            profile = {
                "nickname": nickname,
                "avatar": avatar,
                "is_you": is_you,
            }
            if member_openid:
                result[member_openid] = profile
            if user_id and user_id not in result:
                result[user_id] = profile
        return result

    @staticmethod
    def _qq_official_nickname(event: AstrMessageEvent, target_id: str) -> str:
        """QQ 官方平台昵称：发送者 / mentions / 群友别名。"""
        target_id = str(target_id)

        try:
            if target_id == str(event.get_sender_id()):
                sender_name = str(event.get_sender_name() or "").strip()
                if sender_name and sender_name != target_id:
                    return sender_name
        except Exception:
            pass

        profile = PlatformUtils.get_mentions(event).get(target_id) or {}
        nickname = str(profile.get("nickname") or "").strip()
        if nickname and nickname != target_id:
            return nickname

        try:
            group_id = str(event.get_group_id() or "")
            digest = hashlib.sha256(
                f"{group_id}:{target_id}".encode("utf-8")
            ).hexdigest()[:8]
            return f"群友-{digest.upper()}"
        except Exception:
            return target_id

    @staticmethod
    async def get_user_extra_info(
        event: AstrMessageEvent, target_id: str
    ) -> Optional[Tuple[str, Optional[str]]]:
        """
        从消息平台获取用户额外信息

        Args:
            event: 消息事件
            target_id: 目标用户ID

        Returns:
            用户信息元组 (nickname, sex)，失败返回None
        """
        if event.get_platform_name() == "aiocqhttp":
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                    AiocqhttpMessageEvent,
                )

                assert isinstance(event, AiocqhttpMessageEvent)
                client = event.bot
                user_info = await client.get_stranger_info(user_id=int(target_id))
                raw_nickname = user_info.get("nickname")
                nickname = str(raw_nickname if raw_nickname is not None else "Unknown")
                sex = user_info.get("sex")
                return nickname, sex
            except Exception:
                return None

        if PlatformUtils.is_qq_official(event):
            return PlatformUtils._qq_official_nickname(event, str(target_id)), None

        return None

    @staticmethod
    def is_platform_supported(platform_name: str) -> bool:
        """
        检查平台是否支持

        Args:
            platform_name: 平台名称

        Returns:
            是否支持该平台
        """
        supported_platforms = ["aiocqhttp", "qq_official", "qq_official_webhook"]
        return platform_name in supported_platforms
