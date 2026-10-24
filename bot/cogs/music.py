import asyncio
import logging
import os
import re
import shlex
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import discord
from discord import app_commands
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from bot.checks import UserFacingError

logger = logging.getLogger(__name__)

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "default_search": "ytsearch",
    "extract_flat": False,
    "source_address": "0.0.0.0",
}

TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
IDLE_DISCONNECT_SECONDS = 5 * 60


def _build_ydl_options() -> dict[str, Any]:
    options = YDL_OPTIONS.copy()
    cookies_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if not cookies_file:
        return options

    cookie_path = Path(cookies_file).expanduser()
    if not cookie_path.is_absolute():
        cookie_path = Path(__file__).resolve().parents[2] / cookie_path
    if not cookie_path.is_file():
        raise UserFacingError(
            "O arquivo configurado em `YTDLP_COOKIES_FILE` não foi encontrado. "
            "Confira o caminho no `.env` do servidor."
        )

    options["cookiefile"] = str(cookie_path)
    return options


def format_duration(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


class GuildMusicState:
    def __init__(self, client: discord.Client) -> None:
        self.client = client
        self.queue: deque[dict[str, Any]] = deque()
        self.current: dict[str, Any] | None = None
        self.repeat = False
        self.volume = 0.5
        self.voice_client: discord.VoiceClient | None = None
        self.panel_message: discord.Message | None = None
        self.playback_id = 0
        self._started_at = 0.0
        self._live_retry_count = 0
        self._track_retry_count = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.idle_owner_id: int | None = None
        self.idle_disconnect_task: asyncio.Task[None] | None = None

    def arm_idle_disconnect(self, owner_id: int) -> None:
        self.idle_owner_id = owner_id
        if self.idle_disconnect_task is not None:
            self.idle_disconnect_task.cancel()
        self.idle_disconnect_task = None
        if self.current is not None and self.current.get("is_live"):
            return
        self.idle_disconnect_task = asyncio.create_task(
            self._disconnect_after_inactivity(owner_id)
        )

    def note_activity(self, user_id: int) -> None:
        if user_id == self.idle_owner_id:
            self.arm_idle_disconnect(user_id)

    async def _disconnect_after_inactivity(self, owner_id: int) -> None:
        try:
            await asyncio.sleep(IDLE_DISCONNECT_SECONDS)
            if self.idle_owner_id != owner_id:
                return
            await self.disconnect()
            if self.panel_message is not None:
                try:
                    await self.panel_message.channel.send(
                        "Saí do canal de voz após cinco minutos sem atividade de "
                        "quem iniciou a reprodução."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning(
                        "Não foi possível avisar o canal sobre a desconexão "
                        "automática por inatividade."
                    )
        except asyncio.CancelledError:
            raise
        finally:
            if self.idle_disconnect_task is asyncio.current_task():
                self.idle_disconnect_task = None

    async def disconnect(self) -> None:
        current_task = asyncio.current_task()
        if (
            self.idle_disconnect_task is not None
            and self.idle_disconnect_task is not current_task
        ):
            self.idle_disconnect_task.cancel()
        self.idle_disconnect_task = None
        self.idle_owner_id = None
        self.playback_id += 1
        if self.voice_client is not None:
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()
            if self.voice_client.is_connected():
                await self.voice_client.disconnect(force=True)
        self.voice_client = None
        self.queue.clear()
        self.current = None
        if self.panel_message is not None:
            try:
                await self.panel_message.edit(
                    embed=self.build_embed(),
                    view=None,
                )
            except discord.NotFound:
                self.panel_message = None

    def _make_source(self, track: dict[str, Any]) -> discord.AudioSource:
        stream_url = track.get("stream_url")
        if not stream_url:
            raise UserFacingError("Não foi possível obter uma fonte de áudio válida para esta faixa.")
        before_options = (
            "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
            "-reconnect_on_network_error 1 "
            "-reconnect_on_http_error 429,500,502,503,504"
        )
        headers = track.get("http_headers") or {}
        if headers:
            header_lines = "".join(
                f"{name}: {value}\r\n"
                for name, value in headers.items()
                if "\r" not in name
                and "\n" not in name
                and "\r" not in str(value)
                and "\n" not in str(value)
            )
            if header_lines:
                before_options += f" -headers {shlex.quote(header_lines)}"

        cookies = track.get("http_cookies") or []
        if cookies:
            cookie_lines = "".join(
                f"{cookie.name}={cookie.value}; path={cookie.path}; "
                f"domain={cookie.domain};\r\n"
                for cookie in cookies
            )
            before_options += f" -cookies {shlex.quote(cookie_lines)}"

        return discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                stream_url,
                executable="ffmpeg",
                before_options=before_options,
            ),
            volume=self.volume,
        )

    async def _refresh_panel(self) -> None:
        if self.panel_message is None or self.panel_message.channel is None:
            return
        embed = self.build_embed()
        try:
            await self.panel_message.edit(embed=embed, view=MusicPlayerView(self))
        except discord.NotFound:
            self.panel_message = None

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎶 Player de áudio",
            color=discord.Color.blurple(),
        )
        if self.current is not None:
            title = self.current["title"]
            track_url = self.current.get("url")
            if track_url:
                title = f"[{title}]({track_url})"
            status = (
                "⏸️ Pausada"
                if self.voice_client is not None and self.voice_client.is_paused()
                else "▶️ Tocando"
            )
            if self.current.get("is_live"):
                status = f"{status} · 🔴 AO VIVO"
            embed.description = f"{status}\n**{title}**"
            embed.add_field(
                name="Artista / canal",
                value=self.current.get("uploader") or "Não informado",
                inline=True,
            )
            if not self.current.get("is_live"):
                embed.add_field(
                    name="Duração",
                    value=format_duration(int(self.current.get("duration") or 0)),
                    inline=True,
                )
            if self.current.get("thumbnail"):
                embed.set_thumbnail(url=self.current["thumbnail"])
        else:
            embed.description = (
                "Nada tocando no momento.\nUse `/play` para buscar uma música "
                "ou `/twitch` para iniciar uma live."
            )

        queue_tracks = list(self.queue)
        if queue_tracks:
            upcoming = "\n".join(
                f"`{index}.` [{track['title']}]({track['url']})"
                if track.get("url")
                else f"`{index}.` {track['title']}"
                for index, track in enumerate(queue_tracks[:5], start=1)
            )
            if len(queue_tracks) > 5:
                upcoming += f"\n… e mais {len(queue_tracks) - 5} faixa(s)"
            embed.add_field(name=f"Próximas ({len(queue_tracks)})", value=upcoming, inline=False)
        else:
            embed.add_field(name="Fila", value="Nenhuma faixa na fila.", inline=False)

        embed.add_field(name="Volume", value=f"{self.volume * 100:.0f}%", inline=True)
        embed.add_field(name="Repetição", value="Ativada" if self.repeat else "Desativada", inline=True)
        embed.set_footer(text="Use os botões para controlar a reprodução.")
        return embed

    def _start_track(self, track: dict[str, Any]) -> None:
        if self.voice_client is None:
            raise UserFacingError("O bot nao esta conectado a um canal de voz.")
        loop = asyncio.get_running_loop()
        source = self._make_source(track)
        playback_id = self.playback_id + 1
        try:
            self.voice_client.play(
                source,
                after=lambda error: loop.call_soon_threadsafe(
                    asyncio.create_task, self._on_track_end(playback_id, error)
                ),
            )
        except Exception:
            # A failed VoiceClient.play must not leave an orphan FFmpeg process.
            try:
                source.cleanup()
            except Exception:
                logger.warning("Nao foi possivel limpar fonte de audio apos falha.")
            raise
        # Only publish the new playback state after play() succeeds.
        self.loop = loop
        self.current = track
        self.playback_id = playback_id
        self._started_at = time.monotonic()

    async def _play_next(self) -> None:
        ended_live_stream = bool(self.current is not None and self.current.get("is_live"))
        if self.repeat and self.current is not None and not ended_live_stream:
            self.queue.appendleft(self.current)

        # Invalidate the previous stream before an async re-extraction.
        # Stop, skip and a new /twitch must win over a stale background task.
        self.playback_id += 1
        request_id = self.playback_id
        while self.queue:
            old_track = self.queue.popleft()
            try:
                next_track = await self._refresh_queued_track(old_track)
            except Exception as exc:
                logger.warning("Faixa ignorada (URL indisponivel): %s", type(exc).__name__)
                continue
            if request_id != self.playback_id:
                return
            if self.voice_client is None or not self.voice_client.is_connected():
                return
            self._track_retry_count = 0
            self._live_retry_count = 0
            try:
                self._start_track(next_track)
            except Exception as exc:
                logger.warning("Falha ao iniciar proxima faixa: %s", type(exc).__name__)
                continue
            if ended_live_stream and self.idle_owner_id is not None:
                self.arm_idle_disconnect(self.idle_owner_id)
            await self._refresh_panel()
            return

        if request_id != self.playback_id:
            return
        self.current = None
        if ended_live_stream and self.idle_owner_id is not None:
            self.arm_idle_disconnect(self.idle_owner_id)
        await self._refresh_panel()

    async def _on_track_end(
        self, playback_id: int, error: Exception | None
    ) -> None:
        if playback_id != self.playback_id:
            return
        if error is not None:
            # Error strings may contain signed media URLs or cookies.
            logger.warning("Reproducao interrompida: %s", type(error).__name__)

        elapsed = max(0.0, time.monotonic() - self._started_at)
        if self.current is not None and self.current.get("is_live"):
            if elapsed >= 60:
                self._live_retry_count = 0
            if await self._recover_live(playback_id):
                return
        elif self.current is not None:
            duration = self.current.get("duration") or 0
            premature_end = error is not None or (
                duration >= 30 and elapsed + 15 < duration
            )
            if premature_end and await self._recover_track(playback_id):
                return
        if playback_id == self.playback_id:
            await self._play_next()

    async def _refresh_queued_track(self, track: dict[str, Any]) -> dict[str, Any]:
        """Refresh provider-signed media URLs right before playback.

        Keeping only the extracted CDN URL in the queue makes tracks expire
        while waiting for earlier music to finish.
        """
        if track.get("is_live"):
            refreshed = await self.resolve_live(track["url"])
        else:
            lookup = track.get("url") or track.get("source_query")
            if not lookup:
                raise UserFacingError("A faixa da fila nao possui URL de origem.")
            refreshed = await self.resolve_track(lookup)
            refreshed["source_query"] = track.get("source_query") or lookup
        refreshed["track_id"] = track.get("track_id") or str(uuid4())
        return refreshed

    async def _recover_live(self, playback_id: int) -> bool:
        """Try fresh Twitch access tokens, with bounded retries/backoff.

        FFmpeg may exit with a clean EOF on a rejected HLS segment, so we do
        not rely solely on its after(error) exception.
        """
        current = self.current
        if current is None or not current.get("is_live"):
            return False
        twitch_url = current.get("url")
        if not twitch_url:
            return False
        delays = (1, 3, 5)
        while self._live_retry_count < len(delays):
            delay = delays[self._live_retry_count]
            self._live_retry_count += 1
            await asyncio.sleep(delay)
            if playback_id != self.playback_id:
                return True  # another command changed/stopped playback
            try:
                refreshed = await self.resolve_live(twitch_url)
            except Exception as exc:
                logger.warning("Falha ao renovar live: %s", type(exc).__name__)
                continue
            if playback_id != self.playback_id:
                return True
            if self.voice_client is None or not self.voice_client.is_connected():
                return False
            refreshed["track_id"] = current.get("track_id") or str(uuid4())
            try:
                self._start_track(refreshed)
            except Exception as exc:
                logger.warning("Falha ao reiniciar live: %s", type(exc).__name__)
                return False
            await self._refresh_panel()
            return True
        return False

    async def _recover_track(self, playback_id: int) -> bool:
        """Retry a prematurely interrupted music item only once."""
        current = self.current
        if current is None or current.get("is_live") or self._track_retry_count >= 1:
            return False
        self._track_retry_count += 1
        try:
            refreshed = await self._refresh_queued_track(current)
        except Exception as exc:
            logger.warning("Falha ao renovar musica: %s", type(exc).__name__)
            return False
        if playback_id != self.playback_id:
            return True
        if self.voice_client is None or not self.voice_client.is_connected():
            return False
        try:
            self._start_track(refreshed)
        except Exception as exc:
            logger.warning("Falha ao reiniciar musica: %s", type(exc).__name__)
            return False
        await self._refresh_panel()
        return True

    async def ensure_voice_channel(self, interaction: discord.Interaction) -> discord.VoiceChannel:
        if not isinstance(interaction.user, discord.Member):
            raise UserFacingError("Não foi possível identificar seu membro no servidor.")
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            raise UserFacingError("Você precisa entrar em um canal de voz antes de usar este comando.")

        channel = interaction.user.voice.channel
        guild = interaction.guild
        if guild is None:
            raise UserFacingError("Este comando só pode ser usado em um servidor.")

        voice_client = guild.voice_client
        if voice_client is None:
            self.voice_client = await channel.connect()
        elif voice_client.channel != channel:
            raise UserFacingError("O bot já está em outro canal de voz. Entre no mesmo canal para tocar música.")
        else:
            self.voice_client = voice_client

        if not self.voice_client:
            raise UserFacingError("Não consegui conectar o bot ao canal de voz.")
        return channel

    async def add_to_queue(self, interaction: discord.Interaction, query: str) -> dict[str, Any]:
        await self.ensure_voice_channel(interaction)
        track = await self.resolve_track(query)
        track["track_id"] = str(uuid4())
        track["source_query"] = query.strip()
        self.queue.append(track)
        self.arm_idle_disconnect(interaction.user.id)
        if self.voice_client is not None and not self.voice_client.is_playing() and not self.voice_client.is_paused():
            if self.current is None:
                self._start_track(self.queue.popleft())
                await self._refresh_panel()
                return track
        await self._refresh_panel()
        return track

    async def play_live(
        self,
        interaction: discord.Interaction,
        twitch_url: str,
    ) -> dict[str, Any]:
        track = await self.resolve_live(twitch_url)
        await self.ensure_voice_channel(interaction)

        if self.voice_client is None:
            raise UserFacingError("Não consegui conectar o bot ao canal de voz.")

        self.playback_id += 1
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self.voice_client.stop()
        self.current = None
        track["track_id"] = str(uuid4())
        self._track_retry_count = 0
        self._live_retry_count = 0
        self._start_track(track)
        self.arm_idle_disconnect(interaction.user.id)
        await self._refresh_panel()
        return track

    async def resolve_track(self, query: str) -> dict[str, Any]:
        search_query = query.strip()
        if not search_query:
            raise UserFacingError("Você precisa informar o nome ou o link da música.")

        return await asyncio.to_thread(self._resolve_track, search_query)

    async def resolve_live(self, twitch_url: str) -> dict[str, Any]:
        stream_url = self._validate_twitch_url(twitch_url)
        return await asyncio.to_thread(self._resolve_live, stream_url)

    @staticmethod
    def _validate_twitch_url(twitch_url: str) -> str:
        stream_url = twitch_url.strip()
        if not stream_url:
            raise UserFacingError(
                "Informe o nome do canal ou o link da live da Twitch."
            )

        if re.fullmatch(r"[A-Za-z0-9_]{1,25}", stream_url):
            return f"https://www.twitch.tv/{stream_url}"

        if not stream_url.startswith(("https://", "http://")):
            stream_url = f"https://{stream_url}"

        parsed_url = urlsplit(stream_url)
        path_parts = [part for part in parsed_url.path.split("/") if part]
        if (
            parsed_url.scheme not in {"https", "http"}
            or parsed_url.hostname not in TWITCH_HOSTS
            or len(path_parts) != 1
            or re.fullmatch(r"[A-Za-z0-9_]{1,25}", path_parts[0]) is None
        ):
            raise UserFacingError(
                "Informe o nome do canal ou um link como "
                "`https://www.twitch.tv/nome_do_canal`."
            )
        return f"https://www.twitch.tv/{path_parts[0]}"

    def _resolve_live(self, twitch_url: str) -> dict[str, Any]:
        ydl = YoutubeDL(_build_ydl_options())
        try:
            info = ydl.extract_info(twitch_url, download=False)
        except DownloadError as error:
            raise UserFacingError(
                "Não consegui acessar essa live. Confira o link e se o canal "
                "está transmitindo uma live pública agora."
            ) from error

        if info is None or info.get("is_live") is not True:
            raise UserFacingError(
                "Esse canal não está ao vivo agora ou a live não está acessível."
            )

        formats = info.get("formats") or []
        audio_formats = [
            entry
            for entry in formats
            if entry.get("acodec") not in (None, "none") and entry.get("url")
        ]
        audio_only_formats = [
            entry
            for entry in audio_formats
            if entry.get("vcodec") in (None, "none")
        ]
        candidates = audio_only_formats or audio_formats
        if not candidates:
            raise UserFacingError(
                "Encontrei a live, mas não consegui obter um formato de áudio compatível."
            )

        audio_format = max(
            candidates,
            key=lambda entry: (
                entry.get("protocol") in {"http", "https"},
                entry.get("abr") or 0,
                entry.get("tbr") or 0,
            ),
        )
        stream_url = audio_format["url"]
        thumbnails = info.get("thumbnails") or []
        return {
            "title": info.get("description") or info.get("title") or "Live da Twitch",
            "url": info.get("webpage_url") or twitch_url,
            "thumbnail": info.get("thumbnail")
            or (thumbnails[-1].get("url") if thumbnails else None),
            "stream_url": stream_url,
            "http_headers": audio_format.get("http_headers") or {},
            "http_cookies": ydl.cookiejar.get_cookies_for_url(stream_url),
            "duration": None,
            "uploader": info.get("uploader") or info.get("uploader_id") or "Twitch",
            "is_live": True,
        }

    def _resolve_track(self, search_query: str) -> dict[str, Any]:
        ydl = YoutubeDL(_build_ydl_options())
        info = None
        try:
            if any(marker in search_query for marker in ("youtube.com", "youtu.be", "soundcloud.com", "spotify.com")):
                info = ydl.extract_info(search_query, download=False)
            else:
                info = ydl.extract_info(f"ytsearch1:{search_query}", download=False)
        except Exception as exc:  # pragma: no cover - depende do provedor externo
            error_message = str(exc).casefold().replace("’", "'")
            if "sign in to confirm you're not a bot" in error_message:
                raise UserFacingError(
                    "O YouTube recusou a solicitação deste servidor por uma "
                    "verificação anti-bot. Se você tem autorização para acessar "
                    "o conteúdo, configure um arquivo de cookies válido com "
                    "`YTDLP_COOKIES_FILE` no `.env` do servidor. Cookies são "
                    "credenciais privadas: não os envie no Discord nem os "
                    "adicione ao Git."
                ) from exc
            raise UserFacingError("Não consegui localizar a música informada. Tente outra busca ou um link válido.") from exc

        if info is None:
            raise UserFacingError("Não encontrei resultados para essa busca.")

        if "entries" in info and info["entries"]:
            info = info["entries"][0]
        if info is None:
            raise UserFacingError("Não encontrei resultados para essa busca.")

        formats = info.get("formats") or []
        audio_candidates = [
            entry
            for entry in formats
            if entry.get("acodec") not in (None, "none") and entry.get("url")
        ]
        audio_only_candidates = [
            entry
            for entry in audio_candidates
            if entry.get("vcodec") in (None, "none")
        ]
        direct_audio_candidates = [
            entry
            for entry in audio_only_candidates
            if entry.get("protocol") in {"http", "https"}
        ]
        non_hls_audio_candidates = [
            entry
            for entry in audio_only_candidates
            if entry.get("protocol") not in {"m3u8", "m3u8_native"}
        ]
        candidates = (
            direct_audio_candidates
            or non_hls_audio_candidates
            or audio_only_candidates
            or audio_candidates
        )
        selected_format = (
            max(
                candidates,
                key=lambda entry: (
                    entry.get("protocol") in {"http", "https"},
                    entry.get("abr") or 0,
                    entry.get("tbr") or 0,
                ),
            )
            if candidates
            else None
        )
        stream_url = (
            selected_format.get("url")
            if selected_format is not None
            else info.get("url") or info.get("webpage_url")
        )

        if stream_url is None:
            raise UserFacingError("Não consegui obter um canal de áudio compatível para esta música.")

        thumbnails = info.get("thumbnails") or []
        thumbnail = info.get("thumbnail") or (
            thumbnails[-1].get("url") if thumbnails else None
        )
        return {
            "title": info.get("title") or "Música sem título",
            "url": info.get("webpage_url") or info.get("url") or search_query,
            "thumbnail": thumbnail,
            "stream_url": stream_url,
            "http_headers": (
                selected_format.get("http_headers") or {}
                if selected_format is not None
                else info.get("http_headers") or {}
            ),
            "http_cookies": ydl.cookiejar.get_cookies_for_url(stream_url),
            "duration": info.get("duration") or 0,
            "uploader": info.get("uploader") or "Desconhecido",
            "is_live": False,
        }

    async def pause(self) -> None:
        if self.voice_client is None or not self.voice_client.is_connected():
            raise UserFacingError("O bot não está conectado a um canal de voz.")
        if self.voice_client.is_playing():
            self.voice_client.pause()
        elif self.voice_client.is_paused():
            self.voice_client.resume()
        else:
            raise UserFacingError("Não há música sendo reproduzida no momento.")

    async def resume(self) -> None:
        if self.voice_client is None or not self.voice_client.is_connected():
            raise UserFacingError("O bot não está conectado a um canal de voz.")
        if self.voice_client.is_paused():
            self.voice_client.resume()
        else:
            raise UserFacingError("A reprodução não está pausada.")

    async def skip(self) -> dict[str, Any] | None:
        if self.voice_client is None:
            raise UserFacingError("Nao ha reproducao ativa no momento.")
        was_live = bool(self.current is not None and self.current.get("is_live"))
        self.playback_id += 1
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self.voice_client.stop()
        self.current = None  # Skip must not put the skipped song back on repeat.
        await self._play_next()
        if was_live and self.idle_owner_id is not None:
            self.arm_idle_disconnect(self.idle_owner_id)
        return self.current

    async def stop(self) -> None:
        self.playback_id += 1
        if self.voice_client is not None:
            self.voice_client.stop()
        self.queue.clear()
        self.current = None

    async def queue_command(self) -> discord.Embed:
        return self.build_embed()

    async def select_queued_track(
        self, track_id: str, *, play_now: bool
    ) -> dict[str, Any]:
        if self.voice_client is None or not self.voice_client.is_connected():
            raise UserFacingError("O bot nao esta conectado a um canal de voz.")
        track = next((item for item in self.queue if item["track_id"] == track_id), None)
        if track is None:
            raise UserFacingError("Essa faixa nao esta mais na fila.")
        self.queue.remove(track)
        if play_now:
            request_id = self.playback_id
            try:
                refreshed = await self._refresh_queued_track(track)
            except Exception as exc:
                self.queue.appendleft(track)
                raise UserFacingError("Nao consegui renovar o audio dessa faixa.") from exc
            if request_id != self.playback_id:
                raise UserFacingError("A reproducao mudou durante a selecao da faixa.")
            self.playback_id += 1
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()
            self._track_retry_count = 0
            self._live_retry_count = 0
            self._start_track(refreshed)
            return refreshed
        self.queue.appendleft(track)
        if self.current is None and not self.voice_client.is_playing() and not self.voice_client.is_paused():
            await self._play_next()
        return track

class QueueActionSelect(discord.ui.Select):
    def __init__(self, state: GuildMusicState, *, play_now: bool) -> None:
        queued_tracks = list(state.queue)[:25]
        options = [
            discord.SelectOption(
                label=track["title"][:100],
                value=track["track_id"],
                description=(
                    "Tocar imediatamente"
                    if play_now
                    else "Colocar no início da fila"
                ),
            )
            for track in queued_tracks
        ]
        if not options:
            options = [discord.SelectOption(label="Fila vazia", value="__empty")]
        super().__init__(
            placeholder=(
                "Escolha uma faixa para tocar agora"
                if play_now
                else "Escolha a próxima faixa"
            ),
            min_values=1,
            max_values=1,
            options=options,
            disabled=not queued_tracks,
            row=2 if play_now else 3,
        )
        self.state = state
        self.play_now = play_now

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "__empty":
            await interaction.response.send_message("A fila está vazia.")
            return
        self.state.note_activity(interaction.user.id)
        track = await self.state.select_queued_track(
            self.values[0],
            play_now=self.play_now,
        )
        action = "Tocando agora" if self.play_now else "Próxima faixa"
        await interaction.response.send_message(
            f"🎵 **{action}:** {track['title']}"
        )
        await self.state._refresh_panel()


class MusicPlayerView(discord.ui.View):
    def __init__(self, state: GuildMusicState) -> None:
        super().__init__(timeout=None)
        self.state = state
        self.pause_button.label = (
            "▶️ Continuar"
            if state.voice_client is not None and state.voice_client.is_paused()
            else "⏸️ Pausar"
        )
        self.skip_button.label = "⏭️ Pular"
        self.repeat_button.label = "🔁 Repetir"
        self.stop_button.label = "⏹️ Parar"
        self.volume_down.label = "🔉 Volume −"
        self.volume_up.label = "🔊 Volume +"
        self.add_item(QueueActionSelect(state, play_now=True))
        self.add_item(QueueActionSelect(state, play_now=False))

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
        /,
    ) -> None:
        if isinstance(error, UserFacingError):
            message = str(error)
            if message == "Essa faixa não está mais na fila.":
                message = "A fila mudou desde que este painel foi aberto. Use `/queue` para ver as opções atuais."
            elif message == "Não há música sendo reproduzida no momento.":
                message = "Não há nenhuma música tocando para pausar."
        else:
            logger.error(
                "Falha no controle de música %s.",
                item,
                exc_info=(type(error), error, error.__traceback__),
            )
            message = "Não consegui executar esse controle de música. Tente novamente."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="⏸️", style=discord.ButtonStyle.secondary)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.state.note_activity(interaction.user.id)
        if not interaction.response.is_done():
            await interaction.response.defer()
        if self.state.voice_client is not None and self.state.voice_client.is_paused():
            await self.state.resume()
        else:
            await self.state.pause()
        await self.state._refresh_panel()

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.primary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.state.note_activity(interaction.user.id)
        if not interaction.response.is_done():
            await interaction.response.defer()
        await self.state.skip()
        await self.state._refresh_panel()

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary)
    async def repeat_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.state.note_activity(interaction.user.id)
        if not interaction.response.is_done():
            await interaction.response.defer()
        self.state.repeat = not self.state.repeat
        await self.state._refresh_panel()

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.state.note_activity(interaction.user.id)
        if not interaction.response.is_done():
            await interaction.response.defer()
        await self.state.stop()
        await self.state._refresh_panel()

    @discord.ui.button(label="🔉", style=discord.ButtonStyle.secondary)
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.state.note_activity(interaction.user.id)
        if not interaction.response.is_done():
            await interaction.response.defer()
        self.state.volume = max(0.0, self.state.volume - 0.1)
        if self.state.voice_client is not None and self.state.voice_client.source is not None:
            if hasattr(self.state.voice_client.source, "volume"):
                self.state.voice_client.source.volume = self.state.volume
        await self.state._refresh_panel()

    @discord.ui.button(label="🔊", style=discord.ButtonStyle.secondary)
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.state.note_activity(interaction.user.id)
        if not interaction.response.is_done():
            await interaction.response.defer()
        self.state.volume = min(2.0, self.state.volume + 0.1)
        if self.state.voice_client is not None and self.state.voice_client.source is not None:
            if hasattr(self.state.voice_client.source, "volume"):
                self.state.voice_client.source.volume = self.state.volume
        await self.state._refresh_panel()


def register(tree: app_commands.CommandTree, client: discord.Client) -> None:
    guild_states: dict[int, GuildMusicState] = {}

    def get_state(guild_id: int) -> GuildMusicState:
        return guild_states.setdefault(guild_id, GuildMusicState(client))

    @tree.command(name="play", description="Busca uma música e adiciona à reprodução")
    @app_commands.guild_only()
    @app_commands.describe(busca="Nome ou link da música")
    async def play_command(
        interaction: discord.Interaction,
        busca: str,
    ) -> None:
        state = get_state(interaction.guild_id)
        await interaction.response.defer()
        track = await state.add_to_queue(interaction, busca)
        await interaction.followup.send(
            f"🎵 Adicionada à fila: **{track['title']}**. "
            "Use `/queue` para ver a fila."
        )
        if state.panel_message is None and interaction.channel is not None:
            state.panel_message = await interaction.channel.send(
                embed=state.build_embed(), view=MusicPlayerView(state)
            )

    @tree.command(name="twitch", description="Toca o áudio de uma live da Twitch")
    @app_commands.guild_only()
    @app_commands.describe(canal="Nome do canal ou link da live na Twitch")
    async def twitch_command(
        interaction: discord.Interaction,
        canal: str,
    ) -> None:
        state = get_state(interaction.guild_id)
        await interaction.response.defer()
        track = await state.play_live(interaction, canal)
        await interaction.followup.send(
            f"🔴 Tocando o áudio da live **{track['title']}**. "
            "Use `/stop` para parar ou `/leave` para parar e sair do canal."
        )
        if state.panel_message is None and interaction.channel is not None:
            state.panel_message = await interaction.channel.send(
                embed=state.build_embed(), view=MusicPlayerView(state)
            )

    @tree.command(name="pause", description="Pausa a reprodução atual")
    @app_commands.guild_only()
    async def pause_command(interaction: discord.Interaction) -> None:
        state = get_state(interaction.guild_id)
        state.note_activity(interaction.user.id)
        await state.pause()
        await interaction.response.send_message("⏸️ Reprodução pausada.")

    @tree.command(name="resume", description="Retoma a reprodução pausada")
    @app_commands.guild_only()
    async def resume_command(interaction: discord.Interaction) -> None:
        state = get_state(interaction.guild_id)
        state.note_activity(interaction.user.id)
        await state.resume()
        await interaction.response.send_message("▶️ Reprodução retomada.")

    @tree.command(name="skip", description="Pula para a próxima música da fila")
    @app_commands.guild_only()
    async def skip_command(interaction: discord.Interaction) -> None:
        state = get_state(interaction.guild_id)
        state.note_activity(interaction.user.id)
        next_track = await state.skip()
        if next_track is None:
            await interaction.response.send_message(
                "⏭️ Fim da fila. Nenhuma música restante."
            )
            return
        await interaction.response.send_message(
            f"⏭️ Pulando para: **{next_track['title']}**"
        )

    @tree.command(name="stop", description="Para a reprodução e limpa a fila")
    @app_commands.guild_only()
    async def stop_command(interaction: discord.Interaction) -> None:
        state = get_state(interaction.guild_id)
        state.note_activity(interaction.user.id)
        await state.stop()
        await interaction.response.send_message(
            "⏹️ Reprodução encerrada e fila limpa."
        )

    @tree.command(name="queue", description="Mostra a fila e a música atual")
    @app_commands.guild_only()
    async def queue_command(interaction: discord.Interaction) -> None:
        state = get_state(interaction.guild_id)
        state.note_activity(interaction.user.id)
        embed = await state.queue_command()
        await interaction.response.send_message(
            embed=embed,
            view=MusicPlayerView(state),
        )

    @tree.command(
        name="leave",
        description="Para a música, limpa a fila e desconecta do canal de voz",
    )
    @app_commands.guild_only()
    async def leave_command(interaction: discord.Interaction) -> None:
        state = get_state(interaction.guild_id)
        await state.disconnect()
        await interaction.response.send_message(
            "⏹️ Fila limpa; saí do canal de voz."
        )
