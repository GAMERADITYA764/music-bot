import asyncio
import logging
import glob
import shutil
from pathlib import Path
import json
import time
import os
import random
import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
import yt_dlp
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv('DISCORD_TOKEN', '')
SPOTIFY_ID = os.getenv('SPOTIFY_CLIENT_ID', '')
SPOTIFY_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET', '')
MAX_QUEUE = 100
FILTERS = {'none': '', 'bassboost': 'bass=g=10', 'treble': 'treble=g=8', 'nightcore': 'asetrate=48000*1.25,aresample=48000,atempo=1', 'slow': 'asetrate=48000*0.85,aresample=48000,atempo=1', 'vaporwave': 'asetrate=48000*0.8,aresample=48000,atempo=1', '8d': 'apulsator=hz=0.125', 'karaoke': 'stereotools=mlev=0.03', 'echo': 'aecho=0.8:0.88:60:0.4', 'tremolo': 'tremolo=f=5:d=0.7', 'vibrato': 'vibrato=f=6:d=0.5', 'soft': 'lowpass=f=5000'}

def message_embed(text):
    embed = discord.Embed(description=str(text)[:4096] or '\u200b', color=discord.Color.blurple())
    embed.set_footer(text='🎧 Vector Nodes Music • Fun.')
    return embed

def clock(seconds):
    seconds = max(0, int(seconds or 0))
    return f'{seconds // 60}:{seconds % 60:02d}'
ALLOWED_HOSTS = ('youtube.com', 'youtu.be', 'music.youtube.com', 'soundcloud.com', 'spotify.com', 'open.spotify.com')
YDL_OPTIONS = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True, 'noplaylist': True, 'default_search': 'ytsearch', 'socket_timeout': 15, 'extractor_retries': 2, 'restrictfilenames': True}

def find_ffmpeg():
    """Find FFmpeg even when Windows was not restarted after installation."""
    configured = os.getenv('FFMPEG_PATH', '').strip().strip('"')
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_dir():
            candidate = candidate / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
        if candidate.is_file():
            return str(candidate)
        logging.warning('FFMPEG_PATH does not point to an FFmpeg executable: %s', configured)
    found = shutil.which('ffmpeg')
    if found:
        return found
    base = Path(__file__).resolve().parent
    filename = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    for candidate in (base / filename, base / 'ffmpeg' / 'bin' / filename, base / 'bin' / filename):
        if candidate.is_file():
            return str(candidate)
    if os.name == 'nt':
        local = os.environ.get('LOCALAPPDATA', '')
        if local:
            patterns = [os.path.join(local, 'Microsoft', 'WinGet', 'Packages', '*FFmpeg*', '**', 'ffmpeg.exe'), os.path.join(local, 'Microsoft', 'WinGet', 'Links', 'ffmpeg.exe'), os.path.join(local, 'Programs', 'ffmpeg', '**', 'ffmpeg.exe')]
            for pattern in patterns:
                matches = sorted(glob.glob(pattern, recursive=True))
                if matches:
                    return matches[0]
        for candidate in (Path('C:/ffmpeg/bin/ffmpeg.exe'), Path('C:/Program Files/ffmpeg/bin/ffmpeg.exe'), Path('C:/ProgramData/chocolatey/bin/ffmpeg.exe')):
            if candidate.is_file():
                return str(candidate)
    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).is_file():
            return bundled
    except Exception as error:
        logging.warning('Bundled FFmpeg unavailable: %s', error)
    return None
FFMPEG = find_ffmpeg()
if FFMPEG:
    logging.info('FFmpeg found: %s', FFMPEG)
else:
    logging.warning('FFmpeg not found. Set FFMPEG_PATH to the full path to ffmpeg.exe, or put ffmpeg.exe next to bot.py.')
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)
players = {}
DATA_FILE = os.getenv('DATA_FILE', 'data/playlists.json')
SYNC_MINUTES = max(5, int(os.getenv('SYNC_MINUTES', '30')))
playlist_store = {}

def save_playlists():
    os.makedirs(os.path.dirname(DATA_FILE) or '.', exist_ok=True)
    temp = DATA_FILE + '.tmp'
    with open(temp, 'w', encoding='utf-8') as f:
        json.dump(playlist_store, f, ensure_ascii=False)
    os.replace(temp, DATA_FILE)

def load_playlists():
    global playlist_store
    try:
        with open(DATA_FILE, encoding='utf-8') as f:
            playlist_store = json.load(f)
    except FileNotFoundError:
        pass

@dataclass
class Track:
    title: str
    query: str
    requester: str
    url: str = ''
    duration: int = 0
    author: str = ''
    thumbnail: str = ''

class Player:

    def __init__(self, guild):
        self.guild = guild
        self.queue = deque()
        self.history = deque(maxlen=20)
        self.filter_name = 'none'
        self.started_at = 0.0
        self.paused_at = 0.0
        self.paused_total = 0.0
        self.current = None
        self.volume = 0.5
        self.loop = False
        self.autoplay = False
        self.task = None
        self.finished = asyncio.Event()
        self.channel = None
        self.skip_loop = False
        self.generation = 0
        self.panel_message = None
        self.panel_lock = asyncio.Lock()

    def elapsed(self):
        if not self.started_at:
            return 0
        end = self.paused_at or time.monotonic()
        return max(0, int(end - self.started_at - self.paused_total))

    def mark_pause(self):
        if not self.paused_at:
            self.paused_at = time.monotonic()

    def mark_resume(self):
        if self.paused_at:
            self.paused_total += time.monotonic() - self.paused_at
            self.paused_at = 0.0

    async def update_panel(self, move_to_bottom=False):
        if not self.channel:
            return
        async with self.panel_lock:
            title = discord.utils.escape_markdown(self.current.title)[:180] if self.current else 'Nothing playing'
            voice = self.guild.voice_client
            status = 'Paused ⏸️' if voice and voice.is_paused() else 'Playing ▶️' if voice and voice.is_playing() else 'Idle ⏹️'
            duration = self.current.duration if self.current else 0
            elapsed = self.elapsed() if self.current else 0
            if duration:
                filled = min(15, max(0, round(15 * elapsed / duration)))
                progress = '▬' * filled + '🔘' + '▬' * (15 - filled)
                timeline = f'{clock(elapsed)} {progress} {clock(duration)}'
            else:
                timeline = f'{clock(elapsed)} / unknown'
            embed = discord.Embed(title='🎵 MUSIC PANEL', description=f'**{title}**\n{status}\n{timeline}', color=discord.Color.blurple())
            if self.current and self.current.thumbnail and self.current.thumbnail.startswith(('https://', 'http://')):
                embed.set_image(url=self.current.thumbnail)
            if self.current:
                embed.add_field(name='Requested by', value=discord.utils.escape_markdown(self.current.requester)[:100], inline=True)
            if self.current and self.current.author:
                embed.add_field(name='Music author', value=discord.utils.escape_markdown(self.current.author)[:100], inline=True)
            embed.add_field(name='🎛️ Filter', value=self.filter_name.title(), inline=True)
            embed.add_field(name='📜 Up next', value=str(len(self.queue)), inline=True)
            embed.add_field(name='🔊 Volume', value=f'{round(self.volume * 100)}%', inline=True)
            embed.add_field(name='🔁 Repeat', value='On' if self.loop else 'Off', inline=True)
            embed.add_field(name='♾️ Autoplay', value='On ✅' if self.autoplay else 'Off ⭕', inline=True)
            if self.queue:
                embed.add_field(name='🎶 Next track', value=discord.utils.escape_markdown(self.queue[0].title)[:150], inline=False)
            embed.set_footer(text='🎧 Vector Nodes Music • Fun.')
            try:
                if move_to_bottom and self.panel_message:
                    try:
                        await self.panel_message.delete()
                    except discord.HTTPException:
                        try:
                            await self.panel_message.edit(view=None)
                        except discord.HTTPException:
                            pass
                    self.panel_message = None
                if self.panel_message:
                    await self.panel_message.edit(embed=embed, view=MusicPanel(), allowed_mentions=discord.AllowedMentions.none())
                else:
                    self.panel_message = await self.channel.send(embed=embed, view=MusicPanel(), allowed_mentions=discord.AllowedMentions.none())
            except (discord.NotFound, discord.Forbidden):
                self.panel_message = None

    def start(self):
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.worker())

    async def worker(self):
        try:
            while self.queue:
                generation = self.generation
                self.current = self.queue.popleft()
                self.history.appendleft(self.current)
                self.skip_loop = False
                try:
                    info = await extract(self.current.url or self.current.query)
                    if generation != self.generation:
                        self.current = None
                        continue
                    executable = find_ffmpeg()
                    if not executable:
                        raise RuntimeError('FFmpeg not found. Set FFMPEG_PATH to the full path to ffmpeg.exe, or put ffmpeg.exe next to bot.py and restart.')
                    self.current.duration = int(info.get('duration') or self.current.duration or 0)
                    self.current.author = self.current.author or info.get('artist') or info.get('uploader') or info.get('channel') or ''
                    self.current.thumbnail = self.current.thumbnail or info.get('thumbnail') or ''
                    audio_filter = FILTERS[self.filter_name]
                    ffmpeg_options = '-vn' + (f' -af "{audio_filter}"' if audio_filter else '')
                    source = discord.FFmpegPCMAudio(info['url'], executable=executable, before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin', options=ffmpeg_options)
                    source = discord.PCMVolumeTransformer(source, volume=self.volume)
                    self.started_at = time.monotonic()
                    self.paused_at = 0
                    self.paused_total = 0
                    self.finished.clear()
                    loop = asyncio.get_running_loop()
                    self.guild.voice_client.play(source, after=lambda error: loop.call_soon_threadsafe(self.finished.set))
                    await self.update_panel()
                    await self.finished.wait()
                    if self.loop and (not self.skip_loop):
                        self.queue.appendleft(self.current)
                    elif self.autoplay and not self.queue and generation == self.generation:
                        try:
                            suggested = await autoplay_suggestion(self.current, list(self.history))
                            if suggested and self.autoplay and not self.queue and generation == self.generation:
                                self.queue.append(suggested)
                        except Exception:
                            logging.exception('Autoplay lookup failed')
                            if self.channel:
                                await self.channel.send(embed=message_embed('⚠️ Autoplay could not find another song. Try /play again.'))
                except Exception:
                    logging.exception('Playback failed')
                    if self.channel:
                        await self.channel.send('Could not play this track. Check the bot logs; if FFmpeg is missing, set FFMPEG_PATH to its full path. Trying the next one.')
                self.current = None
        finally:
            self.current = None
            self.task = None
            await self.update_panel()
            if self.queue and self.guild.voice_client and self.guild.voice_client.is_connected():
                self.start()

async def autoplay_suggestion(track, history):
    """Search for related songs; avoid recent video URLs and titles."""
    query = f"{track.author} {track.title} similar songs music"[:250]
    def search():
        options = {**YDL_OPTIONS, 'extract_flat': True, 'noplaylist': False}
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info('ytsearch15:' + query, download=False)
    data = await asyncio.to_thread(search)
    recent_urls = {t.url for t in history if t.url}
    recent_titles = {t.title.casefold().strip() for t in history}
    candidates = []
    for item in (data or {}).get('entries') or []:
        if not item: continue
        vid = item.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid): continue
        url = 'https://www.youtube.com/watch?v=' + vid
        title = item.get('title') or ''
        if not title or url in recent_urls or title.casefold().strip() in recent_titles: continue
        candidates.append(Track(title, url, '♾️ Autoplay', url, int(item.get('duration') or 0), item.get('artist') or item.get('uploader') or item.get('channel') or '', item.get('thumbnail') or 'https://i.ytimg.com/vi/' + vid + '/hqdefault.jpg'))
    return random.choice(candidates[:8]) if candidates else None

async def extract(query):

    def run():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            data = ydl.extract_info(query, download=False)
            if data.get('entries'):
                data = next((x for x in data['entries'] if x), None)
            if not data or not data.get('url'):
                raise ValueError('No playable result')
            return data
    return await asyncio.to_thread(run)

def validate_input(query):
    if re.match('^[a-zA-Z][a-zA-Z0-9+.-]*:', query):
        parsed = urlparse(query)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or (not any((parsed.hostname.lower() == h or parsed.hostname.lower().endswith('.' + h) for h in ALLOWED_HOSTS))):
            raise ValueError('Only YouTube, SoundCloud and Spotify links are supported.')

def get_player(guild):
    if guild.id not in players:
        players[guild.id] = Player(guild)
    return players[guild.id]

async def spotify_tracks(url, requester):
    if not SPOTIFY_ID or not SPOTIFY_SECRET:
        raise ValueError('Spotify links require SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env.')
    match = re.search('/(track|album|playlist)/([A-Za-z0-9]+)', urlparse(url).path)
    if not match:
        raise ValueError('Use a Spotify track, album, or playlist link.')
    kind, item_id = match.groups()
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post('https://accounts.spotify.com/api/token', data={'grant_type': 'client_credentials'}, auth=aiohttp.BasicAuth(SPOTIFY_ID, SPOTIFY_SECRET)) as r:
            if r.status != 200:
                raise ValueError('Spotify authentication failed. Check your credentials.')
            token = (await r.json())['access_token']
        endpoint = f'https://api.spotify.com/v1/{kind}s/{item_id}'
        async with session.get(endpoint, headers={'Authorization': f'Bearer {token}'}) as r:
            if r.status != 200:
                raise ValueError(f'Spotify could not access this {kind} (HTTP {r.status}); it may be private or restricted.')
            data = await r.json()
        if kind == 'track':
            items = [data]
        else:
            page = data.get('tracks', {})
            items = page.get('items', [])
            while page.get('next') and len(items) < MAX_QUEUE:
                next_url = page['next']
                if urlparse(next_url).hostname != 'api.spotify.com':
                    break
                async with session.get(next_url, headers={'Authorization': f'Bearer {token}'}) as r:
                    if r.status != 200:
                        break
                    page = await r.json()
                items.extend(page.get('items', []))
        result = []
        for item in items[:MAX_QUEUE]:
            track = item.get('track', item) if item else None
            if not track or not track.get('name') or (not track.get('artists')):
                continue
            title = track['name'] + ' - ' + ', '.join((a['name'] for a in track['artists']))
            images = (track.get('album') or {}).get('images') or (data.get('images', []) if kind == 'album' else [])
            image = images[0].get('url', '') if images else ''
            result.append(Track(track['name'], 'ytsearch1:' + title, requester, author=', '.join((a['name'] for a in track['artists'])), thumbnail=image))
        return result

def same_voice(interaction):
    voice = interaction.guild.voice_client
    user_channel = interaction.user.voice.channel if interaction.user.voice else None
    if not voice or not user_channel or voice.channel.id != user_channel.id:
        raise ValueError('Join my voice channel first.')
    return voice

@bot.event
async def on_ready():
    if not getattr(bot, 'panel_registered', False):
        bot.add_view(MusicPanel())
        bot.panel_registered = True
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name='🔊/play | 🎶/playlist'))
    await bot.tree.sync()
    if not sync_playlists.is_running():
        sync_playlists.start()
    if not update_panels.is_running():
        update_panels.start()
    logging.info('Ready as %s', bot.user)

@bot.tree.command(description='Play a song by name or YouTube, Spotify or SoundCloud link')
@app_commands.guild_only()
@app_commands.describe(query='Song name or a YouTube, Spotify or SoundCloud link')
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    try:
        if not interaction.user.voice or not interaction.user.voice.channel:
            raise ValueError('Join a voice channel first.')
        validate_input(query)
        voice = interaction.guild.voice_client
        if voice and voice.channel.id != interaction.user.voice.channel.id:
            raise ValueError('I am already in another voice channel.')
        p = get_player(interaction.guild)
        host = (urlparse(query).hostname or '').lower()
        if host == 'spotify.com' or host.endswith('.spotify.com'):
            tracks = await spotify_tracks(query, interaction.user.display_name)
        else:
            info = await extract(query)
            tracks = [Track(info.get('title', query), query, interaction.user.display_name, info.get('webpage_url', ''), int(info.get('duration') or 0), info.get('artist') or info.get('uploader') or info.get('channel') or '', info.get('thumbnail') or '')]
        if not tracks:
            raise ValueError('No playable tracks found.')
        available = MAX_QUEUE - len(p.queue) - (1 if p.current else 0)
        if available <= 0:
            raise ValueError('The queue is full.')
        tracks = tracks[:available]
        if not voice:
            await interaction.user.voice.channel.connect(timeout=20)
        starts_now = p.current is None and not p.queue
        position = len(p.queue) + 1
        p.channel = interaction.channel
        p.queue.extend(tracks)
        first = tracks[0]
        artist = discord.utils.escape_markdown(first.author or 'Not available')
        title = discord.utils.escape_markdown(first.title)
        extra = f'\n**Tracks added:** {len(tracks)}' if len(tracks) > 1 else ''
        embed = discord.Embed(title='🎶 Song added to queue', description=f'**{title}**\n**Artist:** {artist}{extra}'[:4096], color=discord.Color.blurple())
        queue_position = 'Playing next (queue #1)' if starts_now else f'#{position}'
        embed.add_field(name='📋 Queue position', value=queue_position, inline=True)
        if first.thumbnail and first.thumbnail.startswith(('https://', 'http://')):
            embed.set_thumbnail(url=first.thumbnail)
        embed.set_footer(text='🎧 Vector Nodes Music • Fun.')
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await p.update_panel(move_to_bottom=True)
        p.start()
    except Exception as e:
        logging.exception('Play failed')
        await interaction.followup.send(embed=message_embed(f'❌ {str(e)[:300]}'), ephemeral=True)

@bot.tree.command(description='Skip the current track')
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    try:
        voice = same_voice(interaction)
        p = get_player(interaction.guild)
        if not voice.is_playing() and (not voice.is_paused()):
            raise ValueError('Nothing is playing.')
        p.skip_loop = True
        voice.stop()
        await interaction.response.send_message(embed=message_embed('⏭️ Skipped.'))
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)

@bot.tree.command(description='Pause playback')
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    try:
        v = same_voice(interaction)
        if not v.is_playing():
            raise ValueError('Nothing is playing.')
        v.pause()
        get_player(interaction.guild).mark_pause()
        await interaction.response.send_message(embed=message_embed('⏸️ Paused.'))
        await get_player(interaction.guild).update_panel()
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)

@bot.tree.command(description='Resume playback')
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    try:
        v = same_voice(interaction)
        if not v.is_paused():
            raise ValueError('Playback is not paused.')
        v.resume()
        get_player(interaction.guild).mark_resume()
        await interaction.response.send_message(embed=message_embed('▶️ Resumed.'))
        await get_player(interaction.guild).update_panel()
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)

def queue_embeds(p):
    lines = []
    for i, track in enumerate(p.queue, 1):
        title = discord.utils.escape_markdown(track.title)
        author = discord.utils.escape_markdown(track.author or 'Not available')
        lines.append(f'**#{i}** {title}\n🎤 {author}')
    pages = []
    text = ''
    for line in lines:
        line = line[:3900]
        if text and len(text) + len(line) + 2 > 3800:
            pages.append(text)
            text = ''
        text += ('\n\n' if text else '') + line
    if text:
        pages.append(text)
    if not pages:
        pages = ['Queue is empty.']
    embeds = []
    for page in pages:
        embed = discord.Embed(title='📜 Music queue', description=page, color=discord.Color.blurple())
        embed.set_footer(text='🎧 Vector Nodes Music • Fun.')
        embeds.append(embed)
    return embeds

@bot.tree.command(name='queue', description='Show the current queue')
@app_commands.guild_only()
async def show_queue(interaction: discord.Interaction):
    embeds = queue_embeds(get_player(interaction.guild))
    await interaction.response.send_message(embed=embeds[0], allowed_mentions=discord.AllowedMentions.none())
    for page in embeds[1:]:
        await interaction.followup.send(embed=page, allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(description='Set playback volume from 0 to 500 (high volume may distort)')
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 500]):
    try:
        v = same_voice(interaction)
        p = get_player(interaction.guild)
        p.volume = level / 100
        if isinstance(v.source, discord.PCMVolumeTransformer):
            v.source.volume = p.volume
        await interaction.response.send_message(embed=message_embed(f'🔊 Volume: {level}%'))
        await p.update_panel()
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)

@bot.tree.command(description='Toggle repeating the current track')
@app_commands.guild_only()
async def loop(interaction: discord.Interaction):
    try:
        same_voice(interaction)
        p = get_player(interaction.guild)
        p.loop = not p.loop
        await interaction.response.send_message(embed=message_embed(f"🔁 Repeat {('on' if p.loop else 'off')}."))
        await p.update_panel()
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)

@bot.tree.command(description='Turn autoplay on or off when the queue finishes')
@app_commands.guild_only()
async def autoplay(interaction: discord.Interaction):
    try:
        same_voice(interaction)
        p = get_player(interaction.guild)
        p.autoplay = not p.autoplay
        await interaction.response.send_message(embed=message_embed(f"♾️ Autoplay {'enabled ✅' if p.autoplay else 'disabled ⭕'}."))
        await p.update_panel()
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(f'⚠️ {e}'), ephemeral=True)

@bot.tree.command(description='Shuffle upcoming tracks')
@app_commands.guild_only()
async def shuffle(interaction: discord.Interaction):
    try:
        same_voice(interaction)
        p = get_player(interaction.guild)
        items = list(p.queue)
        random.shuffle(items)
        p.queue = deque(items)
        await interaction.response.send_message(embed=message_embed('🔀 Queue shuffled.'))
        await p.update_panel()
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)

@bot.tree.command(description='Remove an upcoming track by queue position')
@app_commands.guild_only()
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1, 100]):
    try:
        same_voice(interaction)
        p = get_player(interaction.guild)
        if position > len(p.queue):
            raise ValueError('That queue position does not exist.')
        item = p.queue[position - 1]
        del p.queue[position - 1]
        await interaction.response.send_message(embed=message_embed(f'Removed **{discord.utils.escape_markdown(item.title)[:150]}**.'), allowed_mentions=discord.AllowedMentions.none())
        await p.update_panel()
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)

@bot.tree.command(description='Stop playback and clear the queue, staying in voice')
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    try:
        v = same_voice(interaction)
        p = get_player(interaction.guild)
        p.generation += 1
        p.queue.clear()
        p.loop = False
        p.skip_loop = True
        if v.is_playing() or v.is_paused():
            v.stop()
        await interaction.response.send_message(embed=message_embed('⏹️ Stopped and cleared the queue. Still connected to voice.'))
        await p.update_panel()
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)

@bot.tree.command(description='Show recently played tracks')
@app_commands.guild_only()
async def history(interaction: discord.Interaction):
    p = get_player(interaction.guild)
    lines = [f'{i}. {discord.utils.escape_markdown(t.title)[:120]}' for i, t in enumerate(list(p.history)[:10], 1)]
    await interaction.response.send_message(embed=message_embed('**Recently played**\n' + ('\n'.join(lines) if lines else 'No history yet.')), allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(description='Remove all upcoming tracks without stopping the current track')
@app_commands.guild_only()
async def clear(interaction: discord.Interaction):
    try:
        same_voice(interaction)
        p = get_player(interaction.guild)
        count = len(p.queue)
        p.queue.clear()
        await interaction.response.send_message(embed=message_embed(f'Cleared {count} upcoming tracks.'))
        await p.update_panel()
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)

@bot.tree.command(description='Check bot status and FFmpeg availability')
@app_commands.guild_only()
async def status(interaction: discord.Interaction):
    p = get_player(interaction.guild)
    ffmpeg_status = 'Available ✅' if find_ffmpeg() else 'Not found ❌'
    await interaction.response.send_message(embed=message_embed(f'**Bot latency:** {round(bot.latency * 1000)} ms\n**FFmpeg:** {ffmpeg_status}\n**Queued tracks:** {len(p.queue)}'), ephemeral=True)

@tasks.loop(seconds=15)
async def update_panels():
    for p in list(players.values()):
        if p.current and p.panel_message:
            try:
                await p.update_panel()
            except discord.HTTPException:
                logging.warning('Panel refresh failed', exc_info=True)

@update_panels.before_loop
async def before_panels():
    await bot.wait_until_ready()

@bot.tree.command(description='Join your voice channel')
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        if not interaction.user.voice or not interaction.user.voice.channel:
            raise ValueError('Join a voice channel first.')
        channel = interaction.user.voice.channel
        v = interaction.guild.voice_client
        if v and v.channel.id != channel.id:
            raise ValueError('I am already in another voice channel.')
        if not v:
            await channel.connect(timeout=30, self_deaf=True)
        await interaction.followup.send(embed=message_embed('Connected to your voice channel.'), ephemeral=True)
    except asyncio.TimeoutError:
        await interaction.followup.send(embed=message_embed('Voice connection timed out. Check outbound UDP and LXC host/provider firewall.'), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(embed=message_embed(str(e)[:250]), ephemeral=True)

@bot.tree.command(description='Stop playback and disconnect from voice')
@app_commands.guild_only()
async def disconnect(interaction: discord.Interaction):
    try:
        v = same_voice(interaction)
        p = get_player(interaction.guild)
        p.generation += 1
        p.queue.clear()
        p.loop = False
        p.skip_loop = True
        if v.is_playing() or v.is_paused():
            v.stop()
        await v.disconnect()
        await interaction.response.send_message(embed=message_embed('Disconnected.'))
        await p.update_panel()
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)

@bot.tree.command(description='Check the bot latency')
@app_commands.guild_only()
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(embed=message_embed(f'🏓 Gateway latency: {round(bot.latency * 1000)} ms'))

async def apply_filter(interaction, name):
    try:
        v = same_voice(interaction)
        p = get_player(interaction.guild)
        p.filter_name = name
        if p.current and (v.is_playing() or v.is_paused()):
            p.queue.appendleft(p.current)
            p.skip_loop = True
            v.stop()
            message = f'Filter: {name}. Current track restarts from the beginning.'
        else:
            message = f'Filter: {name}. It applies to the next track.'
        await interaction.response.send_message(embed=message_embed(message), ephemeral=True)
        await p.update_panel()
    except ValueError as e:
        await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)

@bot.tree.command(name='filter', description='Choose an audio filter; none removes it')
@app_commands.guild_only()
@app_commands.choices(name=[app_commands.Choice(name=k.title(), value=k) for k in FILTERS])
async def filter_command(interaction: discord.Interaction, name: app_commands.Choice[str]):
    await apply_filter(interaction, name.value)

@bot.tree.command(description='Find lyrics for a song or the currently playing track')
@app_commands.guild_only()
async def lyrics(interaction: discord.Interaction, query: str=''):
    await interaction.response.defer(thinking=True)
    p = get_player(interaction.guild)
    query = query.strip() or (p.current.title if p.current else '')
    if not query:
        await interaction.followup.send(embed=message_embed('Provide a song name or play a track first.'), ephemeral=True)
        return
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, headers={'User-Agent': 'VectorNodesMusic/1.0'}) as session:
            async with session.get('https://lrclib.net/api/search', params={'q': query[:200]}) as response:
                if response.status != 200:
                    raise ValueError(f'Lyrics service returned HTTP {response.status}.')
                data = await response.json()
        result = next((item for item in data if item.get('plainLyrics')), None)
        if not result:
            await interaction.followup.send(embed=message_embed('No lyrics found. Try /lyrics with a different song name.'))
            return
        text = result['plainLyrics'][:3800]
        embed = discord.Embed(title=f"Lyrics: {result.get('trackName', query)[:180]}", description=text, color=discord.Color.blurple())
        embed.set_footer(text='Lyrics via LRCLIB • results may not match exactly')
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logging.exception('Lyrics lookup failed')
        await interaction.followup.send(embed=message_embed('Lyrics service unavailable. Please try again later.'), ephemeral=True)

class FilterSelect(discord.ui.Select):

    def __init__(self):
        super().__init__(placeholder='🎛️ Select audio filter', custom_id='music:filter', row=2, options=[discord.SelectOption(label=k.title(), value=k, description='Remove filter' if k == 'none' else f'Apply {k}') for k in FILTERS])

    async def callback(self, interaction):
        try:
            same_voice(interaction)
        except ValueError as e:
            await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)
            return
        await apply_filter(interaction, self.values[0])

class MusicPanel(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(FilterSelect())

    async def interaction_check(self, interaction):
        try:
            same_voice(interaction)
            return True
        except ValueError as e:
            await interaction.response.send_message(embed=message_embed(str(e)), ephemeral=True)
            return False

    @discord.ui.button(label='Pause / Resume', emoji='⏯️', style=discord.ButtonStyle.primary, custom_id='music:toggle')
    async def toggle(self, interaction, button):
        v = same_voice(interaction)
        if v.is_paused():
            v.resume()
            get_player(interaction.guild).mark_resume()
            msg = 'Resumed.'
        elif v.is_playing():
            v.pause()
            get_player(interaction.guild).mark_pause()
            msg = 'Paused.'
        else:
            msg = 'Nothing is playing.'
        await interaction.response.send_message(embed=message_embed(msg), ephemeral=True)
        await get_player(interaction.guild).update_panel()

    @discord.ui.button(label='Skip', emoji='⏭️', custom_id='music:skip')
    async def skip_button(self, interaction, button):
        v = same_voice(interaction)
        p = get_player(interaction.guild)
        if v.is_playing() or v.is_paused():
            p.skip_loop = True
            v.stop()
            msg = 'Skipped.'
        else:
            msg = 'Nothing is playing.'
        await interaction.response.send_message(embed=message_embed(msg), ephemeral=True)

    @discord.ui.button(label='Repeat', emoji='🔁', custom_id='music:repeat')
    async def repeat_button(self, interaction, button):
        p = get_player(interaction.guild)
        p.loop = not p.loop
        await interaction.response.send_message(embed=message_embed(f"Repeat {('on' if p.loop else 'off')}."), ephemeral=True)
        await p.update_panel()

    @discord.ui.button(label='Shuffle', emoji='🔀', custom_id='music:shuffle')
    async def shuffle_button(self, interaction, button):
        p = get_player(interaction.guild)
        items = list(p.queue)
        random.shuffle(items)
        p.queue = deque(items)
        await interaction.response.send_message(embed=message_embed('Queue shuffled.'), ephemeral=True)
        await p.update_panel()

    @discord.ui.button(label='Down', emoji='🔉', custom_id='music:down', row=1)
    async def down_button(self, interaction, button):
        p = get_player(interaction.guild)
        v = same_voice(interaction)
        p.volume = max(0, round(p.volume - 0.1, 2))
        if isinstance(v.source, discord.PCMVolumeTransformer):
            v.source.volume = p.volume
        await interaction.response.send_message(embed=message_embed(f'Volume: {round(p.volume * 100)}%'), ephemeral=True)
        await p.update_panel()

    @discord.ui.button(label='Up', emoji='🔊', custom_id='music:up', row=1)
    async def up_button(self, interaction, button):
        p = get_player(interaction.guild)
        v = same_voice(interaction)
        p.volume = min(5, round(p.volume + 0.1, 2))
        if isinstance(v.source, discord.PCMVolumeTransformer):
            v.source.volume = p.volume
        await interaction.response.send_message(embed=message_embed(f'Volume: {round(p.volume * 100)}%'), ephemeral=True)
        await p.update_panel()

    @discord.ui.button(label='Queue', emoji='📜', custom_id='music:queue', row=1)
    async def queue_button(self, interaction, button):
        p = get_player(interaction.guild)
        embeds = queue_embeds(p)
        await interaction.response.send_message(embed=embeds[0], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        for page in embeds[1:]:
            await interaction.followup.send(embed=page, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label='Autoplay', emoji='♾️', custom_id='music:autoplay', row=1)
    async def autoplay_button(self, interaction, button):
        p = get_player(interaction.guild)
        p.autoplay = not p.autoplay
        await interaction.response.send_message(embed=message_embed(f"♾️ Autoplay {'enabled ✅' if p.autoplay else 'disabled ⭕'}."), ephemeral=True)
        await p.update_panel()

    @discord.ui.button(label='Stop', emoji='⏹️', style=discord.ButtonStyle.danger, custom_id='music:stop')
    async def stop_button(self, interaction, button):
        p = get_player(interaction.guild)
        p.generation += 1
        p.queue.clear()
        p.loop = False
        p.skip_loop = True
        v = same_voice(interaction)
        if v.is_playing() or v.is_paused():
            v.stop()
        await interaction.response.send_message(embed=message_embed('Stopped and cleared the queue. Still connected to voice.'), ephemeral=True)
        await p.update_panel()

async def fetch_playlist(url):
    validate_input(url)
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if host == 'spotify.com' or host.endswith('.spotify.com'):
        if not re.search('/playlist/[A-Za-z0-9]+', parsed.path):
            raise ValueError('Please provide a Spotify playlist URL.')
        tracks = await spotify_tracks(url, 'Synced playlist')
        return [{'title': t.title, 'query': t.query, 'url': t.url, 'author': t.author, 'thumbnail': t.thumbnail} for t in tracks]
    if not (host == 'youtu.be' or host.endswith('.youtube.com') or host == 'youtube.com'):
        raise ValueError('Only YouTube and Spotify playlists can be synced.')

    def run():
        options = {**YDL_OPTIONS, 'noplaylist': False, 'extract_flat': True, 'playlistend': MAX_QUEUE, 'skip_download': True}
        with yt_dlp.YoutubeDL(options) as ydl:
            data = ydl.extract_info(url, download=False)
        if not data or data.get('_type') not in ('playlist', 'multi_video'):
            raise ValueError('Please provide a public YouTube playlist URL.')
        result = []
        for item in data.get('entries', []):
            if not item or not item.get('id'):
                continue
            vid = item['id']
            if not re.fullmatch('[A-Za-z0-9_-]{11}', vid):
                continue
            result.append({'title': item.get('title') or vid, 'author': item.get('artist') or item.get('uploader') or item.get('channel') or '', 'query': 'https://www.youtube.com/watch?v=' + vid, 'url': 'https://www.youtube.com/watch?v=' + vid, 'thumbnail': 'https://i.ytimg.com/vi/' + vid + '/hqdefault.jpg'})
        return result
    return await asyncio.to_thread(run)

async def refresh_entry(entry):
    tracks = await fetch_playlist(entry['url'])
    if not tracks:
        raise ValueError('Playlist is empty or unavailable; existing saved tracks were retained.')
    entry['tracks'] = tracks
    entry['synced_at'] = int(time.time())
    entry.pop('error', None)

@tasks.loop(minutes=SYNC_MINUTES)
async def sync_playlists():
    for guild_entries in list(playlist_store.values()):
        for entry in list(guild_entries.values()):
            try:
                await refresh_entry(entry)
            except Exception as e:
                entry['error'] = str(e)[:200]
                logging.warning('Playlist sync failed: %s', e)
    save_playlists()

@sync_playlists.before_loop
async def before_sync():
    await bot.wait_until_ready()

@app_commands.guild_only()
@bot.tree.command(description='Save a public YouTube or Spotify playlist for automatic sync')
async def playlist_add(interaction: discord.Interaction, name: str, url: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        name = name.strip().lower()
        if not re.fullmatch('[a-z0-9_-]{1,32}', name):
            raise ValueError('Name must be 1–32 letters, numbers, underscores or hyphens.')
        entries = playlist_store.setdefault(str(interaction.guild.id), {})
        if name not in entries and len(entries) >= 10:
            raise ValueError('Maximum 10 saved playlists per server.')
        entry = {'url': url, 'tracks': []}
        await refresh_entry(entry)
        entries[name] = entry
        save_playlists()
        await interaction.followup.send(embed=message_embed(f"Saved **{name}** with {len(entry['tracks'])} tracks. It will refresh every {SYNC_MINUTES} minutes."), ephemeral=True)
    except Exception as e:
        logging.exception('Playlist add failed')
        await interaction.followup.send(embed=message_embed(f'Could not save playlist: {str(e)[:250]}'), ephemeral=True)

@app_commands.guild_only()
@bot.tree.command(description="Show your server's saved and synced playlists")
async def playlist_list(interaction: discord.Interaction):
    entries = playlist_store.get(str(interaction.guild.id), {})
    lines = []
    for name, entry in entries.items():
        stamp = entry.get('synced_at', 0)
        lines.append(f"**{name}** — {len(entry.get('tracks', []))} tracks; last synced <t:{stamp}:R>" + (' ⚠️ last refresh failed' if entry.get('error') else ''))
    await interaction.response.send_message(embed=message_embed('\n'.join(lines) if lines else 'No saved playlists. Use /playlist_add.'), ephemeral=True)

@bot.tree.command(name='playlist', description='Show saved synced playlists')
@app_commands.guild_only()
async def playlist_alias(interaction: discord.Interaction):
    entries = playlist_store.get(str(interaction.guild.id), {})
    lines = [f"**{name}** — {len(entry.get('tracks', []))} tracks" for name, entry in entries.items()]
    await interaction.response.send_message(embed=message_embed('\n'.join(lines) if lines else 'No playlists saved. Use /playlist_add.'), ephemeral=True)

@app_commands.guild_only()
@bot.tree.command(description="Add a saved playlist's latest synced tracks to the queue")
async def playlist_play(interaction: discord.Interaction, name: str):
    await interaction.response.defer(thinking=True)
    try:
        if not interaction.user.voice or not interaction.user.voice.channel:
            raise ValueError('Join a voice channel first.')
        entry = playlist_store.get(str(interaction.guild.id), {}).get(name.strip().lower())
        if not entry:
            raise ValueError('Playlist not found. Use /playlist_list.')
        voice = interaction.guild.voice_client
        if voice and voice.channel.id != interaction.user.voice.channel.id:
            raise ValueError('I am already in another voice channel.')
        p = get_player(interaction.guild)
        available = MAX_QUEUE - len(p.queue) - (1 if p.current else 0)
        items = entry.get('tracks', [])[:max(0, available)]
        if not items:
            raise ValueError('The queue is full or the playlist is empty.')
        if not voice:
            await interaction.user.voice.channel.connect(timeout=20)
        p.channel = interaction.channel
        p.queue.extend((Track(t['title'], t['query'], interaction.user.display_name, t.get('url', ''), author=t.get('author', ''), thumbnail=t.get('thumbnail', '')) for t in items))
        await interaction.followup.send(embed=message_embed(f'Added {len(items)} tracks from **{name}**.'), allowed_mentions=discord.AllowedMentions.none())
        await p.update_panel(move_to_bottom=True)
        p.start()
    except Exception as e:
        await interaction.followup.send(embed=message_embed(str(e)[:250]), ephemeral=True)

@app_commands.guild_only()
@bot.tree.command(description='Stop syncing and remove a saved playlist')
async def playlist_remove(interaction: discord.Interaction, name: str):
    entries = playlist_store.get(str(interaction.guild.id), {})
    if name.strip().lower() not in entries:
        await interaction.response.send_message(embed=message_embed('Playlist not found.'), ephemeral=True)
        return
    del entries[name.strip().lower()]
    save_playlists()
    await interaction.response.send_message(embed=message_embed('Playlist removed.'), ephemeral=True)
load_playlists()
if not TOKEN:
    raise SystemExit('Set DISCORD_TOKEN in your environment or .env file.')
bot.run(TOKEN)
