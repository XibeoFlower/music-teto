import os
import asyncio
import logging

import discord
from discord.ext import commands
import wavelink

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("musicbot")

# ====== Cấu hình từ biến môi trường (đặt trong Railway > Variables) ======
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
LAVALINK_HOST = os.environ.get("LAVALINK_HOST", "lavalinkv4.serenetia.com")
LAVALINK_PORT = int(os.environ.get("LAVALINK_PORT", "443"))
LAVALINK_PASSWORD = os.environ.get("LAVALINK_PASSWORD", "https://dsc.gg/ajidevserver")
LAVALINK_SECURE = os.environ.get("LAVALINK_SECURE", "true").lower() == "true"
COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix=COMMAND_PREFIX, intents=intents)

    async def setup_hook(self) -> None:
        scheme = "https" if LAVALINK_SECURE else "http"
        node = wavelink.Node(
            uri=f"{scheme}://{LAVALINK_HOST}:{LAVALINK_PORT}",
            password=LAVALINK_PASSWORD,
        )
        await wavelink.Pool.connect(nodes=[node], client=self)


bot = MusicBot()


# ====================== SỰ KIỆN WAVELINK ======================

@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    log.info(f"Node '{payload.node.identifier}' đã sẵn sàng (resumed={payload.resumed}).")


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player: wavelink.Player | None = payload.player
    if not player:
        return

    if not player.queue.is_empty:
        next_track = player.queue.get()
        await player.play(next_track)
    else:
        channel = getattr(player, "home", None)
        if channel:
            await channel.send("📭 Hàng đợi đã hết.")


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    # Tự rời voice channel nếu bot ở một mình
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
    log.info(f"Đăng nhập với tên {bot.user} (ID: {bot.user.id})")


# ====================== LỆNH NHẠC ======================

async def _ensure_voice(ctx: commands.Context) -> wavelink.Player | None:
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Bạn cần vào một voice channel trước.")
        return None

    player: wavelink.Player = ctx.voice_client  # type: ignore
    if not player:
        try:
            player = await ctx.author.voice.channel.connect(cls=wavelink.Player)
        except discord.ClientException:
            await ctx.send("❌ Không thể kết nối vào voice channel.")
            return None
        player.home = ctx.channel  # type: ignore
        player.autoplay = wavelink.AutoPlayMode.partial

    return player


@bot.command(name="play", aliases=["p"])
async def play(ctx: commands.Context, *, query: str):
    """Phát nhạc từ link YouTube hoặc từ khoá tìm kiếm. Vd: !play <link youtube>"""
    player = await _ensure_voice(ctx)
    if not player:
        return

    try:
        tracks = await wavelink.Playable.search(query)
    except Exception as e:
        log.exception(e)
        return await ctx.send("❌ Có lỗi khi tìm bài hát.")

    if not tracks:
        return await ctx.send("❌ Không tìm thấy kết quả nào.")

    # Nếu là playlist YouTube
    if isinstance(tracks, wavelink.Playlist):
        added = await player.queue.put_wait(tracks)
        await ctx.send(f"✅ Đã thêm playlist **{tracks.name}** ({added} bài) vào hàng đợi.")
    else:
        track = tracks[0]
        await player.queue.put_wait(track)
        await ctx.send(f"✅ Đã thêm vào hàng đợi: **{track.title}**")

    if not player.playing:
        next_track = player.queue.get()
        await player.play(next_track)


@bot.command(name="skip", aliases=["s"])
async def skip(ctx: commands.Context):
    player: wavelink.Player = ctx.voice_client  # type: ignore
    if not player or not player.playing:
        return await ctx.send("❌ Không có bài nào đang phát.")
    await player.skip(force=True)
    await ctx.send("⏭️ Đã bỏ qua bài hát.")


@bot.command(name="pause")
async def pause(ctx: commands.Context):
    player: wavelink.Player = ctx.voice_client  # type: ignore
    if not player:
        return await ctx.send("❌ Bot không ở trong voice channel.")
    await player.pause(True)
    await ctx.send("⏸️ Đã tạm dừng.")


@bot.command(name="resume")
async def resume(ctx: commands.Context):
    player: wavelink.Player = ctx.voice_client  # type: ignore
    if not player:
        return await ctx.send("❌ Bot không ở trong voice channel.")
    await player.pause(False)
    await ctx.send("▶️ Đã tiếp tục phát.")


@bot.command(name="stop")
async def stop(ctx: commands.Context):
    player: wavelink.Player = ctx.voice_client  # type: ignore
    if not player:
        return await ctx.send("❌ Bot không ở trong voice channel.")
    player.queue.clear()
    await player.stop()
    await ctx.send("⏹️ Đã dừng và xoá hàng đợi.")


@bot.command(name="leave", aliases=["dc", "disconnect"])
async def leave(ctx: commands.Context):
    player: wavelink.Player = ctx.voice_client  # type: ignore
    if not player:
        return await ctx.send("❌ Bot không ở trong voice channel.")
    await player.disconnect()
    await ctx.send("👋 Đã rời voice channel.")


@bot.command(name="queue", aliases=["q"])
async def queue_cmd(ctx: commands.Context):
    player: wavelink.Player = ctx.voice_client  # type: ignore
    if not player or (player.queue.is_empty and not player.playing):
        return await ctx.send("📭 Hàng đợi trống.")

    lines = []
    if player.current:
        lines.append(f"🎶 Đang phát: **{player.current.title}**")
    for i, track in enumerate(list(player.queue)[:10], start=1):
        lines.append(f"{i}. {track.title}")
    if len(player.queue) > 10:
        lines.append(f"... và {len(player.queue) - 10} bài khác.")

    await ctx.send("\n".join(lines))


@bot.command(name="nowplaying", aliases=["np"])
async def nowplaying(ctx: commands.Context):
    player: wavelink.Player = ctx.voice_client  # type: ignore
    if not player or not player.current:
        return await ctx.send("❌ Không có bài nào đang phát.")
    track = player.current
    await ctx.send(f"🎶 Đang phát: **{track.title}** ({track.author})")


@bot.command(name="volume", aliases=["vol"])
async def volume(ctx: commands.Context, value: int):
    player: wavelink.Player = ctx.voice_client  # type: ignore
    if not player:
        return await ctx.send("❌ Bot không ở trong voice channel.")
    value = max(0, min(value, 200))
    await player.set_volume(value)
    await ctx.send(f"🔊 Đã đặt âm lượng: {value}%")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
