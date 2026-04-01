from .fu import Fu

async def setup(bot):
    await bot.add_cog(Fu(bot))
