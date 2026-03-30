# ExcelEvents/__init__.py
from .excelevents import ExcelEvents

async def setup(bot):
    await bot.add_cog(ExcelEvents(bot))
