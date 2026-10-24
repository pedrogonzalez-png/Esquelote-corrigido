import discord
from discord import app_commands

from bot.checks import UserFacingError
from bot.database import Database


async def _set_log_channel(
    interaction: discord.Interaction,
    database: Database,
    channel: discord.TextChannel,
) -> None:
    guild = interaction.guild
    if guild is None:
        raise UserFacingError("Este comando só pode ser usado em um servidor.")
    if channel.guild.id != guild.id:
        raise UserFacingError("Escolha um canal de texto deste servidor.")
    bot_member = guild.me
    if bot_member is None:
        raise UserFacingError("Não consegui verificar minhas permissões neste servidor.")
    permissions = channel.permissions_for(bot_member)
    if not (
        permissions.view_channel
        and permissions.send_messages
        and permissions.embed_links
    ):
        raise UserFacingError(
            "Preciso poder ver o canal, enviar mensagens e incorporar links nele."
        )
    database.set_log_channel_id(guild.id, channel.id)
    await interaction.response.send_message(
        f"Canal de logs definido para {channel.mention}.", ephemeral=True
    )


def register(tree: app_commands.CommandTree, database: Database) -> None:
    configuration = app_commands.Group(
        name="configurar", description="Configura os recursos de moderação do servidor"
    )

    @tree.command(name="setlogs", description="Escolhe o canal de logs de moderação")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal de texto que receberá os logs")
    async def set_logs(
        interaction: discord.Interaction, canal: discord.TextChannel
    ) -> None:
        await _set_log_channel(interaction, database, canal)

    @configuration.command(name="logs", description="Define o canal de logs de moderação")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(canal="Canal de texto que receberá os logs")
    async def configure_logs(
        interaction: discord.Interaction, canal: discord.TextChannel
    ) -> None:
        await _set_log_channel(interaction, database, canal)

    @configuration.command(name="cargo", description="Autoriza um cargo a usar comandos de moderação")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(cargo="Cargo que receberá acesso aos comandos de moderação")
    async def add_moderator_role(
        interaction: discord.Interaction, cargo: discord.Role
    ) -> None:
        if cargo.is_default():
            raise UserFacingError(
                "O cargo @everyone não pode receber acesso aos comandos de moderação."
            )
        database.add_moderator_role(interaction.guild_id, cargo.id)
        await interaction.response.send_message(
            f"O cargo {cargo.mention} agora pode usar os comandos de moderação.",
            ephemeral=True,
        )

    @configuration.command(
        name="remover_cargo", description="Remove o acesso de moderação de um cargo"
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(cargo="Cargo que perderá acesso aos comandos de moderação")
    async def remove_moderator_role(
        interaction: discord.Interaction, cargo: discord.Role
    ) -> None:
        removed = database.remove_moderator_role(interaction.guild_id, cargo.id)
        message = (
            f"O cargo {cargo.mention} perdeu o acesso de moderação."
            if removed
            else f"O cargo {cargo.mention} não estava configurado."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @configuration.command(name="cargo_moderador", description="Configura um cargo de moderador")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(cargo="Cargo que poderá usar comandos de moderação")
    async def configure_moderator_role(
        interaction: discord.Interaction, cargo: discord.Role
    ) -> None:
        if cargo.is_default():
            raise UserFacingError("@everyone não pode ser configurado como moderador.")
        database.add_moderator_role(interaction.guild_id, cargo.id)
        await interaction.response.send_message(
            f"Cargo de moderador configurado: {cargo.mention}.", ephemeral=True
        )

    @configuration.command(name="mutado", description="Registra um cargo de mutado para referência")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(cargo="Cargo de mutado configurado no servidor")
    async def configure_muted_role(
        interaction: discord.Interaction, cargo: discord.Role
    ) -> None:
        if cargo.is_default():
            raise UserFacingError("@everyone não pode ser configurado como cargo de mutado.")
        database.set_muted_role_id(interaction.guild_id, cargo.id)
        await interaction.response.send_message(
            f"Cargo de mutado registrado: {cargo.mention}. "
            "Os timeouts continuam usando o recurso nativo do Discord.",
            ephemeral=True,
        )

    filter_choices = [
        app_commands.Choice(name="Anti-spam", value="anti_spam"),
        app_commands.Choice(name="Anti-flood", value="anti_flood"),
        app_commands.Choice(name="Anti-mention", value="anti_mention"),
        app_commands.Choice(name="Anti-link", value="anti_link"),
        app_commands.Choice(name="Palavras bloqueadas", value="blocked_words"),
        app_commands.Choice(name="Mensagens repetidas", value="anti_repeat"),
        app_commands.Choice(name="Proteção contra raid", value="anti_raid"),
    ]

    @configuration.command(name="filtro", description="Ativa ou desativa um filtro automático")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(filtro=filter_choices)
    @app_commands.describe(filtro="Filtro de moderação automática", ativo="Ativar ou desativar")
    async def configure_filter(
        interaction: discord.Interaction,
        filtro: app_commands.Choice[str],
        ativo: bool,
    ) -> None:
        database.set_auto_mod_setting(interaction.guild_id, filtro.value, ativo)
        state = "ativado" if ativo else "desativado"
        extra = (
            " A proteção contra raid apenas envia um alerta; não pune novos membros."
            if filtro.value == "anti_raid" and ativo
            else ""
        )
        await interaction.response.send_message(
            f"Filtro **{filtro.name}** {state}.{extra}", ephemeral=True
        )

    @configuration.command(name="palavra", description="Adiciona ou remove uma palavra bloqueada")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(
        acao=[
            app_commands.Choice(name="Adicionar", value="adicionar"),
            app_commands.Choice(name="Remover", value="remover"),
        ]
    )
    @app_commands.describe(acao="Ação a realizar", palavra="Palavra ou expressão a bloquear")
    async def configure_blocked_word(
        interaction: discord.Interaction,
        acao: app_commands.Choice[str],
        palavra: app_commands.Range[str, 1, 100],
    ) -> None:
        word = palavra.strip()
        if not word:
            raise UserFacingError("A palavra não pode estar vazia.")
        if acao.value == "adicionar":
            changed = database.add_blocked_word(interaction.guild_id, word)
            result = "adicionada" if changed else "já estava na lista"
        else:
            changed = database.remove_blocked_word(interaction.guild_id, word)
            result = "removida" if changed else "não estava na lista"
        await interaction.response.send_message(
            f"Palavra `{word}` {result}. A lista só é aplicada quando o filtro "
            "**Palavras bloqueadas** estiver ativado.",
            ephemeral=True,
        )

    @tree.command(name="setup", description="Mostra o estado da configuração de moderação")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction) -> None:
        settings = database.get_auto_mod_settings(interaction.guild_id)
        enabled = [
            label
            for key, label in (
                ("anti_spam", "Anti-spam"),
                ("anti_flood", "Anti-flood"),
                ("anti_mention", "Anti-mention"),
                ("anti_link", "Anti-link"),
                ("blocked_words", "Palavras bloqueadas"),
                ("anti_repeat", "Mensagens repetidas"),
                ("anti_raid", "Proteção contra raid"),
            )
            if settings[key]
        ]
        log_channel_id = database.get_log_channel_id(interaction.guild_id)
        log_channel = interaction.guild.get_channel(log_channel_id) if log_channel_id else None
        embed = discord.Embed(title="Configuração de moderação", color=discord.Color.blurple())
        embed.add_field(
            name="Canal de logs",
            value=log_channel.mention if isinstance(log_channel, discord.TextChannel) else "Não configurado",
            inline=False,
        )
        embed.add_field(
            name="Filtros ativos",
            value=", ".join(enabled) if enabled else "Nenhum (todos começam desativados)",
            inline=False,
        )
        embed.add_field(
            name="Punição por advertências",
            value="Timeout de 1 hora ao atingir 3 avisos; repete a cada 3 avisos.",
            inline=False,
        )
        embed.add_field(
            name="Comandos úteis",
            value=(
                "`/setlogs`, `/configurar logs`, `/configurar filtro`, `/configurar palavra`, "
                "`/configurar cargo_moderador`, `/configurar mutado`"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    tree.add_command(configuration)
