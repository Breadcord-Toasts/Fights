import random
import sqlite3
from collections.abc import Callable, Coroutine
from math import floor
from typing import Any

import discord
import discord.ui
from discord import app_commands
from discord.ext import commands

import breadcord


class WinnerSelect(discord.ui.Select):
    def __init__(self, fighter_names: list[str]) -> None:
        super().__init__(
            placeholder="Select who would win",
            options=[
                discord.SelectOption(label=name, value=name)
                for name in fighter_names
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

class VoteButton(discord.ui.Button):
    def __init__(
        self,
        name: str,
        callback: Callable[[discord.Interaction[Any], "VoteButton"], Coroutine[Any, Any, Any]],
    ) -> None:
        super().__init__(
            label=f"{name} would win",
            style=discord.ButtonStyle.primary,
        )
        self.name = name
        self._callback = callback

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._callback(interaction, self)


class VoteView(discord.ui.View):
    def __init__(self, fighter_names: list[str], author_id: int, *, stop_on_vote: bool, timeout: float = 180.0) -> None:
        super().__init__(timeout=timeout)
        self.fighter_names = fighter_names
        self.stop_on_vote = stop_on_vote
        self.votes: dict[int, str] = {}  # voter id -> fighter name
        self.author_id: int = author_id

        if stop_on_vote:
            self.clear_items()

        for fighter_name in fighter_names:
            self.add_item(VoteButton(fighter_name, self.vote_callback))

    async def vote_callback(self, interaction: discord.Interaction, button: VoteButton, /) -> None:
        self.votes[interaction.user.id] = button.name
        await interaction.response.defer(ephemeral=True)
        if self.stop_on_vote:
            self.stop()

    @discord.ui.button(label="End prematurely", style=discord.ButtonStyle.gray, row=1)
    async def end_button(self, interaction: discord.Interaction, _, /) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the command author can end the vote prematurely",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        self.stop()


class Fights(breadcord.module.ModuleCog):
    def __init__(self, module_id: str, /):
        super().__init__(module_id)
        self.db = sqlite3.connect(self.storage_path / 'fights.db')
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS fighters (
                name TEXT PRIMARY KEY,
                image TEXT NOT NULL,
                submitter INTEGER,
                wins INTEGER NOT NULL DEFAULT 0,
                pairings INTEGER NOT NULL DEFAULT 0
            )
        """)
        self.db.commit()

    @commands.hybrid_group(
        name="fight",
        description="Manage fights",
    )
    async def group(self, ctx: commands.Context) -> None:
        return await self.vote(ctx)

    @group.command(description="Vote on the outcome of a fight")
    async def vote(self, ctx: commands.Context) -> None:
        char_query: list[tuple[str, str]] | None = self.db.execute(
            "SELECT name, image, submitter FROM fighters",
        ).fetchall()
        if not char_query:
            await ctx.reply("No fighters have been nominated yet")
            return
        random.shuffle(char_query)

        message: discord.Message | None = None
        group_size: int = 2
        for character_group in zip(*[iter(char_query)] * group_size):
            embeds = [
                discord.Embed(
                    title=name,
                    colour=discord.Colour.green(),
                )
                .set_image(url=image_url)
                .set_footer(
                    text=f"Submitted by {submitter_name.display_name}"
                    if submitter_id and (submitter_name := (
                        (ctx.guild.get_member(submitter_id) if ctx.guild else None)
                        or self.bot.get_user(submitter_id)
                        or await self.bot.fetch_user(submitter_id)
                    )) else None
                )
                for name, image_url, submitter_id in character_group
            ]
            view = VoteView(
                fighter_names=[name for name, *_ in character_group],
                author_id=ctx.author.id,
                stop_on_vote=True,
            )

            if message:
                await message.edit(
                    content="Who would win in a fight?",
                    embeds=embeds,
                    view=view,
                )
            else:
                message = await ctx.reply(
                    content="Who would win in a fight?",
                    embeds=embeds,
                    view=view,
                    mention_author=False,
                )
            timed_out = await view.wait()
            if view.votes:
                for fighter_name, *_ in character_group:
                    self.db.execute(
                        "UPDATE fighters SET pairings = pairings + 1 WHERE name = ?",
                        (fighter_name,),
                    )
                for _, fighter_name in view.votes.items():
                    self.db.execute(
                        "UPDATE fighters SET wins = wins + 1 WHERE name = ?",
                        (fighter_name,),
                    )
                self.db.commit()

            if timed_out:
                await message.edit(
                    content="The vote has timed out! Run the command again to continue",
                    view=None,
                    embeds=[],
                )
                return

        if not message:
            await ctx.reply("Not enough fighters have been nominated yet")
            return

        await message.edit(
            content="All characters have been voted on",
            view=None,
            embeds=[],
        )

    @group.command(description="Nominate a fight")
    @app_commands.describe(
        name="The name of the nominee",
        image="An image of the nominee",
    )
    async def nominate(
        self,
        ctx: commands.Context,
        image: discord.Attachment,
        *,
        name: str,
    ) -> None:
        if not image.width:
            await ctx.reply("Must upload an image")
            return

        file = await image.to_file()  # We want to reupload the image just to make sure it stays valid
        embed = (
            discord.Embed(
                title="Fighter nominated!",
                description=name,
                colour=discord.Colour.green(),
            )
            .set_image(url=f"attachment://{file.filename}")
        )
        response = await ctx.reply(embed=embed, file=file)
        self.db.execute(
            "INSERT INTO fighters (name, image, submitter) VALUES (?, ?, ?)",
            (name, response.embeds[0].image.url, ctx.author.id),
        )
        self.db.commit()

    @group.command(description="List all nominees")
    async def leaderboard(self, ctx: commands.Context) -> None:
        char_query: list[tuple[str, int, int]] | None = self.db.execute(
            "SELECT name, wins, pairings FROM fighters",
        ).fetchall()
        if not char_query:
            await ctx.reply("No fighters have been nominated yet")
            return

        win_ratios = {name: wins / pairings for name, wins, pairings in char_query}
        leaderboard = [
            (
                f"1. `{f'{floor(ratio * 100)}%'.rjust(4)}`"
                f" {name}"
            )
            for name, ratio in sorted(
                win_ratios.items(),
                reverse=True,
            )
        ]
        await ctx.reply(
            embed=discord.Embed(
                title="Fighter leaderboard",
                description=f"Percentages are each fighter's win ratio",
                colour=discord.Colour.green(),
            ).add_field(
                name="Top 10",
                value="\n".join(leaderboard[:10]),
                inline=False,
            ).add_field(
                name="Bottom 5",
                value="\n".join(leaderboard[-5:]),
                inline=False,
            ).set_footer(
                text=f"A total of {len(char_query)} fighters have been nominated"
            ),
        )


async def setup(bot: breadcord.Bot, module: breadcord.module.Module) -> None:
    await bot.add_cog(Fights(module.id))
