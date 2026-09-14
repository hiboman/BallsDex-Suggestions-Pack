from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

from ..models import Suggestion
from .ui import SuggestionsView

_TYPE_BY_CHOICE: dict[str, str] = {
    "Countryball": Suggestion.Type.COUNTRYBALL,
    "Card": Suggestion.Type.CARD,
    "Economy": Suggestion.Type.ECONOMY,
}

_SORT_BY_CHOICE: dict[str, str] = {
    "Creation Date": "creation_date",
    "Upvotes": "upvotes",
}


class Suggestions(commands.Cog):
    """
    Browse and submit community suggestions (Countryballs, Cards, and Economies).
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

    @app_commands.command()
    async def suggestions(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        type: Literal["Countryball", "Card", "Economy"] = "Countryball",
        revamp: bool = False,
        sort: Literal["Creation Date", "Upvotes"] = "Creation Date",
    ):
        """
        Open the community suggestions menu.

        Parameters
        ----------
        type: Literal["Countryball", "Card", "Economy"]
            Which kind of suggestions to browse and submit. Defaults to Countryball.
        revamp: bool
            If set, browse/submit revamp suggestions (proposed replacements for an existing
            Ball/Regime/Special/Economy) instead of new-object suggestions. These are a separate
            queue from regular suggestions of the same type. Defaults to False.
        sort: Literal["Creation Date", "Upvotes"]
            How to order the browsing list - newest first, or most-upvoted first. Defaults to
            Creation Date.
        """
        view = SuggestionsView(
            self.bot,
            interaction.user,
            type=_TYPE_BY_CHOICE[type],
            is_revamp=revamp,
            sort=_SORT_BY_CHOICE[sort],
        )
        await view.initialize()
        await interaction.response.send_message(view=view, files=view.current_files)
        view.original_message = await interaction.original_response()
