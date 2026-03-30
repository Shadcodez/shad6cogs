import asyncio
import datetime
import json
import re
import aiohttp
import discord
from redbot.core import commands
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS

# ────────────────────────────────────────────────
# Queries (unchanged — Anilist GraphQL still works the same in 2026)
# ────────────────────────────────────────────────

SEARCH_ANIME_MANGA_QUERY = """
query ($id: Int, $page: Int, $search: String, $type: MediaType) {
    Page (page: $page, perPage: 10) {
        media (id: $id, search: $search, type: $type) {
            id
            idMal
            description(asHtml: false)
            title {
                english
                romaji
            }
            coverImage {
                medium
            }
            bannerImage
            averageScore
            meanScore
            status
            episodes
            chapters
            genres
            studios {
                nodes {
                    name
                }
            }
            externalLinks {
                url
                site
            }
            nextAiringEpisode {
                timeUntilAiring
            }
        }
    }
}
"""

SEARCH_CHARACTER_QUERY = """
query ($id: Int, $page: Int, $search: String) {
  Page(page: $page, perPage: 10) {
    characters(id: $id, search: $search) {
      id
      description (asHtml: true),
      name {
        first
        last
        native
      }
      image {
        large
      }
      media {
        nodes {
          id
          type
          title {
            romaji
            english
            native
            userPreferred
          }
        }
      }
    }
  }
}
"""

SEARCH_USER_QUERY = """
query ($id: Int, $page: Int, $search: String) {
    Page (page: $page, perPage: 10) {
        users (id: $id, search: $search) {
            id
            name
            siteUrl
            avatar {
                large
            }
            about (asHtml: true),
            stats {
                watchedTime
                chaptersRead
            }
            favourites {
                manga {
                  nodes {
                    id
                    title {
                      romaji
                      english
                      native
                      userPreferred
                    }
                  }
                }
                characters {
                  nodes {
                    id
                    name {
                      first
                      last
                      native
                    }
                  }
                }
                anime {
                  nodes {
                    id
                    title {
                      romaji
                      english
                      native
                      userPreferred
                    }
                  }
                }
            }
        }
    }
}
"""

class AniSearch(commands.Cog):
    """Search for anime, manga, characters and users using Anilist + direct MyAnimeList (Jikan v4) support.
    
    Modern 2026 Redbot updates:
    • Hybrid commands (prefix + slash support)
    • Persistent aiohttp session (better performance)
    • Added genres + studios to Anilist embeds
    • Direct MAL search via Jikan (full compatibility with both sites)
    • Fixed spoiler regex, better error handling, cleaner code
    • Backward-compatible reaction menu (still works perfectly in 2026)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.anilist_url = "https://graphql.anilist.co"
        self.jikan_url = "https://api.jikan.moe/v4"
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        await self.session.close()

    def format_name(self, first_name: str | None, last_name: str | None) -> str:
        if first_name and last_name:
            return f"{first_name} {last_name}"
        return first_name or last_name or "No name"

    def clean_html(self, description: str | None) -> str:
        if not description:
            return ""
        cleanr = re.compile("<.*?>")
        return re.sub(cleanr, "", description)

    def clean_spoilers(self, description: str | None) -> str:
        if not description:
            return ""
        cleanr = re.compile(r'<span class=["\']?spoiler["\']?[^>]*>.*?</span>', re.DOTALL)
        return re.sub(cleanr, "[SPOILER]", description)

    def description_parser(self, description: str | None) -> str:
        description = self.clean_spoilers(description)
        description = self.clean_html(description)
        description = "\n".join(description.split("\n")[:5])
        return description[:400] + "..." if len(description) > 400 else description

    def list_maximum(self, items: list) -> list:
        if len(items) > 5:
            return items[:5] + [f"+ {len(items) - 5} more"]
        return items

    async def _request_anilist(self, query: str, variables: dict | None = None):
        if variables is None:
            variables = {}
        request_json = {"query": query, "variables": variables}
        headers = {"content-type": "application/json"}

        async with self.session.post(self.anilist_url, json=request_json, headers=headers) as response:
            return await response.json()

    async def _search_anime_manga_anilist(self, cmd: str, entered_title: str):
        MediaStatusToString = {
            "FINISHED": "Finished",
            "RELEASING": "Releasing",
            "NOT_YET_RELEASED": "Not yet released",
            "CANCELLED": "Cancelled",
        }

        variables = {"search": entered_title, "page": 1, "type": cmd.upper()}
        resp = await self._request_anilist(SEARCH_ANIME_MANGA_QUERY, variables)
        data = resp.get("data", {}).get("Page", {}).get("media", [])

        if not data:
            return None

        embeds = []
        for media in data:
            link = f"https://anilist.co/{cmd.lower()}/{media['id']}"
            title = media["title"]["english"] or media["title"]["romaji"] or "Unknown"
            description = media.get("description", "")

            time_left = "Never"
            if media.get("nextAiringEpisode"):
                seconds = media["nextAiringEpisode"]["timeUntilAiring"]
                time_left = str(datetime.timedelta(seconds=seconds))

            external_links = ", ".join(
                f"[{link['site']}]({link['url']})" for link in media.get("externalLinks", [])
            ) or None

            embed = discord.Embed(title=title, url=link, color=0x3498DB)
            embed.description = self.description_parser(description)
            embed.set_thumbnail(url=media["coverImage"]["medium"])

            if banner := media.get("bannerImage"):
                embed.set_image(url=banner)

            embed.add_field(name="Score", value=media.get("averageScore") or media.get("meanScore") or "N/A")
            if cmd.upper() == "ANIME":
                embed.add_field(name="Episodes", value=media.get("episodes", "N/A"))
            else:
                embed.add_field(name="Chapters", value=media.get("chapters", "N/A"))

            if genres := ", ".join(media.get("genres", [])):
                embed.add_field(name="Genres", value=genres, inline=False)

            if studios := [n["name"] for n in media.get("studios", {}).get("nodes", [])]:
                embed.add_field(name="Studios", value=", ".join(studios), inline=False)

            if external_links:
                embed.add_field(name="Links", value=external_links, inline=False)

            mal_id = media.get("idMal")
            mal_link = f"https://myanimelist.net/{cmd.lower()}/{mal_id}" if mal_id else "N/A"
            embed.add_field(
                name="More info",
                value=f"[Anilist]({link}) • [MyAnimeList]({mal_link})",
                inline=False,
            )

            embed.set_footer(
                text=f"Status: {MediaStatusToString.get(media['status'], media['status'] or 'N/A')} • Next: {time_left} • Powered by Anilist"
            )
            embeds.append(embed)

        return embeds

    async def _request_jikan(self, path: str, params: dict | None = None):
        if params is None:
            params = {}
        async with self.session.get(f"{self.jikan_url}/{path}", params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

    async def _search_mal(self, cmd: str, entered_title: str):
        data_resp = await self._request_jikan(cmd, {"q": entered_title, "limit": 10, "sfw": "true"})
        items = data_resp.get("data", []) if data_resp else []

        if not items:
            return None

        embeds = []
        for item in items:
            entry = item.get("entry", item)  # Jikan sometimes nests
            title = entry.get("title") or entry.get("title_english") or "Unknown"
            link = entry["url"]
            synopsis = entry.get("synopsis", "No synopsis available.")
            image = entry.get("images", {}).get("jpg", {}).get("large_image_url")

            embed = discord.Embed(title=title, url=link, color=0x3498DB)
            embed.description = self.description_parser(synopsis)
            if image:
                embed.set_thumbnail(url=image)

            embed.add_field(name="Score", value=entry.get("score") or "N/A")
            if cmd == "anime":
                embed.add_field(name="Episodes", value=entry.get("episodes", "N/A"))
            else:
                embed.add_field(name="Chapters", value=entry.get("chapters", "N/A"))

            embed.add_field(name="Status", value=entry.get("status", "N/A"), inline=False)
            embed.set_footer(text="Powered by Jikan (MyAnimeList) • Direct MAL search")
            embeds.append(embed)

        return embeds

    @commands.hybrid_command()
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def anime(self, ctx: commands.Context, *, entered_title: str):
        """Search anime on Anilist (with MAL link)"""
        try:
            embeds = await self._search_anime_manga_anilist("anime", entered_title)
            if embeds:
                await menu(ctx, pages=embeds, controls=DEFAULT_CONTROLS, page=0, timeout=60)
            else:
                await ctx.send("❌ No anime found.")
        except Exception as e:
            await ctx.send(f"❌ Error while searching Anilist: {e}")

    @commands.hybrid_command()
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def manga(self, ctx: commands.Context, *, entered_title: str):
        """Search manga on Anilist (with MAL link)"""
        try:
            embeds = await self._search_anime_manga_anilist("manga", entered_title)
            if embeds:
                await menu(ctx, pages=embeds, controls=DEFAULT_CONTROLS, page=0, timeout=60)
            else:
                await ctx.send("❌ No manga found.")
        except Exception as e:
            await ctx.send(f"❌ Error while searching Anilist: {e}")

    @commands.hybrid_command()
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def malanime(self, ctx: commands.Context, *, entered_title: str):
        """Search anime directly on MyAnimeList via Jikan"""
        try:
            embeds = await self._search_mal("anime", entered_title)
            if embeds:
                await menu(ctx, pages=embeds, controls=DEFAULT_CONTROLS, page=0, timeout=60)
            else:
                await ctx.send("❌ No anime found on MAL.")
        except Exception as e:
            await ctx.send(f"❌ Error while searching MyAnimeList: {e}")

    @commands.hybrid_command()
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def malmanga(self, ctx: commands.Context, *, entered_title: str):
        """Search manga directly on MyAnimeList via Jikan"""
        try:
            embeds = await self._search_mal("manga", entered_title)
            if embeds:
                await menu(ctx, pages=embeds, controls=DEFAULT_CONTROLS, page=0, timeout=60)
            else:
                await ctx.send("❌ No manga found on MAL.")
        except Exception as e:
            await ctx.send(f"❌ Error while searching MyAnimeList: {e}")

    @commands.hybrid_command()
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def character(self, ctx: commands.Context, *, entered_title: str):
        """Search characters on Anilist"""
        try:
            variables = {"search": entered_title, "page": 1}
            resp = await self._request_anilist(SEARCH_CHARACTER_QUERY, variables)
            data = resp.get("data", {}).get("Page", {}).get("characters", [])

            if not data:
                await ctx.send("❌ No characters found.")
                return

            embeds = []
            for char in data:
                link = f"https://anilist.co/character/{char['id']}"
                anime_list = [
                    f"[{a['title']['userPreferred']}](https://anilist.co/anime/{a['id']})"
                    for a in char["media"]["nodes"] if a["type"] == "ANIME"
                ]
                manga_list = [
                    f"[{m['title']['userPreferred']}](https://anilist.co/manga/{m['id']})"
                    for m in char["media"]["nodes"] if m["type"] == "MANGA"
                ]

                embed = discord.Embed(
                    title=self.format_name(char["name"]["first"], char["name"]["last"]),
                    url=link,
                    color=0x3498DB
                )
                embed.description = self.description_parser(char.get("description"))
                embed.set_thumbnail(url=char["image"]["large"])

                if anime_list:
                    embed.add_field(name="Anime", value="\n".join(self.list_maximum(anime_list)), inline=False)
                if manga_list:
                    embed.add_field(name="Manga", value="\n".join(self.list_maximum(manga_list)), inline=False)

                embed.set_footer(text="Powered by Anilist")
                embeds.append(embed)

            await menu(ctx, pages=embeds, controls=DEFAULT_CONTROLS, page=0, timeout=60)
        except Exception as e:
            await ctx.send(f"❌ Error while searching Anilist: {e}")

    @commands.hybrid_command()
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def user(self, ctx: commands.Context, *, entered_title: str):
        """Search users on Anilist"""
        try:
            variables = {"search": entered_title, "page": 1}
            resp = await self._request_anilist(SEARCH_USER_QUERY, variables)
            data = resp.get("data", {}).get("Page", {}).get("users", [])

            if not data:
                await ctx.send("❌ No users found.")
                return

            embeds = []
            for user in data:
                link = f"https://anilist.co/user/{user['id']}"
                embed = discord.Embed(title=user["name"], url=link, color=0x3498DB)
                embed.description = self.description_parser(user.get("about"))
                embed.set_thumbnail(url=user["avatar"]["large"])

                embed.add_field(
                    name="Watch time",
                    value=str(datetime.timedelta(minutes=user["stats"].get("watchedTime", 0)))
                )
                embed.add_field(name="Chapters read", value=user["stats"].get("chaptersRead", "N/A"))

                for cat in ("anime", "manga", "characters"):
                    fav_list = []
                    for node in user["favourites"][cat]["nodes"]:
                        if cat == "characters":
                            name = self.format_name(node["name"]["first"], node["name"]["last"])
                            url_path = "character"
                        else:
                            name = node["title"]["userPreferred"]
                            url_path = cat
                        fav_list.append(f"[{name}](https://anilist.co/{url_path}/{node['id']})")

                    if fav_list:
                        embed.add_field(
                            name=f"Favorite {cat.title()}",
                            value="\n".join(self.list_maximum(fav_list)),
                            inline=False
                        )

                embed.set_footer(text="Powered by Anilist")
                embeds.append(embed)

            await menu(ctx, pages=embeds, controls=DEFAULT_CONTROLS, page=0, timeout=60)
        except Exception as e:
            await ctx.send(f"❌ Error while searching Anilist: {e}")