import os
import re
import json
import logging
import tempfile
import asyncio

import httpx
from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(config: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

config = load_config()

# ---------------- CONFIG ----------------
API_ID = int(os.environ.get("TELEGRAM_API_ID"))
API_HASH = os.environ.get("TELEGRAM_API_HASH")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
BRAND = "gravida_anime"
ADMIN_ID = 7483574670
CHANNEL_USERNAME = "@gravida_anime"

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise ValueError("Missing required env vars: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN")

app = Client(
    "gravida_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

allowed_users = set([ADMIN_ID])
user_data = {}
anime_cache: dict = {}

async def _parse_jikan_entry(a: dict) -> dict:
    mal_id = str(a.get("mal_id", ""))
    title = a.get("title_english") or a.get("title", "Unknown")
    score = a.get("score") or "N/A"
    episodes = a.get("episodes") or "?"
    status = a.get("status", "")
    year = a.get("year") or (a.get("aired", {}).get("prop", {}).get("from", {}).get("year", ""))
    genres = ", ".join(g["name"] for g in a.get("genres", [])[:3])
    synopsis = (a.get("synopsis") or "No synopsis available.")[:300].rstrip()
    if len(a.get("synopsis") or "") > 300:
        synopsis += "…"
    image = a.get("images", {}).get("jpg", {}).get("image_url", "")
    entry = {
        "mal_id": mal_id, "title": title, "score": score,
        "episodes": episodes, "status": status, "year": year,
        "genres": genres, "synopsis": synopsis, "image": image,
    }
    anime_cache[mal_id] = entry
    return entry

async def fetch_trending_anime() -> list[dict]:
    url = "https://api.jikan.moe/v4/top/anime?filter=airing&limit=12"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json().get("data", [])
            results = []
            for a in data:
                mal_id = str(a.get("mal_id", ""))
                title = a.get("title_english") or a.get("title", "Unknown")
                score = a.get("score") or "N/A"
                episodes = a.get("episodes") or "?"
                status = a.get("status", "")
                year = a.get("year") or (a.get("aired", {}).get("prop", {}).get("from", {}).get("year", ""))
                genres = ", ".join(g["name"] for g in a.get("genres", [])[:3])
                synopsis = (a.get("synopsis") or "No synopsis available.")[:300].rstrip()
                if len(a.get("synopsis") or "") > 300:
                    synopsis += "…"
                image = a.get("images", {}).get("jpg", {}).get("image_url", "")
                entry = {
                    "mal_id": mal_id, "title": title, "score": score,
                    "episodes": episodes, "status": status, "year": year,
                    "genres": genres, "synopsis": synopsis, "image": image,
                }
                anime_cache[mal_id] = entry
                results.append(entry)
            return results
    except Exception as e:
        logger.error(f"Jikan fetch failed: {e}")
        return []

async def search_anime(q: str) -> list[dict]:
    url = "https://api.jikan.moe/v4/anime"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params={"q": q, "limit": 8, "order_by": "popularity", "sort": "asc"})
            r.raise_for_status()
            data = r.json().get("data", [])
            return [await _parse_jikan_entry(a) for a in data]
    except Exception as e:
        logger.error(f"Jikan search failed: {e}")
        return []

# ---------------- SECURITY ----------------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def is_allowed(user_id: int) -> bool:
    return user_id in allowed_users

def ensure_user(user_id: int):
    if user_id not in user_data:
        user_data[user_id] = {
            "animes": {},
            "current_anime": None,
            "file_id": None,
            "file_size": 0,
            "file_name": None,
            "caption": None,
            "parsed": None,
            "waiting_for": None,
            "last_search": None,
            "sort_mode": False,
            "sort_queue": [],
            "sort_task": None,
        }

# ---------------- FILENAME PARSER ----------------
JUNK_WORDS = re.compile(
    r"\b(1080p|720p|480p|360p|2160p|4K|UHD|BluRay|BDRip|BDRemux|"
    r"WEB[\-\s]?DL|WEBRip|WEB|HDTV|DVDRip|DVD|"
    r"HEVC|AVC|x264|x265|h264|h265|xvid|"
    r"AAC|AC3|DDP|DD2|DD5|EAC3|FLAC|MP3|Opus|"
    r"Dual[\s\-]?Audio|Multi[\s\-]?Sub|Eng(?:lish)?|Jap(?:anese)?|"
    r"SubsPlease|HorribleSubs|Erai[\-\s]?raws|NanDesuKa|"
    r"EMBER|YIFY|YTS|RARBG|mkv|mp4|avi|10bit|8bit|Hi10P|"
    r"Season|Complete|Batch|OVA|ONA|Special)\b",
    re.IGNORECASE
)

def parse_anime_filename(filename: str) -> dict:
    stem, _, ext = filename.rpartition(".")
    if not stem:
        stem = filename
        ext = ""

    stem = re.sub(r"\[([^\]]*)\]", " ", stem)
    stem = re.sub(r"\(([^\)]*)\)", " ", stem)
    stem = stem.replace("_", " ").replace(".", " ")
    stem = re.sub(r"\s+", " ", stem).strip()

    season = 1
    episode = None

    s_ep = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", stem)
    if s_ep:
        season = int(s_ep.group(1))
        episode = int(s_ep.group(2))
        stem = stem[:s_ep.start()].strip()
    else:
        ep_match = re.search(
            r"(?:^|\s)(?:Episode|Ep\.?|EP\.?)\s*[\-\s]*(\d{1,3})(?:\s|$)",
            stem, re.IGNORECASE
        )
        if ep_match:
            episode = int(ep_match.group(1))
            stem = (stem[:ep_match.start()] + stem[ep_match.end():]).strip()
        else:
            bare = re.search(r"(?<!\d)(\d{1,3})(?!\d)", stem)
            if bare:
                candidate = int(bare.group(1))
                if 1 <= candidate <= 999:
                    episode = candidate
                    stem = (stem[:bare.start()] + stem[bare.end():]).strip()

    stem = JUNK_WORDS.sub(" ", stem)
    stem = re.sub(r"[\u0400-\u04FF\u0500-\u052F]+", " ", stem)
    stem = re.sub(r"[\-–_]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    stem = re.sub(r"^[\s\-–]+|[\s\-–]+$", "", stem).strip()

    title = stem.title() if len(stem) >= 2 else "Unknown Anime"
    return {"title": title, "season": season, "episode": episode, "ext": ext}

def build_new_filename(title: str, season: int, episode: int, ext: str) -> str:
    name = f"[@{BRAND}] {title} - S{season:02d}E{episode:02d}"
    return f"{name}.{ext}" if ext else name

def get_thumbnail_id() -> str | None:
    return config.get("thumbnail_id")

def set_thumbnail_id(file_id: str | None):
    config["thumbnail_id"] = file_id
    save_config(config)

def file_keyboard(current_anime: str) -> InlineKeyboardMarkup:
    rows = []
    if current_anime:
        rows.append([InlineKeyboardButton(
            f"📺 {current_anime}", callback_data=f"anime_{current_anime}"
        )])
    rows.append([InlineKeyboardButton("➕ Change Anime", callback_data="anime_new")])
    rows.append([
        InlineKeyboardButton("✏️ Rename", callback_data="rename"),
        InlineKeyboardButton("🎬 Episode +1", callback_data="episode"),
    ])
    rows.append([InlineKeyboardButton("📝 Caption", callback_data="caption")])
    rows.append([InlineKeyboardButton("📤 Post to Channel", callback_data="post")])
    return InlineKeyboardMarkup(rows)

def status_text(uid: int) -> str:
    d = user_data[uid]
    anime = d["current_anime"] or "None selected"
    ep = d["animes"].get(anime, {}).get("episode", 0) if d["current_anime"] else 0
    fname = d["file_name"] or "No file"
    size_mb = d.get("file_size", 0) / (1024 * 1024)
    thumb = "✅ Set" if get_thumbnail_id() else "❌ None"
    return (
        f"📂 **File:** `{fname}`\n"
        f"📦 **Size:** {size_mb:.1f} MB\n"
        f"📺 **Anime:** {anime}\n"
        f"🎬 **Episode:** {ep}\n"
        f"🖼 **Thumbnail:** {thumb}"
    )

# ---------------- COMMANDS ----------------
@app.on_message(filters.command("start") & filters.private)
async def start(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return
    await message.reply_text(
        f"🎌 **Gravida Anime Bot Online**\n\n"
        "Send me any anime file and I'll:\n"
        "• Auto-detect title, season & episode\n"
        "• Rename with `[@gravida_anime] Title` branding\n"
        "• Handle files up to **2 GB**\n"
        "• Attach a custom thumbnail\n"
        "• Track episodes, generate captions & post to channel\n\n"
        "**Commands:**\n"
        "/start — Welcome message\n"
        "/help — How to use\n"
        "/episodes — View episode tracker\n"
        "/recommend — Browse anime list\n"
        "/adduser <id> — Grant access _(admin)_\n"
        "/removeuser <id> — Revoke access _(admin)_\n"
        "/users — List allowed users _(admin)_",
        quote=True
    )

@app.on_message(filters.command("help") & filters.private)
async def help_command(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return
    user_id = message.from_user.id
    ensure_user(user_id)
    anime = user_data[user_id].get("current_anime") or ""
    await message.reply_text(
        "📖 **How to use:**\n\n"
        "1. Send any anime file — bot renames it instantly\n"
        "2. Use the action buttons below to manage it:\n\n"
        "   • **➕ Change Anime** — type a new anime name\n"
        "   • **✏️ Rename** — re-rename with current settings\n"
        "   • **🎬 Episode +1** — increment episode counter\n"
        "   • **🖼 Set Thumbnail** — send any photo to save as thumbnail\n"
        "   • **📝 Caption** — generate a post caption\n"
        "   • **📤 Post** — send file to the channel\n\n"
        "**Output format:**\n"
        f"`[@{BRAND}] Anime Title - S01E03.mkv`\n\n"
        "**Other commands:**\n"
        "/recommend — browse popular anime list\n"
        "/sort — collect files and send them in season/episode order\n"
        "/search — search for a specific anime\n"
        "/episodes — view episode tracker\n"
        "/thumbnail — manage saved thumbnail\n"
        "/clearthumb — remove saved thumbnail\n\n"
        "**Max file size:** 2 GB",
        reply_markup=file_keyboard(anime) if user_data[user_id].get("file_id") else None,
        quote=True
    )

@app.on_message(filters.command("search") & filters.private)
async def search_command(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return
    user_id = message.from_user.id
    ensure_user(user_id)

    query_text = " ".join(message.command[1:]).strip()
    if not query_text:
        await message.reply_text(
            "🔎 **Usage:** `/search <anime name>`\n\n"
            "Example: `/search Attack on Titan`",
            quote=True
        )
        return

    loading = await message.reply_text(f"🔍 Searching for **{query_text}**…", quote=True)
    results = await search_anime(query_text)

    if not results:
        await loading.edit_text(
            f"❌ No results found for **{query_text}**.\n\nTry a different spelling or use /recommend."
        )
        return

    user_data[user_id]["last_search"] = query_text

    rows = []
    row = []
    for a in results:
        row.append(InlineKeyboardButton(f"📺 {a['title']}", callback_data=f"ainfo_{a['mal_id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    await loading.edit_text(
        f"🔎 **Results for:** {query_text}\n\n"
        "Tap any title to see its details:",
        reply_markup=InlineKeyboardMarkup(rows)
    )

@app.on_message(filters.command("recommend") & filters.private)
async def recommend_command(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return
    user_id = message.from_user.id
    ensure_user(user_id)
    user_data[user_id]["last_search"] = None

    loading = await message.reply_text("🔍 Fetching trending anime…", quote=True)
    anime_list = await fetch_trending_anime()

    if not anime_list:
        await loading.edit_text("❌ Could not fetch trending anime right now. Try again later.")
        return

    rows = []
    row = []
    for a in anime_list:
        row.append(InlineKeyboardButton(f"📺 {a['title']}", callback_data=f"ainfo_{a['mal_id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    await loading.edit_text(
        "🔥 **Trending Anime Right Now**\n\n"
        "Tap any title to view details:",
        reply_markup=InlineKeyboardMarkup(rows)
    )
    rows.append([InlineKeyboardButton("🔎 Search instead", callback_data="search_prompt")])
    await loading.edit_reply_markup(InlineKeyboardMarkup(rows))

@app.on_message(filters.command("episodes") & filters.private)
async def episodes_command(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return
    user_id = message.from_user.id
    ensure_user(user_id)
    animes = user_data[user_id].get("animes", {})
    if not animes:
        await message.reply_text(
            "📭 No episodes tracked yet. Send a file to start.",
            quote=True
        )
        return
    lines = [f"📺 **{a}** — Episode `{info.get('episode', 0):02d}`"
             for a, info in sorted(animes.items())]
    await message.reply_text("🎬 **Episode Tracker:**\n\n" + "\n".join(lines), quote=True)

@app.on_message(filters.command("adduser") & filters.private)
async def add_user(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        user_id = int(message.command[1])
        allowed_users.add(user_id)
        await message.reply_text(f"✅ User `{user_id}` added.", quote=True)
    except (IndexError, ValueError):
        await message.reply_text("Usage: `/adduser <user_id>`", quote=True)

@app.on_message(filters.command("removeuser") & filters.private)
async def remove_user(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        user_id = int(message.command[1])
        allowed_users.discard(user_id)
        await message.reply_text(f"❌ User `{user_id}` removed.", quote=True)
    except (IndexError, ValueError):
        await message.reply_text("Usage: `/removeuser <user_id>`", quote=True)

@app.on_message(filters.command("users") & filters.private)
async def list_users(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    users = "\n".join(f"• `{u}`" for u in sorted(allowed_users))
    await message.reply_text(f"👥 **Allowed Users:**\n{users}", quote=True)

@app.on_message(filters.command("thumbnail") & filters.private)
async def thumbnail_command(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return
    tid = get_thumbnail_id()
    if tid:
        await message.reply_text(
            "🖼 **Thumbnail Settings**\n\n"
            "Status: ✅ **A thumbnail is saved**\n\n"
            "• Send me a new photo anytime to replace it\n"
            "• Reply `/clearthumb` to remove it",
            quote=True
        )
    else:
        await message.reply_text(
            "🖼 **Thumbnail Settings**\n\n"
            "Status: ❌ **No thumbnail set**\n\n"
            "Send me any photo and it will be saved permanently as your thumbnail.\n"
            "It will appear on every file you rename or post to the channel.",
            quote=True
        )

@app.on_message(filters.command("clearthumb") & filters.private)
async def clearthumb_command(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return
    set_thumbnail_id(None)
    await message.reply_text(
        "🗑 **Thumbnail cleared.**\n\nFiles will be sent without a thumbnail from now on.\nSend any photo to set a new one.",
        quote=True
    )

# ---------------- SORTING MODE ----------------
SORT_IDLE_SECONDS = 5

def sort_file_key(item: dict) -> tuple:
    parsed = item["parsed"]
    title = parsed.get("title", "Unknown Anime").casefold()
    season = parsed.get("season") or 1
    episode = parsed.get("episode")
    return (
        title,
        season,
        episode if episode is not None else float("inf"),
        item["order"],
    )

async def sort_batch_after_idle(user_id: int, chat_id: int):
    try:
        await asyncio.sleep(SORT_IDLE_SECONDS)

        d = user_data.get(user_id)
        if not d or not d.get("sort_mode"):
            return

        # Close the batch before sending so later files use the normal flow.
        d["sort_mode"] = False
        d["sort_task"] = None
        queue = d.get("sort_queue", [])
        d["sort_queue"] = []

        if not queue:
            await app.send_message(chat_id, "✅ I am done, sensei.")
            return

        ordered = sorted(queue, key=sort_file_key)
        await app.send_message(
            chat_id,
            f"📚 **Sorting complete**\n\n"
            f"Sending {len(ordered)} file(s) in Season/Episode order…"
        )

        failures = 0
        with tempfile.TemporaryDirectory() as tmpdir:
            thumb_path = None
            thumbnail_id = get_thumbnail_id()
            if thumbnail_id:
                thumb_path = os.path.join(tmpdir, "sort_thumb.jpg")
                try:
                    await app.download_media(thumbnail_id, file_name=thumb_path)
                except Exception as e:
                    logger.warning(f"Could not download sorting thumbnail: {e}")
                    thumb_path = None

            for index, item in enumerate(ordered, start=1):
                original_name = item["file_name"] or f"file_{index}.mkv"
                safe_name = os.path.basename(original_name) or f"file_{index}.mkv"
                local_path = os.path.join(tmpdir, f"{index}_{safe_name}")
                try:
                    await app.download_media(item["file_id"], file_name=local_path)
                    await app.send_document(
                        chat_id,
                        document=local_path,
                        file_name=original_name,
                        caption=f"**{original_name}**",
                        thumb=thumb_path,
                    )
                except Exception as e:
                    failures += 1
                    logger.error(f"Sorting file failed ({original_name}): {e}")
                    await app.send_message(
                        chat_id,
                        f"⚠️ Could not send `{original_name}`:\n`{e}`"
                    )

        if failures:
            await app.send_message(
                chat_id,
                f"✅ I am done, sensei.\n\n"
                f"{len(ordered) - failures}/{len(ordered)} file(s) sent successfully."
            )
        else:
            await app.send_message(chat_id, "✅ I am done, sensei.")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Sorting batch failed: {e}")
        d = user_data.get(user_id)
        if d:
            d["sort_mode"] = False
            d["sort_task"] = None
            d["sort_queue"] = []
        await app.send_message(chat_id, f"❌ Sorting failed:\n`{e}`")

@app.on_message(filters.command("sort") & filters.private)
async def sort_command(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return

    user_id = message.from_user.id
    ensure_user(user_id)
    d = user_data[user_id]

    current_task = d.get("sort_task")
    if current_task and not current_task.done():
        current_task.cancel()

    d["sort_mode"] = True
    d["sort_queue"] = []
    d["sort_task"] = None
    d["waiting_for"] = None

    await message.reply_text(
        "📚 **Sorting mode enabled**\n\n"
        "Send your anime files in any order. I will keep their original names "
        f"and send them in Season/Episode order after {SORT_IDLE_SECONDS} seconds "
        "without a new file.\n\n"
        "Example: `1, 6, 2, 5, 4, 3` → `1, 2, 3, 4, 5, 6`",
        quote=True
    )

# ---------------- FILE RENAME + SEND ----------------
async def send_renamed_file(message: Message, user_id: int):
    d = user_data[user_id]
    file_id = d.get("file_id")
    parsed = d.get("parsed")
    anime = d.get("current_anime") or (parsed["title"] if parsed else "Unknown Anime")
    season = parsed["season"] if parsed else 1
    ep_num = parsed["episode"] if parsed and parsed["episode"] else 1
    ext = parsed["ext"] if parsed else "mkv"

    new_name = build_new_filename(anime, season, ep_num, ext)
    d["file_name"] = new_name

    size_mb = d.get("file_size", 0) / (1024 * 1024)
    status_msg = await message.reply_text(
        f"⏳ Renaming...\n📦 {size_mb:.1f} MB — please wait.",
        quote=False
    )

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = os.path.join(tmpdir, new_name)
            await app.download_media(file_id, file_name=tmp_path)

            thumb_path = None
            if get_thumbnail_id():
                thumb_path = os.path.join(tmpdir, "thumb.jpg")
                await app.download_media(get_thumbnail_id(), file_name=thumb_path)

            await message.reply_document(
                document=tmp_path,
                file_name=new_name,
                caption=f"**{new_name}**",
                thumb=thumb_path,
                quote=False
            )
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Rename/send failed: {e}")
        await status_msg.edit_text(f"❌ Failed:\n`{e}`")

# ---------------- FILE HANDLER ----------------
@app.on_message(filters.document & filters.private)
async def handle_file(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return

    user_id = message.from_user.id
    ensure_user(user_id)

    doc = message.document
    original_name = doc.file_name or "unknown_file"
    parsed = parse_anime_filename(original_name)
    file_size = doc.file_size or 0

    d = user_data[user_id]
    if d.get("sort_mode"):
        d["sort_queue"].append({
            "file_id": doc.file_id,
            "file_name": original_name,
            "parsed": parsed,
            "order": len(d["sort_queue"]),
        })

        current_task = d.get("sort_task")
        if current_task and not current_task.done():
            current_task.cancel()
        d["sort_task"] = asyncio.create_task(
            sort_batch_after_idle(user_id, message.chat.id)
        )

        season = parsed.get("season") or 1
        episode = parsed.get("episode")
        episode_label = f"S{season:02d}E{episode:02d}" if episode is not None else "episode unknown"
        await message.reply_text(
            f"📥 **Queued:** `{original_name}`\n"
            f"Detected: `{episode_label}`\n"
            f"Files waiting: **{len(d['sort_queue'])}**\n\n"
            f"I will sort after {SORT_IDLE_SECONDS} seconds without a new file.",
            quote=False
        )
        return

    user_data[user_id]["file_id"] = doc.file_id
    user_data[user_id]["file_size"] = file_size
    user_data[user_id]["file_name"] = original_name
    user_data[user_id]["parsed"] = parsed
    user_data[user_id]["caption"] = None
    user_data[user_id]["waiting_for"] = None

    detected = parsed["title"]
    parsed_ep = parsed["episode"] if parsed["episode"] is not None else 1
    season = parsed["season"]
    size_mb = file_size / (1024 * 1024)

    user_data[user_id]["animes"][detected] = {"episode": parsed_ep}
    user_data[user_id]["current_anime"] = detected

    await send_renamed_file(message, user_id)

# ---------------- PHOTO HANDLER (thumbnail) ----------------
@app.on_message(filters.photo & filters.private)
async def handle_photo(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return

    user_id = message.from_user.id
    ensure_user(user_id)

    photo = message.photo
    set_thumbnail_id(photo.file_id)
    user_data[user_id]["waiting_for"] = None
    await message.reply_text(
        "🖼 **Thumbnail saved permanently!**\n\n"
        "It will be attached to every file you rename or post until you change or clear it.\n"
        "Use /thumbnail to manage it.",
        quote=True
    )

# ---------------- TEXT HANDLER ----------------
@app.on_message(
    filters.text & filters.private &
    ~filters.command(["start","help","adduser","removeuser","users","episodes","recommend","thumbnail","search","clearthumb","sort"])
)
async def handle_text(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return

    user_id = message.from_user.id
    ensure_user(user_id)
    text = message.text.strip()

    if user_data[user_id]["waiting_for"] == "search_query":
        query_text = text
        user_data[user_id]["waiting_for"] = None
        results = await search_anime(query_text)

        if not results:
            await message.reply_text(
                f"❌ No results found for **{query_text}**.\n\n"
                "Try a different spelling or use /recommend.",
                quote=True
            )
            return

        user_data[user_id]["last_search"] = query_text
        rows = []
        row = []
        for a in results:
            row.append(InlineKeyboardButton(
                f"📺 {a['title']}",
                callback_data=f"ainfo_{a['mal_id']}"
            ))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        await message.reply_text(
            f"🔎 **Results for:** {query_text}\n\n"
            "Tap any title to see its details:",
            reply_markup=InlineKeyboardMarkup(rows),
            quote=True
        )
        return

    if user_data[user_id]["waiting_for"] == "anime_name":
        anime = text.strip()
        user_data[user_id]["current_anime"] = anime
        user_data[user_id]["waiting_for"] = None
        if anime not in user_data[user_id]["animes"]:
            parsed = user_data[user_id].get("parsed")
            ep = parsed["episode"] if parsed and parsed["episode"] else 1
            user_data[user_id]["animes"][anime] = {"episode": ep}
        await message.reply_text(
            f"✅ Anime set to **{anime}**\n\n{status_text(user_id)}",
            reply_markup=file_keyboard(anime),
            quote=True
        )
        return

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return

    results = []
    for line in lines:
        p = parse_anime_filename(line)
        ep = p["episode"] if p["episode"] is not None else 1
        new_name = build_new_filename(p["title"], p["season"], ep, p["ext"])
        results.append(f"• `{line}`\n  → `{new_name}`")

    await message.reply_text(
        "✏️ **Rename preview:**\n\n" + "\n\n".join(results),
        quote=True
    )

# ---------------- BUTTON HANDLER ----------------
@app.on_callback_query()
async def button_handler(client: Client, query: CallbackQuery):
    if not is_allowed(query.from_user.id):
        return

    user_id = query.from_user.id
    ensure_user(user_id)
    data = query.data
    await query.answer()

    # ---------- ANIME INFO (from /recommend) ----------
    if data.startswith("ainfo_"):
        mal_id = data[len("ainfo_"):]
        a = anime_cache.get(mal_id)
        if not a:
            await query.message.edit_text("⚠️ Anime info not found. Use /recommend again.")
            return
        status_icon = "🟢" if "Airing" in a["status"] else "🔴"
        text = (
            f"🎌 **{a['title']}**\n"
            f"{'─' * 28}\n"
            f"⭐ **Score:** {a['score']}   {status_icon} **{a['status']}**\n"
            f"🎬 **Episodes:** {a['episodes']}   📅 **Year:** {a['year'] or 'N/A'}\n"
            f"🏷 **Genres:** {a['genres'] or 'N/A'}\n\n"
            f"📖 **Synopsis:**\n{a['synopsis']}"
        )
        back_cb = "search_back" if user_data[user_id].get("last_search") else "recommend_back"
        back_label = "◀️ Back to Search" if user_data[user_id].get("last_search") else "◀️ Back to Trending"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(back_label, callback_data=back_cb)],
        ])
        await query.message.edit_text(text, reply_markup=kb)
        return

    # ---------- SEARCH FROM TRENDING LIST ----------
    if data == "search_prompt":
        user_data[user_id]["waiting_for"] = "search_query"
        user_data[user_id]["last_search"] = None
        await query.message.edit_text(
            "🔎 **Search for an anime**\n\n"
            "Send me the anime name, for example:\n"
            "`Attack on Titan`"
        )
        return

    # ---------- BACK TO SEARCH RESULTS ----------
    elif data == "search_back":
        query_text = user_data[user_id].get("last_search", "")
        if not query_text:
            await query.message.edit_text("⚠️ Search expired. Use /search again.")
            return
        await query.message.edit_text(f"🔍 Searching for **{query_text}**…")
        results = await search_anime(query_text)
        if not results:
            await query.message.edit_text(f"❌ No results for **{query_text}**.")
            return
        rows = []
        row = []
        for a in results:
            row.append(InlineKeyboardButton(f"📺 {a['title']}", callback_data=f"ainfo_{a['mal_id']}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        await query.message.edit_text(
            f"🔎 **Search results for:** {query_text}",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    # ---------- BACK TO TRENDING LIST ----------
    elif data == "recommend_back":
        await query.message.edit_text("🔍 Fetching trending anime…")
        anime_list = await fetch_trending_anime()
        if not anime_list:
            await query.message.edit_text("❌ Could not fetch trending anime. Try /recommend again.")
            return
        rows = []
        row = []
        for a in anime_list:
            row.append(InlineKeyboardButton(f"📺 {a['title']}", callback_data=f"ainfo_{a['mal_id']}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        await query.message.edit_text(
            "🔥 **Trending Anime Right Now**\n\n"
            "Tap any title to view details:",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        rows.append([InlineKeyboardButton("🔎 Search instead", callback_data="search_prompt")])
        await query.message.edit_reply_markup(InlineKeyboardMarkup(rows))
        return

    # ---------- SELECT / CHANGE ANIME ----------
    if data.startswith("anime_"):
        anime = data[len("anime_"):]

        if anime == "new":
            user_data[user_id]["waiting_for"] = "anime_name"
            await query.message.edit_text(
                "✏️ Send me the anime name as a message now:"
            )
            return

        user_data[user_id]["current_anime"] = anime
        parsed = user_data[user_id].get("parsed")
        if anime not in user_data[user_id]["animes"]:
            ep = parsed["episode"] if parsed and parsed["episode"] else 1
            user_data[user_id]["animes"][anime] = {"episode": ep}

        await query.message.edit_text(
            f"✅ **Anime set to:** {anime}\n\n{status_text(user_id)}",
            reply_markup=file_keyboard(anime)
        )

    # ---------- RENAME ----------
    elif data == "rename":
        anime = user_data[user_id]["current_anime"]
        if not anime:
            await query.message.edit_text(
                "⚠️ Please select an anime first.",
                reply_markup=file_keyboard("")
            )
            return
        await query.message.edit_text(f"⏳ Re-renaming as **{anime}**...")
        await send_renamed_file(query.message, user_id)
        await query.message.reply_text(
            f"⚙️ **Actions:**\n\n{status_text(user_id)}",
            reply_markup=file_keyboard(anime)
        )

    # ---------- EPISODE +1 ----------
    elif data == "episode":
        anime = user_data[user_id]["current_anime"]
        if not anime:
            await query.message.edit_text(
                "⚠️ Please select an anime first.",
                reply_markup=file_keyboard("")
            )
            return
        if anime not in user_data[user_id]["animes"]:
            user_data[user_id]["animes"][anime] = {"episode": 0}
        user_data[user_id]["animes"][anime]["episode"] += 1
        ep = user_data[user_id]["animes"][anime]["episode"]
        parsed = user_data[user_id].get("parsed")
        season = parsed["season"] if parsed else 1
        ext = parsed["ext"] if parsed else "mkv"
        new_name = build_new_filename(anime, season, ep, ext)
        user_data[user_id]["file_name"] = new_name
        await query.message.edit_text(
            f"🎬 **Episode → {ep}**\n`{new_name}`\n\n{status_text(user_id)}",
            reply_markup=file_keyboard(anime)
        )

    # ---------- SET THUMBNAIL ----------
    elif data == "set_thumb":
        thumb_status = "already set — send a new one to replace it" if get_thumbnail_id() else "not set"
        await query.message.edit_text(
            f"🖼 **Set Thumbnail** _(currently {thumb_status})_\n\n"
            "Send me a photo now. It will be saved permanently and attached to all files."
        )

    # ---------- CAPTION ----------
    elif data == "caption":
        anime = user_data[user_id]["current_anime"] or "Unknown Anime"
        ep = user_data[user_id]["animes"].get(anime, {}).get("episode", 0)
        fname = user_data[user_id].get("file_name") or "file.mkv"
        parsed = user_data[user_id].get("parsed")
        season = parsed["season"] if parsed else 1

        caption = (
            f"🔥 **Gravida Anime Release**\n\n"
            f"📺 **Anime:** {anime}\n"
            f"🎬 **Episode:** S{season:02d}E{ep:02d}\n"
            f"📁 **File:** `{fname}`\n"
            f"⚡ **Quality:** 1080p\n\n"
            f"🎌 **Gravida Anime Hub** | @gravida_anime"
        )
        user_data[user_id]["caption"] = caption
        await query.message.edit_text(
            f"📝 **Caption generated:**\n\n{caption}",
            reply_markup=file_keyboard(anime)
        )

    # ---------- POST ----------
    elif data == "post":
        file_id = user_data[user_id].get("file_id")
        caption = user_data[user_id].get("caption") or f"🎌 @{BRAND}"
        fname = user_data[user_id].get("file_name")

        if not file_id:
            await query.message.edit_text("⚠️ No file found. Please send a file first.")
            return

        await query.message.edit_text(f"📤 Posting to {CHANNEL_USERNAME}...")
        try:
            if get_thumbnail_id():
                with tempfile.TemporaryDirectory() as tmpdir:
                    thumb_path = os.path.join(tmpdir, "thumb.jpg")
                    await app.download_media(get_thumbnail_id(), file_name=thumb_path)
                    await app.send_document(
                        chat_id=CHANNEL_USERNAME,
                        document=file_id,
                        file_name=fname,
                        caption=caption,
                        thumb=thumb_path,
                    )
            else:
                await app.send_document(
                    chat_id=CHANNEL_USERNAME,
                    document=file_id,
                    file_name=fname,
                    caption=caption,
                )
            await query.message.edit_text(
                f"🚀 **Posted to {CHANNEL_USERNAME}!**\n\n📁 `{fname}`"
            )
        except Exception as e:
            logger.error(f"Post failed: {e}")
            await query.message.edit_text(
                f"❌ **Post failed:**\n`{e}`\n\n"
                "Make sure the bot is an admin of the channel."
            )


if __name__ == "__main__":
    logger.info("Bot started — Pyrogram (2 GB limit)")
    app.run()
