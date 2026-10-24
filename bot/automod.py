import logging
import re
import time
from collections import defaultdict, deque

import discord

from bot.database import Database
from bot.logging_utils import send_guild_log

logger = logging.getLogger(__name__)
LINK_PATTERN = re.compile(r"(?:https?://|www\.|discord\.gg/)\S+", re.IGNORECASE)
SPAM_WINDOW_SECONDS = 5
SPAM_MESSAGE_LIMIT = 5
FLOOD_WINDOW_SECONDS = 10
FLOOD_MESSAGE_LIMIT = 8
REPEAT_WINDOW_SECONDS = 30
MENTION_LIMIT = 5
RAID_WINDOW_SECONDS = 10
RAID_JOIN_LIMIT = 8


class AutoModerator:
    def __init__(self, database: Database) -> None:
        self.database = database
        self._message_times: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self._message_fingerprints: dict[
            tuple[int, int], deque[tuple[float, str]]
        ] = defaultdict(deque)
        self._join_times: dict[int, deque[float]] = defaultdict(deque)
        self._deleted_message_ids: set[int] = set()

    def consume_deleted_message_id(self, message_id: int) -> bool:
        if message_id in self._deleted_message_ids:
            self._deleted_message_ids.remove(message_id)
            return True
        return False

    async def handle_message(self, message: discord.Message) -> None:
        if (
            message.guild is None
            or message.author.bot
            or not isinstance(message.author, discord.Member)
        ):
            return

        settings = self.database.get_auto_mod_settings(message.guild.id)
        enabled_filters = any(settings.values())
        if not enabled_filters:
            return
        if message.author.guild_permissions.manage_messages or self.database.has_moderator_role(
            message.guild.id, {role.id for role in message.author.roles}
        ):
            return

        now = time.monotonic()
        key = (message.guild.id, message.author.id)
        timestamps = self._message_times[key]
        timestamps.append(now)
        while timestamps and now - timestamps[0] > FLOOD_WINDOW_SECONDS:
            timestamps.popleft()

        normalized_content = " ".join(message.content.casefold().split())
        fingerprints = self._message_fingerprints[key]
        fingerprints.append((now, normalized_content))
        while fingerprints and now - fingerprints[0][0] > REPEAT_WINDOW_SECONDS:
            fingerprints.popleft()

        reason: str | None = None
        if settings["anti_spam"] and sum(
            now - timestamp <= SPAM_WINDOW_SECONDS for timestamp in timestamps
        ) >= SPAM_MESSAGE_LIMIT:
            reason = "Anti-spam: excesso de mensagens em poucos segundos"
        elif settings["anti_flood"] and len(timestamps) >= FLOOD_MESSAGE_LIMIT:
            reason = "Anti-flood: excesso de mensagens em uma janela curta"
        elif settings["anti_mention"] and (
            len(message.mentions) + len(message.role_mentions) > MENTION_LIMIT
        ):
            reason = "Anti-mention: excesso de menções"
        elif settings["anti_link"] and LINK_PATTERN.search(message.content):
            reason = "Anti-link: links não permitidos"
        elif settings["blocked_words"] and any(
            word in message.content.casefold()
            for word in self.database.get_blocked_words(message.guild.id)
        ):
            reason = "Palavra bloqueada detectada"
        elif settings["anti_repeat"] and normalized_content and sum(
            content == normalized_content for _, content in fingerprints
        ) >= 3:
            reason = "Detecção de mensagens repetidas"

        if reason is None:
            return

        try:
            await message.delete()
        except discord.NotFound:
            return
        except discord.Forbidden:
            logger.warning(
                "Não foi possível remover mensagem detectada pelo automod no servidor %s.",
                message.guild.id,
            )
            return
        except discord.HTTPException:
            logger.exception(
                "Erro ao remover mensagem detectada pelo automod no servidor %s.",
                message.guild.id,
            )
            return
        self._deleted_message_ids.add(message.id)
        logger.info(
            "Automod removeu mensagem %s do usuário %s no servidor %s: %s.",
            message.id,
            message.author.id,
            message.guild.id,
            reason,
        )
        await send_guild_log(
            message.guild,
            self.database,
            "Moderação automática",
            f"{message.author} (`{message.author.id}`) em {message.channel.mention}",
            reason,
            moderator="Moderação automática",
            color=discord.Color.orange(),
        )

    async def handle_member_join(self, member: discord.Member) -> None:
        settings = self.database.get_auto_mod_settings(member.guild.id)
        if not settings["anti_raid"]:
            return

        now = time.monotonic()
        joins = self._join_times[member.guild.id]
        joins.append(now)
        while joins and now - joins[0] > RAID_WINDOW_SECONDS:
            joins.popleft()
        if len(joins) == RAID_JOIN_LIMIT:
            await send_guild_log(
                member.guild,
                self.database,
                "Alerta de possível raid",
                member.guild.name,
                f"{RAID_JOIN_LIMIT} membros entraram em até {RAID_WINDOW_SECONDS} segundos.",
                moderator="Proteção contra raid",
                color=discord.Color.red(),
            )
