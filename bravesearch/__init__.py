from .bravesearch import BraveSearch

async def setup(bot):
    await bot.add_cog(BraveSearch(bot))