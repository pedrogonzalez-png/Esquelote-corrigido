import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

from bot.checks import UserFacingError, ensure_moderatable, has_moderation_access
from bot.database import Database
from bot.logging_utils import send_moderation_log

logger = logging.getLogger(__name__)
DEFAULT_REASON = "Não informado"


def _reason(reason: str | None) -> str:
    normalized = (reason or "").strip()
    return normalized[:500] or DEFAULT_REASON


def _log_notice(sent: bool) -> str:
    if sent:
        return ""
    return " A ação foi aplicada, mas o log não foi enviado; confira a configuração e as permissões do canal."


def _parse_timeout_duration(value: str) -> timedelta:
    match = re.fullmatch(r"([1-9]\d{0,6})([smhd])", value.strip().lower())
    if match is None:
        raise UserFacingError(
            "Duração inválida. Use um número seguido de `s`, `m`, `h` ou `d` (ex.: `30m`)."
        )

    amount = int(match.group(1))
    unit = match.group(2)
    seconds_per_unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    duration = timedelta(seconds=amount * seconds_per_unit[unit])
    if duration > timedelta(days=28):
        raise UserFacingError("O timeout não pode ultrapassar 28 dias.")
    return duration


def register(tree: app_commands.CommandTree, database: Database) -> None:
    @tree.command(name="kick", description="Expulsa um membro do servidor")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(kick_members=True)
    @has_moderation_access(database, "kick_members")
    @app_commands.describe(membro="Membro a expulsar", motivo="Motivo da expulsão")
    async def kick(
        interaction: discord.Interaction,
        membro: discord.Member,
        motivo: str | None = None,
    ) -> None:
        ensure_moderatable(interaction, membro)
        reason = _reason(motivo)
        await interaction.response.defer(ephemeral=True)
        await membro.kick(reason=f"{interaction.user} | {reason}")
        case_id = database.add_case(
            interaction.guild_id, membro.id, interaction.user.id, "kick", reason
        )
        logged = await send_moderation_log(
            interaction, "Expulsão", f"{membro} (`{membro.id}`)", reason,
            color=discord.Color.orange(),
        )
        await interaction.followup.send(
            f"{membro.mention} foi expulso (caso #{case_id}). Motivo: {reason}."
            f"{_log_notice(logged)}",
            ephemeral=True,
        )

    @tree.command(name="ban", description="Bane um membro do servidor")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @has_moderation_access(database, "ban_members")
    @app_commands.describe(membro="Membro a banir", motivo="Motivo do banimento")
    async def ban(
        interaction: discord.Interaction,
        membro: discord.Member,
        motivo: str | None = None,
    ) -> None:
        ensure_moderatable(interaction, membro)
        reason = _reason(motivo)
        await interaction.response.defer(ephemeral=True)
        await membro.ban(reason=f"{interaction.user} | {reason}")
        case_id = database.add_case(
            interaction.guild_id, membro.id, interaction.user.id, "ban", reason
        )
        logged = await send_moderation_log(
            interaction, "Banimento", f"{membro} (`{membro.id}`)", reason,
            color=discord.Color.red(),
        )
        await interaction.followup.send(
            f"{membro.mention} foi banido (caso #{case_id}). Motivo: {reason}."
            f"{_log_notice(logged)}",
            ephemeral=True,
        )

    @tree.command(name="unban", description="Remove o banimento de um usuário")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @has_moderation_access(database, "ban_members")
    @app_commands.describe(user_id="ID numérico do usuário", motivo="Motivo do unban")
    async def unban(
        interaction: discord.Interaction, user_id: str, motivo: str | None = None
    ) -> None:
        if not user_id.isdecimal():
            raise UserFacingError("O ID do usuário deve conter apenas números.")
        user = discord.Object(id=int(user_id))
        reason = _reason(motivo)
        await interaction.response.defer(ephemeral=True)
        await interaction.guild.unban(user, reason=f"{interaction.user} | {reason}")
        case_id = database.add_case(
            interaction.guild_id, int(user_id), interaction.user.id, "unban", reason
        )
        logged = await send_moderation_log(
            interaction, "Unban", f"ID `{user_id}`", reason, color=discord.Color.green()
        )
        await interaction.followup.send(
            f"O banimento do usuário `{user_id}` foi removido (caso #{case_id})."
            f"{_log_notice(logged)}",
            ephemeral=True,
        )

    @tree.command(name="timeout", description="Aplica timeout a um membro")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @has_moderation_access(database, "moderate_members")
    @app_commands.describe(
        membro="Membro que receberá timeout",
        duracao="Duração: número seguido de s, m, h ou d (ex.: 30m)",
        motivo="Motivo do timeout",
    )
    async def timeout(
        interaction: discord.Interaction,
        membro: discord.Member,
        duracao: str,
        motivo: str | None = None,
    ) -> None:
        ensure_moderatable(interaction, membro)
        duration = _parse_timeout_duration(duracao)
        reason = _reason(motivo)
        await interaction.response.defer(ephemeral=True)
        await membro.timeout(duration, reason=f"{interaction.user} | {reason}")
        case_id = database.add_case(
            interaction.guild_id,
            membro.id,
            interaction.user.id,
            "timeout",
            reason,
            f"Duração: {duracao}",
        )
        logged = await send_moderation_log(
            interaction,
            "Timeout",
            f"{membro} (`{membro.id}`)",
            reason,
            details=f"Duração: {duracao}",
            color=discord.Color.orange(),
        )
        await interaction.followup.send(
            f"{membro.mention} recebeu timeout por `{duracao}`. "
            f"Caso #{case_id}. Motivo: {reason}.{_log_notice(logged)}",
            ephemeral=True,
        )

    @tree.command(name="untimeout", description="Remove o timeout de um membro")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @has_moderation_access(database, "moderate_members")
    @app_commands.describe(membro="Membro que terá o timeout removido", motivo="Motivo")
    async def untimeout(
        interaction: discord.Interaction,
        membro: discord.Member,
        motivo: str | None = None,
    ) -> None:
        ensure_moderatable(interaction, membro)
        reason = _reason(motivo)
        await interaction.response.defer(ephemeral=True)
        await membro.timeout(None, reason=f"{interaction.user} | {reason}")
        case_id = database.add_case(
            interaction.guild_id, membro.id, interaction.user.id, "untimeout", reason
        )
        logged = await send_moderation_log(
            interaction, "Remoção de timeout", f"{membro} (`{membro.id}`)", reason,
            color=discord.Color.green(),
        )
        await interaction.followup.send(
            f"O timeout de {membro.mention} foi removido (caso #{case_id})."
            f"{_log_notice(logged)}",
            ephemeral=True,
        )

    @tree.command(name="warn", description="Registra um aviso para um membro")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @has_moderation_access(database, "kick_members")
    @app_commands.describe(membro="Membro que receberá o aviso", motivo="Motivo do aviso")
    async def warn(
        interaction: discord.Interaction,
        membro: discord.Member,
        motivo: str,
    ) -> None:
        ensure_moderatable(interaction, membro)
        reason = _reason(motivo)
        await interaction.response.defer(ephemeral=True)
        warning_id = database.add_warning(
            interaction.guild_id, membro.id, interaction.user.id, reason
        )
        warning_count = database.count_warnings(interaction.guild_id, membro.id)
        case_id = database.add_case(
            interaction.guild_id,
            membro.id,
            interaction.user.id,
            "warn",
            reason,
            f"Aviso #{warning_id}; total de avisos: {warning_count}",
        )
        automatic_timeout = warning_count % 3 == 0
        automatic_case_id: int | None = None
        if automatic_timeout:
            await membro.timeout(
                timedelta(hours=1),
                reason=f"Timeout automático após {warning_count} advertências",
            )
            automatic_case_id = database.add_case(
                interaction.guild_id,
                membro.id,
                interaction.user.id,
                "auto_timeout",
                "Limite de 3 advertências atingido",
                "Duração: 1 hora",
            )
        logged = await send_moderation_log(
            interaction,
            "Aviso",
            f"{membro} (`{membro.id}`)",
            reason,
            details=f"Aviso #{warning_id}",
            color=discord.Color.gold(),
        )
        if automatic_timeout:
            logged = (
                await send_moderation_log(
                    interaction,
                    "Timeout automático",
                    f"{membro} (`{membro.id}`)",
                    "Limite de 3 advertências atingido",
                    details="Duração: 1 hora",
                    color=discord.Color.orange(),
                )
            ) and logged
        logger.info(
            "Aviso #%s/caso #%s para usuário %s por moderador %s; total: %s.",
            warning_id,
            case_id,
            membro.id,
            interaction.user.id,
            warning_count,
        )
        punishment_message = (
            f" Atingiu 3 avisos e recebeu timeout automático de 1 hora "
            f"(caso #{automatic_case_id})."
            if automatic_timeout
            else f" Total de avisos: {warning_count}."
        )
        await interaction.followup.send(
            f"{membro.mention} recebeu um aviso (#{warning_id}, caso #{case_id})."
            f"{punishment_message}"
            f"{_log_notice(logged)}",
            ephemeral=True,
        )

    async def show_warnings(
        interaction: discord.Interaction, membro: discord.Member
    ) -> None:
        rows = database.get_warnings(interaction.guild_id, membro.id)
        if not rows:
            message = f"{membro.mention} não possui avisos registrados."
        else:
            entries = [
                f"**Aviso #{row['id']}** — {row['reason']} (moderador <@{row['moderator_id']}>, "
                f"<t:{int(datetime.fromisoformat(row['created_at']).replace(tzinfo=timezone.utc).timestamp())}:f>)"
                for row in rows
            ]
            message = (
                f"**Últimos {len(rows)} avisos de {membro.mention}:**\n"
                + "\n".join(entries)
            )
        await interaction.response.send_message(message[:2000], ephemeral=True)

    @tree.command(name="avisos", description="Mostra o histórico recente de avisos")
    @app_commands.guild_only()
    @has_moderation_access(database, "kick_members")
    @app_commands.describe(membro="Membro cujos avisos serão consultados")
    async def warnings_pt(
        interaction: discord.Interaction, membro: discord.Member
    ) -> None:
        await show_warnings(interaction, membro)

    @tree.command(name="warnings", description="Mostra o histórico recente de avisos")
    @app_commands.guild_only()
    @has_moderation_access(database, "kick_members")
    @app_commands.describe(membro="Membro cujos avisos serão consultados")
    async def warnings_en(
        interaction: discord.Interaction, membro: discord.Member
    ) -> None:
        await show_warnings(interaction, membro)

    @tree.command(name="clearwarns", description="Remove os avisos ativos de um membro")
    @app_commands.guild_only()
    @has_moderation_access(database, "kick_members")
    @app_commands.describe(membro="Membro cujos avisos serão removidos")
    async def clear_warnings(
        interaction: discord.Interaction, membro: discord.Member
    ) -> None:
        ensure_moderatable(interaction, membro)
        await interaction.response.defer(ephemeral=True)
        removed = database.clear_warnings(interaction.guild_id, membro.id)
        case_id = database.add_case(
            interaction.guild_id,
            membro.id,
            interaction.user.id,
            "clearwarns",
            f"{removed} aviso(s) ativo(s) removido(s)",
        )
        logged = await send_moderation_log(
            interaction,
            "Avisos limpos",
            f"{membro} (`{membro.id}`)",
            f"{removed} aviso(s) ativo(s) removido(s)",
            details=f"Caso #{case_id}",
            color=discord.Color.blue(),
        )
        await interaction.followup.send(
            f"{removed} aviso(s) removido(s) de {membro.mention}. "
            f"Os casos registrados permanecem no histórico (caso #{case_id})."
            f"{_log_notice(logged)}",
            ephemeral=True,
        )

    @tree.command(name="case", description="Consulta os detalhes de um caso de moderação")
    @app_commands.guild_only()
    @has_moderation_access(database, "kick_members")
    @app_commands.describe(caso="Número do caso")
    async def moderation_case(
        interaction: discord.Interaction, caso: app_commands.Range[int, 1, 2**31 - 1]
    ) -> None:
        row = database.get_case(interaction.guild_id, caso)
        if row is None:
            await interaction.response.send_message(
                f"O caso #{caso} não foi encontrado neste servidor.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"Caso #{row['id']} — {row['action']}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Usuário", value=f"<@{row['user_id']}> (`{row['user_id']}`)")
        embed.add_field(name="Moderador", value=f"<@{row['moderator_id']}>")
        embed.add_field(name="Motivo", value=row["reason"][:1024], inline=False)
        if row["details"]:
            embed.add_field(name="Detalhes", value=row["details"][:1024], inline=False)
        embed.timestamp = datetime.fromisoformat(row["created_at"]).replace(
            tzinfo=timezone.utc
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="clear", description="Apaga mensagens recentes do canal")
    @app_commands.guild_only()
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    @has_moderation_access(database, "manage_messages")
    @app_commands.describe(quantidade="Quantidade de mensagens a apagar (1 a 100)")
    async def clear(
        interaction: discord.Interaction,
        quantidade: app_commands.Range[int, 1, 100],
    ) -> None:
        channel = interaction.channel
        guild = interaction.guild
        if guild is None or not isinstance(channel, discord.TextChannel):
            raise UserFacingError("Este comando só pode ser usado em um canal de texto.")

        bot_member = guild.me
        if bot_member is None:
            raise UserFacingError("Não consegui verificar as permissões do bot neste servidor.")
        bot_permissions = channel.permissions_for(bot_member)
        if not bot_permissions.manage_messages or not bot_permissions.read_message_history:
            raise UserFacingError(
                "Preciso das permissões **Gerenciar mensagens** e **Ver histórico "
                "de mensagens** neste canal."
            )

        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await channel.purge(limit=quantidade, bulk=True)
        except discord.Forbidden as error:
            logger.warning(
                "Discord negou /clear no canal %s do servidor %s.",
                channel.id,
                guild.id,
            )
            raise UserFacingError(
                "O Discord não permitiu apagar mensagens. Confira minhas permissões "
                "neste canal."
            ) from error

        logger.info(
            "/clear executado por %s no canal %s do servidor %s: %s mensagem(ns) removida(s).",
            interaction.user.id,
            channel.id,
            guild.id,
            len(deleted),
        )
        logged = await send_moderation_log(
            interaction,
            "Limpeza de mensagens",
            channel.mention,
            f"{len(deleted)} mensagem(ns) removida(s).",
            details=f"Solicitadas: {quantidade}",
            color=discord.Color.blue(),
        )
        await interaction.followup.send(
            f"{len(deleted)} mensagem(ns) apagada(s).{_log_notice(logged)}",
            ephemeral=True,
        )
