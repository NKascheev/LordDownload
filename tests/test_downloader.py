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
    def test_accepts_and_canonicalizes_youtube_urls(self) -> None:
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
        with self.assertRaises(DownloadError):
            downloader.normalize_youtube_url("https://youtu.be/" + "a" * 3000)


class FilenameTests(unittest.TestCase):
    def test_removes_path_separators_and_controls(self) -> None:
        value = downloader.sanitize_filename("../../evil\x00name.mp4")
        self.assertNotIn("/", value)
        self.assertNotIn("\\", value)
        self.assertNotIn("\x00", value)
        self.assertNotIn("..", value)

    def test_handles_windows_reserved_names(self) -> None:
        for name in ("CON", "con.txt", "COM1", "LPT9"):
            with self.subTest(name=name):
                self.assertTrue(downloader.sanitize_filename(name).startswith("_"))

    def test_limits_length_and_has_fallback(self) -> None:
        value = downloader.sanitize_filename("x" * 500)
        self.assertLessEqual(len(value), 160)
        self.assertEqual(downloader.sanitize_filename(None), "youtube-video")
        self.assertEqual(downloader.sanitize_filename("..."), "youtube-video")

    def test_keeps_unicode_title(self) -> None:
        value = downloader.sanitize_filename("Лекция — день 1.mp4")
        self.assertIn("Лекция", value)
        self.assertTrue(value.endswith(".mp4"))


class SubtitleConversionTests(unittest.TestCase):
    def test_vtt_becomes_plain_text(self) -> None:
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
        content = """WEBVTT

NOTE this is a comment

00:00.000 --> 00:02.000
<00:00:00.000><c>Hello</c> <00:00:01.000><c>world</c>
"""
        self.assertEqual(downloader.vtt_to_text(content), "Hello world")

    def test_empty_input(self) -> None:
        self.assertEqual(downloader.vtt_to_text(""), "")
        self.assertEqual(downloader.vtt_to_text("WEBVTT\n\n"), "")

    def test_conversion_writes_txt_and_removes_vtt(self) -> None:
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
    def test_quality_deduplication_and_sorting(self) -> None:
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
        selector = downloader._video_format_selector(1080)
        self.assertIn("height=1080", selector)
        with self.assertRaises(DownloadError):
            downloader._video_format_selector(9999)
        with self.assertRaises(DownloadError):
            downloader._video_format_selector(-1)


class ChannelAvatarTests(unittest.TestCase):
    def test_prefers_square_avatar_over_banner(self) -> None:
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
        info = {
            "thumbnails": [
                {"id": "avatar_uncropped", "url": "https://evil.example/avatar.png"}
            ]
        }
        self.assertIsNone(downloader._pick_channel_avatar_url(info))

    def test_rejects_invalid_channel_id_without_network(self) -> None:
        self.assertIsNone(downloader.fetch_channel_avatar("../UC123"))
        self.assertIsNone(downloader.fetch_channel_avatar("UC123"))


class DownloadEntryValidationTests(unittest.TestCase):
    def test_rejects_invalid_mode_before_network(self) -> None:
        with self.assertRaises(DownloadError) as context:
            downloader.download(
                "https://youtu.be/dQw4w9WgXcQ",
                "unknown",
                output_dir=ROOT / ".test-invalid",
            )
        self.assertEqual(context.exception.http_status, 400)

    def test_rejects_video_without_quality(self) -> None:
        with self.assertRaises(DownloadError):
            downloader.download(
                "https://youtu.be/dQw4w9WgXcQ",
                "video",
                output_dir=ROOT / ".test-invalid",
            )

    def test_rejects_unsafe_url_before_creating_directory(self) -> None:
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
