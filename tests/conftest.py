"""Bật nạp tích hợp tuỳ chỉnh trong HA thử."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def khong_mo_cong_rtsp(request):
    """HA thử chặn socket mạng: dựng tích hợp thì không mở máy chủ RTSP bộ đàm — trừ test đánh
    dấu ``may_chu_rtsp`` (tự mở máy chủ, kèm ``socket_enabled``)."""
    if request.node.get_closest_marker("may_chu_rtsp"):
        yield
        return
    from unittest import mock
    with mock.patch("custom_components.dahua_talk.rtsp_intercom.MayChuBoDam.async_start",
                    return_value=False):
        yield
