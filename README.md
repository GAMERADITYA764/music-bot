# Vector Nodes Music — Python panel version

## Hosting panel setup
Upload these files to the root of your bot server. Select a Python 3.10–3.12 egg (3.12 recommended). Set the panel's startup command to `python bot.py`, or use the egg's standard Python startup setting. Configure the panel to install dependencies from `requirements.txt`, or run:

```bash
python -m pip install -r requirements.txt
```

Set the environment variable `DISCORD_TOKEN` in your hosting panel, or create `.env` from `.env.example` and insert your token there. Optional Spotify settings are also listed in `.env.example`. Never upload `.env` or your token to GitHub. Start or restart the bot and inspect panel logs. The bot must be invited using both the `bot` and `applications.commands` scopes, with View Channel, Send Messages, Connect and Speak permissions.

FFmpeg is needed for audio; the bot checks the system installation and falls back to imageio-ffmpeg if supported by your platform. Set `FFMPEG_PATH` if necessary. Your hosting panel/container must permit outbound UDP and return traffic to Discord voice; a voice discovery timeout cannot be fixed just by installing FFmpeg or Python packages.

## Features
Use `/play`, `/join`, `/disconnect`, `/stop`, `/queue`, `/volume`, `/filter`, `/loop`, `/lyrics`, `/ping` and other slash commands. `/stop` keeps the bot in voice; `/disconnect` leaves. A fresh music panel appears after `/play`, with a roughly 15-second timeline refresh. Filters restart the track; volume above 100% can cause distortion. `/playlist_add`, `/playlist`, `/playlist_play` and `/playlist_remove` manage public YouTube and accessible Spotify playlist snapshots; sync defaults to 30 minutes. Spotify requires API credentials and uses metadata matched to YouTube, not Spotify audio. Availability depends on providers and VPS network restrictions. Playlist data is stored in `data/`; configure the panel to preserve that directory on restarts. Private playlist login is not supported.

Song artwork/thumbnail appears in the queue confirmation and music panel when available from the source. Discord must be able to access the image URL. Spotify album artwork is used when provided; Spotify playback still uses a YouTube match.

`/autoplay` or the ♾️ panel button toggles autoplay while you are in the bot's voice channel. When the queue runs out, it searches YouTube for a related song and avoids recently played tracks. These are search-based suggestions, not guaranteed recommendations; if no result is found or the service fails, playback stops. Repeat takes priority over autoplay. Autoplay settings reset on bot restart.
