"""网络请求工具模块"""

from typing import Dict, Optional
from urllib.parse import quote

import aiohttp
from astrbot.api import logger

from .avatar_cache import AvatarCache
from .platform_utils import PlatformUtils

_IMAGE_MAGIC_PREFIXES = (b"\xff\xd8", b"\x89PNG\r\n\x1a\n", b"GIF8")
_THIRDQQ_AVATAR_TEMPLATE = "https://thirdqq.qlogo.cn/qqapp/{appid}/{openid}/640"
_QLOGO_AVATAR_TEMPLATE = "https://q.qlogo.cn/headimg_dl?dst_uin={qq}&spec=640"


def _looks_like_image(payload: bytes) -> bool:
    if not payload:
        return False
    if payload.startswith(_IMAGE_MAGIC_PREFIXES):
        return True
    return payload.startswith(b"RIFF") and b"WEBP" in payload[:16]


class NetworkUtils:
    """网络请求工具类"""

    def __init__(
        self,
        avatar_cache: Optional[AvatarCache] = None,
        context=None,
        qq_official_appid: str = "",
    ):
        self.avatar_cache = avatar_cache
        self.context = context
        self.qq_official_appid = str(qq_official_appid or "").strip()

        # 事件中缓存的头像地址与昵称，key: platform:group:user
        self._profiles: Dict[str, Dict[str, str]] = {}
        self._appid_cache: Dict[str, str] = {}
        self._appid_warned: set[str] = set()

    # ------------------------------------------------------------------
    # 事件资料缓存
    # ------------------------------------------------------------------
    @staticmethod
    def _platform_id(event) -> str:
        try:
            platform_id = event.get_platform_id()
        except Exception:
            platform_id = ""
        if platform_id:
            return str(platform_id)
        try:
            return str(event.get_platform_name() or "unknown")
        except Exception:
            return "unknown"

    @staticmethod
    def _profile_key(platform_id: str, group_id: str, user_id: str) -> str:
        return f"{platform_id}:{group_id}:{user_id}"

    @staticmethod
    def _extract_event_avatar(event) -> str:
        try:
            avatar = str(event.get_extra("sender_avatar") or "").strip()
        except Exception:
            avatar = ""
        if avatar.startswith(("http://", "https://")):
            return avatar

        try:
            message_obj = getattr(event, "message_obj", None)
            raw_message = getattr(message_obj, "raw_message", None)
        except Exception:
            return ""

        candidates = []
        if isinstance(raw_message, dict):
            candidates.append(raw_message.get("author"))
        else:
            candidates.append(getattr(raw_message, "author", None))
        candidates.append(getattr(message_obj, "sender", None))

        for author in candidates:
            if isinstance(author, dict):
                avatar = author.get("avatar")
            else:
                avatar = getattr(author, "avatar", None)
            if isinstance(avatar, str) and avatar.startswith(("http://", "https://")):
                return avatar.strip()
        return ""

    def _update_profile(
        self,
        platform_id: str,
        group_id: str,
        user_id: str,
        avatar: str = "",
        nickname: str = "",
    ) -> None:
        user_id = str(user_id or "").strip()
        if not user_id:
            return
        avatar = str(avatar or "").strip()
        nickname = str(nickname or "").strip()

        key = self._profile_key(platform_id, str(group_id or ""), user_id)
        profile = self._profiles.setdefault(key, {})
        if avatar.startswith(("http://", "https://")):
            profile["avatar"] = avatar
        if nickname and nickname != user_id:
            profile["nickname"] = nickname

    def remember_event(self, event) -> None:
        """从收到的消息事件缓存用户头像与昵称。

        插件对 EventMessageType.ALL 生效，因此普通聊天消息也能提前缓存资料；
        即使目标用户没有单独发过言，QQ 官方 mentions 也会提供其昵称。
        """
        try:
            platform_id = self._platform_id(event)
            group_id = str(event.get_group_id() or "")
            user_id = str(event.get_sender_id() or "")
            self._update_profile(
                platform_id,
                group_id,
                user_id,
                avatar=self._extract_event_avatar(event),
                nickname=str(event.get_sender_name() or ""),
            )

            for mention_id, profile in PlatformUtils.get_mentions(event).items():
                self._update_profile(
                    platform_id,
                    group_id,
                    mention_id,
                    avatar=str(profile.get("avatar") or ""),
                    nickname=str(profile.get("nickname") or ""),
                )
        except Exception as e:
            logger.debug("缓存 QQ 官方用户资料失败: %s", e)

    def _get_profile(
        self, platform_id: str, group_id: str, user_id: str
    ) -> Dict[str, str]:
        return (
            self._profiles.get(
                self._profile_key(platform_id, str(group_id or ""), str(user_id))
            )
            or {}
        )

    # ------------------------------------------------------------------
    # 图片下载
    # ------------------------------------------------------------------
    async def download_image(self, url: str, quiet: bool = False) -> bytes | None:
        """
        下载图片

        Args:
            url: 图片URL
            quiet: 为 True 时降低失败日志级别（头像等可回退场景）

        Returns:
            图片字节数据，失败返回None
        """
        if not url.startswith(("https://", "http://")):
            logger.warning("拒绝不受支持的图片 URL: %s", url)
            return None

        try:
            async with aiohttp.ClientSession(trust_env=True) as client:
                async with client.get(url, timeout=10) as response:
                    response.raise_for_status()
                    if url.startswith("https://") and response.url.scheme != "https":
                        logger.warning("拒绝 HTTPS 图片重定向到非安全协议: %s", response.url)
                        return None
                    return await response.read()
        except Exception as e:
            if quiet:
                logger.debug(f"图片下载失败: {e}")
            else:
                logger.error(f"图片下载失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 头像解析
    # ------------------------------------------------------------------
    @staticmethod
    def _is_numeric_qq(user_id: str) -> bool:
        return user_id.isascii() and user_id.isdigit() and 5 <= len(user_id) <= 12

    @staticmethod
    def _cache_key(platform_id: str, group_id: str, user_id: str) -> str:
        # 保留旧版数字 QQ 的缓存键，避免已有缓存失效
        if NetworkUtils._is_numeric_qq(user_id):
            return user_id
        return f"{platform_id}:{group_id}:{user_id}"

    async def _get_appid(self, event) -> str:
        if self.qq_official_appid:
            return self.qq_official_appid

        platform_id = self._platform_id(event)
        cached = self._appid_cache.get(platform_id)
        if cached:
            return cached

        appid = ""
        if self.context is not None:
            try:
                platform = self.context.get_platform_inst(platform_id)
            except Exception:
                platform = None
            if platform is not None:
                appid = str(getattr(platform, "appid", "") or "").strip()
                if not appid:
                    platform_config = getattr(platform, "config", None)
                    if isinstance(platform_config, dict):
                        appid = str(platform_config.get("appid", "") or "").strip()

        if appid:
            self._appid_cache[platform_id] = appid
        elif platform_id not in self._appid_warned:
            self._appid_warned.add(platform_id)
            logger.warning(
                "QQ 官方平台 %s 未获取到 AppID，头像无法通过 thirdqq 获取；"
                "可在插件配置中填写 qq_official_appid",
                platform_id,
            )
        return appid

    async def _build_avatar_urls(self, event, user_id: str) -> list[str]:
        """根据平台规则构造头像候选地址。"""
        urls: list[str] = []

        if event is not None and PlatformUtils.is_qq_official(event):
            appid = await self._get_appid(event)
            if appid:
                urls.append(
                    _THIRDQQ_AVATAR_TEMPLATE.format(
                        appid=quote(appid, safe=""),
                        openid=quote(str(user_id), safe=""),
                    )
                )

        if self._is_numeric_qq(str(user_id)):
            urls.append(_QLOGO_AVATAR_TEMPLATE.format(qq=user_id))

        return urls

    async def get_avatar(self, user_id: str, event=None) -> bytes | None:
        """
        下载用户头像(支持缓存)

        Args:
            user_id: 用户ID（QQ 号或 QQ 官方 member_openid）
            event: 当前消息事件，用于解析 QQ 官方 appid 与事件头像

        Returns:
            头像字节数据，失败返回None
        """
        user_id = str(user_id or "").strip()
        if not user_id:
            return None

        platform_id = self._platform_id(event) if event is not None else "unknown"
        group_id = str(event.get_group_id() or "") if event is not None else ""
        cache_key = self._cache_key(platform_id, group_id, user_id)

        if self.avatar_cache:
            cached_avatar = self.avatar_cache.get_avatar(cache_key)
            if cached_avatar:
                return cached_avatar

        urls = []
        if event is not None:
            profile_avatar = self._get_profile(
                platform_id, group_id, user_id
            ).get("avatar", "")
            if profile_avatar:
                urls.append(profile_avatar)

        fallback_urls = await self._build_avatar_urls(event, user_id)
        for fallback_url in fallback_urls:
            if fallback_url not in urls:
                urls.append(fallback_url)

        if not urls:
            logger.debug(
                "无法为用户 %s 解析头像地址（平台: %s）",
                user_id,
                self._platform_id(event) if event is not None else "unknown",
            )
            return None

        for avatar_url in urls:
            avatar_data = await self.download_image(avatar_url, quiet=True)
            if not _looks_like_image(avatar_data or b""):
                continue
            if self.avatar_cache and avatar_data:
                self.avatar_cache.set_avatar(cache_key, avatar_data)
            return avatar_data

        logger.debug("头像下载失败，用户: %s", user_id)
        return None

    def get_nickname(self, event, user_id: str) -> str:
        """读取事件缓存中已知的用户昵称。"""
        if event is None:
            return ""
        platform_id = self._platform_id(event)
        group_id = str(event.get_group_id() or "")
        profile = self._get_profile(platform_id, group_id, str(user_id))
        return str(profile.get("nickname") or "")
