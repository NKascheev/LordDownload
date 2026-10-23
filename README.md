# YouTube & Instagram Downloader

A small local web application for downloading public YouTube and Instagram
media. The server listens only on `127.0.0.1`, so it is not accessible from other
computers or from the internet. Media is processed on your own PC, and the
application never asks for account credentials or browser cookies.

## What it supports

- YouTube videos, Shorts, and music links.
- Public Instagram posts, Reels, and IGTV videos.
- Video output as MP4, audio output as MP3, and YouTube subtitles as plain text.
- One video or post per link. Playlists, live streams, private content,
  age-restricted content, and content that requires a login are not supported.

## How to use

### Requirements

- Windows 10 or newer.
- Python 3.10 or newer, installed with **Add Python to PATH** enabled.
- An internet connection and a modern browser.

FFmpeg is installed together with the Python dependencies, so no separate
system installation is needed.

### Start the application

1. Double-click `start.bat`.
2. On the first launch, the script creates a local environment and installs the
   dependencies. This can take a few minutes.
3. The interface opens in your browser at `http://127.0.0.1:8765`. If it does
   not open automatically, open that address manually.
4. Keep the console window open while the application is running.

To stop the application, press `Ctrl+C` in the console or close the console
window. If port `8765` is already in use, start the app from the project folder
with:

```powershell
.venv\Scripts\python.exe app.py --port 8766 --open
```

### Download a file

1. Select **YouTube** or **Instagram**.
2. Paste a link to a single video or post.
3. Click **Получить варианты**.
4. Choose what to download:
   - a video quality;
   - audio only (MP3);
   - subtitles (YouTube only).
5. Click the download button and wait for processing to finish.
6. Click **Скачать файл**. The browser saves the file to its normal downloads
   folder.

Temporary server-side copies are removed when the page is reloaded or closed,
when a download for a different link is started, or automatically after
30 minutes.

## Permitted use

You may use this application only for material that:

- you own;
- is in the public domain;
- is licensed for downloading; or
- you have explicit permission from the rights holder to download.

Your use must also comply with the law in your country and with the terms of the
source platform. Personal use does not automatically make a download legal.

## Prohibited use

Do not use this application to:

- download copyrighted material without permission;
- bypass DRM, paywalls, login restrictions, or other access controls;
- download private, confidential, or personal content without consent;
- redistribute, sell, or publicly share downloaded content without the required
  rights;
- violate YouTube, Instagram, or any other applicable terms of service;
- harass, stalk, or infringe the privacy of any person.

The application does not grant any rights to third-party content and must not be
exposed to a network. You are solely responsible for how you use it. This notice
is not legal advice, and a download can still violate copyright law or platform
terms even when the application is technically able to perform it. If you are
unsure whether a download is allowed, do not download it.
