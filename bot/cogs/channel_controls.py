import discord
from discord import app_commands

from bot.checks import UserFacingError
from bot.database import Database
from bot.logging_utils import send_moderation_log


async def lock_channel(
    channel: discord.TextChannel, database: Database
) -> bool:
    guild = channel.guild
    default_role = guild.default_role
    if database.get_channel_lock(guild.id, channel.id) is not None:
        return False
    overwrite = channel.overwrites_for(default_role)
    database.save_channel_lock(guild.id, channel.id, overwrite.send_messages)
    overwrite.send_messages = False
    try:
        await channel.set_permissions(
            default_role, overwrite=overwrite, reason="Bloqueio de moderação"
        )
    except discord.HTTPException:
        database.remove_channel_lock(guild.id, channel.id)
        raise
    return True


async def unlock_channel(
    channel: discord.TextChannel, database: Database
) -> bool:
    row = database.get_channel_lock(channel.guild.id, channel.id)
    if row is None:
        return False
    overwrite = channel.overwrites_for(channel.guild.default_role)
    saved_value = row["send_messages"]
    overwrite.send_messages = None if saved_value is None else bool(saved_value)
    await channel.set_permissions(
        channel.guild.default_role,
        overwrite=overwrite,
        reason="Desbloqueio de moderação",
    )
    database.remove_channel_lock(channel.guild.id, channel.id)
    return True


def register(tree: app_commands.CommandTree, database: Database) -> None:
    @tree.command(name="slowmode", description="Define o modo lento do canal")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.describe(segundos="Intervalo entre mensagens: 0 a 21600 segundos")
    async def slowmode(
        interaction: discord.Interaction,
        segundos: app_commands.Range[int, 0, 21600],
    ) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            raise UserFacingError("Use este comando em um canal de texto.")
        await interaction.channel.edit(
            slowmode_delay=segundos,
            reason=f"Configurado por {interaction.user}",
        )
        await interaction.response.send_message(
            f"Modo lento definido para {segundos} segundo(s).", ephemeral=True
        )
        await send_moderation_log(
            interaction,
            "Modo lento alterado",
            interaction.channel.mention,
            f"Intervalo: {segundos} segundo(s).",
            color=discord.Color.blue(),
        )

    @tree.command(name="lock", description="Bloqueia o envio de mensagens neste canal")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def lock(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            raise UserFacingError("Use este comando em um canal de texto.")
        try:
            changed = await lock_channel(interaction.channel, database)
        except discord.HTTPException as error:
            raise UserFacingError(
                "Não tenho permissão para alterar as permissões deste canal."
            ) from error
        message = (
            f"{interaction.channel.mention} foi bloqueado."
            if changed
            else f"{interaction.channel.mention} já está bloqueado pelo bot."
        )
        await interaction.response.send_message(message, ephemeral=True)
        if changed:
            await send_moderation_log(
                interaction,
                "Canal bloqueado",
                interaction.channel.mention,
                "Envio de mensagens bloqueado para @everyone.",
                color=discord.Color.red(),
            )

    @tree.command(name="unlock", description="Restaura o envio de mensagens neste canal")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def unlock(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            raise UserFacingError("Use este comando em um canal de texto.")
        try:
            changed = await unlock_channel(interaction.channel, database)
        except discord.HTTPException as error:
            raise UserFacingError(
                "Não tenho permissão para alterar as permissões deste canal."
            ) from error
        message = (
            f"{interaction.channel.mention} foi desbloqueado."
            if changed
            else "Este canal não possui bloqueio registrado pelo bot."
        )
        await interaction.response.send_message(message, ephemeral=True)
        if changed:
            await send_moderation_log(
                interaction,
                "Canal desbloqueado",
                interaction.channel.mention,
                "Permissão de envio restaurada ao valor anterior.",
                color=discord.Color.green(),
            )

    @tree.command(name="lockdown", description="Bloqueia ou restaura todos os canais de texto")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.describe(ativar="Ative o lockdown ou restaure os bloqueios registrados")
    async def lockdown(
        interaction: discord.Interaction, ativar: bool
    ) -> None:
        guild = interaction.guild
        if guild is None:
            raise UserFacingError("Este comando só pode ser usado em um servidor.")
        await interaction.response.defer(ephemeral=True)

        changed = 0
        failed: list[str] = []
        if ativar:
            for channel in guild.text_channels:
                try:
                    if await lock_channel(channel, database):
                        changed += 1
                except discord.HTTPException:
                    failed.append(channel.name)
        else:
            channel_ids = {
                row["channel_id"] for row in database.get_channel_locks(guild.id)
            }
            for channel_id in channel_ids:
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel):
                    continue
                try:
                    if await unlock_channel(channel, database):
                        changed += 1
                except discord.HTTPException:
                    failed.append(channel.name)

        action = "ativado" if ativar else "desativado"
        message = f"Lockdown {action}: {changed} canal(is) alterado(s)."
        if failed:
            message += "\nNão foi possível alterar: " + ", ".join(failed[:20])
        await interaction.followup.send(message, ephemeral=True)
        await send_moderation_log(
            interaction,
            f"Lockdown {action}",
            guild.name,
            f"{changed} canal(is) alterado(s).",
            details=("Falhas: " + ", ".join(failed[:20])) if failed else None,
            color=discord.Color.red() if ativar else discord.Color.green(),
        )
