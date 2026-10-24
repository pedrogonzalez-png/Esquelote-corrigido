import logging
from datetime import datetime, timezone

import discord

from bot.database import Database

logger = logging.getLogger(__name__)


def _add_event_timestamp(embed: discord.Embed, occurred_at: datetime) -> None:
    embed.timestamp = occurred_at
    embed.add_field(
        name="Data e hora",
        value=discord.utils.format_dt(occurred_at, style="F"),
        inline=False,
    )


def build_message_edit_embed(
    before: discord.Message,
    after: discord.Message,
) -> discord.Embed:
    embed = discord.Embed(color=discord.Color.blue())
    occurred_at = after.edited_at or datetime.now(timezone.utc)
    embed.set_author(
        name=f"{before.author} editou uma mensagem de texto",
        icon_url=before.author.display_avatar.url,
    )
    _add_event_timestamp(embed, occurred_at)
    embed.add_field(
        name="Canal de texto",
        value=before.channel.mention,
        inline=False,
    )
    embed.add_field(
        name="Antiga mensagem",
        value=_message_code_block(before.content),
        inline=False,
    )
    embed.add_field(
        name="Nova mensagem",
        value=_message_code_block(after.content),
        inline=False,
    )
    embed.set_footer(text=f"ID do usuário: {before.author.id}")

    image = next(
        (
            attachment
            for attachment in after.attachments
            if attachment.content_type
            and attachment.content_type.startswith("image/")
        ),
        None,
    )
    if image is not None:
        embed.set_image(url=image.url)
    return embed


def build_message_delete_embed(
    message: discord.Message | None,
    channel_text: str,
    message_id: int,
    *,
    occurred_at: datetime,
) -> discord.Embed:
    embed = discord.Embed(color=discord.Color.dark_grey())
    if message is not None:
        embed.set_author(
            name=f"{message.author} teve uma mensagem apagada",
            icon_url=message.author.display_avatar.url,
        )
        user_id = message.author.id
        content = message.content
        attachments = message.attachments
    else:
        embed.title = "Mensagem apagada"
        user_id = None
        content = ""
        attachments = []

    _add_event_timestamp(embed, occurred_at)
    embed.add_field(name="Canal de texto", value=channel_text, inline=False)
    embed.add_field(
        name="Mensagem apagada",
        value=_message_code_block(content)
        if message is not None
        else "Conteúdo indisponível (mensagem não estava no cache do bot).",
        inline=False,
    )
    attachment_lines = [
        f"{attachment.filename}: {attachment.url}"
        for attachment in attachments
    ]
    if attachment_lines:
        attachment_text = ""
        for line in attachment_lines:
            next_line = f"{attachment_text}\n{line}" if attachment_text else line
            if len(next_line) > 1024:
                break
            attachment_text = next_line
        embed.add_field(
            name="Anexos da mensagem",
            value=attachment_text or "Anexo disponível, mas o link excede o limite.",
            inline=False,
        )

    footer = f"ID da mensagem: {message_id}"
    if user_id is not None:
        footer = f"ID do usuário: {user_id} • {footer}"
    embed.set_footer(text=footer)
    return embed


def _message_code_block(content: str) -> str:
    safe_content = (content or "[sem texto]").replace("```", "ˋˋˋ")
    return f"```text\n{safe_content[:990]}\n```"


async def send_moderation_log(
    interaction: discord.Interaction,
    action: str,
    target: str,
    reason: str,
    *,
    color: discord.Color = discord.Color.orange(),
    details: str | None = None,
) -> bool:
    guild = interaction.guild
    database = getattr(interaction.client, "database")
    if guild is None:
        return False

    return await send_guild_log(
        guild,
        database,
        action,
        target,
        reason,
        moderator=f"{interaction.user.mention} (`{interaction.user.id}`)",
        moderator_id=interaction.user.id,
        color=color,
        details=details,
    )


async def send_guild_log(
    guild: discord.Guild,
    database: Database,
    action: str,
    target: str,
    reason: str,
    *,
    moderator: str | None = None,
    moderator_id: int | None = None,
    color: discord.Color = discord.Color.orange(),
    details: str | None = None,
    occurred_at: datetime | None = None,
) -> bool:
    embed = discord.Embed(title=f"Moderação: {action}", color=color)
    _add_event_timestamp(embed, occurred_at or datetime.now(timezone.utc))
    embed.add_field(name="Alvo", value=target, inline=True)
    if moderator:
        moderator_value = moderator
        if moderator_id is not None and f"`{moderator_id}`" not in moderator:
            moderator_value += f" (`{moderator_id}`)"
        embed.add_field(name="Moderador", value=moderator_value, inline=True)
    embed.add_field(name="Motivo", value=reason[:1024], inline=False)
    if details:
        embed.add_field(name="Detalhes", value=details[:1024], inline=False)

    return await send_guild_log_embed(guild, database, embed)


async def send_guild_log_embed(
    guild: discord.Guild,
    database: Database,
    embed: discord.Embed,
) -> bool:
    channel_id = database.get_log_channel_id(guild.id)
    if channel_id is None:
        logger.debug("Canal de logs não configurado no servidor %s.", guild.id)
        return False

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Não foi possível acessar o canal de logs %s do servidor %s.",
                channel_id,
                guild.id,
            )
            return False

    if not isinstance(channel, discord.TextChannel):
        logger.error("O canal de logs %s não é um canal de texto.", channel_id)
        return False

    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        logger.exception(
            "Não foi possível enviar um log ao canal %s do servidor %s.",
            channel_id,
            guild.id,
        )
        return False
    return True
