# Chess Studio Website

A browser-based version of Chess Studio's online play experience, using the online game server from the supplied Python project.

## Included

- Responsive landing page and sign-in / account creation
- Player ratings and starting levels
- Bullet (1 minute), Blitz (5 minute), and Rapid (10 minute) matchmaking
- Legal browser chessboard, move history, clocks, and board orientation
- Built-in server opponents when matchmaking is quiet
- In-game chat, draw offers, resignation, and Elo updates
- Existing server-side account storage and game rules

This web version focuses on the online arena. The original desktop-only screens (local settings, puzzle pages, desktop graphics, and other Tkinter interfaces) have not all been recreated in the browser.

## Run locally

Requires Python 3.10+ and internet access in the browser for the hosted chess-rules library and web fonts.

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

Open <http://localhost:8765>.

## Publish on Render

1. Put the contents of this folder in a GitHub repository.
2. In Render, create a new **Web Service** and connect that repository. The included `render.yaml` can also be used as a Blueprint.
3. Use build command `pip install -r requirements.txt` and start command `python server.py` if asked manually.
4. Once the deployment finishes, open the Render URL. Create an account on the live site.
5. For persistent accounts, add a persistent disk on the hosting service and set `CHESS_STUDIO_DATA` to a JSON file path on that disk (for example `/var/data/chess_studio_server_data.json`). Without persistent storage, some hosting plans may erase account data when the service restarts.

The first registered account is marked as the server owner by the original backend. Set `CHESS_STUDIO_OWNER` explicitly only if you need to override that behavior.

## Notes

- The online backend uses `python-chess`; it does not require Tkinter or Pillow to run.
- The site serves its page and API from the same origin, so no separate API URL is needed.
- This package is ready for deployment, but it is not live on the public internet until you deploy it to a hosting provider.
