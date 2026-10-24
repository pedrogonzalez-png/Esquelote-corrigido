import discord
from discord import app_commands


def build_userinfo_embed(member: discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title=f"Informações de {member.display_name}",
        color=member.color if member.color.value else discord.Color.blurple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Usuário", value=f"{member.mention}\n`{member}`", inline=True)
    embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Cargo mais alto", value=member.top_role.mention, inline=True)
    embed.add_field(
        name="Conta criada",
        value=discord.utils.format_dt(member.created_at, style="F"),
        inline=True,
    )
    if member.joined_at is not None:
        embed.add_field(
            name="Entrou no servidor",
            value=discord.utils.format_dt(member.joined_at, style="F"),
            inline=True,
        )

    roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
    role_text = ", ".join(roles[:20]) if roles else "Nenhum cargo adicional"
    if len(roles) > 20:
        role_text += f" e mais {len(roles) - 20}"
    embed.add_field(name=f"Cargos ({len(roles)})", value=role_text[:1024], inline=False)
    embed.set_footer(text=f"Membro do servidor • {member.guild.name}")
    return embed


def register(tree: app_commands.CommandTree) -> None:
    @tree.command(name="ping", description="Mostra a latência atual do bot")
    @app_commands.guild_only()
    async def ping(interaction: discord.Interaction) -> None:
        latency_ms = round(interaction.client.latency * 1000)
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"Latência do bot: **{latency_ms} ms**",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed)

    @tree.command(name="userinfo", description="Mostra informações de um membro")
    @app_commands.guild_only()
    @app_commands.describe(membro="Membro que deseja consultar (opcional)")
    async def userinfo(
        interaction: discord.Interaction,
        membro: discord.Member | None = None,
    ) -> None:
        target = membro or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message(
                "Não consegui encontrar as informações desse membro.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=build_userinfo_embed(target))
