"""Loa camera thành một media_player: ``tts.speak`` và ``play_media`` phát ra loa camera.

Nhạc (``play_media`` không kèm ``announce``) phát NỀN: lệnh trả về ngay, bài mới thay bài đang phát,
Stop dừng được. Đo 30/09/2026: YouTube đẩy một bài vào loa camera thì lệnh phát treo suốt bài, loa
không có Stop — cách duy nhất để im là khởi động lại HA, và HA còn mất 1 phút mới tắt xong vì luồng
phát đang gửi dở. Thông báo (``announce``, ``tts.speak``) vẫn chờ phát xong như cũ để nối tiếp nhau.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.components.media_player.const import ATTR_MEDIA_ANNOUNCE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DahuaTalkConfigEntry
from .entity import DahuaTalkEntity
from .talk import TalkError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: DahuaTalkConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([DahuaTalkPlayer(entry)])


class DahuaTalkPlayer(DahuaTalkEntity, MediaPlayerEntity):
    _attr_translation_key = "speaker"
    _attr_supported_features = (MediaPlayerEntityFeature.PLAY_MEDIA
                                 | MediaPlayerEntityFeature.BROWSE_MEDIA
                                 | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
                                 | MediaPlayerEntityFeature.STOP)

    def __init__(self, entry: DahuaTalkConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}-speaker"
        self._attr_state = MediaPlayerState.IDLE
        #: Bài nhạc đang phát nền và cờ dừng của nó.
        self._bai: asyncio.Task | None = None
        self._huy: threading.Event | None = None

    async def async_play_media(self, media_type: MediaType | str, media_id: str,
                               **kwargs: Any) -> None:
        if media_source.is_media_source_id(media_id):
            play = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            media_id = play.url
        url = async_process_play_media_url(self.hass, media_id)
        if kwargs.get(ATTR_MEDIA_ANNOUNCE):
            await self._phat(url, threading.Event())
            return
        await self.async_media_stop()
        self._huy = huy = threading.Event()
        self._bai = self.hass.async_create_background_task(
            self._phat(url, huy), f"{self.entity_id} play_media")

    async def _phat(self, url: str, huy: threading.Event) -> None:
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()
        try:
            await self._entry.runtime_data.speaker.async_play_url(url, huy)
        except (TalkError, OSError) as exc:
            _LOGGER.warning("%s: cannot play to camera speaker: %s", self.entity_id, exc)
        finally:
            if self._huy in (None, huy):    # bài mới đã thay thì để bài mới giữ trạng thái
                self._attr_state = MediaPlayerState.IDLE
                self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        """Dừng bài nhạc đang phát nền (luồng gửi thoát sau khúc đang gửi, ffmpeg tắt)."""
        bai, huy = self._bai, self._huy
        if huy is not None:
            huy.set()
        if bai is not None and not bai.done():
            await asyncio.wait({bai})
        self._bai = self._huy = None

    async def async_will_remove_from_hass(self) -> None:
        await self.async_media_stop()

    async def async_browse_media(self, media_content_type: MediaType | str | None = None,
                                 media_content_id: str | None = None) -> BrowseMedia:
        return await media_source.async_browse_media(
            self.hass, media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"))
