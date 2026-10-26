# Chess Studio Web

This is a separate browser project based on the uploaded `chessnew.py`. The original desktop Python file is not edited or required to run this web build. The original file uses Tkinter (a desktop UI toolkit); this project recreates the gold-and-cream Chess Studio menu in HTML/CSS and provides a browser chessboard.

## What's included

- Gold/cream menu styled after the provided Chess Studio screenshot
- Play vs Bot, Coach, Final Boss, and local 2-player games
- Legal chess moves, check/checkmate detection, board flip, move list, and a mate-in-one puzzle
- Chess Basics and Inventory pages
- Existing Chess Studio online server API kept from the source file, with a static web handler added so the same service serves the browser UI and `/api/*`
- Render Blueprint (`render.yaml`) and `Procfile` for deployment

The browser chessboard uses a small self-contained JavaScript rules engine included in this folder, so local play does not depend on a third-party chess library. The Fredoka font is loaded from Google Fonts when internet access is available; system fonts are used as a fallback. The offline/local game runs in the browser; online accounts and rated matchmaking require this Python server to be running and deployed.

## Run it on your Mac

1. Download and unzip `chess-studio-web.zip`.
2. Double-click `start-local.command`. On macOS, if Gatekeeper blocks the file, right-click it and choose **Open**. The script creates a private Python virtual environment and installs the one server dependency.
3. In Chrome, visit `http://localhost:8765`. Keep the Terminal window open while playing.
4. Stop the local server with `Control-C` in the Terminal window.

You can also start it manually from Terminal with `python3 -m venv .venv`, `source .venv/bin/activate`, `python -m pip install -r requirements.txt`, and `python server.py`.

## Deploy publicly with Render

A live public link cannot be created from this package alone: deployment requires a hosting account and a repository the owner can authorize. The project contains `render.yaml` for Render's Blueprint flow:

1. Create a GitHub repository and upload all files in this folder (not the ZIP file itself).
2. In Render, create a new **Blueprint** from that GitHub repository.
3. Let Render use `render.yaml`, then deploy the web service.
4. Open the URL Render gives you. Check the home page and test a local game first; then test account creation and online matchmaking.

The server writes account/game data to `chess_studio_server_data.json`. A free service may not keep local files through restarts or redeploys, so choose persistent storage and set `CHESS_STUDIO_DATA` to a path on that storage if you need account data to survive restarts. Do not publish database files or credentials in a public repository.

## Important differences from the desktop build

A Tkinter app cannot be displayed by Chrome directly. This project ports a focused version of the core menu/game experience rather than automatically converting every one of the desktop app's 31,000+ lines. The browser UI is a distinct implementation that follows the reference screenshot; advanced desktop-only screens and integrations are not fully ported yet. The original `chessnew.py` remains unchanged.
