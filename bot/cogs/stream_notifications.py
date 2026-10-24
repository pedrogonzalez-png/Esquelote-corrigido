import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.checks import UserFacingError
from bot.database import Database

logger = logging.getLogger(__name__)

TWITCH_API_BASE = "https://api.twitch.tv/helix"
TWITCH_OAUTH_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_LOGIN_PATTERN = re.compile(r"^[a-zA-Z0-9_]{1,25}$")
POLL_INTERVAL_SECONDS = 60


def normalize_streamer_login(streamer: str) -> str:
    login = streamer.strip().removeprefix("@").casefold()
    if not TWITCH_LOGIN_PATTERN.fullmatch(login):
        raise UserFacingError(
            "Informe o nome de usuário da Twitch (1 a 25 letras, números "
            "ou sublinhados), sem URL."
        )
    return login


def build_stream_announcement(
    streamer_login: str,
    stream: dict[str, Any] | None = None,
    *,
    test: bool = False,
) -> discord.Embed:
    stream = stream or {}
    login = str(stream.get("user_login") or streamer_login)
    display_name = str(stream.get("user_name") or streamer_login)
    title = str(stream.get("title") or "Transmissão de teste")
    game = str(stream.get("game_name") or "Não informado")
    stream_url = f"https://www.twitch.tv/{login}"

    prefix = "🧪 TESTE — " if test else ""
    embed = discord.Embed(
        title=f"{prefix}🔴 AO VIVO NA TWITCH!",
        description=f"**{display_name} começou uma transmissão!**",
        url=stream_url,
        color=discord.Color.purple(),
    )
    embed.add_field(name="Título", value=title[:1024], inline=False)
    embed.add_field(name="Jogo", value=game[:1024], inline=True)
    embed.add_field(
        name="Assistir à transmissão",
        value=f"[Abrir live na Twitch]({stream_url})",
        inline=True,
    )

    started_at = stream.get("started_at")
    if isinstance(started_at, str):
        timestamp = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        embed.timestamp = timestamp

    thumbnail = stream.get("thumbnail_url")
    if isinstance(thumbnail, str) and thumbnail:
        embed.set_image(
            url=thumbnail.replace("{width}", "1280").replace("{height}", "720")
        )
    embed.set_footer(text=f"Twitch: {display_name}")
    return embed


class StreamNotificationService:
    def __init__(self, client: commands.Bot, database: Database) -> None:
        self.client = client
        self.database = database
        self.client_id = os.getenv("TWITCH_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._missing_credentials_warned = False

    def start(self) -> None:
        if not self.poll_streams.is_running():
            self.poll_streams.start()

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_streams(self) -> None:
        registrations = self.database.get_all_stream_notifications()
        if not registrations:
            return
        if not self.client_id or not self.client_secret:
            if not self._missing_credentials_warned:
                logger.error(
                    "Notificações da Twitch estão configuradas, mas "
                    "TWITCH_CLIENT_ID ou TWITCH_CLIENT_SECRET não foi definido."
                )
                self._missing_credentials_warned = True
            return

        self._missing_credentials_warned = False
        logins = sorted({row["streamer_login"] for row in registrations})
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                streams = await self._fetch_live_streams(session, logins)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            logger.error("Falha ao consultar transmissões na Twitch: %s", error)
            return

        for registration in registrations:
            login = registration["streamer_login"]
            stream = streams.get(login)
            if stream is None:
                if registration["last_stream_id"] is not None:
                    self.database.set_stream_notification_last_id(
                        registration["guild_id"], login, None
                    )
                continue

            stream_id = str(stream["id"])
            if registration["last_stream_id"] == stream_id:
                continue

            channel = await self._get_text_channel(registration["channel_id"])
            if channel is None:
                logger.error(
                    "Canal de anúncio %s da Twitch não está disponível "
                    "para o streamer %s no servidor %s.",
                    registration["channel_id"],
                    login,
                    registration["guild_id"],
                )
                self.database.set_stream_notification_last_id(
                    registration["guild_id"], login, stream_id
                )
                continue

            try:
                await channel.send(
                    embed=build_stream_announcement(login, stream),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Não foi possível anunciar a live de %s no canal %s.",
                    login,
                    channel.id,
                )
                continue

            self.database.set_stream_notification_last_id(
                registration["guild_id"], login, stream_id
            )

    @poll_streams.before_loop
    async def before_poll_streams(self) -> None:
        await self.client.wait_until_ready()

    async def _get_access_token(
        self, session: aiohttp.ClientSession
    ) -> str:
        if (
            self._access_token is not None
            and time.monotonic() < self._token_expires_at
        ):
            return self._access_token

        async with session.post(
            TWITCH_OAUTH_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
        ) as response:
            response.raise_for_status()
            payload = await response.json()

        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("A Twitch não retornou um token de acesso válido.")
        expires_in = int(payload.get("expires_in", 0))
        self._access_token = token
        self._token_expires_at = time.monotonic() + max(0, expires_in - 60)
        return token

    async def _fetch_live_streams(
        self,
        session: aiohttp.ClientSession,
        logins: list[str],
    ) -> dict[str, dict[str, Any]]:
        token = await self._get_access_token(session)
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}",
        }
        streams: dict[str, dict[str, Any]] = {}
        for start in range(0, len(logins), 100):
            params = [
                ("user_login", login)
                for login in logins[start : start + 100]
            ]
            async with session.get(
                f"{TWITCH_API_BASE}/streams",
                headers=headers,
                params=params,
            ) as response:
                response.raise_for_status()
                payload = await response.json()

            for stream in payload.get("data", []):
                login = stream.get("user_login")
                if isinstance(login, str):
                    streams[login.casefold()] = stream
        return streams

    async def _get_text_channel(
        self, channel_id: int
    ) -> discord.TextChannel | None:
        channel = self.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _check_channel_permissions(
        self, guild: discord.Guild, channel: discord.TextChannel
    ) -> None:
        if channel.guild.id != guild.id:
            raise UserFacingError("Escolha um canal de texto deste servidor.")
        bot_member = guild.me
        if bot_member is None:
            raise UserFacingError(
                "Não consegui verificar minhas permissões neste servidor."
            )
        permissions = channel.permissions_for(bot_member)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
        ):
            raise UserFacingError(
                "Preciso poder ver o canal, enviar mensagens e incorporar links nele."
            )

    def register_commands(self, tree: app_commands.CommandTree) -> None:
        @tree.command(
            name="stream-add",
            description="Cadastra um streamer da Twitch e o canal de anúncio",
        )
        @app_commands.guild_only()
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.describe(
            streamer="Nome de usuário da Twitch, sem URL",
            canal="Canal de texto onde os anúncios serão publicados",
        )
        async def stream_add(
            interaction: discord.Interaction,
            streamer: app_commands.Range[str, 1, 25],
            canal: discord.TextChannel,
        ) -> None:
            guild = interaction.guild
            if guild is None:
                raise UserFacingError("Use este comando dentro de um servidor.")
            login = normalize_streamer_login(streamer)
            await self._check_channel_permissions(guild, canal)
            updated = self.database.add_stream_notification(
                guild.id, login, canal.id
            )
            result = "atualizado" if updated else "cadastrado"
            await interaction.response.send_message(
                f"Streamer `{login}` {result}. Os anúncios serão enviados em "
                f"{canal.mention}. A verificação ocorre a cada "
                f"{POLL_INTERVAL_SECONDS} segundos.",
                ephemeral=True,
            )

        @tree.command(
            name="stream-remove",
            description="Remove um streamer das notificações da Twitch",
        )
        @app_commands.guild_only()
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.describe(streamer="Nome de usuário da Twitch")
        async def stream_remove(
            interaction: discord.Interaction,
            streamer: app_commands.Range[str, 1, 25],
        ) -> None:
            guild = interaction.guild
            if guild is None:
                raise UserFacingError("Use este comando dentro de um servidor.")
            login = normalize_streamer_login(streamer)
            removed = self.database.remove_stream_notification(guild.id, login)
            message = (
                f"`{login}` foi removido das notificações."
                if removed
                else f"`{login}` não estava cadastrado."
            )
            await interaction.response.send_message(message, ephemeral=True)

        @tree.command(
            name="stream-list",
            description="Lista os streamers monitorados neste servidor",
        )
        @app_commands.guild_only()
        @app_commands.checks.has_permissions(manage_guild=True)
        async def stream_list(interaction: discord.Interaction) -> None:
            guild = interaction.guild
            if guild is None:
                raise UserFacingError("Use este comando dentro de um servidor.")
            registrations = self.database.get_stream_notifications(guild.id)
            if not registrations:
                await interaction.response.send_message(
                    "Nenhum streamer está cadastrado neste servidor.",
                    ephemeral=True,
                )
                return

            lines = [
                f"• **{row['streamer_login']}** → <#{row['channel_id']}>"
                for row in registrations
            ]
            chunks: list[str] = []
            current = ""
            for line in lines:
                candidate = f"{current}\n{line}" if current else line
                if len(candidate) > 1900:
                    chunks.append(current)
                    current = line
                else:
                    current = candidate
            if current:
                chunks.append(current)

            await interaction.response.send_message(
                f"**Streamers monitorados ({len(registrations)}):**\n{chunks[0]}",
                ephemeral=True,
            )
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=True)

        @tree.command(
            name="stream-test",
            description="Envia uma mensagem de teste de anúncio da Twitch",
        )
        @app_commands.guild_only()
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.describe(streamer="Nome de usuário da Twitch cadastrado")
        async def stream_test(
            interaction: discord.Interaction,
            streamer: app_commands.Range[str, 1, 25],
        ) -> None:
            guild = interaction.guild
            if guild is None:
                raise UserFacingError("Use este comando dentro de um servidor.")
            login = normalize_streamer_login(streamer)
            registration = self.database.get_stream_notification(guild.id, login)
            if registration is None:
                raise UserFacingError(
                    f"`{login}` não está cadastrado. Use `/stream-add` primeiro."
                )

            channel = await self._get_text_channel(registration["channel_id"])
            if channel is None:
                raise UserFacingError(
                    "O canal cadastrado não está disponível. Cadastre novamente "
                    "com `/stream-add`."
                )
            await self._check_channel_permissions(guild, channel)
            try:
                await channel.send(
                    embed=build_stream_announcement(login, test=True),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException) as error:
                raise UserFacingError(
                    "Não consegui enviar o teste. Verifique minhas permissões "
                    "no canal cadastrado."
                ) from error

            await interaction.response.send_message(
                f"Mensagem de teste enviada em {channel.mention}.",
                ephemeral=True,
            )


def register(
    tree: app_commands.CommandTree,
    client: commands.Bot,
    database: Database,
) -> StreamNotificationService:
    service = StreamNotificationService(client, database)
    service.register_commands(tree)
    return service
