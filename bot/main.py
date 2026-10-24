import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from bot.automod import AutoModerator
from bot.prefix_commands import register as register_prefix_commands
from bot.cogs import (
    channel_controls,
    configuration,
    general,
    global_cleanup,
    help as help_commands,
    moderation,
    music,
    stream_notifications,
)
from bot.checks import ModerationAccessDenied, UserFacingError
from bot.database import Database
from bot.logging_utils import (
    build_message_delete_embed,
    build_message_edit_embed,
    send_guild_log,
    send_guild_log_embed,
)

logger = logging.getLogger(__name__)


class ModerationClient(commands.Bot):
    def __init__(self, database: Database) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="e!", intents=intents, help_command=None)
        self.database = database


async def sync_guild_commands(
    client: ModerationClient, guild: discord.Guild
) -> None:
    client.tree.clear_commands(guild=guild)
    commands = await client.tree.sync(guild=guild)
    logger.info(
        "Registros slash locais antigos removidos do servidor %s (%s); "
        "%s comandos locais restantes.",
        guild.name,
        guild.id,
        len(commands),
    )


async def respond_to_bot_mention(
    message: discord.Message,
    bot_user: discord.ClientUser | discord.User | None,
) -> None:
    if (
        bot_user is None
        or message.guild is None
        or message.author.bot
        or bot_user not in message.mentions
    ):
        return

    response = (
        f"💀 | Olá {message.author.mention}! Meu prefixo neste servidor é `e!`. "
        "Para ver o que eu posso fazer, use `e!ajuda`!"
    )
    try:
        await message.channel.send(response)
    except (discord.Forbidden, discord.HTTPException):
        logger.exception(
            "Não foi possível responder à menção ao bot no servidor %s.",
            message.guild.id,
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN não foi definido. Copie .env.example para .env "
            "e configure o token do bot."
        )

    database = Database(Path(__file__).resolve().parent.parent / "data" / "moderation.db")
    client = ModerationClient(database)
    moderation.register(client.tree, database)
    configuration.register(client.tree, database)
    general.register(client.tree)
    help_commands.register(client.tree, database)
    channel_controls.register(client.tree, database)
    music.register(client.tree, client)
    stream_notification_service = stream_notifications.register(
        client.tree, client, database
    )
    register_prefix_commands(client)
    global_cleanup.register_prefix_command(client, database)
    automoderator = AutoModerator(database)
    synced = False

    @client.event
    async def on_ready() -> None:
        nonlocal synced
        if not synced:
            for guild in client.guilds:
                await sync_guild_commands(client, guild)
            global_commands = await client.tree.sync()
            logger.info(
                "%s comandos slash sincronizados globalmente: %s",
                len(global_commands),
                ", ".join(command.name for command in global_commands),
            )
            synced = True
        logger.info("Bot conectado como %s.", client.user)
        stream_notification_service.start()

    @client.event
    async def on_guild_join(guild: discord.Guild) -> None:
        if synced:
            await sync_guild_commands(client, guild)

    @client.event
    async def on_message(message: discord.Message) -> None:
        await automoderator.handle_message(message)
        await respond_to_bot_mention(message, client.user)
        await client.process_commands(message)

    @client.event
    async def on_command_error(
        context: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            await context.send(
                "Não reconheci esse comando. Use `e!ajuda` para ver a lista."
            )
            return
        if isinstance(error, commands.UserInputError):
            await context.send(
                f"Não consegui ler os argumentos. Confira o formato com `e!ajuda`. ({error})"
            )
            return
        logger.error(
            "Falha ao processar comando com prefixo %s.",
            context.command.qualified_name if context.command else "desconhecido",
            exc_info=(type(error), error, error.__traceback__),
        )
        await context.send("Ocorreu um erro ao executar esse comando.")

    async def find_recent_audit_entry(
        guild: discord.Guild,
        action: discord.AuditLogAction,
        *,
        target_id: int | None = None,
        channel_id: int | None = None,
    ) -> discord.AuditLogEntry | None:
        try:
            async for entry in guild.audit_logs(limit=10, action=action):
                if (
                    datetime.now(timezone.utc) - entry.created_at
                ).total_seconds() > 15:
                    continue
                if target_id is not None and (
                    entry.target is None or entry.target.id != target_id
                ):
                    continue
                if channel_id is not None:
                    audit_channel = getattr(
                        getattr(entry, "extra", None), "channel", None
                    )
                    if audit_channel is None or audit_channel.id != channel_id:
                        continue
                return entry
        except (discord.Forbidden, discord.HTTPException):
            logger.debug(
                "Não foi possível consultar a auditoria do servidor %s.",
                guild.id,
            )
        return None

    @client.event
    async def on_raw_message_delete(
        payload: discord.RawMessageDeleteEvent,
    ) -> None:
        if automoderator.consume_deleted_message_id(payload.message_id):
            return
        guild = client.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        message = payload.cached_message
        channel = guild.get_channel_or_thread(payload.channel_id)
        channel_text = (
            channel.mention if channel is not None else f"`{payload.channel_id}`"
        )
        await send_guild_log_embed(
            guild,
            database,
            build_message_delete_embed(
                message,
                channel_text,
                payload.message_id,
                occurred_at=datetime.now(timezone.utc),
            ),
        )

    @client.event
    async def on_bulk_message_delete(messages: list[discord.Message]) -> None:
        if not messages or messages[0].guild is None:
            return
        guild = messages[0].guild
        channel = messages[0].channel
        affected_users = {
            f"{message.author} (`{message.author.id}`)"
            for message in messages
            if not message.author.bot
        }
        audit_entry = await find_recent_audit_entry(
            guild,
            discord.AuditLogAction.message_delete,
            channel_id=channel.id,
        )
        await send_guild_log(
            guild,
            database,
            "Mensagens apagadas em lote",
            f"{len(messages)} mensagem(ns) em {channel.mention}",
            (
                audit_entry.reason
                if audit_entry is not None and audit_entry.reason
                else "Discord reportou uma exclusão em lote."
            ),
            moderator=(
                f"{audit_entry.user} (`{audit_entry.user.id}`)"
                if audit_entry is not None and audit_entry.user is not None
                else "Não identificado"
            ),
            moderator_id=(
                audit_entry.user.id
                if audit_entry is not None and audit_entry.user is not None
                else None
            ),
            details=(
                "Usuários afetados: "
                + ", ".join(sorted(affected_users))[:650]
                + "\nIDs das mensagens: "
                + ", ".join(str(message.id) for message in messages)[:200]
                if affected_users
                else (
                    "IDs das mensagens: "
                    + ", ".join(str(message.id) for message in messages)[:900]
                )
            ),
            occurred_at=(
                audit_entry.created_at if audit_entry is not None else None
            ),
            color=discord.Color.dark_grey(),
        )

    @client.event
    async def on_message_edit(
        before: discord.Message, after: discord.Message
    ) -> None:
        if (
            before.guild is None
            or before.author.bot
            or before.content == after.content
        ):
            return
        await send_guild_log_embed(
            before.guild,
            database,
            build_message_edit_embed(before, after),
        )

    @client.event
    async def on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot or before.channel == after.channel:
            return

        if before.channel is None and after.channel is not None:
            action = "Entrada em call"
            reason = f"{member} entrou em uma call."
            details = f"Canal: {after.channel.mention}"
            color = discord.Color.green()
        elif before.channel is not None and after.channel is None:
            action = "Saída de call"
            reason = f"{member} saiu de uma call."
            details = f"Canal: {before.channel.mention}"
            color = discord.Color.red()
        elif before.channel is not None and after.channel is not None:
            action = "Mudança de call"
            reason = f"{member} mudou de canal de voz."
            details = (
                f"De: {before.channel.mention}\n"
                f"Para: {after.channel.mention}"
            )
            color = discord.Color.blue()
        else:
            return

        await send_guild_log(
            member.guild,
            database,
            action,
            f"{member} (`{member.id}`)",
            reason,
            moderator="Evento de voz",
            details=details,
            color=color,
        )

    @client.event
    async def on_member_join(member: discord.Member) -> None:
        await send_guild_log(
            member.guild,
            database,
            "Entrada de membro",
            f"{member} (`{member.id}`)",
            "O membro entrou no servidor.",
            moderator="Evento do servidor",
            color=discord.Color.green(),
        )
        await automoderator.handle_member_join(member)

    @client.event
    async def on_member_remove(member: discord.Member) -> None:
        action = "Saída de membro"
        moderator = "Não identificado"
        reason = "O membro saiu do servidor."
        audit_entry = await find_recent_audit_entry(
            member.guild,
            discord.AuditLogAction.kick,
            target_id=member.id,
        )
        if audit_entry is not None:
            if client.user is not None and audit_entry.user_id == client.user.id:
                return
            action = "Expulsão"
            moderator = (
                str(audit_entry.user)
                if audit_entry.user is not None
                else "Não identificado"
            )
            reason = audit_entry.reason or "Motivo não informado"
        await send_guild_log(
            member.guild,
            database,
            action,
            f"{member} (`{member.id}`)",
            reason,
            moderator=moderator,
            moderator_id=audit_entry.user_id if audit_entry is not None else None,
            occurred_at=audit_entry.created_at if audit_entry is not None else None,
            color=discord.Color.orange(),
        )

    @client.event
    async def on_member_ban(guild: discord.Guild, user: discord.User) -> None:
        moderator = "Não identificado"
        reason = "Banimento registrado pelo Discord."
        audit_entry = await find_recent_audit_entry(
            guild,
            discord.AuditLogAction.ban,
            target_id=user.id,
        )
        if audit_entry is not None:
            if client.user is not None and audit_entry.user_id == client.user.id:
                return
            moderator = (
                str(audit_entry.user)
                if audit_entry.user is not None
                else "Não identificado"
            )
            reason = audit_entry.reason or reason
        await send_guild_log(
            guild,
            database,
            "Banimento",
            f"{user} (`{user.id}`)",
            reason,
            moderator=moderator,
            moderator_id=audit_entry.user_id if audit_entry is not None else None,
            occurred_at=audit_entry.created_at if audit_entry is not None else None,
            color=discord.Color.red(),
        )

    @client.event
    async def on_member_unban(guild: discord.Guild, user: discord.User) -> None:
        moderator = "Não identificado"
        reason = "Banimento removido no Discord."
        audit_entry = await find_recent_audit_entry(
            guild,
            discord.AuditLogAction.unban,
            target_id=user.id,
        )
        if audit_entry is not None:
            if client.user is not None and audit_entry.user_id == client.user.id:
                return
            moderator = (
                str(audit_entry.user)
                if audit_entry.user is not None
                else "Não identificado"
            )
            reason = audit_entry.reason or reason
        await send_guild_log(
            guild,
            database,
            "Unban",
            f"{user} (`{user.id}`)",
            reason,
            moderator=moderator,
            moderator_id=audit_entry.user_id if audit_entry is not None else None,
            occurred_at=audit_entry.created_at if audit_entry is not None else None,
            color=discord.Color.green(),
        )

    @client.event
    async def on_member_update(
        before: discord.Member, after: discord.Member
    ) -> None:
        if before.guild.id != after.guild.id:
            return
        changes: list[str] = []
        old_roles = {role.id: role for role in before.roles}
        new_roles = {role.id: role for role in after.roles}
        added = [role.mention for role_id, role in new_roles.items() if role_id not in old_roles]
        removed = [role.mention for role_id, role in old_roles.items() if role_id not in new_roles]
        timeout_audit_entry: discord.AuditLogEntry | None = None
        timeout_changed = (
            before.timed_out_until != after.timed_out_until
        )
        if timeout_changed:
            timeout_audit_entry = await find_recent_audit_entry(
                after.guild,
                discord.AuditLogAction.member_update,
                target_id=after.id,
            )
            if (
                timeout_audit_entry is not None
                and not (
                    hasattr(timeout_audit_entry.before, "timed_out_until")
                    or hasattr(timeout_audit_entry.after, "timed_out_until")
                )
            ):
                timeout_audit_entry = None
            if (
                timeout_audit_entry is not None
                and client.user is not None
                and timeout_audit_entry.user_id == client.user.id
            ):
                timeout_changed = False
        if added:
            changes.append("Cargos adicionados: " + ", ".join(added[:10]))
        if removed:
            changes.append("Cargos removidos: " + ", ".join(removed[:10]))
        if before.nick != after.nick:
            changes.append(f"Apelido: `{before.nick or before.name}` → `{after.nick or after.name}`")
        if timeout_changed:
            action = (
                "Timeout aplicado"
                if after.timed_out_until
                else "Timeout removido"
            )
            changes.append(f"{action}: {after.timed_out_until or 'sem timeout'}")
        if changes:
            await send_guild_log(
                after.guild,
                database,
                (
                    "Timeout atualizado"
                    if timeout_changed and len(changes) == 1
                    else "Atualização de membro"
                ),
                f"{after} (`{after.id}`)",
                (
                    timeout_audit_entry.reason or "Timeout alterado no servidor."
                    if timeout_changed and timeout_audit_entry is not None
                    else "Dados de membro alterados."
                ),
                moderator=(
                    f"{timeout_audit_entry.user} (`{timeout_audit_entry.user_id}`)"
                    if timeout_changed
                    and timeout_audit_entry is not None
                    and timeout_audit_entry.user is not None
                    else "Auditoria do servidor"
                ),
                moderator_id=(
                    timeout_audit_entry.user_id
                    if timeout_changed and timeout_audit_entry is not None
                    else None
                ),
                details="\n".join(changes),
                occurred_at=(
                    timeout_audit_entry.created_at
                    if timeout_changed and timeout_audit_entry is not None
                    else None
                ),
                color=discord.Color.blue(),
            )

    @client.event
    async def on_guild_update(
        before: discord.Guild, after: discord.Guild
    ) -> None:
        changes = []
        for attribute, label in (
            ("name", "Nome"),
            ("description", "Descrição"),
            ("verification_level", "Nível de verificação"),
            ("explicit_content_filter", "Filtro de conteúdo"),
            ("default_notifications", "Notificações padrão"),
        ):
            old_value = getattr(before, attribute)
            new_value = getattr(after, attribute)
            if old_value != new_value:
                changes.append(f"{label}: `{old_value}` → `{new_value}`")
        if changes:
            await send_guild_log(
                after,
                database,
                "Configuração do servidor alterada",
                after.name,
                "Uma configuração importante do servidor foi alterada.",
                moderator="Auditoria do servidor",
                details="\n".join(changes),
                color=discord.Color.blue(),
            )

    @client.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandInvokeError):
            cause = error.original
        else:
            cause = error

        if isinstance(cause, ModerationAccessDenied):
            message = str(cause)
        elif isinstance(cause, app_commands.MissingPermissions):
            message = "Você não tem as permissões necessárias para usar este comando."
        elif isinstance(cause, app_commands.BotMissingPermissions):
            message = "Não consigo executar esta ação: faltam permissões para o bot."
        elif isinstance(cause, UserFacingError):
            message = str(cause)
        elif isinstance(cause, discord.Forbidden):
            logger.warning("Ação proibida pela API do Discord: %s", cause)
            message = (
                "O Discord recusou a ação. Verifique minhas permissões e a hierarquia "
                "dos cargos."
            )
        else:
            logger.error(
                "Falha no comando slash %s.",
                interaction.command.name if interaction.command else "desconhecido",
                exc_info=(type(cause), cause, cause.__traceback__),
            )
            message = "Ocorreu um erro inesperado ao executar o comando."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    client.run(token)
