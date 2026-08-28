import os
import asyncio
import logging
import time
from collections import OrderedDict

import discord
from discord import app_commands
import wavelink

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("musicbot")

# ====== Cấu hình ======
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

# Node chính: Singapore (gần VN nhất)
LAVALINK_HOST = os.environ.get("LAVALINK_HOST", "lava1.horizxon.studio")
LAVALINK_PORT = int(os.environ.get("LAVALINK_PORT", "80"))
LAVALINK_PASSWORD = os.environ.get("LAVALINK_PASSWORD", "horizxon.studio")
LAVALINK_SECURE = os.environ.get("LAVALINK_SECURE", "false").lower() == "true"

SEARCH_TIMEOUT = int(os.environ.get("SEARCH_TIMEOUT", "10"))

# Cache search
_search_cache = OrderedDict()
_CACHE_MAXSIZE = 50
_CACHE_TTL = 300


class MusicBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        scheme = "https" if LAVALINK_SECURE else "http"
        node = wavelink.Node(
            uri=f"{scheme}://{LAVALINK_HOST}:{LAVALINK_PORT}",
            password=LAVALINK_PASSWORD,
        )
        try:
            await wavelink.Pool.connect(nodes=[node], client=self)
            log.info(f"✅ Đã kết nối Lavalink node: {LAVALINK_HOST}:{LAVALINK_PORT}")
        except Exception as e:
            log.error(f"❌ Không kết nối được Lavalink node: {e}")
            log.error("Vui lòng kiểm tra LAVALINK_HOST trong Railway Variables.")

        await self.tree.sync()
        log.info("Đã đồng bộ slash commands.")


bot = MusicBot()


# ====================== CACHE & HELPER ======================

def _get_cached(query: str):
    now = time.time()
    key = query.lower().strip()
    if key in _search_cache:
        ts, tracks = _search_cache[key]
        if now - ts < _CACHE_TTL:
            _search_cache.move_to_end(key)
            return tracks
        else:
            del _search_cache[key]
    return None


def _set_cached(query: str, tracks):
    key = query.lower().strip()
    _search_cache[key] = (time.time(), tracks)
    _search_cache.move_to_end(key)
    while len(_search_cache) > _CACHE_MAXSIZE:
        _search_cache.popitem(last=False)


async def _search_with_timeout(query: str):
    cached = _get_cached(query)
    if cached is not None:
        log.info(f"[CACHE HIT] {query}")
        return cached

    start = time.time()
    try:
        tracks = await asyncio.wait_for(
            wavelink.Playable.search(query),
            timeout=SEARCH_TIMEOUT
        )
        elapsed = time.time() - start
        log.info(f'[SEARCH] "{query}" mất {elapsed:.2f}s')
        _set_cached(query, tracks)
        return tracks
    except asyncio.TimeoutError:
        log.warning(f'[TIMEOUT] Search "{query}" quá {SEARCH_TIMEOUT}s')
        return None
    except Exception as e:
        log.exception(f"[SEARCH ERROR] {e}")
        return None


# ====================== SỰ KIỆN ======================

@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    log.info(f"Node '{payload.node.identifier}' đã sẵn sàng.")


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player: wavelink.Player | None = payload.player
    if not player:
        return

    if not player.queue.is_empty:
        next_track = player.queue.get()
        await player.play(next_track)
        if not player.queue.is_empty:
            upcoming = player.queue[0]
            log.info(f"[PREFETCH] Chuẩn bị sẵn: {upcoming.title}")
    else:
        channel = getattr(player, "home", None)
        if channel:
            await channel.send("📭 Hàng đợi đã hết.")


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    if member.bot:
        return
    vc = member.guild.voice_client
    if vc and isinstance(vc.channel, (discord.VoiceChannel, discord.StageChannel)):
        if len([m for m in vc.channel.members if not m.bot]) == 0:
            await asyncio.sleep(30)
            if vc.channel and len([m for m in vc.channel.members if not m.bot]) == 0:
                await vc.disconnect()


@bot.event
async def on_ready():
    log.info(f"Đăng nhập: {bot.user} (ID: {bot.user.id})")


# ====================== VOICE HELPER ======================

async def _ensure_voice(interaction: discord.Interaction) -> wavelink.Player | None:
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ Bạn cần vào voice channel trước.", ephemeral=True)
        return None

    player: wavelink.Player = interaction.guild.voice_client  # type: ignore
    if not player:
        try:
            player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
        except discord.ClientException:
            await interaction.response.send_message("❌ Không thể kết nối voice channel.", ephemeral=True)
            return None
        player.home = interaction.channel  # type: ignore
        player.autoplay = wavelink.AutoPlayMode.partial

    return player


# ====================== SLASH COMMANDS ======================

@bot.tree.command(name="play", description="Phát nhạc từ link YouTube hoặc từ khoá tìm kiếm")
@app_commands.describe(query="Link YouTube hoặc từ khoá tìm kiếm")
async def play(interaction: discord.Interaction, query: str):
    start_total = time.time()
    await interaction.response.defer()

    player = await _ensure_voice(interaction)
    if not player:
        return

    tracks = await _search_with_timeout(query)
    if tracks is None:
        return await interaction.followup.send("⏱️ Tìm nhạc quá lâu, Lavalink node có thể bị lag. Thử lại sau hoặc đổi node.")
    if not tracks:
        return await interaction.followup.send("❌ Không tìm thấy kết quả.")

    if isinstance(tracks, wavelink.Playlist):
        added = await player.queue.put_wait(tracks)
        await interaction.followup.send(f"✅ Đã thêm playlist **{tracks.name}** ({added} bài) vào hàng đợi.")
    else:
        track = tracks[0]
        await player.queue.put_wait(track)
        await interaction.followup.send(f"✅ Đã thêm: **{track.title}**")

    if not player.playing:
        next_track = player.queue.get()
        await player.play(next_track)

    total = time.time() - start_total
    log.info(f"[PLAY] Tổng thờ gian xử lý /play: {total:.2f}s")


@bot.tree.command(name="skip", description="Bỏ qua bài hát đang phát")
async def skip(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client  # type: ignore
    if not player or not player.playing:
        return await interaction.response.send_message("❌ Không có bài nào đang phát.", ephemeral=True)
    await player.skip(force=True)
    await interaction.response.send_message("⏭️ Đã bỏ qua bài hát.")


@bot.tree.command(name="pause", description="Tạm dừng phát nhạc")
async def pause(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client  # type: ignore
    if not player:
        return await interaction.response.send_message("❌ Bot không ở trong voice channel.", ephemeral=True)
    await player.pause(True)
    await interaction.response.send_message("⏸️ Đã tạm dừng.")


@bot.tree.command(name="resume", description="Tiếp tục phát nhạc")
async def resume(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client  # type: ignore
    if not player:
        return await interaction.response.send_message("❌ Bot không ở trong voice channel.", ephemeral=True)
    await player.pause(False)
    await interaction.response.send_message("▶️ Đã tiếp tục phát.")


@bot.tree.command(name="stop", description="Dừng phát và xoá hàng đợi")
async def stop(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client  # type: ignore
    if not player:
        return await interaction.response.send_message("❌ Bot không ở trong voice channel.", ephemeral=True)
    player.queue.clear()
    await player.stop()
    await interaction.response.send_message("⏹️ Đã dừng và xoá hàng đợi.")


@bot.tree.command(name="leave", description="Rồi voice channel")
async def leave(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client  # type: ignore
    if not player:
        return await interaction.response.send_message("❌ Bot không ở trong voice channel.", ephemeral=True)
    await player.disconnect()
    await interaction.response.send_message("👋 Đã rồi voice channel.")


@bot.tree.command(name="queue", description="Xem hàng đợi nhạc")
async def queue_cmd(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client  # type: ignore
    if not player or (player.queue.is_empty and not player.playing):
        return await interaction.response.send_message("📭 Hàng đợi trống.", ephemeral=True)

    lines = []
    if player.current:
        lines.append(f"🎶 Đang phát: **{player.current.title}**")
    for i, track in enumerate(list(player.queue)[:10], start=1):
        lines.append(f"{i}. {track.title}")
    if len(player.queue) > 10:
        lines.append(f"... và {len(player.queue) - 10} bài khác.")

    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="nowplaying", description="Xem bài hát đang phát")
async def nowplaying(interaction: discord.Interaction):
    player: wavelink.Player = interaction.guild.voice_client  # type: ignore
    if not player or not player.current:
        return await interaction.response.send_message("❌ Không có bài nào đang phát.", ephemeral=True)
    track = player.current
    await interaction.response.send_message(f"🎶 Đang phát: **{track.title}** ({track.author})")


@bot.tree.command(name="volume", description="Điều chỉnh âm lượng (0-200)")
@app_commands.describe(value="Âm lượng từ 0 đến 200")
async def volume(interaction: discord.Interaction, value: int):
    player: wavelink.Player = interaction.guild.voice_client  # type: ignore
    if not player:
        return await interaction.response.send_message("❌ Bot không ở trong voice channel.", ephemeral=True)
    value = max(0, min(value, 200))
    await player.set_volume(value)
    await interaction.response.send_message(f"🔊 Đã đặt âm lượng: {value}%")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
