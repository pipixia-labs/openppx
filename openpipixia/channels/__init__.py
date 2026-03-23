"""Channel interfaces and adapters."""

from .base import BaseChannel
from .dingtalk import DingTalkChannel
from .discord import DiscordChannel
from .email import EmailChannel
from .factory import build_channel_manager, parse_enabled_channels, validate_channel_setup
from .feishu import FeishuChannel
from .local import LocalChannel
from .qq import QQChannel
from .slack import SlackChannel
from .manager import ChannelManager
from .telegram import TelegramChannel
from .whatsapp import WhatsAppChannel

__all__ = [
    "BaseChannel",
    "ChannelManager",
    "DingTalkChannel",
    "DiscordChannel",
    "EmailChannel",
    "FeishuChannel",
    "LocalChannel",
    "QQChannel",
    "SlackChannel",
    "TelegramChannel",
    "WhatsAppChannel",
    "build_channel_manager",
    "parse_enabled_channels",
    "validate_channel_setup",
]
