from redbot.core import commands
from .excelrichembeds import ExcelRichEmbeds

async def setup(bot: commands.Bot):
    await bot.add_cog(ExcelRichEmbeds(bot))
