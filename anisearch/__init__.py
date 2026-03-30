from redbot.core.bot import Red
from .anisearch import AniSearch

async def setup(bot: Red):
    await bot.add_cog(AniSearch(bot))