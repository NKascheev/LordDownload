"""Tests for the local downloader application."""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import downloader  # noqa: E402
from downloader import DownloadError  # noqa: E402


class UrlValidationTests(unittest.TestCase):
    """Validation and normalization of YouTube links."""
    def test_accepts_and_canonicalizes_youtube_urls(self) -> None:
        """Verify accepts and canonicalizes youtube urls."""
        video_id = "dQw4w9WgXcQ"
        cases = [
            f"https://www.youtube.com/watch?v={video_id}",
            f"http://youtube.com/watch?v={video_id}&t=42&list=PL123",
            f"https://youtu.be/{video_id}?t=15",
            f"https://www.youtu.be/{video_id}/",
            f"https://m.youtube.com/shorts/{video_id}",
            f"https://music.youtube.com/watch?v={video_id}&si=abc",
            f"https://www.youtube.com/embed/{video_id}/",
            f"https://www.youtube.com/live/{video_id}",
            f"https://www.youtube-nocookie.com/embed/{video_id}",
            f"https://www.youtube.com/watch?v={video_id}#fragment",
        ]
        expected = f"https://www.youtube.com/watch?v={video_id}"
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(downloader.normalize_youtube_url(value), expected)

    def test_rejects_unsafe_or_non_video_urls(self) -> None:
        """Verify rejects unsafe or non video urls."""
        video_id = "dQw4w9WgXcQ"
        cases = [
            None,
            123,
            "",
            "   ",
            f"ftp://youtube.com/watch?v={video_id}",
            f"javascript:alert(1)//youtube.com/watch?v={video_id}",
            f"https://evil.example/watch?v={video_id}",
            f"https://youtube.com.evil.example/watch?v={video_id}",
            f"https://evil.example/youtube.com/watch?v={video_id}",
            f"https://youtube.com/playlist?list=PL123",
            f"https://youtube.com/channel/UC123",
            f"https://youtube.com/watch?v=too-short",
            f"https://youtube.com/watch?v={video_id}&v={video_id}",
            f"https://user:pass@youtube.com/watch?v={video_id}",
            f"https://youtube.com:443/watch?v={video_id}",
            f"https://youtube.com/shorts/{video_id}/extra",
            f"https://youtube.com/watch?v={video_id}\r\nX-Test: 1",
            f"https://youtube.com/watch?v={video_id}&x=1\nmalicious",
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(DownloadError) as context:
                    downloader.normalize_youtube_url(value)
                self.assertEqual(context.exception.http_status, 400)

    def test_rejects_overlong_url(self) -> None:
        """Verify rejects overlong url."""
        with self.assertRaises(DownloadError):
            downloader.normalize_youtube_url("https://youtu.be/" + "a" * 3000)


class InstagramUrlValidationTests(unittest.TestCase):
    """Validation and normalization of Instagram links."""
    def test_accepts_and_canonicalizes_instagram_urls(self) -> None:
        """Verify accepts and canonicalizes instagram urls."""
        cases = {
            "https://www.instagram.com/p/BQ0eAlwhDrw/": (
                "https://www.instagram.com/p/BQ0eAlwhDrw/"
            ),
            "https://www.instagram.com/reel/Chunk8-jurw/?igsh=abc": (
                "https://www.instagram.com/reel/Chunk8-jurw/"
            ),
            "https://instagram.com/reels/Chunk8-jurw/": (
                "https://www.instagram.com/reel/Chunk8-jurw/"
            ),
            "http://m.instagram.com/tv/BkfuX9UB-eK": (
                "https://www.instagram.com/tv/BkfuX9UB-eK/"
            ),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(downloader.normalize_instagram_url(value), expected)

    def test_rejects_unsafe_or_non_post_urls(self) -> None:
        """Verify rejects unsafe or non post urls."""
        cases = [
            None,
            "",
            "https://evil.example/reel/Chunk8-jurw/",
            "https://instagram.com.evil.example/reel/Chunk8-jurw/",
            "https://www.instagram.com/explore/",
            "https://www.instagram.com/instagram/",
            "https://www.instagram.com/reel/abc/",
            "https://www.instagram.com/reel/Chunk8-jurw/extra",
            "https://user:pass@instagram.com/reel/Chunk8-jurw/",
            "https://instagram.com:443/reel/Chunk8-jurw/",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(DownloadError):
                    downloader.normalize_instagram_url(value)

    def test_media_normalizer_dispatches_by_source(self) -> None:
        """Verify media normalizer dispatches by source."""
        self.assertEqual(
            downloader.normalize_media_url("https://youtu.be/dQw4w9WgXcQ", "youtube"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertEqual(
            downloader.normalize_media_url(
                "https://www.instagram.com/reel/Chunk8-jurw/",
                "instagram",
            ),
            "https://www.instagram.com/reel/Chunk8-jurw/",
        )
        with self.assertRaises(DownloadError):
            downloader.normalize_media_url("https://example.com/", "unknown")


class FilenameTests(unittest.TestCase):
    """Safe filename generation."""
    def test_removes_path_separators_and_controls(self) -> None:
        """Verify removes path separators and controls."""
        value = downloader.sanitize_filename("../../evil\x00name.mp4")
        self.assertNotIn("/", value)
        self.assertNotIn("\\", value)
        self.assertNotIn("\x00", value)
        self.assertNotIn("..", value)

    def test_handles_windows_reserved_names(self) -> None:
        """Verify handles windows reserved names."""
        for name in ("CON", "con.txt", "COM1", "LPT9"):
            with self.subTest(name=name):
                self.assertTrue(downloader.sanitize_filename(name).startswith("_"))

    def test_limits_length_and_has_fallback(self) -> None:
        """Verify limits length and has fallback."""
        value = downloader.sanitize_filename("x" * 500)
        self.assertLessEqual(len(value), 160)
        self.assertEqual(downloader.sanitize_filename(None), "youtube-video")
        self.assertEqual(downloader.sanitize_filename("..."), "youtube-video")

    def test_keeps_unicode_title(self) -> None:
        """Verify keeps unicode title."""
        value = downloader.sanitize_filename("Лекция — день 1.mp4")
        self.assertIn("Лекция", value)
        self.assertTrue(value.endswith(".mp4"))

    def test_display_filename_keeps_extension(self) -> None:
        """Verify long titles are shortened without losing the extension."""
        value = downloader._display_filename("x" * 500, "dQw4w9WgXcQ", "1080p", "mp4")
        self.assertTrue(value.endswith(".mp4"))
        self.assertLessEqual(len(value), 170)


class SubtitleConversionTests(unittest.TestCase):
    """Subtitle conversion and output handling."""
    def test_vtt_becomes_plain_text(self) -> None:
        """Verify vtt becomes plain text."""
        content = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:02.500 align:start position:0%
<v Speaker>Hello &amp; welcome</v>

00:00:02.500 --> 00:00:04.000
Hello &amp; welcome

00:00:04.000 --> 00:00:06.000
Second line
with <i>markup</i>
"""
        self.assertEqual(
            downloader.vtt_to_text(content),
            "Hello & welcome\nSecond line with markup",
        )

    def test_srt_is_supported(self) -> None:
        """Verify srt is supported."""
        content = """1
00:00:01,000 --> 00:00:02,000
Первая строка

2
00:00:02,500 --> 00:00:04,000
Вторая &amp; последняя
"""
        self.assertEqual(
            downloader.vtt_to_text(content),
            "Первая строка\nВторая & последняя",
        )

    def test_ignores_notes_and_inline_timestamps(self) -> None:
        """Verify ignores notes and inline timestamps."""
        content = """WEBVTT

NOTE this is a comment

00:00.000 --> 00:02.000
<00:00:00.000><c>Hello</c> <00:00:01.000><c>world</c>
"""
        self.assertEqual(downloader.vtt_to_text(content), "Hello world")

    def test_empty_input(self) -> None:
        """Verify empty input."""
        self.assertEqual(downloader.vtt_to_text(""), "")
        self.assertEqual(downloader.vtt_to_text("WEBVTT\n\n"), "")

    def test_conversion_writes_txt_and_removes_vtt(self) -> None:
        """Verify conversion writes txt and removes vtt."""
        workdir = ROOT / ".test-subtitles"
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir()
        try:
            source = workdir / "media.en.vtt"
            source.write_text(
                "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n",
                encoding="utf-8",
            )
            target = downloader._convert_subtitles(workdir)
            self.assertEqual(target.name, "subtitles.txt")
            self.assertEqual(target.read_text(encoding="utf-8-sig").strip(), "Hello")
            self.assertFalse(source.exists())
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


class MetadataCollectionTests(unittest.TestCase):
    """Metadata extraction and format selection."""
    def test_quality_deduplication_and_sorting(self) -> None:
        """Verify quality deduplication and sorting."""
        info = {
            "duration": 60,
            "formats": [
                {"vcodec": "avc1.4d", "height": 720, "ext": "mp4", "fps": 30, "filesize": 2000},
                {"vcodec": "vp9", "height": 720, "ext": "webm", "fps": 60, "filesize": 1800},
                {"vcodec": "avc1.4d", "height": 1080, "ext": "mp4", "fps": 30, "filesize": 3000},
                {"vcodec": "av01", "height": 1080, "ext": "mp4", "fps": 60, "filesize": 2800},
                {"vcodec": "none", "acodec": "mp4a", "ext": "m4a", "filesize": 500},
                {"format_id": "sb0", "vcodec": "none", "height": 90, "ext": "mhtml"},
                {"vcodec": "none", "height": 480, "ext": "mp4"},
            ],
        }
        qualities = downloader._collect_qualities(info)
        self.assertEqual([item["height"] for item in qualities], [1080, 720])
        self.assertEqual(qualities[0]["codec"], "H.264")
        self.assertEqual(qualities[0]["filesize"], 3500)
        self.assertEqual(qualities[1]["codec"], "H.264")

    def test_subtitles_manual_wins_over_auto(self) -> None:
        """Verify subtitles manual wins over auto."""
        info = {
            "subtitles": {"en": [{"ext": "vtt"}], "ru": [{"ext": "vtt"}]},
            "automatic_captions": {"en": [{"ext": "vtt"}], "de": [{"ext": "vtt"}]},
        }
        subtitles = downloader._collect_subtitles(info)
        by_code = {item["code"]: item for item in subtitles}
        self.assertFalse(by_code["en"]["auto"])
        self.assertFalse(by_code["ru"]["auto"])
        self.assertTrue(by_code["de"]["auto"])
        self.assertEqual(len(subtitles), 3)

    def test_rejects_invalid_subtitle_codes(self) -> None:
        """Verify rejects invalid subtitle codes."""
        info = {
            "subtitles": {
                "../evil": [{"ext": "vtt"}],
                "en": [{"ext": "vtt"}],
                "a" * 100: [{"ext": "vtt"}],
            }
        }
        self.assertEqual(
            [item["code"] for item in downloader._collect_subtitles(info)],
            ["en"],
        )

    def test_video_selector_is_numeric_only(self) -> None:
        """Verify video selector is numeric only."""
        selector = downloader._format_selector("youtube", 1080)
        self.assertIn("height=1080", selector)
        with self.assertRaises(DownloadError):
            downloader._format_selector("youtube", 9999)
        with self.assertRaises(DownloadError):
            downloader._format_selector("youtube", -1)

    def test_instagram_selector_is_numeric_only(self) -> None:
        """Verify instagram selector is numeric only."""
        selector = downloader._format_selector("instagram", 1280)
        self.assertIn("height=1280", selector)
        with self.assertRaises(DownloadError):
            downloader._format_selector("instagram", 9999)


class ChannelAvatarTests(unittest.TestCase):
    """Channel avatar selection."""
    def test_prefers_square_avatar_over_banner(self) -> None:
        """Verify prefers square avatar over banner."""
        info = {
            "thumbnails": [
                {
                    "id": "banner_uncropped",
                    "url": "https://yt3.googleusercontent.com/banner=s0",
                },
                {
                    "id": "7",
                    "url": "https://yt3.googleusercontent.com/avatar=s900-c-k-c0x00ffffff-no-rj",
                    "width": 900,
                    "height": 900,
                },
            ]
        }
        self.assertEqual(
            downloader._pick_channel_avatar_url(info),
            "https://yt3.googleusercontent.com/avatar=s176-c-k-c0x00ffffff-no-rj",
        )

    def test_accepts_square_fallback_without_known_id(self) -> None:
        """Verify accepts square fallback without known id."""
        info = {
            "thumbnails": [
                {
                    "url": "https://yt3.ggpht.com/avatar=s0",
                    "width": 128,
                    "height": 128,
                }
            ]
        }
        self.assertEqual(
            downloader._pick_channel_avatar_url(info),
            "https://yt3.ggpht.com/avatar=s176-c-k-c0x00ffffff-no-rj",
        )

    def test_rejects_foreign_avatar_host(self) -> None:
        """Verify rejects foreign avatar host."""
        info = {
            "thumbnails": [
                {"id": "avatar_uncropped", "url": "https://evil.example/avatar.png"}
            ]
        }
        self.assertIsNone(downloader._pick_channel_avatar_url(info))

    def test_rejects_invalid_channel_id_without_network(self) -> None:
        """Verify rejects invalid channel id without network."""
        self.assertIsNone(downloader.fetch_channel_avatar("../UC123"))
        self.assertIsNone(downloader.fetch_channel_avatar("UC123"))


class RequestValidationTests(unittest.TestCase):
    """Shared download request validation."""

    def test_video_request_returns_validated_options(self) -> None:
        """Verify a video request keeps its height and clears other options."""
        request = downloader.validate_request("youtube", "video", 1080)
        self.assertEqual(request.mode, "video")
        self.assertEqual(request.height, 1080)
        self.assertIsNone(request.subtitle_lang)
        self.assertFalse(request.auto_subtitle)

    def test_subtitle_request_keeps_language_and_auto_flag(self) -> None:
        """Verify a subtitle request keeps its language and automatic flag."""
        request = downloader.validate_request("youtube", "subtitles", None, "en", True)
        self.assertEqual(request.mode, "subtitles")
        self.assertIsNone(request.height)
        self.assertEqual(request.subtitle_lang, "en")
        self.assertTrue(request.auto_subtitle)

    def test_invalid_combinations_are_rejected(self) -> None:
        """Verify unsupported sources, modes, and values are rejected."""
        cases = [
            ("instagram", "subtitles", None, "en", False),
            ("youtube", "unknown", None, None, False),
            ("youtube", "video", 0, None, False),
            ("youtube", "subtitles", None, "../etc", False),
            ("youtube", "subtitles", None, "en", "yes"),
        ]
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(DownloadError):
                    downloader.validate_request(*values)


class DownloadEntryValidationTests(unittest.TestCase):
    """Early validation in the download entry point."""
    def test_rejects_invalid_mode_before_network(self) -> None:
        """Verify rejects invalid mode before network."""
        with self.assertRaises(DownloadError) as context:
            downloader.download(
                "https://youtu.be/dQw4w9WgXcQ",
                "unknown",
                output_dir=ROOT / ".test-invalid",
            )
        self.assertEqual(context.exception.http_status, 400)

    def test_rejects_video_without_quality(self) -> None:
        """Verify rejects video without quality."""
        with self.assertRaises(DownloadError):
            downloader.download(
                "https://youtu.be/dQw4w9WgXcQ",
                "video",
                output_dir=ROOT / ".test-invalid",
            )

    def test_rejects_unsafe_url_before_creating_directory(self) -> None:
        """Verify rejects unsafe url before creating directory."""
        output_dir = ROOT / ".test-should-not-exist"
        shutil.rmtree(output_dir, ignore_errors=True)
        with self.assertRaises(DownloadError):
            downloader.download(
                "https://evil.example/watch?v=dQw4w9WgXcQ",
                "audio",
                output_dir=output_dir,
            )
        self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
