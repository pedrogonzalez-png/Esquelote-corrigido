import discord
from discord import app_commands

from bot.database import Database

GENERAL_CATEGORY = (
    "📌 Geral",
    "general",
    None,
    (
        ("/ajuda", "Abre esta central de comandos."),
        ("/userinfo [membro]", "Mostra informações de um membro do servidor."),
        ("/ping", "Mostra a latência atual do bot."),
    ),
)

HELP_CATEGORIES = (
    (
        "🎵 Música",
        "music",
        None,
        (
            ("/play <busca>", "Busca uma música pelo nome ou link e adiciona à fila."),
            ("/twitch <canal>", "Toca o áudio de uma live pública; informe o nome do canal ou o link."),
            ("/pause", "Pausa a música atual."),
            ("/resume", "Continua a reprodução pausada."),
            ("/skip", "Pula para a próxima faixa."),
            ("/stop", "Encerra a reprodução e limpa a fila."),
            ("/queue", "Mostra a fila e permite escolher a próxima faixa."),
            ("/leave", "Para a música, limpa a fila e desconecta o bot."),
        ),
    ),
    (
        "🛡️ Moderação",
        "moderation",
        "kick_members",
        (
            ("/warn <membro> <motivo>", "Registra uma advertência."),
            ("/avisos <membro>", "Consulta as advertências recentes."),
            ("/warnings <membro>", "Consulta as advertências recentes."),
            ("/clearwarns <membro>", "Limpa as advertências ativas."),
            ("/case <número>", "Consulta um caso de moderação."),
        ),
    ),
    (
        "🔨 Banimentos",
        "bans",
        "ban_members",
        (
            ("/ban <membro> [motivo]", "Bane um membro."),
            ("/unban <user_id> [motivo]", "Remove um banimento pelo ID."),
        ),
    ),
    (
        "👢 Expulsões",
        "kicks",
        "kick_members",
        (("/kick <membro> [motivo]", "Expulsa um membro do servidor."),),
    ),
    (
        "⏱️ Timeout",
        "timeout",
        "moderate_members",
        (
            ("/timeout <membro> <duração> [motivo]", "Aplica um timeout."),
            ("/untimeout <membro> [motivo]", "Remove o timeout."),
        ),
    ),
    (
        "🧹 Mensagens",
        "messages",
        "manage_messages",
        (
            ("/clear <quantidade>", "Apaga de 1 a 100 mensagens do canal."),
            (
                "e!clearglobal <message_id>",
                "Apaga a mensagem indicada e as cópias com texto exato do mesmo autor em todos os canais de texto e voz.",
            ),
        ),
    ),
    (
        "⚙️ Administração",
        "administration",
        "manage_guild",
        (
            ("/setlogs <canal>", "Escolhe o canal que receberá os logs de moderação."),
            ("/setup", "Mostra a configuração de moderação."),
            ("/configurar logs <canal>", "Define o canal de registros."),
            ("/configurar cargo <cargo>", "Dá acesso de moderação a um cargo."),
            ("/configurar remover_cargo <cargo>", "Remove esse acesso."),
            ("/configurar cargo_moderador <cargo>", "Define o cargo de moderador."),
            ("/configurar mutado <cargo>", "Registra o cargo de mutado."),
            ("/configurar filtro <filtro> <ativo>", "Ativa ou desativa um filtro."),
            ("/configurar palavra <ação> <palavra>", "Gerencia palavras bloqueadas."),
            ("/lockdown <ativar>", "Bloqueia ou libera os canais do servidor."),
        ),
    ),
    (
        "🔴 Notificações de lives",
        "stream-notifications",
        "manage_guild",
        (
            ("/stream-add <streamer> <canal>", "Cadastra um streamer e o canal de anúncio da Twitch."),
            ("/stream-remove <streamer>", "Remove um streamer monitorado da Twitch."),
            ("/stream-list", "Lista os streamers monitorados neste servidor."),
            ("/stream-test <streamer>", "Envia um teste de anúncio da Twitch."),
        ),
    ),
    (
        "🔧 Canais",
        "channels",
        "manage_channels",
        (
            ("/slowmode <segundos>", "Define o intervalo entre mensagens."),
            ("/lock", "Bloqueia o canal atual."),
            ("/unlock", "Libera o canal novamente."),
        ),
    ),
)


def _available_categories(
    permissions: discord.Permissions,
    has_moderator_role: bool,
) -> tuple[tuple[str, str, str | None, tuple[tuple[str, str], ...]], ...]:
    categories = [GENERAL_CATEGORY]
    for category in HELP_CATEGORIES:
        _, key, permission, _ = category
        is_administrator = permissions.administrator
        has_native_permission = bool(permission and getattr(permissions, permission))
        role_can_moderate = (
            has_moderator_role
            and permission not in {"manage_guild", "manage_channels", None}
        )
        if (
            key == "music"
            or is_administrator
            or has_native_permission
            or role_can_moderate
        ):
            categories.append(category)
    return tuple(categories)


def _prefix_forms(command: str) -> str:
    if command.startswith("/resume"):
        return "e!resume / e!despause"
    return command.replace("/", "e!", 1)


def _format_command(command: str, description: str) -> str:
    if command.startswith("e!"):
        return f"**`{command}`** — {description}"
    if command.partition(" ")[0] in {
        "/stream-add",
        "/stream-remove",
        "/stream-list",
        "/stream-test",
    }:
        return f"**`{command}`** — {description}"
    return (
        f"**`{command}`** · **`{_prefix_forms(command)}`** — "
        f"{description}"
    )


def _add_command_fields(
    embed: discord.Embed,
    commands: tuple[tuple[str, str], ...],
) -> None:
    fields: list[str] = []
    current_lines: list[str] = []
    current_length = 0
    for command, description in commands:
        line = _format_command(command, description)
        line_length = len(line) + (1 if current_lines else 0)
        if current_lines and current_length + line_length > 1024:
            fields.append("\n".join(current_lines))
            current_lines = []
            current_length = 0
            line_length = len(line)
        current_lines.append(line)
        current_length += line_length

    if current_lines:
        fields.append("\n".join(current_lines))

    for index, value in enumerate(fields):
        name = "Comandos" if index == 0 else f"Comandos (continuação {index + 1})"
        embed.add_field(name=name, value=value, inline=False)


def build_help_embed(
    permissions: discord.Permissions,
    has_moderator_role: bool,
    category_key: str | None = None,
) -> discord.Embed:
    available_categories = _available_categories(permissions, has_moderator_role)
    embed = discord.Embed(
        title="📚 Central de Ajuda",
        description=(
            "Escolha uma categoria para consultar os comandos. "
            "Você pode usar os comandos com `/` ou com o prefixo `e!`."
        ),
        color=discord.Color.blurple(),
    )

    if category_key is None:
        category_names = "\n".join(
            f"• {category[0]}" for category in available_categories
        )
        embed.add_field(name="Categorias disponíveis", value=category_names, inline=False)
    else:
        selected_category = next(
            (category for category in available_categories if category[1] == category_key),
            None,
        )
        if selected_category is None:
            embed.description = "Essa categoria não está disponível para você."
        else:
            category_name, _, _, category_commands = selected_category
            embed.description = f"Comandos da categoria **{category_name}**."
            _add_command_fields(embed, category_commands)

    embed.set_footer(text="As categorias de moderação dependem das suas permissões.")
    return embed


class HelpCategorySelect(discord.ui.Select):
    def __init__(
        self,
        permissions: discord.Permissions,
        has_moderator_role: bool,
        selected_category: str | None,
    ) -> None:
        categories = _available_categories(permissions, has_moderator_role)
        options = [
            discord.SelectOption(
                label="Visão geral",
                value="overview",
                description="Veja as categorias disponíveis.",
                default=selected_category is None,
            )
        ]
        options.extend(
            discord.SelectOption(
                label=category[0],
                value=category[1],
                description=f"Consultar comandos de {category[0].split(' ', 1)[-1].lower()}.",
                default=category[1] == selected_category,
            )
            for category in categories
            if category[1] != "general"
        )
        super().__init__(
            placeholder="Selecione uma categoria de comandos",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.permissions = permissions
        self.has_moderator_role = has_moderator_role

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0]
        category_key = None if selected == "overview" else selected
        embed = build_help_embed(
            self.permissions,
            self.has_moderator_role,
            category_key,
        )
        await interaction.response.edit_message(
            embed=embed,
            view=HelpCategoryView(
                self.permissions,
                self.has_moderator_role,
                interaction.user.id,
                category_key,
            ),
        )


class HelpCategoryView(discord.ui.View):
    def __init__(
        self,
        permissions: discord.Permissions,
        has_moderator_role: bool,
        owner_id: int,
        selected_category: str | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.add_item(
            HelpCategorySelect(
                permissions,
                has_moderator_role,
                selected_category,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Este menu pertence a quem executou `/ajuda`. Use o comando para abrir o seu.",
            ephemeral=True,
        )
        return False


def register(tree: app_commands.CommandTree, database: Database) -> None:
    @tree.command(
        name="ajuda",
        description="Veja os comandos disponíveis para você",
    )
    @app_commands.guild_only()
    async def help_command(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Não consegui conferir suas permissões neste servidor.",
                ephemeral=True,
            )
            return

        has_moderator_role = database.has_moderator_role(
            interaction.guild_id,
            {role.id for role in interaction.user.roles},
        )
        permissions = interaction.user.guild_permissions
        embed = build_help_embed(permissions, has_moderator_role)
        await interaction.response.send_message(
            embed=embed,
            view=HelpCategoryView(
                permissions,
                has_moderator_role,
                interaction.user.id,
            ),
        )
