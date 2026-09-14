from typing import TYPE_CHECKING

from .cog import Suggestions

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot


async def setup(bot: "BallsDexBot"):
    await bot.add_cog(Suggestions(bot))
