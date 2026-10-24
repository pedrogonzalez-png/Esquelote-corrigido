import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.checks import ModerationAccessDenied, UserFacingError

logger = logging.getLogger(__name__)


class PrefixResponse:
    def __init__(self, ctx: commands.Context) -> None:
        self.ctx = ctx
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **_: Any) -> None:
        self._done = True

    async def send_message(
        self,
        content: str | None = None,
        **kwargs: Any,
    ) -> discord.Message:
        self._done = True
        kwargs.pop("ephemeral", None)
        return await self.ctx.send(content, **kwargs)

    async def edit_message(self, **kwargs: Any) -> discord.Message:
        self._done = True
        return await self.ctx.send(**kwargs)


class PrefixFollowup:
    def __init__(self, ctx: commands.Context) -> None:
        self.ctx = ctx

    async def send(
        self,
        content: str | None = None,
        **kwargs: Any,
    ) -> discord.Message:
        kwargs.pop("ephemeral", None)
        return await self.ctx.send(content, **kwargs)


class PrefixInteraction:
    def __init__(self, ctx: commands.Context) -> None:
        self.guild = ctx.guild
        self.guild_id = ctx.guild.id if ctx.guild else None
        self.user = ctx.author
        self.channel = ctx.channel
        self.client = ctx.bot
        self.response = PrefixResponse(ctx)
        self.followup = PrefixFollowup(ctx)
        self.command: app_commands.Command[Any, ..., Any] | None = None

    @property
    def permissions(self) -> discord.Permissions:
        if isinstance(self.user, discord.Member):
            return self.user.guild_permissions
        return discord.Permissions.none()

    @property
    def app_permissions(self) -> discord.Permissions:
        if (
            self.guild is not None
            and self.guild.me is not None
            and isinstance(self.channel, discord.abc.GuildChannel)
        ):
            return self.channel.permissions_for(self.guild.me)
        return discord.Permissions.none()


def _split_arguments(raw_arguments: str) -> list[tuple[str, int, int]]:
    return [
        (match.group(), match.start(), match.end())
        for match in re.finditer(r"\S+", raw_arguments)
    ]


def _parse_boolean(value: str) -> bool:
    normalized = value.casefold()
    if normalized in {"true", "sim", "s", "yes", "y", "on", "ativar", "ativo"}:
        return True
    if normalized in {"false", "não", "nao", "n", "no", "off", "desativar", "inativo"}:
        return False
    raise UserFacingError("Use `ativar` ou `desativar` para esta opção.")


async def _convert_argument(
    ctx: commands.Context,
    parameter: app_commands.Parameter,
    raw_value: str,
) -> Any:
    value = raw_value.strip()
    choices = parameter.choices
    if choices:
        for choice in choices:
            if value.casefold() in {
                choice.name.casefold(),
                str(choice.value).casefold(),
            }:
                return choice
        options = ", ".join(f"`{choice.name}`" for choice in choices)
        raise UserFacingError(f"Opção inválida. Escolha uma destas: {options}.")

    option_type = parameter.type.name
    if option_type == "user":
        return await commands.MemberConverter().convert(ctx, value)
    if option_type == "role":
        return await commands.RoleConverter().convert(ctx, value)
    if option_type == "channel":
        return await commands.TextChannelConverter().convert(ctx, value)
    if option_type == "integer":
        try:
            converted = int(value)
        except ValueError as error:
            raise UserFacingError(f"`{value}` precisa ser um número inteiro.") from error
        if parameter.min_value is not None and converted < parameter.min_value:
            raise UserFacingError(
                f"`{parameter.name}` precisa ser no mínimo {parameter.min_value}."
            )
        if parameter.max_value is not None and converted > parameter.max_value:
            raise UserFacingError(
                f"`{parameter.name}` não pode ser maior que {parameter.max_value}."
            )
        return converted
    if option_type == "boolean":
        return _parse_boolean(value)
    if option_type == "string":
        if parameter.min_value is not None and len(value) < parameter.min_value:
            raise UserFacingError(
                f"`{parameter.name}` precisa ter pelo menos {parameter.min_value} caractere(s)."
            )
        if parameter.max_value is not None and len(value) > parameter.max_value:
            raise UserFacingError(
                f"`{parameter.name}` pode ter no máximo {parameter.max_value} caractere(s)."
            )
    return value


async def _parse_parameters(
    ctx: commands.Context,
    command: app_commands.Command[Any, ..., Any],
    raw_arguments: str,
) -> dict[str, Any]:
    tokens = _split_arguments(raw_arguments)
    values: dict[str, Any] = {}
    cursor = 0

    for index, parameter in enumerate(command.parameters):
        is_last_string = (
            index == len(command.parameters) - 1
            and parameter.type.name == "string"
        )
        if is_last_string:
            remaining = raw_arguments[tokens[cursor][1] :].strip() if cursor < len(tokens) else ""
            if not remaining:
                if parameter.required:
                    raise UserFacingError(
                        f"Faltou preencher o argumento `{parameter.name}`."
                    )
                continue
            if len(remaining) >= 2 and remaining[0] == remaining[-1] and remaining[0] in {"'", '"'}:
                remaining = remaining[1:-1]
            values[parameter.name] = await _convert_argument(
                ctx,
                parameter,
                remaining,
            )
            cursor = len(tokens)
            continue

        if cursor >= len(tokens):
            if parameter.required:
                raise UserFacingError(
                    f"Faltou preencher o argumento `{parameter.name}`."
                )
            continue

        raw_value = tokens[cursor][0]
        cursor += 1
        values[parameter.name] = await _convert_argument(
            ctx,
            parameter,
            raw_value,
        )

    if cursor < len(tokens):
        raise UserFacingError("Há argumentos extras no comando.")
    return values


def _check_error_message(error: app_commands.AppCommandError) -> str:
    if isinstance(error, ModerationAccessDenied):
        return str(error)
    if isinstance(error, app_commands.MissingPermissions):
        return "Você não tem as permissões necessárias para usar este comando."
    if isinstance(error, app_commands.BotMissingPermissions):
        return "O bot não tem as permissões necessárias para executar este comando."
    if isinstance(error, app_commands.CheckFailure):
        return "Você não pode usar este comando aqui."
    return "Ocorreu um erro ao verificar as permissões deste comando."


async def _invoke_app_command(
    ctx: commands.Context,
    command: app_commands.Command[Any, ..., Any],
    raw_arguments: str,
) -> None:
    interaction = PrefixInteraction(ctx)
    interaction.command = command

    if command.guild_only and ctx.guild is None:
        await ctx.send("Este comando só pode ser usado em um servidor.")
        return
    try:
        if not await command._check_can_run(interaction):  # type: ignore[arg-type]
            await ctx.send("Você não pode usar este comando.")
            return
    except app_commands.AppCommandError as error:
        await ctx.send(_check_error_message(error))
        return

    try:
        arguments = await _parse_parameters(ctx, command, raw_arguments)
        await command.callback(interaction, **arguments)
    except commands.CommandError as error:
        await ctx.send(f"Não consegui interpretar os argumentos: {error}.")
    except UserFacingError as error:
        await ctx.send(str(error))
    except app_commands.AppCommandError as error:
        await ctx.send(_check_error_message(error))
    except discord.Forbidden:
        await ctx.send("O Discord recusou a ação. Confira minhas permissões e a hierarquia dos cargos.")
    except Exception:
        logger.exception("Falha no comando de prefixo %s.", command.qualified_name)
        await ctx.send("Ocorreu um erro inesperado ao executar o comando.")


def register(bot: commands.Bot) -> None:
    resume_command: app_commands.Command[Any, ..., Any] | None = None
    for registered in bot.tree.get_commands():
        if isinstance(registered, app_commands.Group):
            if registered.name == "configurar":
                prefix_group = commands.Group(
                    _configuration_group,
                    name=registered.name,
                    invoke_without_command=True,
                )
                for child in registered.commands:
                    prefix_group.add_command(
                        _make_prefix_command(child, child.name)
                    )
                bot.add_command(prefix_group)
            continue
        if registered.name == "resume":
            resume_command = registered
        bot.add_command(
            _make_prefix_command(registered, registered.name)
        )
    if resume_command is not None:
        bot.add_command(_make_prefix_command(resume_command, "despause"))


async def _configuration_group(ctx: commands.Context) -> None:
    await ctx.send(
        "Use `e!configurar logs`, `e!configurar cargo`, "
        "`e!configurar remover_cargo`, `e!configurar cargo_moderador`, "
        "`e!configurar mutado`, `e!configurar filtro` ou "
        "`e!configurar palavra`."
    )


def _make_prefix_command(
    slash_command: app_commands.Command[Any, ..., Any],
    name: str,
) -> commands.Command[Any, ..., Any]:
    async def invoke(ctx: commands.Context, *, arguments: str = "") -> None:
        await _invoke_app_command(ctx, slash_command, arguments)

    return commands.Command(invoke, name=name)
