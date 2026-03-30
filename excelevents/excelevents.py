# excelevents.py
import asyncio
import csv
import io
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple
import aiohttp

import discord
import openpyxl
from redbot.core import commands, Config, data_manager
from redbot.core.bot import Red


class ExcelEvents(commands.Cog):
    """Bulk Discord Scheduled Events from Excel/CSV with robust image support."""

    MAX_ROWS = 500
    MAX_IMAGE_SIZE = 15 * 1024 * 1024

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=9876543210987654321, force_registration=True)
        defaults_guild = {
            "event_mappings": {},
            "last_synced": None,
            "announcement_mode": False,
            "announcement_channel": None,
            "reminder_mode": False,
            "reminder_channel": None,
            "reminder_minutes": [60, 15, 5],
            "reminder_sent": {},
        }
        self.config.register_guild(**defaults_guild)
        self.reminder_task = None
        self.session = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        if self.reminder_task is None or self.reminder_task.done():
            self.reminder_task = asyncio.create_task(self._reminder_task())

    def cog_unload(self):
        if self.session:
            asyncio.create_task(self.session.close())
        if self.reminder_task and not self.reminder_task.done():
            self.reminder_task.cancel()

    # ====================== REFINED IMAGE DOWNLOADER ======================
    async def _download_image(self, url: str) -> Optional[bytes]:
        if not url or not str(url).startswith(("http://", "https://")):
            return None

        url = url.strip()

        if "imgur.com" in url:
            url = url.replace(".jpeg", ".jpg").replace(".JPEG", ".jpg")
            if "i.imgur.com" not in url and "imgur.com" in url:
                image_id = url.split("/")[-1].split("?")[0].split(".")[0]
                url = f"https://i.imgur.com/{image_id}.jpg"
            if "?" in url:
                url = url.split("?")[0]

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://imgur.com/",
        }

        timeout = aiohttp.ClientTimeout(total=25)

        for attempt in range(2):
            try:
                async with self.session.get(url, headers=headers, timeout=timeout, allow_redirects=True) as resp:
                    if resp.status != 200:
                        continue

                    # Only reject if Content-Length is present AND too large
                    content_length_header = resp.headers.get("Content-Length")
                    if content_length_header is not None:
                        try:
                            content_length = int(content_length_header)
                            if content_length > self.MAX_IMAGE_SIZE:
                                continue
                        except ValueError:
                            pass

                    data = await resp.read()

                    if len(data) > self.MAX_IMAGE_SIZE:
                        continue

                    content_type = resp.headers.get("Content-Type", "").lower()
                    if len(data) > 1024 and (
                        any(x in content_type for x in ("image/", "jpeg", "png", "gif", "webp")) or
                        data.startswith((b'\xff\xd8', b'\x89PNG', b'GIF8', b'RIFF'))
                    ):
                        return data
            except Exception:
                if attempt == 0:
                    await asyncio.sleep(1.5)
                continue
        return None

    async def _parse_datetime(self, value) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, (int, float)):
            try:
                base = datetime(1899, 12, 30)
                dt = base + timedelta(days=value)
                return dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
        value_str = str(value).strip()
        if not value_str:
            return None
        formats = [
            "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S",
            "%m/%d/%y %H:%M", "%m/%d/%y %H:%M:%S",
            "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p",
            "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %I:%M %p",
            "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(value_str, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    def _normalize_key(self, name: str) -> str:
        return str(name).strip().lower()

    def _is_valid_xlsx(self, file_path: Path) -> bool:
        try:
            with open(file_path, "rb") as f:
                header = f.read(4)
            return header[:2] == b'PK'
        except Exception:
            return False

    def _get_column_indices(self, headers: List[str]) -> Dict[str, int]:
        col_map = {}
        aliases = {
            "name": ["name", "event name", "title", "event"],
            "start": ["start", "start time", "start date", "date", "when"],
            "end": ["end", "end time", "end date"],
            "description": ["description", "desc", "details"],
            "type": ["type", "event type", "format", "kind"],
            "location": ["location", "place", "venue", "address", "link"],
            "channelid": ["channelid", "channel id", "channel", "voice channel", "stage channel"],
            "image": ["image", "cover", "banner", "imageurl", "cover image", "event image"],
        }
        for i, h in enumerate(headers):
            if not h:
                continue
            norm = self._normalize_key(h)
            for canonical, alias_list in aliases.items():
                if any(norm == self._normalize_key(a) for a in alias_list):
                    col_map[canonical] = i
                    break
            else:
                col_map[norm] = i
        return col_map

    def _get_cell(self, row: tuple, col_map: Dict[str, int], key: str, default=None):
        idx = col_map.get(key)
        if idx is not None and idx < len(row):
            val = row[idx]
            return val if val is not None else default
        return default

    async def _create_event_with_image(self, guild: discord.Guild, data: Dict, image_bytes: Optional[bytes] = None) -> Optional[discord.ScheduledEvent]:
        name = str(data.get("name", "")).strip()
        if not name or len(name) > 100:
            return None

        start_time = await self._parse_datetime(data.get("start"))
        if not start_time:
            return None

        end_time = await self._parse_datetime(data.get("end"))
        description = str(data.get("description", "")).strip()[:1000] or None
        event_type_str = str(data.get("type", "")).strip().lower() or "voice"
        location = str(data.get("location", "")).strip() or None
        channel_id_input = data.get("channelid")

        if event_type_str in ("external", "url", "link"):
            entity_type = discord.EntityType.external
        elif event_type_str == "stage":
            entity_type = discord.EntityType.stage_instance
        else:
            entity_type = discord.EntityType.voice

        channel = None
        if entity_type in (discord.EntityType.voice, discord.EntityType.stage_instance) and channel_id_input:
            try:
                ch_id = int(str(channel_id_input).strip())
                temp_ch = guild.get_channel(ch_id)
                if temp_ch and (
                    (entity_type == discord.EntityType.voice and isinstance(temp_ch, discord.VoiceChannel)) or
                    (entity_type == discord.EntityType.stage_instance and isinstance(temp_ch, discord.StageChannel))
                ):
                    channel = temp_ch
            except Exception:
                pass

        # Build the kwargs, including image at creation time
        create_kwargs = {
            "name": name,
            "description": description,
            "start_time": start_time,
            "end_time": end_time,
            "entity_type": entity_type,
            "privacy_level": discord.PrivacyLevel.guild_only,
        }

        if image_bytes:
            create_kwargs["image"] = image_bytes

        try:
            if entity_type == discord.EntityType.external:
                if not location:
                    return None
                create_kwargs["location"] = location
            else:
                if not channel:
                    return None
                create_kwargs["channel"] = channel

            event = await guild.create_scheduled_event(**create_kwargs)
            await asyncio.sleep(2.0)

            # If image was passed at creation, verify it stuck; if not, retry via edit
            if image_bytes:
                try:
                    event = await guild.fetch_scheduled_event(event.id)
                    if not event.cover_image:
                        await event.edit(image=image_bytes)
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

            await asyncio.sleep(1.0)
            return event
        except Exception:
            return None

    async def _update_event(self, event: discord.ScheduledEvent, data: Dict, image_bytes: Optional[bytes] = None):
        try:
            edit_kwargs = {
                "name": str(data.get("name", "")).strip(),
                "description": str(data.get("description", "")).strip()[:1000] or None,
                "start_time": await self._parse_datetime(data.get("start")),
                "end_time": await self._parse_datetime(data.get("end")),
            }
            if image_bytes:
                edit_kwargs["image"] = image_bytes
            await event.edit(**edit_kwargs)
            await asyncio.sleep(1.2)
        except Exception:
            pass

    def _create_event_embed(self, event: discord.ScheduledEvent) -> discord.Embed:
        embed = discord.Embed(
            title=event.name[:256],
            description=(event.description or "No description provided.")[:4096],
            color=discord.Color.blurple(),
            url=event.url,
        )
        if event.start_time:
            ts = int(event.start_time.timestamp())
            embed.add_field(name="🕒 Start", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=True)
        if event.end_time:
            ts = int(event.end_time.timestamp())
            embed.add_field(name="🕒 End", value=f"<t:{ts}:F>", inline=True)

        loc = event.location or (event.channel.mention if event.channel else "Voice/Stage")
        embed.add_field(name="📍 Location", value=loc, inline=False)
        embed.add_field(name="Type", value=event.entity_type.name.replace("_", " ").title(), inline=True)
        embed.set_footer(text="New Event • Synced via ExcelEvents • RedBot 2026")
        return embed

    def _create_reminder_embed(self, event: discord.ScheduledEvent, minutes: int) -> discord.Embed:
        embed = discord.Embed(
            title=f"⏰ {event.name} starts in {minutes} minutes!",
            description=(event.description or "")[:4096],
            color=discord.Color.orange(),
            url=event.url,
        )
        if event.start_time:
            ts = int(event.start_time.timestamp())
            embed.add_field(name="Exact Time", value=f"<t:{ts}:F>", inline=False)
        loc = event.location or (event.channel.mention if event.channel else "Voice/Stage")
        embed.add_field(name="📍 Location", value=loc, inline=False)
        embed.set_footer(text=f"Reminder • ExcelEvents")
        return embed

    async def _validate_excel(self, file_path: Path, guild: discord.Guild) -> Tuple[List[str], List[str]]:
        errors: List[str] = []
        warnings: List[str] = []

        if not file_path.exists():
            errors.append("No events.xlsx file found. Use `upload` or `paste` first.")
            return errors, warnings

        if file_path.stat().st_size == 0:
            errors.append("The uploaded file is empty.")
            return errors, warnings

        is_real_xlsx = self._is_valid_xlsx(file_path)

        try:
            if not is_real_xlsx:
                raise zipfile.BadZipFile("Not a valid .xlsx")

            wb = openpyxl.load_workbook(file_path, data_only=True)
            ws = wb.active
            if ws is None or ws.max_row < 1:
                errors.append("Worksheet is empty or unreadable.")
                return errors, warnings

            header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
            headers = [str(cell).strip().lower() if cell is not None else "" for cell in header_row]
            col_map = self._get_column_indices(headers)

        except (zipfile.BadZipFile, openpyxl.utils.exceptions.InvalidFileException):
            errors.append("❌ This is **not** a valid .xlsx file. Use `paste` instead.")
            return errors, warnings
        except Exception as e:
            errors.append(f"Failed to read file: {type(e).__name__} – {e}")
            return errors, warnings

        required = {"name", "start"}
        missing = [col for col in required if col not in col_map]
        if missing:
            errors.append(f"Missing required column(s): {', '.join(missing)}")

        row_num = 1
        seen_names: set[str] = set()

        for row in ws.iter_rows(min_row=2, values_only=True):
            row_num += 1
            if not row or all(v is None for v in row):
                continue

            name = str(self._get_cell(row, col_map, "name", "")).strip()
            if not name:
                errors.append(f"Row {row_num}: Missing or empty **Name**")
                continue

            if len(name) > 100:
                errors.append(f"Row {row_num}: Name too long (max 100 characters)")

            key = self._normalize_key(name)
            if key in seen_names:
                warnings.append(f"Row {row_num}: Duplicate name '{name}'")
            seen_names.add(key)

            start_dt = await self._parse_datetime(self._get_cell(row, col_map, "start"))
            if not start_dt:
                errors.append(f"Row {row_num}: Invalid **Start** time format")
            elif start_dt < datetime.now(timezone.utc):
                warnings.append(f"Row {row_num}: Start time is in the past")

        if not seen_names:
            errors.append("No valid event rows found in the file.")

        return errors, warnings

    async def _reminder_task(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                for guild in self.bot.guilds:
                    config = self.config.guild(guild)
                    if not await config.reminder_mode():
                        continue
                    ch_id = await config.reminder_channel()
                    channel = guild.get_channel(ch_id) if ch_id else None
                    if not (channel and channel.permissions_for(guild.me).send_messages):
                        continue

                    mappings = await config.event_mappings()
                    reminder_sent = await config.reminder_sent() or {}

                    for event_id in list(mappings.values()):
                        try:
                            event = await guild.fetch_scheduled_event(event_id)
                            if event.status not in (discord.ScheduledEventStatus.scheduled, discord.ScheduledEventStatus.active):
                                continue
                            if not event.start_time:
                                continue

                            minutes_until = (event.start_time - datetime.now(timezone.utc)).total_seconds() / 60
                            for min_before in await config.reminder_minutes():
                                if min_before > 0 and abs(minutes_until - min_before) <= 7:
                                    sent_list = reminder_sent.get(str(event_id), [])
                                    if min_before not in sent_list:
                                        embed = self._create_reminder_embed(event, min_before)
                                        await channel.send(embed=embed)
                                        reminder_sent.setdefault(str(event_id), []).append(min_before)
                                        await asyncio.sleep(1.5)
                        except Exception:
                            continue

                    await config.reminder_sent.set(reminder_sent)
            except Exception:
                pass
            await asyncio.sleep(300)

    # ====================== COMMANDS ======================
    @commands.group(name="excelevents", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_events=True)
    async def excelevents(self, ctx: commands.Context):
        """Main command group for managing bulk Discord events from Excel/CSV."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @excelevents.command(name="guide")
    async def guide(self, ctx: commands.Context):
        """Shows a friendly, detailed getting started guide."""
        embed = discord.Embed(
            title="🎉 Excelevents - Getting Started!",
            description="Turn your spreadsheet into **beautiful Discord Scheduled Events** in seconds! 🚀\n\nLet me walk you through everything step-by-step:",
            color=discord.Color.gold()
        )

        embed.add_field(
            name="1. Prepare Your Spreadsheet",
            value=(
                "**Required columns:** `name`, `start`\n"
                "**Recommended:** `end`, `description`, `type` (voice/stage/external), `location`, `channelid`, `image`\n\n"
                "**Supported Date/Time formats:**\n"
                "• `2026-04-05 20:00`\n"
                "• `04/05/2026 8:00 PM`\n"
                "• `4/5/26 20:00`\n"
                "• Excel date serial numbers also work!\n\n"
                "For images: Use direct links (best with `.jpg` from Imgur) in the `image` column."
            ),
            inline=False
        )

        embed.add_field(
            name="2. Load Your Data",
            value=(
                "**Excel users:** `[p]excelevents upload` + attach your `.xlsx` file\n"
                "**CSV users:** `[p]excelevents paste` then paste your CSV data right after the command"
            ),
            inline=False
        )

        embed.add_field(
            name="3. Validate (Optional but Smart)",
            value="`[p]excelevents check`\n\nCatches errors or warnings before you sync. Highly recommended!",
            inline=False
        )

        embed.add_field(
            name="4. Sync to Discord",
            value=(
                "`[p]excelevents sync`\n"
                "(You can attach one image to the sync command to apply to **all** events)\n\n"
                "The bot will:\n"
                "• Create new events\n"
                "• Update existing ones\n"
                "• Delete events you removed from the spreadsheet\n"
                "• Automatically download and attach images"
            ),
            inline=False
        )

        embed.add_field(
            name="📣 Automatic Announcements",
            value=(
                "Setup: `[p]excelevents announcement toggle #announce-channel`\n\n"
                "**How it works with example:**\n"
                "Every time you run `sync` and **new** events are created, a nice embed is automatically posted in that channel.\n"
                "Example: If you just added \"Game Night\" and \"Movie Marathon\", both will instantly appear in #announcements so your whole server sees them right away!"
            ),
            inline=False
        )

        embed.add_field(
            name="⏰ Reminders",
            value=(
                "Setup:\n"
                "1. `[p]excelevents reminder toggle #reminder-channel`\n"
                "2. `[p]excelevents reminder times 60 15 5`\n\n"
                "**How it works with example:**\n"
                "If an event starts at 8:00 PM, the bot will automatically send reminders in the channel **60 minutes**, **15 minutes**, and **5 minutes** before it starts."
            ),
            inline=False
        )

        embed.add_field(
            name="🧹 Clear Data",
            value=(
                "`[p]excelevents clear`\n\n"
                "Deletes your local events file and resets all tracking.\n"
                "**Note:** This does **not** delete any events already created on Discord."
            ),
            inline=False
        )

        embed.set_footer(text="💡 Pro tip: Run `[p]excelevents template` to get a ready-to-use example CSV!")

        await ctx.send(embed=embed)

    @excelevents.command(name="template")
    async def template(self, ctx: commands.Context):
        """Sends a ready-to-use CSV template."""
        example = (
            "name,start,end,description,type,location,channelid,image\n"
            'Game Night,2026-04-05 20:00,2026-04-05 22:00,Weekly game night,voice,,"123456789012345678",https://i.imgur.com/3eQczTs.jpg\n'
        )
        await ctx.send(f"**CSV Template:**\n```csv\n{example}\n```")

    @excelevents.command(name="upload")
    async def upload(self, ctx: commands.Context):
        """Upload an .xlsx file to be used for events."""
        if not ctx.message.attachments:
            await ctx.send("❌ Please attach an `.xlsx` or `.xls` file.")
            return
        attachment = ctx.message.attachments[0]
        if not attachment.filename.lower().endswith((".xlsx", ".xls")):
            await ctx.send("❌ Only `.xlsx` or `.xls` files are supported.")
            return

        data_path: Path = data_manager.cog_data_path(self)
        data_path.mkdir(parents=True, exist_ok=True)
        file_path = data_path / "events.xlsx"

        if file_path.exists():
            file_path.unlink()

        await attachment.save(str(file_path))
        await ctx.send("✅ File uploaded (old file replaced). Use `check`.")

    @excelevents.command(name="paste")
    async def paste(self, ctx: commands.Context):
        """Paste CSV data to create the events file."""
        lines = ctx.message.content.splitlines()
        csv_text = "\n".join(lines[1:]) if len(lines) > 1 else ""

        if not csv_text.strip():
            await ctx.send("❌ Please paste CSV data after the command.")
            return

        data_path = data_manager.cog_data_path(self)
        data_path.mkdir(parents=True, exist_ok=True)
        file_path = data_path / "events.xlsx"

        if file_path.exists():
            file_path.unlink()

        try:
            input_io = io.StringIO(csv_text.strip())
            reader = csv.reader(input_io, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL, skipinitialspace=True)
            rows = [[cell.strip() for cell in row] for row in reader if row and any(cell.strip() for cell in row)]

            if len(rows) < 1:
                await ctx.send("❌ No valid rows found.")
                return
            if len(rows) - 1 > self.MAX_ROWS:
                rows = rows[:self.MAX_ROWS + 1]
                await ctx.send(f"⚠️ Only first {self.MAX_ROWS} events saved.")

            if rows:
                header_len = len(rows[0])
                for i in range(1, len(rows)):
                    rows[i] += [''] * (header_len - len(rows[i]))

            wb = openpyxl.Workbook()
            ws = wb.active
            for row in rows:
                ws.append(row)
            wb.save(file_path)

            await ctx.send(f"✅ CSV saved! **{len(rows)-1}** events loaded.\nUse `check`.")
        except Exception as e:
            await ctx.send(f"❌ Failed to parse CSV: {type(e).__name__} – {e}")

    @excelevents.command(name="check")
    async def check(self, ctx: commands.Context):
        """Validate the events file before syncing."""
        data_path = data_manager.cog_data_path(self)
        file_path = data_path / "events.xlsx"
        await ctx.send("🔍 Running validation...")
        errors, warnings = await self._validate_excel(file_path, ctx.guild)

        if errors:
            await ctx.send("**Validation Failed:**\n" + "\n".join(f"❌ {msg}" for msg in errors))
        elif warnings:
            await ctx.send("**✅ Valid with warnings:**\n" + "\n".join(f"⚠️ {msg}" for msg in warnings) + "\n\nYou may now run `sync`.")
        else:
            await ctx.send("✅ **Perfect!** Ready to sync.")

    @excelevents.command(name="sync")
    async def sync(self, ctx: commands.Context):
        """Sync the spreadsheet to Discord Scheduled Events (create/update/delete + images)."""
        if not ctx.guild.me.guild_permissions.manage_events:
            await ctx.send("❌ I need the **Manage Events** permission.")
            return

        data_path = data_manager.cog_data_path(self)
        file_path = data_path / "events.xlsx"
        if not file_path.exists():
            await ctx.send("❌ No file found. Use `upload` or `paste` first.")
            return

        errors, warnings = await self._validate_excel(file_path, ctx.guild)
        if errors:
            await ctx.send("⚠️ Validation failed. Run `check` first.")
            return

        await ctx.send("🔄 Syncing events with image support...")

        try:
            wb = openpyxl.load_workbook(file_path, data_only=True)
            ws = wb.active
            header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
            headers = [str(cell).strip().lower() if cell is not None else "" for cell in header_row]
            col_map = self._get_column_indices(headers)

            global_image_bytes = None
            if ctx.message.attachments:
                att = ctx.message.attachments[0]
                if att.content_type and att.content_type.startswith("image/") and att.size < self.MAX_IMAGE_SIZE:
                    global_image_bytes = await att.read()

            mappings = await self.config.guild(ctx.guild).event_mappings()
            new_mappings: Dict[str, int] = {}
            active_keys = set()
            processed = 0
            new_events_created = []

            row_num = 1
            for row in ws.iter_rows(min_row=2, values_only=True):
                row_num += 1
                if not row or all(v is None for v in row):
                    continue

                name = str(self._get_cell(row, col_map, "name", "")).strip()
                if not name:
                    continue

                key = self._normalize_key(name)
                active_keys.add(key)

                data = {
                    "name": name,
                    "start": self._get_cell(row, col_map, "start"),
                    "end": self._get_cell(row, col_map, "end"),
                    "description": self._get_cell(row, col_map, "description"),
                    "type": self._get_cell(row, col_map, "type"),
                    "location": self._get_cell(row, col_map, "location"),
                    "channelid": self._get_cell(row, col_map, "channelid"),
                }

                image_url = str(self._get_cell(row, col_map, "image", "")).strip()
                image_bytes = None

                if image_url:
                    image_bytes = await self._download_image(image_url)
                    if image_bytes:
                        await ctx.send(f"✅ Row {row_num}: Image downloaded ({len(image_bytes)//1024} KB) for **{name}**")
                    else:
                        await ctx.send(f"⚠️ Row {row_num}: Image failed for **{name}** — event created without cover")
                elif global_image_bytes:
                    image_bytes = global_image_bytes
                    await ctx.send(f"✅ Row {row_num}: Using attached image for **{name}**")

                start_time = await self._parse_datetime(data["start"])
                if not start_time:
                    continue

                if key in mappings:
                    try:
                        event = await ctx.guild.fetch_scheduled_event(mappings[key])
                        await self._update_event(event, data, image_bytes)
                        new_mappings[key] = event.id
                        processed += 1
                        continue
                    except Exception:
                        pass

                new_event = await self._create_event_with_image(ctx.guild, data, image_bytes)
                if new_event:
                    new_mappings[key] = new_event.id
                    new_events_created.append(new_event)
                    processed += 1
                else:
                    await ctx.send(f"⚠️ Failed to create event: {name}")

            # Cleanup
            deleted = 0
            for old_key, old_id in list(mappings.items()):
                if old_key not in active_keys:
                    try:
                        event = await ctx.guild.fetch_scheduled_event(old_id)
                        await event.delete()
                        deleted += 1
                    except Exception:
                        pass

            await self.config.guild(ctx.guild).event_mappings.set(new_mappings)
            await self.config.guild(ctx.guild).last_synced.set(datetime.now(timezone.utc).isoformat())

            # Write IDs and URLs back
            try:
                wb = openpyxl.load_workbook(file_path, data_only=True)
                ws = wb.active
                headers = [str(cell).strip().lower() if cell is not None else "" for cell in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]

                id_col = next((c + 1 for c, h in enumerate(headers) if h == "discord event id"), ws.max_column + 1)
                url_col = next((c + 1 for c, h in enumerate(headers) if h == "discord event url"), ws.max_column + 1)

                ws.cell(1, id_col, "Discord Event ID")
                ws.cell(1, url_col, "Discord Event URL")

                for r_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                    name = str(self._get_cell(row, col_map, "name", "")).strip()
                    if name and self._normalize_key(name) in new_mappings:
                        eid = new_mappings[self._normalize_key(name)]
                        try:
                            ev = await ctx.guild.fetch_scheduled_event(eid)
                            ws.cell(r_idx, id_col, eid)
                            ws.cell(r_idx, url_col, ev.url)
                        except Exception:
                            pass
                wb.save(file_path)
            except Exception:
                pass

            # Announcements
            announced = 0
            if await self.config.guild(ctx.guild).announcement_mode():
                ann_ch_id = await self.config.guild(ctx.guild).announcement_channel()
                if ann_ch_id:
                    channel = ctx.guild.get_channel(ann_ch_id)
                    if channel and channel.permissions_for(ctx.guild.me).send_messages:
                        for event in new_events_created:
                            try:
                                await channel.send(embed=self._create_event_embed(event))
                                announced += 1
                                await asyncio.sleep(0.8)
                            except Exception:
                                pass

            result = f"**✅ Sync Complete**\n• Processed: **{processed}**\n• Active: **{len(new_mappings)}**\n• Deleted: **{deleted}**"
            if announced:
                result += f"\n📢 Announced **{announced}** new events!"
            result += "\n📊 Spreadsheet updated with Discord Event IDs & URLs."
            await ctx.send(result)

        except Exception as e:
            await ctx.send(f"❌ Sync failed: {type(e).__name__}: {e}")

    @excelevents.command(name="status")
    async def status(self, ctx: commands.Context):
        """Show current status of the ExcelEvents cog."""
        data_path = data_manager.cog_data_path(self)
        file_path = data_path / "events.xlsx"
        mappings = await self.config.guild(ctx.guild).event_mappings()
        await ctx.send(
            f"**ExcelEvents Status**\n"
            f"• File exists: **{file_path.exists()}**\n"
            f"• Tracked events: **{len(mappings)}**"
        )

    @excelevents.group(name="announcement", invoke_without_command=True)
    async def announcement_group(self, ctx: commands.Context):
        """Manage announcement settings for new events."""
        await ctx.send_help(ctx.command)

    @announcement_group.command(name="toggle")
    async def toggle_announcement(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Toggle or set the announcement channel."""
        config = self.config.guild(ctx.guild)
        if channel is None:
            new_mode = not await config.announcement_mode()
            await config.announcement_mode.set(new_mode)
            await ctx.send(f"✅ Announcement mode **{'enabled' if new_mode else 'disabled'}**.")
            return
        await config.announcement_channel.set(channel.id)
        await config.announcement_mode.set(True)
        await ctx.send(f"✅ Announcement mode enabled → {channel.mention}")

    @excelevents.group(name="reminder", invoke_without_command=True)
    async def reminder_group(self, ctx: commands.Context):
        """Manage reminder settings."""
        await ctx.send_help(ctx.command)

    @reminder_group.command(name="toggle")
    async def toggle_reminder(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Toggle or set the reminder channel."""
        config = self.config.guild(ctx.guild)
        if channel is None:
            new_mode = not await config.reminder_mode()
            await config.reminder_mode.set(new_mode)
            await ctx.send(f"✅ Reminder mode **{'enabled' if new_mode else 'disabled'}**.")
            return
        await config.reminder_channel.set(channel.id)
        await config.reminder_mode.set(True)
        await ctx.send(f"✅ Reminder mode enabled → {channel.mention}")

    @reminder_group.command(name="times")
    async def reminder_times(self, ctx: commands.Context, *minutes: int):
        """Set reminder times (in minutes before start)."""
        valid = [m for m in minutes if m > 0]
        if not valid:
            await ctx.send("❌ Please provide positive numbers.")
            return
        await self.config.guild(ctx.guild).reminder_minutes.set(valid)
        await ctx.send(f"✅ Reminder times updated to: **{valid}** minutes before start.")

    @excelevents.command(name="clear")
    async def clear(self, ctx: commands.Context):
        """Delete the events file and reset mappings."""
        data_path = data_manager.cog_data_path(self)
        file_path = data_path / "events.xlsx"
        if file_path.exists():
            file_path.unlink()
            await self.config.guild(ctx.guild).event_mappings.set({})
            await ctx.send("✅ Events file deleted and mappings reset.")
        else:
            await ctx.send("No file to clear.")

    @excelevents.command(name="testimage")
    async def testimage(self, ctx: commands.Context, *, url: str):
        """Debug tool: Test downloading a single image URL."""
        await ctx.send(f"🔍 Testing: `{url}`")
        image_bytes = await self._download_image(url)
        if image_bytes:
            await ctx.send(f"✅ Success! Downloaded **{len(image_bytes)//1024} KB** image.")
        else:
            await ctx.send("❌ Download failed.")


async def setup(bot: Red):
    await bot.add_cog(ExcelEvents(bot))
