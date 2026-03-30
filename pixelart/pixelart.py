"""
PixelArt Cog for Red Discord Bot
Converts images into pixel art with interactive controls.
Inspired by giventofly/pixelit.
"""

import asyncio
import io
import re
from typing import Dict, List, Optional

import aiohttp
import discord
from PIL import Image
from redbot.core import commands
from redbot.core.bot import Red

# ============================================================================
# Constants
# ============================================================================

MAX_DIMENSION = 2048
MAX_FILE_SIZE = 8_388_608  # 8 MiB

URL_PATTERN = re.compile(
    r"(https?://[^\s<>\"']+\.(?:png|jpg|jpeg|gif|webp|bmp)(?:[?#][^\s<>\"']*)?)",
    re.IGNORECASE,
)

DISCORD_CDN_PATTERN = re.compile(
    r"(https?://(?:cdn|media)\.discordapp\.(?:com|net)/attachments/[^\s<>\"']+)",
    re.IGNORECASE,
)

# Pillow resampling constants (compatible with Pillow <10 and >=10)
NEAREST = Image.NEAREST
BOX = Image.BOX
LANCZOS = Image.LANCZOS

# ============================================================================
# Color Palettes
# ============================================================================

PALETTES: Dict[str, Optional[List[tuple[int, int, int]]]] = {
    "None": None,
    "16-Bit": [
        (26, 28, 44), (93, 39, 93), (177, 62, 83), (239, 125, 87),
        (255, 205, 117), (167, 240, 112), (56, 183, 100), (37, 113, 121),
        (41, 54, 111), (59, 93, 201), (65, 166, 246), (115, 239, 247),
        (244, 244, 244), (148, 176, 194), (86, 108, 134), (51, 60, 87),
    ],
    "PICO-8": [
        (0, 0, 0), (29, 43, 83), (126, 37, 83), (0, 135, 81),
        (171, 82, 54), (95, 87, 79), (194, 195, 199), (255, 241, 232),
        (255, 0, 77), (255, 163, 0), (255, 236, 39), (0, 228, 54),
        (41, 173, 255), (131, 118, 156), (255, 119, 168), (255, 204, 170),
    ],
    "Game Boy": [
        (15, 56, 15), (48, 98, 48), (139, 172, 15), (155, 188, 15),
    ],
    "Commodore 64": [
        (0, 0, 0), (255, 255, 255), (136, 0, 0), (170, 255, 238),
        (204, 68, 204), (0, 204, 85), (0, 0, 170), (238, 238, 119),
        (221, 136, 85), (102, 68, 0), (255, 119, 119), (51, 51, 51),
        (119, 119, 119), (170, 255, 102), (0, 136, 255), (187, 187, 187),
    ],
    "CGA": [
        (0, 0, 0), (0, 170, 170), (170, 0, 170), (170, 170, 170),
    ],
    "Grayscale": [
        (0, 0, 0), (85, 85, 85), (170, 170, 170), (255, 255, 255),
    ],
    "Sepia": [
        (44, 33, 24), (90, 65, 42), (138, 109, 72), (183, 155, 110),
        (224, 202, 162), (250, 237, 210),
    ],
    "Neon": [
        (0, 0, 0), (255, 0, 102), (0, 255, 102), (0, 102, 255),
        (255, 255, 0), (255, 0, 255), (0, 255, 255), (255, 255, 255),
    ],
    "Pastel": [
        (240, 234, 214), (250, 235, 215), (255, 213, 209), (238, 210, 210),
        (255, 218, 193), (255, 253, 208), (255, 245, 200), (200, 235, 210),
        (210, 225, 200), (195, 215, 240), (215, 200, 235), (225, 205, 230),
        (235, 220, 200), (210, 200, 190),
    ],
    "Autumn": [
        (43, 24, 11), (97, 49, 24), (164, 74, 30), (204, 119, 34),
        (230, 172, 51), (189, 140, 60), (107, 86, 47), (56, 61, 38),
    ],
    "Ocean": [
        (0, 22, 51), (0, 49, 83), (0, 84, 119), (0, 131, 143),
        (0, 180, 170), (100, 217, 197), (178, 236, 225), (240, 255, 250),
    ],
    "Candy Cane": [
        (255, 255, 255), (255, 240, 245), (255, 182, 193), (255, 105, 120),
        (220, 20, 60), (178, 34, 34), (144, 12, 30), (152, 224, 173),
        (240, 248, 240),
    ],
    "Halloween": [
        (0, 0, 0), (45, 45, 45), (101, 67, 33), (139, 90, 43),
        (204, 85, 0), (255, 103, 0), (255, 204, 0), (75, 0, 130),
    ],
    "Christmas": [
        (0, 80, 0), (0, 128, 0), (34, 139, 34), (80, 180, 80),
        (139, 69, 19), (101, 67, 33), (178, 34, 34), (220, 20, 60),
        (255, 215, 0), (255, 245, 200), (255, 255, 255),
    ],
    "Cyberpunk": [
        (10, 10, 18), (25, 10, 45), (50, 20, 70), (75, 0, 130),
        (255, 0, 110), (255, 50, 180), (0, 255, 255), (0, 180, 255),
        (0, 110, 255), (255, 255, 0), (70, 70, 90), (200, 200, 220),
    ],
}

PALETTE_NAMES: List[str] = list(PALETTES.keys())

PALETTE_DESCRIPTIONS: Dict[str, str] = {
    "None": "Keep original colours",
    "16-Bit": "Classic 16-colour retro look",
    "PICO-8": "Fantasy console palette",
    "Game Boy": "Classic 4-shade green monochrome",
    "Commodore 64": "Authentic C64 colours",
    "CGA": "Original 4-colour CGA mode",
    "Grayscale": "4-shade black & white",
    "Sepia": "Warm vintage photo tones",
    "Neon": "Bright glowing colours",
    "Pastel": "Soft eggshell & muted tones",
    "Autumn": "Warm fall season tones",
    "Ocean": "Cool blue-green aquatic palette",
    "Candy Cane": "Red & white peppermint stripes",
    "Halloween": "Neon orange, brown, yellow & black",
    "Christmas": "Festive greens, reds & gold",
    "Cyberpunk": "Neon pink, cyan & dark violet",
}

# ============================================================================
# Processing
# ============================================================================


def process_image(
    img: Image.Image,
    scale: int,
    palette_name: str,
    grayscale: bool,
) -> Image.Image:
    img = img.convert("RGB")
    orig_w, orig_h = img.size

    scale = max(2, min(50, scale))
    small_w = max(1, orig_w // scale)
    small_h = max(1, orig_h // scale)

    small = img.resize((small_w, small_h), BOX)

    if grayscale:
        small = small.convert("L").convert("RGB")

    palette = PALETTES.get(palette_name)
    if palette:
        pixels = small.load()
        for y in range(small_h):
            for x in range(small_w):
                r, g, b = pixels[x, y]
                best = palette[0]
                best_dist = float("inf")
                for pr, pg, pb in palette:
                    dist = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
                    if dist < best_dist:
                        best_dist = dist
                        best = (pr, pg, pb)
                pixels[x, y] = best

    return small.resize((orig_w, orig_h), NEAREST)


def image_to_file(img: Image.Image, filename: str = "pixelart.png") -> discord.File:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename=filename)


# ============================================================================
# UI Components
# ============================================================================


class PaletteSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=name,
                description=PALETTE_DESCRIPTIONS.get(name, ""),
                default=(name == "None"),
            )
            for name in PALETTE_NAMES
        ]
        super().__init__(
            placeholder="🎨 Select palette…",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: PixelArtView = self.view
        view.palette_name = self.values[0]
        for opt in self.options:
            opt.default = opt.label == self.values[0]
        await view.refresh(interaction)


class PixelArtView(discord.ui.View):
    def __init__(self, ctx: commands.Context, original: Image.Image, *, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.original = original
        self.scale: int = 8
        self.palette_name: str = "None"
        self.grayscale: bool = False
        self.message: Optional[discord.Message] = None

        self.add_item(PaletteSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.ctx.author:
            await interaction.response.send_message("Only the command author can use this.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def build_embed(self) -> discord.Embed:
        grayscale_status = "On 🟢" if self.grayscale else "Off ⚪"
        lines = [
            f"**Scale:** 1/{self.scale}",
            f"**Palette:** {self.palette_name}",
            f"**Grayscale:** {grayscale_status}",
        ]
        embed = discord.Embed(
            title="🖼️ Pixel Art Editor",
            color=0x2F3136,
            description=" • ".join(lines),
        )
        embed.set_image(url="attachment://pixelart.png")
        embed.set_footer(text=f"Requested by {self.ctx.author.display_name}")
        return embed

    def render(self) -> discord.File:
        result = process_image(self.original, self.scale, self.palette_name, self.grayscale)
        return image_to_file(result)

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(None, self.render)
        embed = self.build_embed()
        await interaction.message.edit(embed=embed, attachments=[file], view=self)

    @discord.ui.button(label="Scale −", style=discord.ButtonStyle.secondary, emoji="➖", row=1)
    async def scale_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.scale = max(2, self.scale - 2)
        await self.refresh(interaction)

    @discord.ui.button(label="Scale +", style=discord.ButtonStyle.secondary, emoji="➕", row=1)
    async def scale_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.scale = min(50, self.scale + 2)
        await self.refresh(interaction)

    @discord.ui.button(label="Grayscale Off", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_grayscale(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.grayscale = not self.grayscale
        button.label = f"Grayscale {'On' if self.grayscale else 'Off'}"
        button.style = discord.ButtonStyle.success if self.grayscale else discord.ButtonStyle.secondary
        await self.refresh(interaction)

    @discord.ui.button(label="Save", style=discord.ButtonStyle.green, emoji="💾", row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(None, self.render)
        embed = self.build_embed()
        embed.title = "🖼️ Pixel Art (Saved)"
        embed.color = discord.Color.green()
        await interaction.message.edit(embed=embed, attachments=[file], view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="✖", row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        self.stop()


# ============================================================================
# Cog
# ============================================================================


class PixelArt(commands.Cog):
    """Convert images to pixel art with adjustable scale, palette and grayscale."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    @staticmethod
    def _is_image_attachment(att: discord.Attachment) -> bool:
        if att.content_type and att.content_type.startswith("image/"):
            return True
        name = (att.filename or "").lower()
        return name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))

    @staticmethod
    def _extract_image_url(msg: discord.Message) -> Optional[str]:
        for att in msg.attachments:
            if PixelArt._is_image_attachment(att):
                return att.url

        for embed in msg.embeds:
            if embed.image and embed.image.url:
                return embed.image.url
            if embed.thumbnail and embed.thumbnail.url:
                return embed.thumbnail.url

        if msg.content:
            match = URL_PATTERN.search(msg.content)
            if match:
                return match.group(0)
            match = DISCORD_CDN_PATTERN.search(msg.content)
            if match:
                return match.group(0)

        return None

    async def _resolve_replied_message(self, ctx: commands.Context) -> Optional[discord.Message]:
        ref = ctx.message.reference
        if not ref or not ref.message_id:
            return None

        if isinstance(ref.resolved, discord.Message):
            return ref.resolved

        try:
            return await ctx.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return None

    async def find_image_url(self, ctx: commands.Context, given_url: Optional[str]) -> Optional[str]:
        if given_url:
            return given_url

        url = self._extract_image_url(ctx.message)
        if url:
            return url

        replied = await self._resolve_replied_message(ctx)
        if replied:
            url = self._extract_image_url(replied)
            if url:
                return url

        return None

    async def download_image(self, url: str) -> Optional[Image.Image]:
        try:
            session = await self._get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                if resp.content_length and resp.content_length > MAX_FILE_SIZE:
                    return None
                data = await resp.read()
                if len(data) > MAX_FILE_SIZE:
                    return None

            img = Image.open(io.BytesIO(data))
            img = img.convert("RGB")

            w, h = img.size
            if w > MAX_DIMENSION or h > MAX_DIMENSION:
                ratio = min(MAX_DIMENSION / w, MAX_DIMENSION / h)
                img = img.resize((int(w * ratio), int(h * ratio)), LANCZOS)

            return img
        except Exception:
            return None

    @commands.command(name="pixel")
    @commands.cooldown(1, 12, commands.BucketType.user)
    @commands.max_concurrency(4, commands.BucketType.guild)
    async def pixel(self, ctx: commands.Context, *, url: Optional[str] = None):
        """
        Convert an image to pixel art with interactive controls.

        Provide an image in one of these ways:
        • Attach it directly to your message
        • Paste a direct image URL as an argument
        • Reply to a message that contains an image
        """
        image_url = await self.find_image_url(ctx, url)
        if not image_url:
            await ctx.send_help()
            return

        async with ctx.typing():
            img = await self.download_image(image_url)
            if img is None:
                error_embed = discord.Embed(
                    title="❌ Processing Failed",
                    description=(
                        "Couldn't download or open the image.\n\n"
                        "Common reasons:\n"
                        "• Invalid or expired URL\n"
                        "• File larger than 8 MB\n"
                        "• Not a supported image format (png/jpg/gif/webp/bmp)"
                    ),
                    color=discord.Color.red(),
                )
                await ctx.send(embed=error_embed)
                return

            view = PixelArtView(ctx, img)
            loop = asyncio.get_running_loop()
            file = await loop.run_in_executor(None, view.render)
            embed = view.build_embed()

            msg = await ctx.send(embed=embed, file=file, view=view)
            view.message = msg

