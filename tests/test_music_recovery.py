"""Tests for Esquelote music's restart and authorization-refresh behavior.

Loads GuildMusicState via AST to avoid needing real Discord credentials, voice
sockets, Twitch access or third-party packages in a unit-test environment.
Integration with real providers must still be tested separately.
"""
from __future__ import annotations

import ast
import asyncio
from collections import deque
from pathlib import Path
import logging
import os
import shlex
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4


REPO = Path(os.environ.get("ESQUELOTE_ROOT", Path(__file__).resolve().parents[1])).resolve()
MUSIC_PATH = REPO / "bot" / "cogs" / "music.py"


class FakeVoice:
    def __init__(self):
        self.playing = False
        self.stopped = 0
        self.source = None

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return False

    def is_connected(self):
        return True

    def stop(self):
        self.stopped += 1
        self.playing = False

    def play(self, source, *, after):
        self.source = source
        self.playing = True


class UserFacingError(Exception):
    pass


def load_state(source_path=MUSIC_PATH):
    text = Path(source_path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GuildMusicState")
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls],
        type_ignores=[],
    )
    mock_asyncio = types.SimpleNamespace(
        sleep=AsyncMock(), get_running_loop=asyncio.get_running_loop, create_task=asyncio.create_task
    )
    env = dict(
        asyncio=mock_asyncio, logging=logging, logger=logging.getLogger("esquelote-test"),
        deque=deque, discord=types.SimpleNamespace(), time=time, os=os,
        shlex=shlex, uuid4=uuid4, UserFacingError=UserFacingError,
        Any=object,
    )
    exec(compile(ast.fix_missing_locations(module), str(source_path), "exec"), env)
    return env["GuildMusicState"], env


def make_state():
    cls, env = load_state()
    state = cls(object())
    state.voice_client = FakeVoice()
    state._refresh_panel = AsyncMock()

    def start(track):
        state.playback_id += 1
        state.current = track
        state._started_at = time.monotonic()
        state.voice_client.playing = True

    state._start_track = start
    return state, env


class MusicRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_refresh_re_resolves_original_url(self):
        state, _ = make_state()
        state.current = {"title": "old", "is_live": False}
        state.queue.append({
            "title": "next", "is_live": False,
            "url": "https://www.youtube.com/watch?v=test123",
            "stream_url": "https://old.cdn/expired", "track_id": "abc",
        })
        state.resolve_track = AsyncMock(return_value={
            "title": "next", "url": "https://www.youtube.com/watch?v=test123",
            "stream_url": "https://fresh.cdn/valid", "is_live": False,
        })
        await state._play_next()
        state.resolve_track.assert_awaited_once_with("https://www.youtube.com/watch?v=test123")
        self.assertEqual(state.current["stream_url"], "https://fresh.cdn/valid")
        self.assertEqual(state.current["track_id"], "abc")

    async def test_live_eof_refreshes_signed_url(self):
        state, env = make_state()
        state.current = {"is_live": True, "url": "https://www.twitch.tv/tester", "track_id": "livetrack"}
        state.playback_id = 7
        state._started_at = time.monotonic() - 2
        state.resolve_live = AsyncMock(return_value={
            "is_live": True, "url": "https://www.twitch.tv/tester",
            "stream_url": "https://new.cdn/playlist.m3u8",
        })
        await state._on_track_end(7, None)  # FFmpeg can give clean EOF on 403
        state.resolve_live.assert_awaited_once()
        self.assertEqual(state.current["stream_url"], "https://new.cdn/playlist.m3u8")
        self.assertEqual(state.current["track_id"], "livetrack")
        self.assertEqual(state._live_retry_count, 1)
        self.assertEqual(env["asyncio"].sleep.await_args.args[0], 1)

    async def test_live_retries_are_bounded_if_provider_rejects(self):
        state, env = make_state()
        state.current = {"is_live": True, "url": "https://www.twitch.tv/tester"}
        state.playback_id = 5
        state._started_at = time.monotonic() - 2
        state.resolve_live = AsyncMock(side_effect=UserFacingError("offline"))
        await state._on_track_end(5, None)
        self.assertEqual(state.resolve_live.await_count, 3)
        self.assertEqual(state._live_retry_count, 3)
        self.assertIsNone(state.current)
        self.assertEqual(env["asyncio"].sleep.await_count, 3)

    async def test_early_music_end_retries_only_once(self):
        state, env = make_state()
        state.current = {
            "title": "song", "is_live": False,
            "url": "https://www.youtube.com/watch?v=x", "duration": 200,
            "track_id": "my-track",
        }
        state.playback_id = 2
        state._started_at = time.monotonic() - 5
        state.resolve_track = AsyncMock(return_value={
            "title": "song", "is_live": False, "url": "https://www.youtube.com/watch?v=x",
            "duration": 200, "stream_url": "https://fresh.cdn/song",
        })
        await state._on_track_end(2, None)
        self.assertEqual(state._track_retry_count, 1)
        self.assertEqual(state.current["stream_url"], "https://fresh.cdn/song")
        new_id = state.playback_id
        state._started_at = time.monotonic() - 2
        await state._on_track_end(new_id, None)
        self.assertEqual(state.resolve_track.await_count, 1)
        self.assertIsNone(state.current)

    async def test_natural_music_end_does_not_retry(self):
        state, _ = make_state()
        state.current = {"is_live": False, "duration": 40}
        state.playback_id = 3
        state._started_at = time.monotonic() - 40
        state.resolve_track = AsyncMock()
        await state._on_track_end(3, None)
        state.resolve_track.assert_not_awaited()
        self.assertIsNone(state.current)

    async def test_stop_during_refresh_does_not_restart_audio(self):
        state, _ = make_state()
        state.current = {"is_live": False}
        state.queue.append({"url": "https://www.youtube.com/watch?v=x", "is_live": False})

        async def interrupted(_):
            state.playback_id += 1  # user called stop while yt-dlp was fetching
            state.queue.clear()
            state.current = None
            return {"url": "https://www.youtube.com/watch?v=x", "is_live": False}

        state.resolve_track = AsyncMock(side_effect=interrupted)
        await state._play_next()
        self.assertIsNone(state.current)
        self.assertFalse(state.voice_client.playing)

    async def test_skip_does_not_repeat_skipped_track(self):
        state, _ = make_state()
        state.current = {"title": "skip-me", "url": "https://a", "is_live": False}
        state.repeat = True
        state.queue.append({"title": "next", "url": "https://b", "is_live": False})
        state.resolve_track = AsyncMock(return_value={"title": "next", "url": "https://b", "is_live": False})
        track = await state.skip()
        self.assertEqual(track["title"], "next")
        self.assertEqual(len(state.queue), 0)

    async def test_queued_track_not_replayed_after_failed_refresh(self):
        state, _ = make_state()
        state.queue.append({"url": "https://not-accessible", "is_live": False})
        state.resolve_track = AsyncMock(side_effect=UserFacingError("provider refused"))
        await state._play_next()
        self.assertIsNone(state.current)
        self.assertEqual(len(state.queue), 0)

    async def test_select_play_now_refreshes_url(self):
        state, _ = make_state()
        state.current = {"title": "currently", "is_live": False}
        state.playback_id = 4
        state.voice_client.playing = True
        state.queue.append({
            "title": "chosen", "url": "https://www.youtube.com/watch?v=chosen",
            "stream_url": "https://expired.cdn", "track_id": "target", "is_live": False,
        })
        state.resolve_track = AsyncMock(return_value={
            "title": "chosen", "url": "https://www.youtube.com/watch?v=chosen",
            "stream_url": "https://fresh.cdn", "is_live": False,
        })
        track = await state.select_queued_track("target", play_now=True)
        self.assertEqual(track["stream_url"], "https://fresh.cdn")
        self.assertEqual(state.current["track_id"], "target")
        self.assertEqual(state.voice_client.stopped, 1)

    async def test_old_end_callback_is_ignored(self):
        state, _ = make_state()
        state.current = {"title": "newer", "duration": 100, "is_live": False}
        state.playback_id = 10
        state._started_at = time.monotonic() - 1
        state.resolve_track = AsyncMock()
        await state._on_track_end(9, RuntimeError("old ffmpeg callback"))
        state.resolve_track.assert_not_awaited()
        self.assertEqual(state.current["title"], "newer")

    async def test_start_track_updates_state_only_after_play_succeeds(self):
        state, _ = make_state()
        state.current = {"title": "old"}
        state.playback_id = 12
        resource = types.SimpleNamespace(cleanup=lambda: None)
        state._make_source = lambda _: resource
        state._start_track = type(state)._start_track.__get__(state)
        state._start_track({"title": "new"})
        self.assertEqual(state.current["title"], "new")
        self.assertEqual(state.playback_id, 13)
        self.assertIs(state.voice_client.source, resource)

    async def test_start_failure_cleans_source_without_changing_track(self):
        state, _ = make_state()
        state.current = {"title": "playing"}
        state.playback_id = 8
        cleaned = []
        resource = types.SimpleNamespace(cleanup=lambda: cleaned.append(True))
        state._make_source = lambda _: resource
        state._start_track = type(state)._start_track.__get__(state)
        class FailingVoice(FakeVoice):
            def play(self, source, *, after):
                raise RuntimeError("Voice client unavailable")
        state.voice_client = FailingVoice()
        with self.assertRaises(RuntimeError):
            state._start_track({"title": "new"})
        self.assertEqual(cleaned, [True])
        self.assertEqual(state.current["title"], "playing")
        self.assertEqual(state.playback_id, 8)

    async def test_cancel_during_live_refresh_does_not_restart(self):
        state, _ = make_state()
        state.current = {"url": "https://www.twitch.tv/tester", "is_live": True}
        state.playback_id = 21
        state._started_at = time.monotonic() - 1
        async def canceled(_):
            state.playback_id += 1
            state.current = None
            return {"url": "https://www.twitch.tv/tester", "is_live": True}
        state.resolve_live = AsyncMock(side_effect=canceled)
        await state._on_track_end(21, None)
        self.assertIsNone(state.current)
        self.assertFalse(state.voice_client.playing)

    def test_compiles_and_reconnect_flags_exclude_403(self):
        code = MUSIC_PATH.read_text(encoding="utf-8")
        compile(code, str(MUSIC_PATH), "exec")
        self.assertIn('"-reconnect_on_http_error 429,500,502,503,504"', code)
        self.assertIn('"-reconnect_on_network_error 1 "', code)
        self.assertNotIn("yt-dlp>=2024.12.23,<2026.0", (REPO / "requirements.txt").read_text())


if __name__ == "__main__":
    unittest.main()
