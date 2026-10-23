"""Tests for the local downloader application."""

from __future__ import annotations

import http.client
import json
import logging
import shutil
import sys
import threading
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402


class ServerSecurityTests(unittest.TestCase):
    """HTTP routing, validation, and security behavior."""
    runtime: Path

    @classmethod
    def setUpClass(cls) -> None:
        """Create the shared test server."""
        logging.disable(logging.CRITICAL)
        cls.runtime = ROOT / ".test-runtime"
        shutil.rmtree(cls.runtime, ignore_errors=True)
        cls.server = app.create_server(
            0,
            runtime_dir=cls.runtime,
            max_file_mb=16,
            job_ttl_min=1,
            max_active_jobs=1,
        )
        cls.port = int(cls.server.server_address[1])
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            name="test-server",
            daemon=True,
        )
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.host = f"127.0.0.1:{cls.port}"
        cls.token = cls._bootstrap()["csrf_token"]

    @classmethod
    def tearDownClass(cls) -> None:
        """Stop the test server and remove temporary data."""
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.runtime, ignore_errors=True)
        logging.disable(logging.NOTSET)

    @classmethod
    def _request(
        cls,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        host: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Send an HTTP request and return status, headers, and body."""
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=8)
        request_headers = {"Host": host or cls.host}
        if headers:
            request_headers.update(headers)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, response_headers, payload
        finally:
            connection.close()

    @classmethod
    def _bootstrap(cls) -> dict:
        """Fetch bootstrap data from the test server."""
        status, _, body = cls._request("GET", "/api/bootstrap")
        assert status == 200
        return json.loads(body)

    def _json_headers(self) -> dict[str, str]:
        """Build JSON request headers with CSRF and Origin."""
        return {
            "Content-Type": "application/json",
            "Origin": self.origin,
            "X-CSRF-Token": self.token,
        }

    def _post_json(
        self,
        path: str,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], dict]:
        """Send a JSON POST request and decode the response."""
        merged = self._json_headers()
        if headers:
            merged.update(headers)
        status, response_headers, body = self._request(
            "POST",
            path,
            body=json.dumps(payload).encode("utf-8"),
            headers=merged,
        )
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}
        return status, response_headers, data

    def test_index_has_security_headers(self) -> None:
        """Verify index has security headers."""
        status, headers, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Скачать видео".encode("utf-8"), body)
        self.assertNotIn(b"LocalTube", body)
        self.assertNotIn("Только на этом ПК".encode("utf-8"), body)
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["cross-origin-opener-policy"], "same-origin")
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(headers["cache-control"], "no-cache")

    def test_host_header_must_match_loopback(self) -> None:
        """Verify host header must match loopback."""
        status, _, body = self._request(
            "GET",
            "/api/health",
            host="evil.example",
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_host")

    def test_localhost_host_header_is_allowed(self) -> None:
        """Verify localhost host header is allowed."""
        status, _, _ = self._request(
            "GET",
            "/api/health",
            host=f"localhost:{self.port}",
        )
        self.assertEqual(status, 200)

    def test_post_requires_origin(self) -> None:
        """Verify post requires origin."""
        status, _, data = self._post_json(
            "/api/info",
            {"url": "https://youtu.be/dQw4w9WgXcQ"},
            headers={"Origin": ""},
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "invalid_origin")

    def test_post_rejects_foreign_origin(self) -> None:
        """Verify post rejects foreign origin."""
        status, _, data = self._post_json(
            "/api/info",
            {"url": "https://youtu.be/dQw4w9WgXcQ"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "invalid_origin")

    def test_post_requires_csrf(self) -> None:
        """Verify post requires csrf."""
        status, _, data = self._post_json(
            "/api/info",
            {"url": "https://youtu.be/dQw4w9WgXcQ"},
            headers={"X-CSRF-Token": "wrong"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "csrf")

    def test_post_requires_json_content_type(self) -> None:
        """Verify post requires json content type."""
        status, _, data = self._request(
            "POST",
            "/api/info",
            body=b"url=x",
            headers={
                "Content-Type": "text/plain",
                "Origin": self.origin,
                "X-CSRF-Token": self.token,
            },
        )
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(data)["error"]["code"], "invalid_content_type")

    def test_post_rejects_unknown_fields(self) -> None:
        """Verify post rejects unknown fields."""
        status, _, data = self._post_json(
            "/api/download",
            {
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "mode": "audio",
                "unexpected": True,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "unknown_field")

    def test_download_validation_happens_before_job(self) -> None:
        """Verify download validation happens before job."""
        status, _, data = self._post_json(
            "/api/download",
            {"url": "https://youtu.be/dQw4w9WgXcQ", "mode": "video"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "invalid_quality")

        status, _, data = self._post_json(
            "/api/download",
            {
                "url": "https://www.instagram.com/reel/Chunk8-jurw/",
                "source": "instagram",
                "mode": "subtitles",
                "subtitle_lang": "en",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "subtitles_unavailable")

        status, _, data = self._post_json(
            "/api/info",
            {"url": "https://youtu.be/dQw4w9WgXcQ", "source": "unknown"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "invalid_source")

        status, _, data = self._post_json(
            "/api/download",
            {
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "mode": "subtitles",
                "subtitle_lang": "../etc",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "invalid_subtitle")

        status, _, data = self._post_json(
            "/api/download",
            {"url": "https://evil.example/watch?v=dQw4w9WgXcQ", "mode": "audio"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "unsupported_host")

    def test_static_serving_does_not_expose_project_files(self) -> None:
        """Verify static serving does not expose project files."""
        status, headers, body = self._request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers["content-type"])
        self.assertIn(b"ensureSession", body)

        for path in ("/app.py", "/static/%2e%2e/app.py", "/static/../downloader.py", "/.runtime/"):
            with self.subTest(path=path):
                status, _, _ = self._request("GET", path)
                self.assertEqual(status, 404)

    def test_media_routes_reject_invalid_ids(self) -> None:
        """Verify media routes reject invalid ids."""
        for path in (
            "/api/avatar/not-a-channel",
            "/api/avatar/../app.py",
            "/api/avatar/UC123",
            "/api/thumbnail/bad",
            "/api/thumbnail/../app.py",
        ):
            with self.subTest(path=path):
                status, _, _ = self._request("GET", path)
                self.assertEqual(status, 404)

    def test_api_unknown_route_and_method(self) -> None:
        """Verify api unknown route and method."""
        status, _, data = self._request("GET", "/api/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(data)["error"]["code"], "not_found")

        status, _, data = self._request("PUT", "/api/info")
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(data)["error"]["code"], "method_not_allowed")

    def test_head_has_no_body_but_headers(self) -> None:
        """Verify head has no body but headers."""
        status, headers, body = self._request("HEAD", "/static/styles.css")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertTrue(headers["content-length"].isdigit())

    def test_bootstrap_is_not_cacheable(self) -> None:
        """Verify bootstrap is not cacheable."""
        status, headers, body = self._request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        payload = json.loads(body)
        self.assertIn("csrf_token", payload)
        self.assertNotIn("folder", payload)

    def test_ready_file_is_served_with_attachment(self) -> None:
        """Verify ready file is served with attachment."""
        job_id = uuid.uuid4().hex
        output_dir = self.runtime / "jobs" / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        file_path = output_dir / "video.mp4"
        file_path.write_bytes(b"test-video")
        job = app.Job(
            job_id=job_id,
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            source="youtube",
            mode="video",
            height=144,
            subtitle_lang=None,
            auto_subtitle=False,
            output_dir=output_dir,
        )
        job.status = "ready"
        job.phase = "Готово"
        job.progress = 100.0
        job.path = file_path
        job.filename = "Тестовое видео [dQw4w9WgXcQ] 144p.mp4"
        job.content_type = "video/mp4"
        job.size = len(b"test-video")
        try:
            with self.server.jobs._lock:
                self.server.jobs._jobs[job_id] = job

            status, _, body = self._request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(status, 200)
            public = json.loads(body)
            self.assertEqual(public["file"]["url"], f"/api/jobs/{job_id}/file")
            self.assertEqual(public["file"]["name"], job.filename)

            status, _, body = self._request("HEAD", f"/api/jobs/{job_id}/file")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"")
            self.assertTrue(file_path.exists())

            status, headers, body = self._request("GET", f"/api/jobs/{job_id}/file")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"test-video")
            self.assertEqual(headers["content-type"], "video/mp4")
            self.assertIn("attachment;", headers["content-disposition"])
            self.assertIn("filename*=UTF-8''", headers["content-disposition"])
            self.assertEqual(headers["cache-control"], "private, no-store")

            # A browser download must not delete the temporary file.
            self.assertIsNotNone(self.server.jobs.get(job_id))
            self.assertTrue(file_path.exists())

            status, _, data = self._post_json(
                "/api/jobs/delete",
                {"job_id": job_id},
            )
            self.assertEqual(status, 200)
            self.assertTrue(data["deleted"])
            self.assertIsNone(self.server.jobs.get(job_id))
            self.assertFalse(file_path.exists())
            status, _, _ = self._request("GET", f"/api/jobs/{job_id}/file")
            self.assertEqual(status, 404)
        finally:
            with self.server.jobs._lock:
                self.server.jobs._jobs.pop(job_id, None)
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_missing_ready_file_is_not_found(self) -> None:
        """Verify missing ready file is not found."""
        status, _, _ = self._request(
            "GET",
            "/api/jobs/" + "0" * 32 + "/file",
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
