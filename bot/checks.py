import discord
from discord import app_commands

from bot.database import Database


class ModerationAccessDenied(app_commands.CheckFailure):
    pass


class UserFacingError(Exception):
    pass


PERMISSION_LABELS = {
    "kick_members": "Expulsar membros",
    "ban_members": "Banir membros",
    "moderate_members": "Moderar membros",
    "manage_messages": "Gerenciar mensagens",
}


def has_moderation_access(database: Database, permission: str):
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise ModerationAccessDenied("Este comando só pode ser usado em um servidor.")

        if getattr(interaction.user.guild_permissions, permission) or interaction.user.guild_permissions.administrator:
            return True

        if database.has_moderator_role(
            interaction.guild.id, {role.id for role in interaction.user.roles}
        ):
            return True

        raise ModerationAccessDenied(
            f"Você precisa da permissão **{PERMISSION_LABELS.get(permission, permission)}** "
            "ou de um cargo de moderador configurado."
        )

    return app_commands.check(predicate)


def ensure_moderatable(
    interaction: discord.Interaction, target: discord.Member
) -> None:
    guild = interaction.guild
    moderator = interaction.user
    if guild is None or not isinstance(moderator, discord.Member):
        raise UserFacingError("Este comando só pode ser usado em um servidor.")
    if target.id == moderator.id:
        raise UserFacingError("Você não pode aplicar esta ação em si mesmo.")
    if target.id == guild.owner_id:
        raise UserFacingError("Não é possível aplicar esta ação ao dono do servidor.")
    if target.top_role >= moderator.top_role and moderator.id != guild.owner_id:
        raise UserFacingError("Você não pode moderar alguém com cargo igual ou superior ao seu.")

    bot_member = guild.me
    if bot_member is None or target.top_role >= bot_member.top_role:
        raise UserFacingError("Meu cargo precisa estar acima do cargo do membro para fazer isso.")
