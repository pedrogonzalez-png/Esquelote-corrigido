import logging

import discord
from discord.ext import commands

from bot.checks import UserFacingError
from bot.database import Database

logger = logging.getLogger(__name__)

MessageChannel = discord.TextChannel | discord.VoiceChannel | discord.StageChannel


async def locate_reference(
    channels: list[MessageChannel],
    message_id: int,
) -> discord.Message | None:
    for channel in channels:
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            continue
        except (discord.Forbidden, discord.HTTPException) as error:
            raise UserFacingError(
                f"Não consegui localizar a mensagem com segurança em "
                f"{channel.mention}; nenhuma mensagem foi apagada."
            ) from error
    return None


async def collect_exact_matches(
    channels: list[MessageChannel],
    reference: discord.Message,
    status: discord.Message,
) -> dict[int, list[discord.Message]]:
    matches: dict[int, list[discord.Message]] = {
        channel.id: [] for channel in channels
    }
    author_id = reference.author.id
    original_content = reference.content

    try:
        for channel_index, channel in enumerate(channels, start=1):
            async for message in channel.history(limit=None):
                if (
                    message.author.id == author_id
                    and message.content == original_content
                ):
                    matches[channel.id].append(message)

            if channel_index % 5 == 0:
                await status.edit(
                    content=(
                        f"🔎 Busca em andamento: {channel_index}/"
                        f"{len(channels)} canais verificados. Ainda não "
                        "apaguei nenhuma mensagem."
                    )
                )
    except (discord.Forbidden, discord.HTTPException) as error:
        logger.exception(
            "Falha ao ler histórico no comando clearglobal do servidor %s.",
            reference.guild.id,
        )
        raise UserFacingError(
            "A busca foi interrompida por uma falha ao ler o histórico de um "
            "canal. Nenhuma mensagem foi apagada. Confira as permissões do "
            "bot e tente novamente."
        ) from error

    return matches


def register_prefix_command(
    bot: commands.Bot,
    database: Database,
) -> None:
    @bot.command(
        name="clearglobal",
        help="Apaga, nos canais de texto e voz, mensagens idênticas às de um ID.",
    )
    async def clear_global(ctx: commands.Context, message_id: str) -> None:
        guild = ctx.guild
        moderator = ctx.author
        if guild is None or not isinstance(moderator, discord.Member):
            await ctx.send("Este comando só pode ser usado em um servidor.")
            return

        if not (
            moderator.guild_permissions.manage_messages
            or moderator.guild_permissions.administrator
            or database.has_moderator_role(
                guild.id,
                {role.id for role in moderator.roles},
            )
        ):
            await ctx.send(
                "Você precisa de **Gerenciar mensagens** ou de um cargo de "
                "moderador configurado para usar este comando."
            )
            return

        if not message_id.isdecimal() or int(message_id) <= 0:
            await ctx.send("Informe um ID de mensagem válido.")
            return

        bot_member = guild.me
        if bot_member is None:
            await ctx.send("Não consegui verificar minhas permissões neste servidor.")
            return

        channels: list[MessageChannel] = [
            *guild.text_channels,
            *guild.voice_channels,
            *guild.stage_channels,
        ]
        inaccessible = [
            channel.mention
            for channel in channels
            if not (
                (permissions := channel.permissions_for(bot_member)).view_channel
                and permissions.read_message_history
                and permissions.manage_messages
            )
        ]
        if inaccessible:
            channel_list = ", ".join(inaccessible[:10])
            if len(inaccessible) > 10:
                channel_list += f" e mais {len(inaccessible) - 10}"
            await ctx.send(
                "Não iniciei a busca: preciso de **Ver canal**, **Ver histórico "
                "de mensagens** e **Gerenciar mensagens** em todos os canais "
                f"de texto e voz. Permissões faltando em: {channel_list}."
            )
            return

        status = await ctx.send(
            "🔎 Localizando a mensagem e verificando os canais de texto e voz. "
            "A busca pode demorar em servidores com muito histórico."
        )
        reference: discord.Message | None = None
        try:
            reference = await locate_reference(channels, int(message_id))
        except UserFacingError as error:
            await status.edit(content=str(error))
            return

        if reference is None:
            await status.edit(
                content="Não encontrei essa mensagem em nenhum canal de texto "
                "ou voz "
                "deste servidor. Nenhuma mensagem foi apagada."
            )
            return
        if not reference.content:
            await status.edit(
                content="A mensagem original não tem conteúdo textual. Para "
                "evitar apagar mensagens sem correspondência segura, não fiz "
                "nenhuma alteração."
            )
            return

        try:
            matches = await collect_exact_matches(channels, reference, status)
        except UserFacingError as error:
            await status.edit(content=str(error))
            return

        found_count = sum(len(channel_matches) for channel_matches in matches.values())
        deleted_count = 0
        already_missing = 0
        failed_channels: set[str] = set()

        for channel in channels:
            for message in matches[channel.id]:
                try:
                    await message.delete()
                    deleted_count += 1
                except discord.NotFound:
                    already_missing += 1
                except discord.Forbidden:
                    logger.warning(
                        "Sem permissão para apagar mensagem %s no canal %s "
                        "durante clearglobal.",
                        message.id,
                        channel.id,
                    )
                    failed_channels.add(channel.mention)
                except discord.HTTPException:
                    logger.exception(
                        "Falha ao apagar mensagem %s no canal %s pelo comando "
                        "clearglobal.",
                        message.id,
                        channel.id,
                    )
                    failed_channels.add(channel.mention)

        result = (
            f"Busca concluída. Encontrei {found_count} mensagem(ns) exatamente "
            f"iguais, do mesmo autor, e apaguei {deleted_count}."
        )
        if already_missing:
            result += f" {already_missing} já tinham sido apagadas."
        if failed_channels:
            result += (
                " Não consegui apagar mensagens em: "
                + ", ".join(sorted(failed_channels)[:10])
                + "."
            )
        await status.edit(content=result)
        logger.info(
            "clearglobal no servidor %s por %s: encontradas=%s, apagadas=%s, "
            "já ausentes=%s, canais com falhas=%s.",
            guild.id,
            moderator.id,
            found_count,
            deleted_count,
            already_missing,
            len(failed_channels),
        )
