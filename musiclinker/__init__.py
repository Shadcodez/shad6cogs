from .musiclinker import MusicLinker


async def setup(bot):
    await bot.add_cog(MusicLinker(bot))
