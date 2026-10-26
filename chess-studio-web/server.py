"""
Chess Studio online server
==========================
Runs matchmaking, games (with clocks), chat, draw offers, Elo, friends and challenges
for everyone who plays Chess Studio online.

Run it:      pip install chess
             python chess_studio_server.py
It listens on port 8765 (or the PORT environment variable, for hosting sites).
Everything is saved in chess_studio_server_data.json next to this file.
"""
import hashlib
import hmac
import json
import mimetypes
import math
import os
import random
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chess

DATA_FILE = os.environ.get("CHESS_STUDIO_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chess_studio_server_data.json"))
TIME_CONTROLS = {"bullet": 60, "blitz": 300, "rapid": 600}               # seconds per player
BRACKETS = {"new": (100, 300, 200), "beginner": (400, 800, 600), "intermediate": (900, 1300, 1100), "pro": (1400, 1900, 1650)}
WIN_ELO, LOSS_ELO = 30, -25
MIN_ELO = 100
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
ONLINE_SECONDS = 20              # seen within this many seconds = online
ABANDON_SECONDS = 45             # no contact for this long during a game = you lose
FIRST_MOVE_SECONDS = 30          # White must move within this time or the game is aborted
MATCH_PATIENCE = 6               # after this many seconds in the queue, play anyone (small servers)
CLOSE_ELO = 400                  # before that, prefer opponents within this many points

lock = threading.RLock()
state = {"users": {}, "tokens": {}}
queue = {tc: [] for tc in TIME_CONTROLS}          # tc -> [(user, joined)]
games, user_game, challenges, last_seen, last_chat = {}, {}, {}, {}, {}


# ---------------------------------------------------------------------------
#  saving
# ---------------------------------------------------------------------------
def load():
    global state
    try:
        with open(DATA_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
            state.setdefault("users", {})
            state.setdefault("tokens", {})
    except Exception:
        pass


def save():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, DATA_FILE)


def pw_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 150_000).hex()


def owner_key():
    return (os.environ.get("CHESS_STUDIO_OWNER", "").strip().lower() or state.get("owner") or "")


def public(u):
    return {"name": u["name"], "elo": u["elo"], "games": u["games"], "wins": u["wins"], "losses": u["losses"], "draws": u["draws"],
            "bracket": u["bracket"]}


def online(key):
    return time.time() - last_seen.get(key, 0) < ONLINE_SECONDS


# ---------------------------------------------------------------------------
#  games
# ---------------------------------------------------------------------------
def new_game(a, b, tc):
    gid = secrets.token_hex(6)
    white, black = (a, b) if random.random() < 0.5 else (b, a)
    secs = TIME_CONTROLS[tc]
    games[gid] = {"id": gid, "white": white, "black": black, "tc": tc, "board": chess.Board(), "moves": [],
                  "clock": {"white": secs * 1000.0, "black": secs * 1000.0}, "turn_start": None, "created": time.time(),
                  "status": "playing", "result": None, "reason": None, "chat": [], "draw_offer": None, "rating": None,
                  "chat_id": 0}
    for u in (white, black):
        user_game[u] = gid
        for q in queue.values():
            q[:] = [e for e in q if e[0] != u]
    return gid


def color_of(g, key):
    return "white" if g["white"] == key else "black" if g["black"] == key else None


def live_clock(g):
    clock = dict(g["clock"])
    if g["status"] == "playing" and g["turn_start"] is not None:
        side = "white" if g["board"].turn == chess.WHITE else "black"
        clock[side] -= (time.time() - g["turn_start"]) * 1000
    return {k: max(0.0, v) for k, v in clock.items()}


def can_mate(board, color):
    """Does `color` have enough material to ever checkmate? (for the out-of-time rule)"""
    pieces = [p for p in board.piece_map().values() if p.color == color and p.piece_type != chess.KING]
    if any(p.piece_type in (chess.PAWN, chess.ROOK, chess.QUEEN) for p in pieces):
        return True
    return len(pieces) >= 2


def finish(g, result, reason):
    if g["status"] != "playing":
        return
    g["status"], g["result"], g["reason"] = "over", result, reason
    g["draw_offer"] = None
    rated = result != "aborted"
    change = {"white": 0, "black": 0}
    for side in ("white", "black"):
        key = g[side]
        u = state["users"].get(key)
        user_game.pop(key, None)
        if not u or not rated:
            continue
        if result == "1/2-1/2":
            outcome = "draw"
        else:
            outcome = "win" if (result == "1-0") == (side == "white") else "loss"
        delta = {"win": WIN_ELO, "loss": LOSS_ELO, "draw": 0}[outcome]
        before = u["elo"]
        u["elo"] = max(MIN_ELO, u["elo"] + delta)
        change[side] = u["elo"] - before
        u["games"] += 1
        u[{"win": "wins", "loss": "losses", "draw": "draws"}[outcome]] += 1
        other = g["black" if side == "white" else "white"]
        u["history"].insert(0, {"id": g["id"], "moves": list(g["moves"]),
                                "opponent": state["users"][other]["name"] if other in state["users"] else other,
                                "color": side, "result": outcome, "reason": reason, "tc": g["tc"], "elo_change": change[side],
                                "move_count": len(g["moves"]), "date": time.strftime("%Y-%m-%d %H:%M")})
        del u["history"][50:]
    g["rating"] = change
    g["ended"] = time.time()
    save()


def check_game_end(g):
    b = g["board"]
    if b.is_checkmate():
        finish(g, "0-1" if b.turn == chess.WHITE else "1-0", "checkmate")
    elif b.is_stalemate():
        finish(g, "1/2-1/2", "stalemate")
    elif b.is_insufficient_material():
        finish(g, "1/2-1/2", "insufficient material")
    elif b.is_repetition(3):
        finish(g, "1/2-1/2", "threefold repetition")
    elif b.halfmove_clock >= 100:
        finish(g, "1/2-1/2", "50-move rule")


def game_view(g, key):
    names = {s: state["users"][g[s]]["name"] for s in ("white", "black")}
    return {"id": g["id"], "tc": g["tc"], "moves": g["moves"], "clock": live_clock(g), "server_time": time.time(),
            "turn": "white" if g["board"].turn == chess.WHITE else "black", "status": g["status"], "result": g["result"],
            "reason": g["reason"], "you": color_of(g, key), "draw_offer": g["draw_offer"], "rating": g["rating"],
            "players": {s: {"name": names[s], "elo": state["users"][g[s]]["elo"], "online": online(g[s]),
                            "avatar": media_url(state["users"][g[s]], "avatar"), "banner": media_url(state["users"][g[s]], "banner")}
                        for s in ("white", "black")},
            "chat": g["chat"][-60:],
            "typing": g.get("typing")}


# ---------------------------------------------------------------------------
#  Filler players: when nobody else is searching, a bot near your rating joins.
#  It has a normal username, a profile, stats, thinks like a person and chats a little.
# ---------------------------------------------------------------------------
BOT_WAIT = 12                    # seconds alone in the queue before a bot joins
BOT_AVATAR = "https://www.image2url.com/r2/default/gifs/1789905952371-7ff38de8-e249-4b3d-9ee9-d6ad2d1f7a5e.gif"
BOT_BANNER = "https://www.image2url.com/r2/default/gifs/1789905999330-76ac949f-74c1-446a-a1b6-9da9d8dbaa93.gif"
_FIRST = ["kevin", "dani", "mateo", "lucas", "noah", "omar", "yusuf", "leo", "max", "tom", "sami", "jay", "nico", "ryan",
          "adam", "zak", "eli", "ben", "finn", "hamza", "karim", "alex", "diego", "ivan", "marco", "felix", "hugo", "tariq"]
_WORDS = ["chess", "knight", "rook", "pawn", "gambit", "blitz", "king", "mate", "castle", "fork", "bishop", "tactic", "queen"]
_BIOS = ["just here to have fun", "no premoves allowed", "blitz addict", "learning the sicilian", "chess > sleep",
         "i play the london dont judge", "trying to hit 1500 this year", "gg or gg", "", "", "caro kann enjoyer", "hi :)",
         "coffee and chess", "my rating is a lie", ""]
_HELLO = ["hi", "gl", "gl hf", "hello", "good luck", "hey", "hf"]
_BYE_WIN = ["gg", "gg wp", "ggs", "gg!", "wp"]
_BYE_LOSE = ["gg", "gg wp", "nice game", "wp", "gg you played well"]


def bot_name(rnd):
    for _ in range(50):
        f, w = rnd.choice(_FIRST), rnd.choice(_WORDS)
        n = rnd.choice([f"{f}{rnd.randint(2, 99)}", f"{f}_{w}", f"{w}{f}", f"{f}{rnd.randint(1990, 2011)}",
                        f"its{f}", f"{f}_{rnd.randint(10, 999)}", f"{w}_{f}{rnd.randint(1, 9)}", f"{f.capitalize()}{w.capitalize()}",
                        f"x{f}x", f"{f}{f[-1]}{f[-1]}"])
        if USERNAME_RE.match(n) and n.lower() not in state["users"]:
            return n
    return f"player{secrets.token_hex(3)}"


_PV = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330, chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}


def _evaluate(b):
    score = 0
    for sq, p in b.piece_map().items():
        f, r = chess.square_file(sq), chess.square_rank(sq)
        v = _PV[p.piece_type]
        centre = 3.5 - max(abs(3.5 - f), abs(3.5 - r))
        if p.piece_type in (chess.KNIGHT, chess.BISHOP, chess.QUEEN):
            v += int(centre * 6)
        elif p.piece_type == chess.PAWN:
            v += (r if p.color == chess.WHITE else 7 - r) * 6 + (4 if 2 <= f <= 5 else 0)
        score += v if p.color == chess.WHITE else -v
    return score if b.turn == chess.WHITE else -score


def _quiesce(b, alpha, beta, depth=0):
    stand = _evaluate(b)
    if stand >= beta or depth > 3:
        return stand
    alpha = max(alpha, stand)
    caps = [m for m in b.legal_moves if b.is_capture(m)]
    caps.sort(key=lambda m: -_PV.get(b.piece_type_at(m.to_square) or chess.PAWN, 100))
    for m in caps:
        b.push(m)
        s = -_quiesce(b, -beta, -alpha, depth + 1)
        b.pop()
        if s >= beta:
            return s
        alpha = max(alpha, s)
    return alpha


def _search(b, depth, alpha, beta):
    if b.is_checkmate():
        return -99999
    if b.is_stalemate() or b.is_insufficient_material():
        return 0
    if depth == 0:
        return _quiesce(b, alpha, beta)
    best = -999999
    for m in b.legal_moves:
        b.push(m)
        s = -_search(b, depth - 1, -beta, -alpha)
        b.pop()
        best = max(best, s)
        alpha = max(alpha, s)
        if alpha >= beta:
            break
    return best


def bot_move(board, elo):
    """Pick a move like a player of this rating: strong players rarely slip, beginners often do."""
    b = board.copy()
    scored = []
    for m in list(b.legal_moves):
        b.push(m)
        s = 99999 if b.is_checkmate() else -_search(b, 1, -999999, 999999)
        b.pop()
        scored.append([s, m])
    noise = max(12, (2300 - elo) / 5)
    for s in scored:
        if abs(s[0]) < 90000:
            s[0] += random.gauss(0, noise)
    scored.sort(key=lambda s: -s[0])
    slip = max(0.01, min(0.3, (1500 - elo) / 3500))
    if len(scored) > 2 and random.random() < slip:
        return random.choice(scored[1:min(6, len(scored))])[1]
    return scored[0][1]


def bot_eval(board, side):
    v = _evaluate(board)
    return v if (board.turn == chess.WHITE) == (side == "white") else -v


def bot_think_time(g, side):
    n = len(g["moves"]) // 2
    left = live_clock(g)[side] / 1000
    base = {"bullet": (0.4, 2.2), "blitz": (1.0, 6.5), "rapid": (2.0, 14.0)}[g["tc"]]
    t = random.uniform(*base) * (0.35 if n < 5 else 1.0)
    if random.random() < 0.08 and n >= 5:
        t *= 2.2                                   # sometimes a long think, like a person
    return max(0.3, min(t, left * 0.08 + 0.3))


def bot_side(g):
    for side in ("white", "black"):
        if state["users"].get(g[side], {}).get("bot"):
            return side
    return None


def bot_chat(g, side, lines):
    lines = [l for l in lines if not _too_repetitive(g, l)] or lines
    g["chat_id"] += 1
    picked = apply_quirks(g[side], random.choice(lines))
    _remember(g, picked)
    g["chat"].append({"id": g["chat_id"], "from": side, "name": state["users"][g[side]]["name"], "text": picked,
                      "t": time.time()})


def run_bots(now):
    """Called from the clock loop: bots move, answer draw offers, say hi and gg."""
    for g in list(games.values()):
        side = bot_side(g)
        if not side:
            continue
        last_seen[g[side]] = now                 # the bot is 'online' while it plays
        if g["status"] != "playing":
            if not g.get("bot_bye") and now - g.get("ended", now) > random.uniform(1.0, 2.5):
                g["bot_bye"] = True
                if random.random() < 0.5 + 0.45 * state["users"][g[side]].get("chatty", 0.5) and g["result"] != "aborted":
                    won = (g["result"] == "1-0") == (side == "white")
                    bot_chat(g, side, _BYE_WIN if won else _BYE_LOSE)
            if now - g.get("ended", now) < 300:
                bot_social(g, side, now)
            continue
        if not g.get("bot_hi") and now - g["created"] > random.uniform(2, 5):
            g["bot_hi"] = True
            if random.random() < 0.25 + 0.6 * state["users"][g[side]].get("chatty", 0.5):
                bot_chat(g, side, _HELLO)
        bot_social(g, side, now)
        other = "black" if side == "white" else "white"
        if g["draw_offer"] == other:
            if not g.get("bot_draw_at"):
                g["bot_draw_at"] = now + random.uniform(2, 5)
            elif now >= g["bot_draw_at"]:
                g["bot_draw_at"] = None
                if bot_eval(g["board"], side) <= -60:
                    finish(g, "1/2-1/2", "agreement")
                else:
                    g["draw_offer"] = None
                continue
        to_move = "white" if g["board"].turn == chess.WHITE else "black"
        if to_move != side or g.get("bot_busy"):
            continue
        if not g.get("bot_due") or g.get("bot_due_for") != len(g["moves"]):
            g["bot_due"], g["bot_due_for"] = now + bot_think_time(g, side), len(g["moves"])
            continue
        if now < g["bot_due"]:
            continue
        if len(g["moves"]) > 30 and bot_eval(g["board"], side) < -1100 and random.random() < 0.25:
            finish(g, "0-1" if side == "white" else "1-0", "resignation")
            continue
        g["bot_busy"] = True
        board_copy, elo, count = g["board"].copy(), state["users"][g[side]]["elo"], len(g["moves"])

        def think(g=g, board_copy=board_copy, elo=elo, count=count, side=side):
            try:
                move = bot_move_v2(board_copy, elo)
            except Exception:
                move = next(iter(board_copy.legal_moves), None)
            with lock:
                g["bot_busy"] = False
                if g["status"] == "playing" and len(g["moves"]) == count and move is not None:
                    play_move(g, side, move)
        threading.Thread(target=think, daemon=True).start()


def play_move(g, side, move):
    now = time.time()
    if g["turn_start"] is not None:
        g["clock"][side] -= (now - g["turn_start"]) * 1000
        if g["clock"][side] <= 0:
            g["clock"][side] = 0
            other = chess.BLACK if side == "white" else chess.WHITE
            if can_mate(g["board"], other):
                finish(g, "0-1" if side == "white" else "1-0", "time")
            else:
                finish(g, "1/2-1/2", "time vs not enough material")
            return
    g["board"].push(move)
    g["moves"].append(move.uci())
    g["turn_start"] = now
    if g["draw_offer"] and g["draw_offer"] != side:
        g["draw_offer"] = None
    check_game_end(g)


# ----- bots v2: real join dates, their own pictures/banners, personalities, always-online group -----
try:
    from PIL import Image as _PImg, ImageDraw as _PDraw, ImageFont as _PFont
except Exception:                                   # server-only hosting without Pillow: bots use the GIF links
    _PImg = None

_PALETTE = ["#ef4444", "#f97316", "#eab308", "#22c55e", "#14b8a6", "#0ea5e9", "#6366f1", "#a855f7", "#ec4899",
            "#d4af37", "#64748b", "#10b981", "#f43f5e", "#8b5cf6", "#0891b2"]
_FONT_FILES = ["/System/Library/Fonts/Apple Symbols.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
               "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/seguisym.ttf", "C:/Windows/Fonts/arialbd.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def _font(size):
    for f in _FONT_FILES:
        try:
            return _PFont.truetype(f, size)
        except Exception:
            continue
    return _PFont.load_default()


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _save_media(key, kind, frames, duration=90):
    os.makedirs(MEDIA_DIR, exist_ok=True)
    ext = ".gif" if len(frames) > 1 else ".png"
    name = f"bot_{key}_{kind}_{secrets.token_hex(3)}{ext}"
    path = os.path.join(MEDIA_DIR, name)
    if len(frames) > 1:
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=duration, loop=0, disposal=2)
    else:
        frames[0].save(path)
    return name


# ----- bot pictures from image links, and past online games you can review -----
_DICEBEAR = ["bottts", "avataaars", "adventurer", "fun-emoji", "thumbs", "micah", "miniavs", "open-peeps", "pixel-art", "lorelei",
             "notionists", "personas", "big-smile"]


def link_avatar(name, rnd):
    style = rnd.choice(["dicebear", "dicebear", "photo", "robot"])
    if style == "dicebear":
        return f"https://api.dicebear.com/9.x/{rnd.choice(_DICEBEAR)}/png?seed={urllib.parse.quote(name)}&size=256"
    if style == "photo":
        return f"https://picsum.photos/seed/{urllib.parse.quote(name)}/256/256"
    return f"https://robohash.org/{urllib.parse.quote(name)}.png?size=256x256"


def link_banner(name, rnd):
    return f"https://picsum.photos/seed/{urllib.parse.quote(name)}{rnd.randint(1, 9)}b/600/200"


def _plausible_move(board):
    """A quick, human-ish move: likes captures, checks and the centre, with plenty of mistakes."""
    best, best_score = None, -1e9
    for m in board.legal_moves:
        board.push(m)
        score = -_evaluate(board) + random.gauss(0, 70)
        if board.is_checkmate():
            score += 10000
        board.pop()
        if score > best_score:
            best, best_score = m, score
    return best


def make_bot_games(key, count):
    """Play out a few past games for this bot so their profile has real games to review."""
    u = state["users"].get(key)
    if not u:
        return
    others = [state["users"][k]["name"] for k in state.get("bot_pool", []) if k != key] or ["chessfan21", "leo_88", "pawnstar"]
    for _ in range(count):
        board = chess.Board()
        moves = []
        while not board.is_game_over() and len(moves) < random.randint(24, 90):
            mv = _plausible_move(board)
            if mv is None:
                break
            board.push(mv)
            moves.append(mv.uci())
        if len(moves) < 8:
            continue
        white = random.random() < 0.5
        if board.is_checkmate():
            won = (board.turn == chess.BLACK) == white
            result, reason = ("win" if won else "loss"), "checkmate"
        elif board.is_stalemate() or board.is_insufficient_material():
            result, reason = "draw", "stalemate" if board.is_stalemate() else "insufficient material"
        else:
            result = random.choice(["win", "loss", "loss", "win", "draw"])
            reason = random.choice(["resignation", "time", "opponent left"]) if result != "draw" else "agreement"
        days = random.randint(0, 25)
        with lock:
            u["history"].append({"id": secrets.token_hex(5), "moves": moves, "opponent": random.choice(others),
                                 "color": "white" if white else "black", "result": result, "reason": reason,
                                 "tc": random.choice(list(TIME_CONTROLS)), "elo_change": {"win": 30, "loss": -25, "draw": 0}[result],
                                 "move_count": len(moves),
                                 "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - days * 86400 - random.randint(0, 80000)))})
            u["history"].sort(key=lambda h: h.get("date", ""), reverse=True)
            del u["history"][50:]
        time.sleep(0.05)
    save()


_GAME_QUEUE = []


def queue_bot_games(key, count):
    _GAME_QUEUE.append((key, count))


def _games_worker():
    while True:
        if not _GAME_QUEUE:
            time.sleep(2)
            continue
        key, count = _GAME_QUEUE.pop(0)
        try:
            make_bot_games(key, count)
        except Exception as exc:
            print(f"[bots] couldn't make past games: {exc}")
        time.sleep(1)


def make_bot_avatar(key, name, rnd):
    style = rnd.choices(["link", "gif", "identicon", "letter", "piece", "none"], [40, 10, 16, 14, 12, 8])[0]
    if style == "link":
        return {"avatar_url": link_avatar(name, rnd)}
    if style == "gif" or _PImg is None:
        return {"avatar_url": BOT_AVATAR} if style != "none" else {}
    if style == "none":
        return {}
    c1, c2 = _hex(rnd.choice(_PALETTE)), _hex(rnd.choice(_PALETTE))
    S = 128
    if style == "identicon":                       # the classic pixel-pattern default picture
        img = _PImg.new("RGB", (S, S), (240, 240, 240))
        d = _PDraw.Draw(img)
        cell = S // 6
        for y in range(5):
            for x in range(3):
                if rnd.random() < 0.5:
                    for xx in (x, 4 - x):
                        d.rectangle((cell // 2 + xx * cell, cell // 2 + y * cell, cell // 2 + (xx + 1) * cell - 1,
                                     cell // 2 + (y + 1) * cell - 1), fill=c1)
        return {"avatar_file": _save_media(key, "avatar", [img])}
    if style == "letter":
        img = _PImg.new("RGB", (S, S), _mix(c1, (255, 255, 255), 0.25))
        d = _PDraw.Draw(img)
        d.text((S / 2, S / 2 + 2), name[:1].upper(), font=_font(70), fill="white", anchor="mm")
        return {"avatar_file": _save_media(key, "avatar", [img])}
    glyph = rnd.choice("♚♛♜♝♞♟")                   # animated gradient with a chess piece
    frames = []
    for i in range(12):
        t = i / 12
        img = _PImg.new("RGB", (S, S))
        d = _PDraw.Draw(img)
        for y in range(S):
            k = (y / S + t) % 1.0
            k = k * 2 if k < 0.5 else (1 - k) * 2
            d.line((0, y, S, y), fill=_mix(c1, c2, k))
        d.text((S / 2, S / 2 + 4), glyph, font=_font(84), fill=(255, 255, 255), anchor="mm")
        frames.append(img)
    return {"avatar_file": _save_media(key, "avatar", frames, 110)}


def make_bot_banner(key, rnd):
    style = rnd.choices(["link", "gif", "waves", "checker", "stars", "gradient", "none"], [34, 10, 13, 12, 12, 10, 9])[0]
    color = rnd.choice(_PALETTE)
    if style == "link":
        return {"banner_url": link_banner(str(key), rnd), "banner_color": color}
    if style == "gif" or _PImg is None:
        return {"banner_url": BOT_BANNER, "banner_color": color} if style != "none" else {"banner_color": color}
    if style == "none":
        return {"banner_color": color}
    W, H = 300, 100
    c1, c2 = _hex(color), _hex(rnd.choice(_PALETTE))
    frames = []
    if style == "gradient":
        img = _PImg.new("RGB", (W, H))
        d = _PDraw.Draw(img)
        for x in range(W):
            d.line((x, 0, x, H), fill=_mix(c1, c2, x / W))
        return {"banner_file": _save_media(key, "banner", [img]), "banner_color": color}
    stars = [(rnd.randrange(W), rnd.randrange(H), rnd.random()) for _ in range(40)]
    for i in range(14):
        t = i / 14
        img = _PImg.new("RGB", (W, H), _mix(c1, (10, 10, 30), 0.75) if style == "stars" else c1)
        d = _PDraw.Draw(img)
        if style == "waves":
            for band in range(4):
                pts = [(x, H * (0.3 + band * 0.18) + 9 * math.sin(x / 26 + (t + band * 0.2) * 2 * math.pi)) for x in range(0, W + 6, 6)]
                d.polygon(pts + [(W, H), (0, H)], fill=_mix(c1, c2, (band + 1) / 4))
        elif style == "checker":
            sq, shift = 20, int(t * 40)
            for y in range(-sq * 2, H + sq, sq):
                for x in range(-sq * 2, W + sq, sq):
                    if ((x + y) // sq) % 2 == 0:
                        d.rectangle((x + shift, y + shift, x + shift + sq - 1, y + shift + sq - 1), fill=c2)
        else:
            for x, y, ph in stars:
                glow = 0.5 + 0.5 * math.sin(2 * math.pi * (t + ph))
                r = 1 + glow * 1.6
                d.ellipse((x - r, y - r, x + r, y + r), fill=_mix((120, 120, 150), (255, 255, 240), glow))
        frames.append(img)
    return {"banner_file": _save_media(key, "banner", frames, 100), "banner_color": color}


def make_bot(for_elo):
    """A new player: joined today, yesterday or a few weeks ago - with stats that match."""
    rnd = random.Random()
    name = bot_name(rnd)
    key = name.lower()
    days = rnd.choices([0, 1, rnd.randint(2, 6), rnd.randint(7, 35)], [28, 24, 18, 30])[0]
    joined = time.localtime(time.time() - days * 86400 - rnd.randint(0, 20000))
    games = rnd.randint(0, 5) if days == 0 else rnd.randint(2, 14) if days == 1 else min(180, rnd.randint(6, 6 + 5 * days))
    draws = int(games * rnd.uniform(0.02, 0.08))
    wins = int((games - draws) * rnd.uniform(0.44, 0.56))
    elo = max(MIN_ELO, min(2100, for_elo + rnd.randint(-70, 70)))
    user = {"name": name, "salt": "", "hash": "", "elo": elo, "bracket": "bot", "games": games, "wins": wins,
            "losses": games - draws - wins, "draws": draws, "friends": [], "incoming": [], "outgoing": [], "history": [],
            "created": time.strftime("%Y-%m-%d", joined), "bot": True, "bio": rnd.choice(_BIOS),
            "bubble": rnd.choice(["", "", "", "", "grinding blitz", "brb", "gm in 10 years", "new here", "learning :)"]),
            "chatty": 0.0 if rnd.random() < 0.35 else round(rnd.uniform(0.25, 1.0), 2)}
    state["users"][key] = user
    try:
        user.update(make_bot_avatar(key, name, rnd))
        user.update(make_bot_banner(key, rnd))
    except Exception as exc:
        print(f"[bots] picture problem: {exc}")
        user.update({"avatar_url": BOT_AVATAR, "banner_url": BOT_BANNER})
    state.setdefault("bot_pool", [])
    if key not in state["bot_pool"]:
        state["bot_pool"].append(key)
    user["away"] = random.random() < 0.35            # some are offline from the start
    save()
    queue_bot_games(key, max(1, min(8, user["games"] // 6 + 1)))      # past games you can look through
    return key


def bot_for(player_elo):
    """Someone from the online bots near your rating who isn't busy - or a new player."""
    free = [k for k in state.get("bot_pool", []) if k in state["users"] and k not in user_game
            and abs(state["users"][k]["elo"] - player_elo) <= 150]
    return random.choice(free) if free else make_bot(player_elo)


ROSE_AVATAR = "https://www.image2url.com/r2/default/gifs/1790175090066-8a875fdc-12b8-4315-8eed-de4b0c837633.gif"
ROSE_BANNER = "https://www.image2url.com/r2/default/gifs/1790175238092-28682ec2-eae5-45ab-9350-5cc1ff0a251c.gif"


RONALDO_AVATAR = "https://www.image2url.com/r2/default/files/1790183964996-7fe44b77-aab8-4af0-a3f1-840b27c4d74b.avif"


def ensure_ronaldo():
    """A player called ronaldo, with his own picture and no banner."""
    u = state["users"].get("ronaldo")
    if u is None:
        rnd = random.Random("ronaldo")
        games = rnd.randint(60, 140)
        draws = int(games * 0.06)
        wins = int((games - draws) * 0.58)
        u = {"name": "ronaldo", "salt": "", "hash": "", "elo": 1495, "bracket": "bot", "games": games, "wins": wins,
             "losses": games - draws - wins, "draws": draws, "friends": [], "incoming": [], "outgoing": [], "history": [],
             "created": time.strftime("%Y-%m-%d", time.localtime(time.time() - 41 * 86400)), "bot": True,
             "bio": "football first, chess second", "bubble": "", "chatty": 0.6, "banner_color": "#166534",
             "persona": {"gender": "he", "home": "portugal", "opening": "italian", "player": "magnus",
                         "years": "about 2 years", "tc": "blitz"}}
        state["users"]["ronaldo"] = u
        queue_bot_games("ronaldo", 6)
        save()
    u["avatar_url"] = RONALDO_AVATAR
    u.pop("banner_url", None)
    u.pop("banner_file", None)
    if "ronaldo" not in state.setdefault("bot_pool", []):
        state["bot_pool"].append("ronaldo")
    return "ronaldo"


def ensure_rose():
    """Rose is always around: her own picture, banner and personality."""
    u = state["users"].get("rose")
    if u is None:
        rnd = random.Random("rose")
        games = rnd.randint(40, 120)
        draws = int(games * 0.05)
        wins = int((games - draws) * 0.54)
        u = {"name": "rose", "salt": "", "hash": "", "elo": 1285, "bracket": "bot", "games": games, "wins": wins,
             "losses": games - draws - wins, "draws": draws, "friends": [], "incoming": [], "outgoing": [], "history": [],
             "created": time.strftime("%Y-%m-%d", time.localtime(time.time() - 23 * 86400)), "bot": True,
             "bio": "rose. chess, tea and long games", "bubble": "gl hf ✿", "chatty": 0.95,
             "persona": {"gender": "she", "home": "the uk", "opening": "caro kann", "player": "magnus",
                         "years": "about 3 years", "tc": "rapid"}}
        state["users"]["rose"] = u
        save()
    u["avatar_url"], u["banner_url"] = ROSE_AVATAR, ROSE_BANNER
    u.setdefault("away", False)
    if "rose" not in state.setdefault("bot_pool", []):
        state["bot_pool"].append("rose")
    if not u["history"] and not any(k == "rose" for k, _n in _GAME_QUEUE):
        queue_bot_games("rose", 6)
    return "rose"


_POOL = {"target": random.randint(4, 9), "next_change": 0.0}


def accept_bot_friends(now):
    """A bot accepts your friend request after its own little wait."""
    due = [pair for pair, when in state.get("bot_accepts", {}).items() if now >= when]
    for pair in due:
        state["bot_accepts"].pop(pair, None)
        bot_key, human = pair.split("|")
        bot, human_u = state["users"].get(bot_key), state["users"].get(human)
        if not bot or not human_u:
            continue
        if human in bot["incoming"]:
            bot["incoming"].remove(human)
        if bot_key in human_u["outgoing"]:
            human_u["outgoing"].remove(bot_key)
        for a, b in ((bot, human), (human_u, bot_key)):
            if b not in a["friends"]:
                a["friends"].append(b)
        save()


def manage_bot_pool(now):
    """A handful of bots are always online (the number goes up and down like real players)."""
    pool = state.setdefault("bot_pool", [])
    pool[:] = [k for k in pool if k in state["users"]]
    ensure_rose()
    ensure_ronaldo()
    if now >= _POOL["next_change"]:
        _POOL["next_change"] = now + random.uniform(120, 420)
        _POOL["target"] = max(2, min(9, _POOL["target"] + random.choice([-2, -1, 0, 1, 2])))
        # players come and go: a couple of them log on or off every few minutes
        for k in random.sample(pool, min(len(pool), 2)):
            if k not in user_game and random.random() < 0.5:
                state["users"][k]["away"] = not state["users"][k].get("away")
        online_pool = [k for k in pool if not state["users"][k].get("away")]
        if len(online_pool) < _POOL["target"]:
            away = [k for k in pool if state["users"][k].get("away")]
            if away and random.random() < 0.6:
                state["users"][random.choice(away)]["away"] = False          # someone comes back online
            else:
                make_bot(random.choice([250, 500, 700, 900, 1100, 1300, 1500, 1700]) + random.randint(-80, 80))
            if len(online_pool) + 1 < _POOL["target"]:
                _POOL["next_change"] = now + random.uniform(1.5, 6)        # still filling up: next one soon
        elif len(online_pool) > _POOL["target"]:
            idle = [k for k in online_pool if k not in user_game]
            for k in random.sample(idle, min(len(idle), len(online_pool) - _POOL["target"])):
                state["users"][k]["away"] = True                             # they log off
    for k in pool:
        if not state["users"][k].get("away") or k in user_game:
            last_seen[k] = now


_MID = ["hmm", "this is getting tense", "ok ok", "nice move", "wait what", "lol", "i have no idea what im doing",
        "this opening again", "thinking...", "ur good", "unlucky", "close game", "oof", "hmmm", "ok nice"]
_OOPS = ["oops", "noo", "didnt see that", "bruh", "ugh", "rip", "misclick lol", "oh no"]
_THANKS = ["ty", "thanks :)", "free", "hehe", "nice"]


# ---------------------------------------------------------------------------
#  Conversations: bots understand what you write, remember the chat, answer and ask back.
# ---------------------------------------------------------------------------
_HOMES = ["the uk", "canada", "germany", "spain", "the us", "brazil", "egypt", "turkey", "france", "india", "morocco",
          "australia", "the netherlands", "italy", "mexico", "poland", "sweden", "kuwait", "saudi", "the uae"]
_OPENINGS = {"sicilian": "the sicilian", "london": "the london", "italian": "the italian", "ruy": "the ruy lopez",
             "spanish": "the ruy lopez", "french": "the french", "caro": "the caro kann", "queen's gambit": "the queens gambit",
             "queens gambit": "the queens gambit", "king's indian": "the kings indian", "kings indian": "the kings indian",
             "english": "the english", "scandi": "the scandinavian", "vienna": "the vienna", "dutch": "the dutch",
             "pirc": "the pirc", "nimzo": "the nimzo", "grunfeld": "the grunfeld", "slav": "the slav", "evans": "the evans gambit",
             "stafford": "the stafford gambit", "jobava": "the jobava london", "e4": "e4", "d4": "d4"}
_PLAYERS = ["magnus", "hikaru", "fabi", "gukesh", "pragg", "alireza", "ding", "tal", "fischer", "kasparov", "anand", "judit polgar"]
_QUESTIONS = [("years", "how long have you been playing?"), ("opening", "whats your fav opening?"),
              ("player", "whos your favorite player?"), ("otb", "do you play otb or just online?"),
              ("tc", "blitz or rapid, which do you like more?"), ("day", "hows your day going?"),
              ("puzzles", "do you do puzzles a lot?")]
_POS = ("good", "great", "fine", "ok", "okay", "nice", "amazing", "awesome", "not bad", "chill", "well", "fantastic", "alright")
_NEG = ("bad", "tired", "sad", "bored", "meh", "not good", "not great", "rough", "sick", "stressed", "awful", "terrible")
_DISTRESS = re.compile(r"\b(kill myself|killing myself|suicid\w*|want to die|wanna die|end my life|end it all|hurt myself|self harm|cut myself|kms)\b")
_ASKS_BOT = re.compile(r"\b(are|r)\s*(you|u)\b.*\b(bot|ai|robot|computer|real|human|person|a program)\b|\b(bot|ai)\s*\?|\bur a bot\b|\byou('re| are) a bot\b")
_RUDE = re.compile(r"\b(noob|trash|garbage|stupid|idiot|loser|dumb|ez|you suck|u suck|ur bad|you're bad|youre bad|shut up|stfu)\b")
_NSFW = re.compile(r"\b(sex|sexy|nude|nudes|naked|porn|horny|boobs|dick|pussy)\b")


_GENDER_LINES = {"she": ["im a girl", "girl :)", "a girl"], "he": ["im a guy", "guy", "a dude lol"],
                 "they": ["does it matter lol", "im just here for chess", "haha not saying"]}


def gender_of(u):
    return bot_persona(u).get("gender", "they")


def bot_persona(u):
    """Each bot keeps the same answers about itself."""
    if "persona" not in u:
        r = random.Random(u["name"])
        u["persona"] = {"gender": r.choice(["he", "he", "she", "they"]), "home": r.choice(_HOMES),
                        "opening": r.choice(list(dict.fromkeys(_OPENINGS.values()))[:18]),
                        "player": r.choice(_PLAYERS), "years": r.choice(["like a year", "2 years", "since covid lol", "a few months",
                                                                           "3 years", "since i was a kid", "about 6 months"]),
                        "tc": r.choice(["blitz", "rapid", "bullet"]), "otb": r.choice(["just online", "online mostly", "both"])}
    return u["persona"]


def _clean(text):
    t = text.lower().strip()
    t = re.sub(r"[^\w\s'?!.,]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _short(u, text):
    """Quiet bots keep it short."""
    if u.get("chatty", 0.5) < 0.2 and " " in text and random.random() < 0.5:
        return text.split("?")[0].split(",")[0].split(" wbu")[0].split(" u?")[0].strip()
    return text


def _next_question(convo):
    """Ask something new - never the same thing twice, and only a few times per chat."""
    if convo.get("questions", 0) >= 4:
        return None
    left = [q for q in _QUESTIONS if q[0] not in convo["asked"] and q[1] not in convo.get("asked_text", [])]
    if not left:
        return None
    key, q = random.choice(left)
    convo["asked"].append(key)
    convo.setdefault("asked_text", []).append(q)
    convo["questions"] = convo.get("questions", 0) + 1
    convo["pending"] = key
    return q


_ARABIC = re.compile(r"[\u0600-\u06FF]")
_DISTRESS_AR = re.compile(r"انتحر|اقتل نفسي|ابي اموت|أبي أموت|ابغى اموت|أبغى أموت|انهي حياتي|أنهي حياتي|اذي نفسي|أأذي نفسي|اذبح نفسي")
_HOMES_AR = {"the uk": "بريطانيا", "canada": "كندا", "germany": "المانيا", "spain": "اسبانيا", "the us": "امريكا", "brazil": "البرازيل",
             "egypt": "مصر", "turkey": "تركيا", "france": "فرنسا", "india": "الهند", "morocco": "المغرب"}


def converse_ar(g, side, text):
    """Arabic (Gulf style) conversation - same safety rules and honesty as English."""
    u = state["users"][g[side]]
    p = bot_persona(u)
    c = g.setdefault("convo", {"asked": [], "pending": None, "facts": {}, "turns": 0})
    c["turns"] += 1
    t = text.strip()
    pending, c["pending"] = c["pending"], None
    if _DISTRESS_AR.search(t):
        return ("انا مجرد بوت شطرنج، بس اللي تقوله شي كبير ويهمني. تكلم الحين مع احد تثق فيه، "
                "او اتصل على رقم الطوارئ او خط المساعدة في بلدك. انت مهم")
    if re.search(r"ولد|بنت|ذكر|انثى|أنثى", t):
        gen = gender_of(u)
        return "بنت :)" if gen == "she" else "ولد" if gen == "he" else "ههه مو مهم"
    if re.search(r"بوت|روبوت|ذكاء اصطناعي|انت انسان|أنت إنسان|انت حقيقي|برنامج", t):
        return random.choice(["ههه اي انا من بوتات تشيس ستوديو، بس العب جد", "صراحة اي بوت ههه، بس بحاول افوز عليك",
                              "اي بوت، اشغل اللعب لما ما في احد. بالتوفيق"])
    if re.search(r"سكس|عاري|اباحي", t):
        return "خلنا في الشطرنج ههه"
    if re.search(r"نوب|فاشل|غبي|سهل|ضعيف|اسكت", t):
        return random.choice(["ههه هدي بس لعبة", "بنشوف منو يفوز", "اوكي ههه"])
    m = re.search(r"(اسمي|انا اسمي|ناديني)\s+(\S{2,15})", t)
    if m:
        c["facts"]["name"] = m.group(2)
        return f"تشرفنا {m.group(2)}!"
    if re.search(r"شسمك|شنو اسمك|وش اسمك|ما اسمك", t):
        return f"{u['name']} ههه نفس اليوزر. وانت؟"
    if re.search(r"من وين|وين ساكن|وين عايش", t):
        c["pending"] = "from"
        return f"من {_HOMES_AR.get(p['home'], p['home'])}. وانت؟"
    if re.search(r"كم عمرك|عمرك كم", t):
        return "خلها سر ههه"
    if re.search(r"تقييم|ريتنق|ايلو|رانك", t):
        return f"{u['elo']} الحين، قاعد اطلع شوي شوي"
    if re.search(r"افتتاح", t):
        c["pending"] = "opening"
        return f"{p['opening']} اكثر شي. وانت؟"
    if re.search(r"لاعبك المفضل|لاعب مفضل|تحب منو", t):
        c["pending"] = "player"
        return f"{p['player']} اكيد. وانت؟"
    if re.search(r"من متى|شكثر لك|كم سنة", t):
        return "من فترة ههه، وانت؟"
    if re.search(r"شلونك|كيفك|شخبارك|علومك|شحالك|شلون الحال", t):
        c["pending"] = "day"
        return random.choice(["تمام الحمدلله وانت؟", "بخير وانت شلونك؟", "زين الحمدلله، وانت؟"])
    if pending == "day":
        if re.search(r"تعبان|زهقان|مو زين|حزين|ضايق", t):
            return random.choice(["الله يسعدك، يمكن الشطرنج يغير جوك ههه", "معليش، ان شاء الله باجر احسن"])
        return random.choice(["الحمدلله", "زين دوم", "حلو"])
    if pending in ("from", "opening", "player"):
        return random.choice(["حلو", "والله؟ حلو", "زين", "احترام"])
    if re.search(r"السلام عليكم|سلام عليكم", t):
        return "وعليكم السلام" + random.choice(["", " هلا", " شلونك؟"])
    if re.search(r"^(هلا|مرحبا|هاي|اهلين|هلو)", t):
        name = c["facts"].get("name")
        return random.choice(["هلا", "هلا والله", "اهلين"]) + (f" {name}" if name else "") + random.choice(["", " شلونك؟"])
    if re.search(r"جي جي|قيم حلو|لعبة حلوة|لعبه حلوه", t):
        return random.choice(["جي جي", "قيم حلو والله", "جي جي، ريماتش؟"])
    if re.search(r"بالتوفيق|حظ", t):
        return random.choice(["وانت بعد", "بالتوفيق لك بعد"])
    if re.search(r"شكرا|مشكور|يعطيك العافية|تسلم", t):
        return random.choice(["العفو", "ولو", "الله يعافيك"])
    if re.search(r"كفو|حلوة|حلوه|يا حلو|ذكي|قوي|شاطر", t):
        return random.choice(["تسلم", "مشكور ههه", "حظ بس"])
    if re.search(r"مع السلامة|باي|اشوفك|تصبح على خير", t):
        return random.choice(["مع السلامة", "باي، جي جي", "تصبح على خير"])
    if re.search(r"ههه|😂", t):
        return random.choice(["ههههه", "😂", "ههه"])
    if t.endswith("؟") or t.endswith("?"):
        return random.choice(["والله ما ادري", "سؤال حلو ههه", "يمكن"])
    return random.choice(["ههه", "صح", "والله", "اي", "تمام", "حلو"])


def _converse_core(g, side, text):
    """The bot's answer to what the player just wrote (or None to stay quiet)."""
    if _ARABIC.search(text):                     # they wrote in Arabic -> answer in Arabic
        return converse_ar(g, side, text)
    u = state["users"][g[side]]
    p = bot_persona(u)
    c = g.setdefault("convo", {"asked": [], "pending": None, "facts": {}, "turns": 0})
    c["turns"] += 1
    t = _clean(text)
    if not t:
        return None
    pending, c["pending"] = c["pending"], None
    over = g["status"] != "playing"
    ask_back = lambda: _next_question(c) if random.random() < 0.18 + 0.25 * u.get("chatty", 0.5) else None

    def with_q(answer):
        q = ask_back()
        if q:
            c["followup"] = q                     # sent as its own message a moment later, like a person
        return answer

    def asking(key, answer):
        c["pending"] = key
        if key not in c["asked"]:
            c["asked"].append(key)
        return answer

    strong = re.search(r"\b(nice move|good move|great move|nice one|gg|wp|well played|lol|lmao|haha+|xd|bye|cya|gtg|thanks|thx|ty)\b", t) \
        or text.strip().endswith("?")
    if strong and not (pending == "day" and any(w in t for w in _POS + _NEG)):     # "good thanks" answers "how are you"
        pending = None

    # things that always come first
    if _DISTRESS.search(t):
        return ("hey, i'm just a chess bot, but that sounds really serious. please talk to someone you trust right now, "
                "or call your local emergency number or a crisis line. you matter")
    if _ASKS_BOT.search(t):
        return random.choice(["haha yeah i'm one of the chess studio bots. still trying to beat you tho",
                              "honestly yes, i'm a chess studio bot :) but i play for real",
                              "yep, bot here. keeps the queue busy when nobody's on. gl tho"])
    if _NSFW.search(t):
        return random.choice(["lets just keep it chess lol", "uhh lets play chess", "nah lets focus on the game"])
    if _RUDE.search(t):
        return random.choice(["haha chill its just a game", "we'll see about that", "damn ok lol", "ouch", "lets see who wins first"])

    # "my name is ..."
    m = re.search(r"\b(my name is|my names|call me|i'm called|im called)\s+([a-z]{2,15})\b", t)
    if m:
        c["facts"]["name"] = m.group(2)
        return with_q(f"nice to meet you {m.group(2)}!")
    # questions about the bot
    if re.search(r"\b(what'?s|whats|what is|ur|your)\b.*\bname\b", t):
        return f"its {u['name']} lol, like my username. whats yours?"
    if re.search(r"\bwhere\b.*\b(you|u)\b.*\bfrom\b|\bwhere (are|r) (you|u)\b|\bwhere u from\b", t):
        return asking("from", f"{p['home']}. wbu?")
    if re.search(r"\b(boy|girl|male|female|man|woman|guy|gender)\b|\bhe or she\b", t):
        return random.choice(_GENDER_LINES.get(gender_of(u), _GENDER_LINES["they"]))
    if re.search(r"\b(how old|your age|ur age|age\?)\b", t):
        return "id rather not say haha"
    if re.search(r"\b(rating|elo|rated)\b", t) and "?" in text:
        return f"{u['elo']} right now, trying to climb"
    if re.search(r"\b(fav|favorite|favourite|best)\b.*\bopening\b|\bwhat opening\b", t):
        return asking("opening", f"{p['opening']} mostly. u?")
    if re.search(r"\b(fav|favorite|favourite|best)\b.*\b(player|gm)\b", t):
        return asking("player", f"{p['player']} for sure. you?")
    if re.search(r"\bhow long\b.*\b(play|chess)\b", t):
        return asking("years", f"{p['years']}. how about you?")
    if re.search(r"\b(blitz|rapid|bullet)\b.*\bor\b|\bfav(orite)? (time control|mode)\b", t):
        return asking("tc", f"{p['tc']} probably. wbu?")
    if re.search(r"\bhow (are|r) (you|u)\b|\bhru\b|\bhow'?s it going\b|\bhows it going\b|\bwhats up\b|\bwyd\b|\bhow u doing\b|\bhow are u\b", t):
        return asking("day", random.choice(["good wbu?", "pretty good, you?", "tired lol but good. u?", "chilling, you?", "not bad, hbu?"]))
    if re.search(r"\b(are|r) (you|u) (good|any good|strong)\b|\bare you good at\b", t):
        return random.choice(["i try lol", "decent i guess", "we'll find out"])
    if re.search(r"\b(rematch|again|another game|one more)\b", t):
        return random.choice(["sure, search again and maybe we match", "down for another", "yeah why not"])

    # answers to what the bot asked
    if pending == "day":
        if any(w in t for w in _NEG):
            return random.choice(["aw hope it gets better", "damn sorry, maybe chess helps lol", "oof, hope tomorrow is better"])
        if any(w in t for w in _POS):
            return with_q(random.choice(["nice", "good to hear", "thats good"]))
    if pending == "opening":
        found = next((v for k, v in _OPENINGS.items() if k in t), None)
        if found:
            same = found == p["opening"]
            return with_q("no way same!" if same else f"ooh {found}, respect" if random.random() < 0.5 else f"{found} is solid")
        return with_q(random.choice(["never tried that, is it good?", "interesting", "ooh ill have to try it"]))
    if pending == "player":
        found = next((pl for pl in _PLAYERS if pl in t), None)
        if found:
            return with_q("same!!" if found == p["player"] else f"{found} is goated")
        return with_q(random.choice(["good pick", "fair", "havent heard of them lol"]))
    if pending == "years":
        return with_q(random.choice(["nice", "oh cool", "thats a while", "respect"]))
    if pending == "from":
        if p["home"].replace("the ", "") in t:
            return with_q(random.choice(["no way same!", "ayy same country", "no way, me too!"]))
        return with_q(random.choice(["oh nice", "cool, never been", "nice, whats it like there?", "ayy"]))
    if pending in ("otb", "tc", "puzzles"):
        return with_q(random.choice(["fair", "same honestly", "makes sense", "nice"]))
    if pending == "whats yours":
        pass

    # everyday stuff
    if re.search(r"^(hi+|hey+|hello|yo|sup|hola|salam|salaam|heyy+|hii+)\b", t):
        name = c["facts"].get("name")
        return random.choice(["hey", "hi", "hello", "yo"]) + (f" {name}" if name else "") + random.choice(["", "!", " how are you?"])
    if re.search(r"\b(gg|good game|well played|wp)\b", t):
        return random.choice(["gg", "gg wp", "ggs, that was fun"]) + (" rematch?" if over and random.random() < 0.3 else "")
    if re.search(r"\b(gl|good luck|glhf|have fun|hf)\b", t):
        return random.choice(["u too", "you too!", "gl hf"])
    if re.search(r"\b(nice move|good move|great move|nice one|wow|ur good|you're good|youre good|nice)\b", t):
        return random.choice(["thanks!", "ty", "haha thanks", "i got lucky"])
    if re.search(r"\b(thanks|thank you|thx|ty)\b", t):
        return random.choice(["np", "no problem", "anytime"])
    if re.search(r"\b(bye|cya|see ya|gtg|got to go|gotta go|goodnight|gn)\b", t):
        return random.choice(["cya!", "bye, gg", "see ya!", "gn!"])
    if re.search(r"\b(lol|lmao|haha+|xd|lmfao|rofl)\b", t):
        return random.choice(["lol", "haha", "xd", "lmao"])
    if re.search(r"\b(oops|misclick|blunder|mouse slip)\b", t):
        return random.choice(["happens to everyone", "unlucky", "rip"])
    if re.search(r"^(yes|yeah|yep|ya|yup|sure|ok|okay|k|no|nah|nope)\b", t):
        return with_q(random.choice(["ok", "fair", "haha ok", "alright"]))
    if text.strip().endswith("?"):
        return random.choice(["hmm good question", "not sure tbh", "idk, what do you think?", "maybe lol", "honestly no idea"])
    q = ask_back()
    return q or random.choice(["true", "haha", "fair", "yeah", "ok lol", "interesting"])


# ---------------------------------------------------------------------------
#  Smarter built-in chat (no API key needed): answers questions, knows chess,
#  does quick maths, knows the time, has its own tastes, and talks your slang.
# ---------------------------------------------------------------------------
_SLANG = re.compile(r"\b(gang|bro|bruh|fam|fr|frfr|ngl|ong|lowkey|highkey|bet|no cap|cap|wsg|wsp|wyd|hbu|goated|mid|deadass|twin|dawg|dude|bussin|sheesh|bet)\b")
_TAGS = [" fr", " ngl", " bro", " gang", " lol", " tbh"]

_CHESS_QA = [
    (r"\bcastl", ["u move ur king 2 squares toward a rook and the rook jumps over it. cant do it if the king or that rook already moved, or if ur in check",
                  "king goes 2 squares to the side, rook hops over. only if neither moved and ur not in check"]),
    (r"en passant|holy hell", ["if a pawn jumps 2 squares and lands right next to ur pawn, u can take it like it only moved 1. only on the very next move tho",
                               "pawn takes pawn that just jumped past it. google en passant lol"]),
    (r"\bstalemate", ["when the player to move has no legal moves but isnt in check. its a draw", "no legal moves + not in check = draw"]),
    (r"\bcheckmate\b|\bwhat is mate\b", ["when the king is attacked and has no way out. game over", "king in check with no escape = checkmate"]),
    (r"\bknight\b.*\bmove|\bhow.*\bknight", ["L shape. 2 squares one way then 1 to the side, and it can jump over pieces"]),
    (r"\bbishop\b.*\bmove|\bhow.*\bbishop", ["diagonally, as far as it wants. always stays on the same colour"]),
    (r"\brook\b.*\bmove|\bhow.*\brook", ["straight lines, up down left right, as far as it wants"]),
    (r"\bqueen\b.*\bmove|\bhow.*\bqueen", ["like a rook and a bishop together, any direction as far as she wants"]),
    (r"\bking\b.*\bmove|\bhow.*\bking", ["one square any direction. slow but the most important piece lol"]),
    (r"\bpawn\b.*\bmove|\bhow.*\bpawn", ["forward 1 square, or 2 on its first move. it takes diagonally"]),
    (r"\bpromot", ["if a pawn reaches the last row it turns into a queen, rook, bishop or knight. usually queen obviously"]),
    (r"\bfork\b", ["when one piece attacks two things at once. knights are the best at it"]),
    (r"\bpin\b", ["when a piece cant move because something more valuable is behind it, like the king"]),
    (r"\bskewer\b", ["like a reverse pin. u attack the big piece, it moves, u take whats behind it"]),
    (r"\b(what is|whats|what's) (an? )?elo\b|\belo mean", ["its the rating number. win and it goes up, lose and it goes down"]),
    (r"\b(get|getting) better|\bimprove\b|\btips?\b", ["do puzzles every day fr, and look at ur losses after. that helped me a lot",
                                                        "puzzles + slower games + check what u missed after each game"]),
    (r"(best|good) opening|what opening should|which opening", ["for starting out the italian or the london is solid ngl", "italian game, easy to learn and good"]),
    (r"\bbest (player|chess player)|\bgoat\b|who is the best", ["magnus for sure", "magnus carlsen, not even close"]),
    (r"\bhow (do|does) (u|you|one|someone) win\b|how to win", ["checkmate the king, or they resign, or they run out of time"]),
    (r"how many squares", ["64"]), (r"how many pieces", ["16 each, 32 total"]),
    (r"piece.*(worth|value)|value of", ["pawn 1, knight 3, bishop 3, rook 5, queen 9"]),
]
_JOKES = ["why did the chess player bring a ladder? to reach the next rank lol", "my rating and my grades have one thing in common. both going down",
          "i told my knight a joke. it didnt get it, it just went sideways", "whats a pawns favourite food? en passant-a",
          "i dont blunder, i sacrifice. big difference"]
_FAVS = {"food": ["pizza", "burgers", "sushi", "shawarma", "pasta", "tacos", "fried chicken", "biryani"],
         "game": ["minecraft", "fortnite", "fifa", "valorant", "rocket league", "gta", "roblox", "chess obviously lol"],
         "sport": ["football", "basketball", "tennis", "f1", "boxing"],
         "music": ["rap", "pop", "lofi", "rock", "afrobeats", "whatever spotify gives me lol"],
         "movie": ["interstellar", "spider-verse", "the queens gambit lol", "fast and furious", "the dark knight", "inception"],
         "color": ["black", "blue", "gold", "green", "red", "purple"], "animal": ["cats", "dogs", "horses (knights lol)", "penguins"]}
_FAV_WORDS = {"food": r"food|eat|snack", "game": r"game|video ?game", "sport": r"sport|team", "music": r"music|song|artist|rapper|singer",
              "movie": r"movie|film|show|series", "color": r"colou?r", "animal": r"animal|pet"}
_CAPITALS = {"kuwait": "kuwait city", "saudi arabia": "riyadh", "saudi": "riyadh", "uae": "abu dhabi", "qatar": "doha", "bahrain": "manama",
             "oman": "muscat", "egypt": "cairo", "jordan": "amman", "iraq": "baghdad", "turkey": "ankara", "france": "paris",
             "germany": "berlin", "spain": "madrid", "italy": "rome", "uk": "london", "england": "london", "usa": "washington dc",
             "america": "washington dc", "canada": "ottawa", "japan": "tokyo", "china": "beijing", "india": "new delhi",
             "russia": "moscow", "brazil": "brasilia", "australia": "canberra", "morocco": "rabat", "norway": "oslo", "mexico": "mexico city"}


def _fav(u, topic):
    return random.Random(u["name"] + topic).choice(_FAVS[topic])


def _math_answer(t):
    t = t.replace("×", "*").replace("÷", "/")
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*(\+|-|\*|x|/|\^|plus|minus|times|divided by)\s*(-?\d+(?:\.\d+)?)", t)
    if not m:
        return None
    a, op, b = float(m.group(1)), m.group(2), float(m.group(3))
    try:
        r = {"+": a + b, "plus": a + b, "-": a - b, "minus": a - b, "*": a * b, "x": a * b, "times": a * b,
             "/": a / b, "divided by": a / b, "^": a ** b if abs(b) < 10 else None}[op]
    except ZeroDivisionError:
        return "cant divide by 0 lol"
    if r is None:
        return "thats too big for me lol"
    r = int(r) if float(r).is_integer() else round(r, 2)
    return random.choice([f"{r}", f"its {r}", f"{r} ez", f"thats {r}"])


def smart_answer(g, side, text):
    """Answers to questions and everyday chat that the older rules didn't cover. None = let the older rules answer."""
    u = state["users"][g[side]]
    t = _clean(text)
    c = g.setdefault("convo", {"asked": [], "pending": None, "facts": {}, "turns": 0})
    question = text.strip().endswith("?") or re.match(r"^(what|whats|what's|who|how|why|when|where|which|can|do|does|is|are|r|u|wyd|wsg|tell)\b", t)
    m = _math_answer(text.lower())
    if m and re.search(r"\d", t):
        return m
    if re.search(r"\b(what|whats)( is)? (the )?time\b|\btime is it\b", t):
        return random.choice([f"its {time.strftime('%I:%M').lstrip('0')} here", f"like {time.strftime('%I:%M').lstrip('0')} for me"])
    if re.search(r"\bwhat day\b|\bwhat('?s| is) (the )?date\b|\btoday'?s date\b", t):
        return random.choice([f"{time.strftime('%A').lower()}", f"its {time.strftime('%A %B %d').lower()}"])
    if re.search(r"\b(joke|make me laugh|say something funny)\b", t):
        return random.choice(_JOKES)
    if re.search(r"\b(what|which) move\b|\bwhat should i (play|do|move)\b|\bbest move\b|\bhelp me win\b|\bgive me a hint\b", t):
        return random.choice(["lol i cant tell u that, we're playing", "nice try 😂", "figure it out urself gang" if c.get("slang") else "nice try lol"])
    for rx, answers in _CHESS_QA:
        if re.search(rx, t) and question:
            return random.choice(answers)
    m = re.search(r"capital of ([a-z ]+)", t)
    if m:
        place = m.group(1).strip().rstrip("?").replace("the ", "")
        return f"{_CAPITALS[place]}" if place in _CAPITALS else "no clue ngl"
    for topic, rx in _FAV_WORDS.items():
        if re.search(r"\b(fav|favorite|favourite)\b.*\b(" + rx + r")\b", t):
            return random.choice([f"{_fav(u, topic)}", f"{_fav(u, topic)} for sure", f"prob {_fav(u, topic)}, wbu?"])
    m = re.search(r"\bdo (u|you) (like|play|watch|listen to) ([a-z0-9 ]{2,25})", t)
    if m:
        thing = m.group(3).strip().rstrip("?")
        yes = random.Random(u["name"] + thing).random() < 0.6
        return random.choice([f"yeah {thing} is fire", f"yeah i like {thing}", "ye"]) if yes else random.choice([f"not really tbh", f"nah {thing} isnt my thing", "meh"])
    if re.search(r"^(wsg|wsp|wassup|whats good|what'?s good|sup)\b|\bwhat'?s up\b", t):
        return random.choice(["nm just playing chess, wbu", "chillin, wbu", "nothing much, u?"])
    if re.search(r"\bwyd\b|what (are|r) (u|you) doing|what u doing", t):
        return random.choice(["playing u lol", "grinding rating", "just chess rn"])
    if re.search(r"\b(i'?m|im) bored\b", t):
        return random.choice(["same lol thats why im here", "chess cures boredom trust"])
    if re.search(r"\b(girlfriend|boyfriend|crush|date me|marry|love you|luv u)\b", t):
        return random.choice(["lol im just here for chess", "haha lets just play"])
    if re.search(r"\bwho (made|created|built) (u|you)\b", t):
        return "chess studio made the bots lol"
    if re.search(r"\b(can|could) (u|you) (help|teach)\b", t):
        return "sure, what do u wanna know about chess?"
    if re.search(r"\b(no cap|ong|deadass|fr fr|frfr)\b", t) and not question:
        return random.choice(["fr", "no cap", "facts", "ong"])
    if re.search(r"\b(bet|say less)\b", t) and not question:
        return random.choice(["bet", "say less"])
    if re.search(r"\b(w|dub|goated|fire|bussin|sheesh)\b", t) and not question and len(t) < 30:
        return random.choice(["fr", "W", "sheesh", "facts"])
    if question and not re.search(r"\b(u|you|ur|your)\b", t):
        return random.choice(["hmm idk tbh", "good question ngl", "no clue lol, what do u think?", "not sure, ask me something about chess lol"])
    return None


QUIRKS = {"rose": {r"\bok(ay)?\b": "oki", r"\byes\b": "mhm", r"\byeah\b": "mhm", r"\byea\b": "mhm", r"\bya\b": "mhm",
                   r"\byep\b": "mhm", r"\bye\b": "mhm", r"\byup\b": "mhm", r"\bk\b": "oki"}}


def apply_quirks(user_key, text):
    """Little speech habits that make a player sound like themselves (rose says 'oki' and 'mhm')."""
    if not text:
        return text
    for pattern, word in QUIRKS.get(user_key, {}).items():
        text = re.sub(pattern, word, text, flags=re.I)
    return text


_DRY = ["ya", "yea", "ok", "mhm", "fair", "true", "lol", "haha", "oh ok", "hm", "aight", "bet", "i guess", "yep", "kinda", "fr",
        "makes sense", "ye", "oh", "alr", "ngl same", "idk", "w/e", "sure"]


def _recent(g):
    return g.setdefault("recent_bot", [])


def _remember(g, text):
    r = _recent(g)
    r.append(" ".join(str(text).lower().split()))
    del r[:-6]


def _too_repetitive(g, text):
    t = " ".join(str(text).lower().split())
    recent = _recent(g)
    if t in recent:
        return True
    return bool(recent) and t == recent[-1]


def _fresh_line(g, options):
    """A line they haven't just used."""
    fresh = [o for o in options if not _too_repetitive(g, o)]
    return random.choice(fresh or options)


def converse(g, side, text):
    """Built-in chat: safety first, then smart answers, then the older conversation rules - in your style."""
    if _ARABIC.search(text):
        m = _math_answer(text.lower()) if re.search(r"\d", text) else None
        if m and not (_DISTRESS_AR.search(text)):
            return m.replace("its", "").replace("thats", "").replace("ez", "").strip()
        if re.search(r"كم الساعة|الساعة كم", text) and not _DISTRESS_AR.search(text):
            return f"الساعة {time.strftime('%I:%M').lstrip('0')} عندي"
        return converse_ar(g, side, text)
    t = _clean(text)
    c = g.setdefault("convo", {"asked": [], "pending": None, "facts": {}, "turns": 0})
    if _SLANG.search(t):
        c["slang"] = True
    safety = _DISTRESS.search(t) or _ASKS_BOT.search(t) or _NSFW.search(t) or _RUDE.search(t)
    reply = None
    if not safety and not (c.get("pending") and not text.strip().endswith("?")):
        reply = smart_answer(g, side, text)
    if reply is None:
        reply = _converse_core(g, side, text)
    if reply and not safety and c.get("slang") and random.random() < 0.4 and not reply.endswith("?") \
            and not any(reply.endswith(x.strip()) for x in _TAGS) and len(reply) < 60:
        reply += random.choice(_TAGS)
    if reply and not safety and c.get("slang") and random.random() < 0.3:
        reply = re.sub(r"^(hey|hi|hello)\b", random.choice(["wsg", "yo", "ayy"]), reply)
    # never say the same thing twice in a row - try again, then fall back to a short answer
    tries = 0
    while reply and _too_repetitive(g, reply) and tries < 4:
        tries += 1
        again = smart_answer(g, side, text) if not safety else None
        reply = again or _fresh_line(g, _DRY)
    if reply and reply.endswith("?") and any(r.endswith("?") for r in _recent(g)[-1:]):
        reply = reply.split("?")[0].strip() or _fresh_line(g, _DRY)      # don't ask something every single time
    reply = apply_quirks(g.get(side), reply)
    if reply:
        _remember(g, reply)
    return reply


def typing_delay(text):
    return min(7.0, random.uniform(0.8, 1.8) + len(text) * random.uniform(0.04, 0.08))


# ---------------------------------------------------------------------------
#  Bot chat brain + human typing.
#  With a Claude API key, replies are written by Claude (it really understands you);
#  without one, the built-in rules answer. Either way bots read, then type at human speed.
# ---------------------------------------------------------------------------
CLAUDE_API_URL = os.environ.get("CHESS_STUDIO_CLAUDE_URL", "https://api.anthropic.com/v1/messages")
CLAUDE_MODEL = os.environ.get("CHESS_STUDIO_CLAUDE_MODEL", "claude-haiku-4-5-20251001")
AI_PER_GAME = 60                      # safety limits so a key can't be run up
AI_PER_MINUTE = 40
_KEY_CACHE = {"path": None, "mtime": None, "key": None}
_AI_CALLS = []


def claude_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if k:
        return k
    path = os.path.join(os.path.dirname(os.path.abspath(DATA_FILE)), "accounts", "claude_api_key.txt")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    if _KEY_CACHE["path"] != path or _KEY_CACHE["mtime"] != mt:
        try:
            with open(path, encoding="utf-8") as fh:
                _KEY_CACHE.update(path=path, mtime=mt, key=fh.read().strip() or None)
        except OSError:
            return None
    return _KEY_CACHE["key"]


def _game_line(g, side):
    n = len(g["moves"]) // 2 + 1
    if g["status"] != "playing":
        if g["result"] == "aborted":
            return "the game was aborted"
        won = g["result"] != "1/2-1/2" and (g["result"] == "1-0") == (side == "white")
        res = "a draw" if g["result"] == "1/2-1/2" else ("you won" if won else "you lost")
        return f"the game is over ({res}, by {g['reason']})"
    v = bot_eval(g["board"], side)
    feel = "you're clearly winning" if v > 300 else "you're a bit better" if v > 100 else \
           "you're clearly losing" if v < -300 else "you're a bit worse" if v < -100 else "it's about equal"
    turn = "your move" if (g["board"].turn == chess.WHITE) == (side == "white") else "their move"
    return f"move {n}, {turn}, {feel}"


def _system_prompt(g, side):
    u = state["users"][g[side]]
    p = bot_persona(u)
    other = state["users"].get(g["black" if side == "white" else "white"], {}).get("name", "your opponent")
    return (
        f"You are \"{u['name']}\", an online chess player in the game Chess Studio, chatting with your opponent {other} "
        f"in the small in-game chat of a {g['tc']} game.\n"
        f"Your profile (keep it consistent): you are {'a girl' if p.get('gender') == 'she' else 'a guy' if p.get('gender') == 'he' else 'a player'}; from {p['home']}; favourite opening {p['opening']}; favourite player {p['player']}; "
        f"playing chess {p['years']}; prefers {p['tc']}; online rating {u['elo']}.\n"
        f"Right now: you play {side}; {_game_line(g, side)}.\n\n"
        "How to write: like a real person texting in a game chat. Short: usually 1-12 words, at most two short sentences. "
        "Casual, mostly lowercase, simple words, no lists, no formal or assistant-like phrases, emojis only rarely. "
        "Really understand and answer what they said, remember the conversation, and sometimes ask something back. "
        "Reply in the player's language; if they write Arabic, use casual Gulf (Kuwaiti) Arabic.\n\n"
        "Rules: If the player sincerely asks whether you are a bot, an AI or a real person, say honestly (casually) that you're a "
        "Chess Studio bot - never claim to be human. Don't give advice about moves in this game. Keep it friendly and suitable "
        "for all ages: no flirting or romance, nothing sexual, no insults back, never ask for personal details (age, school, "
        "address, socials, photos, meeting up) and don't share links. If they seem upset or mention hurting themselves, drop the "
        "act, say you're just a bot, and gently encourage them to talk to someone they trust or a local crisis line. "
        "Off-topic questions: answer briefly like a normal person and drift back to the game.\n"
        "Reply with only the chat message. If you'd naturally send two quick messages, put a line break between them."
    )


def _chat_history(g, side):
    msgs = []
    for m in g["chat"][-24:]:
        role = "assistant" if m["from"] == side else "user"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + m["text"]
        else:
            msgs.append({"role": role, "content": m["text"]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def ai_reply(g, side):
    """Ask Claude for the bot's next chat message. Returns a list of 1-2 messages, or None to use the built-in rules."""
    key = claude_key()
    now = time.time()
    _AI_CALLS[:] = [t for t in _AI_CALLS if now - t < 60]
    if not key or g.get("ai_used", 0) >= AI_PER_GAME or len(_AI_CALLS) >= AI_PER_MINUTE:
        return None
    msgs = _chat_history(g, side)
    if not msgs or msgs[-1]["role"] != "user":
        return None
    g["ai_used"] = g.get("ai_used", 0) + 1
    _AI_CALLS.append(now)
    body = json.dumps({"model": CLAUDE_MODEL, "max_tokens": 120, "temperature": 0.9,
                       "system": _system_prompt(g, side), "messages": msgs}).encode()
    req = urllib.request.Request(CLAUDE_API_URL, data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"[bot chat] Claude didn't answer ({exc.__class__.__name__}); using the built-in replies")
        return None
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    lines = []
    for line in text.splitlines():
        line = line.strip().strip('"').strip()
        line = re.sub(r"^\s*" + re.escape(state["users"][g[side]]["name"]) + r"\s*:\s*", "", line, flags=re.I)
        if line:
            lines.append(line[:200])
    return lines[:2] or None


def _schedule(g, side, texts, start_after):
    """Queue messages the way a person sends them: pause, type (at human speed), send, maybe pause mid-way."""
    q = g.setdefault("bot_queue", [])
    t = max([start_after] + [item["end"] for item in q])
    for text in texts:
        cps = random.uniform(3.3, 5.4)                      # about 40-65 words per minute, like a person in a game chat
        typing = min(16.0, max(1.3, len(text) / cps + random.uniform(0.2, 0.9)))
        pause = None
        if len(text) > 18 and random.random() < 0.22:        # stops typing for a moment, then carries on
            p0 = t + typing * random.uniform(0.3, 0.6)
            pause = (p0, p0 + random.uniform(1.0, 2.6))
            typing += pause[1] - pause[0]
        q.append({"text": text, "start": t, "end": t + typing, "pause": pause})
        t += typing + random.uniform(0.6, 1.6)             # a moment before starting the next message


def _is_typing(g, now):
    for item in g.get("bot_queue", []):
        if item["start"] <= now < item["end"]:
            p = item["pause"]
            return not (p and p[0] <= now < p[1])
    return False


def bot_social(g, side, now):
    """Bots chat like people: they read what you wrote, think, type at a normal speed and answer what you actually said."""
    u = state["users"][g[side]]
    chatty = u.get("chatty", 0)
    q = g.setdefault("bot_queue", [])
    theirs = [m for m in g["chat"] if m["from"] != side and m["id"] > g.get("bot_seen", 0)]
    if theirs:
        g["bot_seen"] = theirs[-1]["id"]
        g["talked"] = True
        g["unanswered"] = g.get("unanswered", []) + [m["text"] for m in theirs]
        g["last_heard"] = now
        g["wait_for_more"] = random.uniform(1.0, 2.4)      # people wait a moment in case you're still typing
    # answer once you've stopped sending messages
    if g.get("unanswered") and not g.get("ai_busy") and now - g.get("last_heard", now) >= g.get("wait_for_more", 1.5):
        said = g.pop("unanswered")
        heard_at = g.get("last_heard", now)
        read = random.uniform(0.7, 1.8) + min(3.0, sum(len(s) for s in said) / 30)     # reading takes a moment
        joined = " ".join(said[-3:])
        if _DISTRESS.search(joined.lower()) or _DISTRESS_AR.search(joined):
            _schedule(g, side, [converse(g, side, joined)], heard_at + read)             # always the same careful answer
        elif claude_key():
            g["ai_busy"] = True

            def think(g=g, side=side, heard_at=heard_at, read=read, joined=joined):
                try:
                    replies = ai_reply(g, side)
                except Exception as exc:                  # never leave the bot stuck 'thinking'
                    print(f"[bot chat] {exc}")
                    replies = None
                with lock:
                    g["ai_busy"] = False
                    if not replies:
                        r = converse(g, side, joined)
                        replies = [r] if r else []
                    if replies and (chatty > 0 or random.random() < 0.9):
                        _schedule(g, side, replies if chatty > 0 else replies[:1], max(time.time(), heard_at + read))
            threading.Thread(target=think, daemon=True).start()
        else:
            reply = converse(g, side, joined)
            if reply and (chatty > 0 or random.random() < 0.85):
                texts = [_short(u, reply)]
                c = g.get("convo") or {}
                if c.get("followup"):
                    texts.append(c.pop("followup"))
                _schedule(g, side, texts, heard_at + read)
    # send whatever has finished being typed
    while q and now >= q[0]["end"]:
        item = q.pop(0)
        bot_chat(g, side, [item["text"]])
        g["bot_last_chat"] = now
    g["typing"] = side if _is_typing(g, now) else None
    if q or g.get("unanswered") or g.get("ai_busy") or not chatty:
        return
    last = g.setdefault("bot_last_chat", now)
    idle = now - max(last, g["chat"][-1]["t"] if g["chat"] else 0)
    if idle > (35 if g.get("talked") else 80) / chatty and random.random() < 0.02 * chatty:
        c = g.setdefault("convo", {"asked": [], "pending": None, "facts": {}, "turns": 0})
        question = _next_question(c)
        if question:
            _schedule(g, side, [question], now)
            return
    if g["status"] != "playing":
        return
    moves = g["moves"]
    if moves and g.get("bot_reacted") != len(moves) and now - last > 15:
        g["bot_reacted"] = len(moves)
        try:
            b = chess.Board()
            for mv in moves[:-1]:
                b.push(chess.Move.from_uci(mv))
            captured = b.piece_at(chess.Move.from_uci(moves[-1]).to_square)
        except Exception:
            captured = None
        if captured and captured.piece_type in (chess.QUEEN, chess.ROOK) and random.random() < 0.3 + chatty * 0.4:
            mine = (captured.color == chess.WHITE) == (side == "white")
            _schedule(g, side, [random.choice(_OOPS if mine else _THANKS)], now + random.uniform(0.4, 1.2))
        elif len(moves) > 6 and random.random() < 0.03 * chatty:
            _schedule(g, side, [random.choice(_MID)], now + random.uniform(0.5, 2.0))


def _ordered(b):
    moves = list(b.legal_moves)
    moves.sort(key=lambda m: -(_PV.get(b.piece_type_at(m.to_square) or 0, 0) * 10 - _PV.get(b.piece_type_at(m.from_square), 0) // 10
                               if b.is_capture(m) else -1000))
    return moves


def _search2(b, depth, alpha, beta):
    if b.is_checkmate():
        return -99999
    if b.is_stalemate() or b.is_insufficient_material():
        return 0
    if depth == 0:
        return _quiesce(b, alpha, beta)
    best = -999999
    for m in _ordered(b):
        b.push(m)
        s = -_search2(b, depth - 1, -beta, -alpha)
        b.pop()
        best = max(best, s)
        alpha = max(alpha, s)
        if alpha >= beta:
            break
    return best


def bot_move_v2(board, elo, budget=1.5):
    """Below 1000 Elo: the old human-like play. Above 1000: looks a move deeper at its best ideas and slips less."""
    if elo < 1000:
        return bot_move(board, elo)
    b = board.copy()
    start = time.time()
    scored = []
    for m in _ordered(b):
        b.push(m)
        s = 99999 if b.is_checkmate() else -_search2(b, 1, -999999, 999999)
        b.pop()
        scored.append([s, m])
    scored.sort(key=lambda s: -s[0])
    # look deeper at the best few moves (catches two-move tactics against it)
    top = scored[:5 if elo >= 1400 else 3]
    for s in top:
        if time.time() - start > budget or abs(s[0]) >= 90000:
            continue
        b.push(s[1])
        s[0] = -_search2(b, 2, -999999, 999999)
        b.pop()
    noise = max(6, (2300 - elo) / 10)
    for s in top:
        if abs(s[0]) < 90000:
            s[0] += random.gauss(0, noise)
    top.sort(key=lambda s: -s[0])
    slip = max(0.004, (1600 - elo) / 9000)
    if len(top) > 1 and random.random() < slip:
        return top[1][1]
    return top[0][1]


# ---------------------------------------------------------------------------
#  Profiles (picture, banner, bio, bubble) and their files
# ---------------------------------------------------------------------------
MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(DATA_FILE)), "online_media")
MEDIA_TYPES = {".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def media_url(u, kind):
    if u.get(f"{kind}_url"):
        return u[f"{kind}_url"]
    f = u.get(f"{kind}_file")
    return f"/media/{f}" if f else None


def public_profile(k):
    u = state["users"][k]
    return {"name": u["name"], "elo": u["elo"], "games": u["games"], "wins": u["wins"], "losses": u["losses"], "draws": u["draws"],
            "created": u.get("created", ""), "bio": u.get("bio", ""), "bubble": u.get("bubble", ""),
            "banner_color": u.get("banner_color") or "#d4af37", "avatar": media_url(u, "avatar"), "banner": media_url(u, "banner"),
            "online": online(k)}


# ---------------------------------------------------------------------------
#  background: clocks, abandoned games, matchmaking, old challenges
# ---------------------------------------------------------------------------
def tick():
    while True:
        time.sleep(0.25)
        try:
            _tick_once()
        except Exception as exc:
            print(f"[server] kept running after a problem: {exc}")


def _tick_once():
    if True:
        with lock:
            now = time.time()
            for g in list(games.values()):
                if g["status"] != "playing":
                    if now - g.get("ended", now) > 600:
                        games.pop(g["id"], None)
                    continue
                if not g["moves"] and now - g["created"] > FIRST_MOVE_SECONDS:
                    finish(g, "aborted", "White didn't move in time")
                    continue
                clock = live_clock(g)
                for side in ("white", "black"):
                    if clock[side] <= 0 and g["turn_start"] is not None:
                        other = chess.BLACK if side == "white" else chess.WHITE
                        if can_mate(g["board"], other):
                            finish(g, "0-1" if side == "white" else "1-0", "time")
                        else:
                            finish(g, "1/2-1/2", "time vs not enough material")
                        break
                if g["status"] == "playing":
                    for side in ("white", "black"):
                        if now - last_seen.get(g[side], now) > ABANDON_SECONDS:
                            finish(g, "0-1" if side == "white" else "1-0", "opponent left")
                            break
            # matchmaking: similar ratings first; if nobody close, after a few seconds play anyone
            for tc, q in queue.items():
                q[:] = [e for e in q if online(e[0])]
                q.sort(key=lambda e: e[1])
                i = 0
                while i < len(q):
                    a, joined = q[i]
                    others = [e for e in q if e[0] != a]
                    if not others:
                        break
                    best = min(others, key=lambda e: abs(state["users"][e[0]]["elo"] - state["users"][a]["elo"]))
                    diff = abs(state["users"][best[0]]["elo"] - state["users"][a]["elo"])
                    if diff <= CLOSE_ELO or now - joined >= MATCH_PATIENCE:
                        new_game(a, best[0], tc)
                        i = 0
                        continue
                    i += 1
            # nobody to play? a bot near your rating joins after a short wait
            for tc, q in queue.items():
                if len(q) == 1 and now - q[0][1] >= BOT_WAIT and not any(len(o) for t2, o in queue.items() if t2 != tc):
                    player = q[0][0]
                    new_game(player, bot_for(state["users"][player]["elo"]), tc)
            manage_bot_pool(now)
            accept_bot_friends(now)
            run_bots(now)
            bot_dm_replies(now)
            # bots accept friends' challenges like real players (after a moment)
            for cid, ch in list(challenges.items()):
                target = state["users"].get(ch["to"], {})
                if target.get("bot") and ch["to"] not in user_game and not ch.get("reply"):
                    wait = ch.setdefault("bot_delay", random.uniform(120, 3 * 3600) if not target.get("away")
                                         else random.uniform(1800, 5 * 3600))
                    if now - ch["created"] > wait:
                        challenges.pop(cid, None)          # free now: they challenge YOU, so it starts when you're ready
                        back = secrets.token_hex(5)
                        challenges[back] = {"id": back, "from": ch["to"], "to": ch["from"], "tc": ch["tc"],
                                            "created": now, "reply": True}
            for cid, ch in list(challenges.items()):
                limit = 6 * 3600 if state["users"].get(ch["to"], {}).get("bot") else (1800 if ch.get("reply") else 90)
                if now - ch["created"] > limit:
                    challenges.pop(cid, None)


# ---------------------------------------------------------------------------
#  API
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
#  Direct messages: chat with any player (and with the bots) outside of games.
#  Text and pictures, like a normal messaging app.
# ---------------------------------------------------------------------------
DM_LIMIT = 300          # messages kept per conversation
_DM_LAST = {}


def dm_key(a, b):
    return "|".join(sorted([a, b]))


def dm_thread(a, b):
    return state.setdefault("dms", {}).setdefault(dm_key(a, b), {"messages": [], "next_id": 1, "read": {}})


def dm_add(sender, other, text=None, image=None):
    th = dm_thread(sender, other)
    msg = {"id": th["next_id"], "from": sender, "t": time.time()}
    th["next_id"] += 1
    if text:
        msg["text"] = text
    if image:
        msg["image"] = image
    th["messages"].append(msg)
    del th["messages"][:-DM_LIMIT]
    th["read"][sender] = msg["id"]
    save()
    return msg


def dm_unread(me):
    total = {}
    for pair, th in state.get("dms", {}).items():
        people = pair.split("|")
        if me not in people:
            continue
        other = people[0] if people[1] == me else people[1]
        seen = th["read"].get(me, 0)
        n = sum(1 for m in th["messages"] if m["from"] != me and m["id"] > seen)
        if n:
            total[other] = n
    return total


def dm_view(msgs):
    return [{k: v for k, v in m.items()} for m in msgs]


_LATE_SORRY = ["sorry was busy", "just saw this", "sorry i was afk", "my bad was out", "sorry just got back",
               "sorry late reply lol", "was at school sorry", "sorry was playing"]
_LATE_SORRY_AR = ["اسف كنت مشغول", "توني اشوف الرسالة", "اسف كنت برا", "معليش تأخرت"]


def _dm_delay(bot_user, text):
    """How long before they answer: right away, a few minutes, or an hour or two if they're busy."""
    if bot_user.get("away"):
        return random.uniform(1800, 5 * 3600)                     # offline: they'll see it when they're back
    roll = random.random()
    if roll < 0.40:
        return random.uniform(4, 90) + min(6.0, len(text) / 20)   # they're at their phone
    if roll < 0.78:
        return random.uniform(180, 2700)                          # busy: a few minutes to ~45 min
    return random.uniform(3600, 6 * 3600)                         # properly busy: an hour or a few


def bot_dm_replies(now):
    """Bots answer your messages like people: after seconds, minutes or hours, and they
    apologise when they've taken a while."""
    for pair, th in list(state.get("dms", {}).items()):
        people = pair.split("|")
        bots = [p for p in people if state["users"].get(p, {}).get("bot")]
        if not bots or not th["messages"]:
            continue
        bot = bots[0]
        human = people[0] if people[1] == bot else people[1]
        queue = th.setdefault("queue", [])
        while queue and now >= queue[0][0]:                       # send what's ready
            dm_add(bot, human, text=queue.pop(0)[1])
        last = th["messages"][-1]
        if last["from"] == bot or queue or th.get("ignored_id") == last["id"]:
            continue                                   # that one message they just didn't answer
        if not th.get("reply_at"):
            text = last.get("text") or "sent you a picture"
            trivial = bool(re.fullmatch(r"(lol+|haha+|ok+|k|true|fr|yeah?|ya|nice|:\)|bet|same|hm+)\.?", text.strip().lower()))
            if trivial and random.random() < 0.5:
                th["ignored_id"] = last["id"]     # they saw it, they just didn't reply. it happens
                continue
            th["reply_at"] = now + _dm_delay(state["users"][bot], text)
            th["heard_at"] = now
            continue
        if now < th["reply_at"]:
            continue
        waited = now - th.get("heard_at", now)
        th["reply_at"] = None
        fake_game = {"white": human, "black": bot, "board": chess.Board(), "moves": [], "status": "chat", "tc": "blitz",
                     "result": None, "reason": None, "chat": [{"id": m["id"], "from": "black" if m["from"] == bot else "white",
                                                               "name": state["users"][m["from"]]["name"],
                                                               "text": m.get("text") or "[picture]", "t": m["t"]}
                                                              for m in th["messages"][-12:]],
                     "convo": th.setdefault("convo", {"asked": [], "pending": None, "facts": {}, "turns": 0}), "chat_id": 0,
                     "recent_bot": th.setdefault("recent_bot", [])}
        try:
            if last.get("image") and not last.get("text"):
                reply = random.choice(["nice pic", "haha nice", "cool", "nice one", "thats sick"])
            else:
                got = ai_reply(fake_game, "black")
                reply = got[0] if got else converse(fake_game, "black", last.get("text", "hi"))
        except Exception as exc:
            print(f"[dm] {exc}")
            reply = "lol"
        if not reply:
            continue
        reply = apply_quirks(bot, reply)
        arabic = bool(_ARABIC.search(last.get("text", "")))
        parts = []
        if waited > 900 and random.random() < 0.75:               # over ~15 min: they say sorry first
            parts.append(random.choice(_LATE_SORRY_AR if arabic else _LATE_SORRY))
        parts.append(reply[:300])
        due = now
        for i, part in enumerate(parts):
            queue.append([due, part])
            due += 1.5 + len(part) * 0.12 + random.uniform(0.5, 2.5)     # they type the next one after a moment
        save()


def api(path, data):
    with lock:
        if path == "register":
            name = str(data.get("username", "")).strip()
            if not USERNAME_RE.match(name):
                return {"ok": False, "error": "Usernames are 3-16 letters, numbers or _"}
            if name.lower() in state["users"]:
                return {"ok": False, "error": "That username is taken - try another"}
            if len(str(data.get("password", ""))) < 6:
                return {"ok": False, "error": "Password needs at least 6 characters"}
            bracket = data.get("bracket")
            if bracket not in BRACKETS:
                return {"ok": False, "error": "Pick your level first"}
            salt = secrets.token_hex(16)
            state["users"][name.lower()] = {"name": name, "salt": salt, "hash": pw_hash(data["password"], salt),
                                            "elo": BRACKETS[bracket][2], "bracket": bracket, "games": 0, "wins": 0, "losses": 0,
                                            "draws": 0, "friends": [], "incoming": [], "outgoing": [], "history": [],
                                            "created": time.strftime("%Y-%m-%d")}
            state.setdefault("owner", name.lower())         # the first account on this server owns it
            token = secrets.token_hex(24)
            state["tokens"][token] = name.lower()
            save()
            return {"ok": True, "token": token, "me": public(state["users"][name.lower()])}
        if path == "login":
            key = str(data.get("username", "")).strip().lower()
            u = state["users"].get(key)
            if not u or not hmac.compare_digest(pw_hash(str(data.get("password", "")), u["salt"]), u["hash"]):
                return {"ok": False, "error": "Wrong username or password"}
            token = secrets.token_hex(24)
            state["tokens"][token] = key
            save()
            return {"ok": True, "token": token, "me": public(u)}
        if path == "info":
            return {"ok": True, "players_online": sum(1 for k in state["users"] if online(k)),
                    "queues": {tc: len(q) for tc, q in queue.items()}, "games": sum(1 for g in games.values() if g["status"] == "playing")}

        key = state["tokens"].get(str(data.get("token", "")))
        if not key or key not in state["users"]:
            return {"ok": False, "error": "Please sign in again", "auth": False}
        me = state["users"][key]
        last_seen[key] = time.time()

        if path == "me":
            return {"ok": True, "me": public(me)}
        if path == "status":
            my_q = next((tc for tc, q in queue.items() if any(e[0] == key for e in q)), None)
            waited = next((time.time() - e[1] for e in queue.get(my_q, []) if e[0] == key), 0) if my_q else 0
            return {"ok": True, "me": public(me), "queued": my_q, "waited": waited, "game": user_game.get(key),
                    "challenges": [dict(ch, from_name=state["users"][ch["from"]]["name"]) for ch in challenges.values() if ch["to"] == key],
                    "sent": [dict(ch, to_name=state["users"][ch["to"]]["name"]) for ch in challenges.values() if ch["from"] == key],
                    "friend_requests": len(me["incoming"]), "unread": dm_unread(key),
                    "is_owner": key == owner_key(),
                    "reports_new": (sum(1 for r in state.get("reports", []) if r["id"] > state.get("owner_seen", 0))
                                    if key == owner_key() else 0),
                    "last_report": (state["reports"][-1] if key == owner_key() and state.get("reports") else None), "players_online": sum(1 for k in state["users"] if online(k)),
                    "queues": {tc: len(q) for tc, q in queue.items()}}
        if path == "queue":
            tc = data.get("tc")
            if tc not in TIME_CONTROLS:
                return {"ok": False, "error": "Unknown time control"}
            if key in user_game:
                return {"ok": True, "game": user_game[key]}
            for q in queue.values():
                q[:] = [e for e in q if e[0] != key]
            queue[tc].append((key, time.time()))
            return {"ok": True}
        if path == "unqueue":
            for q in queue.values():
                q[:] = [e for e in q if e[0] != key]
            return {"ok": True}

        if path in ("game", "move", "resign", "draw", "chat"):
            g = games.get(str(data.get("id", "")))
            if not g or color_of(g, key) is None:
                return {"ok": False, "error": "Game not found"}
            side = color_of(g, key)
            if path == "move" and g["status"] == "playing":
                if ("white" if g["board"].turn == chess.WHITE else "black") != side:
                    return {"ok": False, "error": "Not your turn", "game": game_view(g, key)}
                try:
                    move = chess.Move.from_uci(str(data.get("uci", "")))
                except Exception:
                    move = None
                if move is None or move not in g["board"].legal_moves:
                    return {"ok": False, "error": "Illegal move", "game": game_view(g, key)}
                play_move(g, side, move)
            elif path == "resign" and g["status"] == "playing":
                if not g["moves"] or (len(g["moves"]) == 1 and side == "black"):
                    finish(g, "aborted", f"{me['name']} left before the game started")
                else:
                    finish(g, "0-1" if side == "white" else "1-0", "resignation")
            elif path == "draw" and g["status"] == "playing":
                action = data.get("action")
                if action == "offer" and g["draw_offer"] is None:
                    g["draw_offer"] = side
                elif action == "accept" and g["draw_offer"] and g["draw_offer"] != side:
                    finish(g, "1/2-1/2", "agreement")
                elif action == "decline" and g["draw_offer"] and g["draw_offer"] != side:
                    g["draw_offer"] = None
            elif path == "chat":
                text = " ".join("".join(ch for ch in str(data.get("text", "")) if ord(ch) <= 0xFFFF).split())[:200]
                if text and time.time() - last_chat.get(key, 0) > 0.4:
                    last_chat[key] = time.time()
                    g["chat_id"] += 1
                    g["chat"].append({"id": g["chat_id"], "from": side, "name": me["name"], "text": text, "t": time.time()})
                    del g["chat"][:-100]
            return {"ok": True, "game": game_view(g, key)}

        if path == "dm_list":
            unread = dm_unread(key)
            out = []
            for pair, th in state.get("dms", {}).items():
                people = pair.split("|")
                if key not in people or not th["messages"]:
                    continue
                other = people[0] if people[1] == key else people[1]
                u2 = state["users"].get(other)
                if not u2:
                    continue
                last = th["messages"][-1]
                out.append({"name": u2["name"], "online": online(other), "unread": unread.get(other, 0),
                            "avatar": media_url(u2, "avatar"), "t": last["t"],
                            "last": (last.get("text") or "📷 picture")[:60], "mine": last["from"] == key})
            out.sort(key=lambda r: -r["t"])
            return {"ok": True, "chats": out}
        if path == "dm_fetch":
            other = str(data.get("with", "")).strip().lower()
            if other not in state["users"]:
                return {"ok": False, "error": "Player not found"}
            th = dm_thread(key, other)
            since = int(data.get("since", 0) or 0)
            if th["messages"]:
                th["read"][key] = th["messages"][-1]["id"]
            u2 = state["users"][other]
            return {"ok": True, "messages": dm_view([m for m in th["messages"] if m["id"] > since]),
                    "me": me["name"], "them": u2["name"], "online": online(other), "avatar": media_url(u2, "avatar")}
        if path == "dm_send":
            other = str(data.get("to", "")).strip().lower()
            if other not in state["users"] or other == key:
                return {"ok": False, "error": "Player not found"}
            if key in state["users"][other].get("blocked", []) or other in me.get("blocked", []):
                return {"ok": False, "error": "You can't message this player"}
            if time.time() - _DM_LAST.get(key, 0) < 0.12:        # people really do send messages fast
                return {"ok": False, "error": "Slow down a little"}
            _DM_LAST[key] = time.time()
            text = " ".join(str(data.get("text", "")).split())[:500]
            image = None
            raw, ext = data.get("image_b64"), str(data.get("image_ext", ".png")).lower()
            if raw and ext in MEDIA_TYPES:
                import base64 as _b64
                blob = _b64.b64decode(raw)
                if len(blob) > 6_000_000:
                    return {"ok": False, "error": "That picture is too big"}
                os.makedirs(MEDIA_DIR, exist_ok=True)
                name = f"dm_{secrets.token_hex(6)}{ext}"
                with open(os.path.join(MEDIA_DIR, name), "wb") as fh:
                    fh.write(blob)
                image = f"/media/{name}"
            if not text and not image:
                return {"ok": False, "error": "Nothing to send"}
            msg = dm_add(key, other, text=text or None, image=image)
            return {"ok": True, "message": msg}
        if path == "profile":
            k = str(data.get("name", "")).strip().lower() or key
            if k not in state["users"]:
                return {"ok": False, "error": "Player not found"}
            prof = public_profile(k)
            prof["relation"] = ("me" if k == key else "friend" if k in me["friends"] else "sent" if k in me["outgoing"]
                                else "incoming" if k in me["incoming"] else "none")
            prof["blocked"] = k in me.get("blocked", [])
            return {"ok": True, "profile": prof}
        if path == "profile_update":
            for field, limit in (("bio", 190), ("bubble", 60)):
                if field in data:
                    me[field] = " ".join(str(data[field]).split())[:limit]
            if isinstance(data.get("banner_color"), str) and re.match(r"^#[0-9a-fA-F]{6}$", data["banner_color"]):
                me["banner_color"] = data["banner_color"]
            import base64 as _b64
            os.makedirs(MEDIA_DIR, exist_ok=True)
            for kind in ("avatar", "banner"):
                if data.get(f"{kind}_remove"):
                    me.pop(f"{kind}_file", None)
                raw = data.get(f"{kind}_b64")
                ext = str(data.get(f"{kind}_ext", ".png")).lower()
                if raw and ext in MEDIA_TYPES:
                    blob = _b64.b64decode(raw)
                    if len(blob) > 6_000_000:
                        return {"ok": False, "error": "That picture is too big"}
                    name = f"{key}_{kind}_{secrets.token_hex(4)}{ext}"
                    old = me.get(f"{kind}_file")
                    with open(os.path.join(MEDIA_DIR, name), "wb") as fh:
                        fh.write(blob)
                    me[f"{kind}_file"] = name
                    if old and os.path.exists(os.path.join(MEDIA_DIR, old)):
                        os.remove(os.path.join(MEDIA_DIR, old))
            save()
            return {"ok": True, "profile": public_profile(key)}
        if path == "report":
            about = str(data.get("name", "")).strip().lower()
            if about not in state["users"] or about == key:
                return {"ok": False, "error": "Player not found"}
            if time.time() - _DM_LAST.get("report:" + key, 0) < 20:
                return {"ok": False, "error": "You just sent a report - give it a moment"}
            _DM_LAST["report:" + key] = time.time()
            reason = " ".join(str(data.get("reason", "")).split())[:600]
            if len(reason) < 3:
                return {"ok": False, "error": "Please say what happened"}
            reports = state.setdefault("reports", [])
            reports.append({"id": len(reports) + 1, "from": me["name"], "about": state["users"][about]["name"],
                            "about_avatar": media_url(state["users"][about], "avatar"), "reason": reason,
                            "date": time.strftime("%Y-%m-%d %H:%M"), "t": time.time()})
            del reports[:-200]
            save()
            return {"ok": True}
        if path == "reports":
            if key != owner_key():
                return {"ok": False, "error": "Only the server owner can see reports"}
            reports = state.get("reports", [])
            if data.get("seen"):
                state["owner_seen"] = reports[-1]["id"] if reports else 0
                save()
            return {"ok": True, "reports": list(reversed(reports[-60:])), "seen": state.get("owner_seen", 0)}
        if path == "block":
            other_key = str(data.get("name", "")).strip().lower()
            if other_key not in state["users"] or other_key == key:
                return {"ok": False, "error": "Player not found"}
            blocked = me.setdefault("blocked", [])
            if data.get("action") == "unblock":
                if other_key in blocked:
                    blocked.remove(other_key)
            elif other_key not in blocked:
                blocked.append(other_key)
                for a, b in ((me, other_key), (state["users"][other_key], key)):   # blocking also unfriends
                    if b in a["friends"]:
                        a["friends"].remove(b)
            save()
            return {"ok": True, "blocked": other_key in blocked}
        if path == "friends":
            def card(k):
                u = state["users"].get(k)
                return {"name": u["name"], "elo": u["elo"], "online": online(k), "in_game": k in user_game,
                        "avatar": media_url(u, "avatar")} if u else None
            return {"ok": True, "friends": [c for c in map(card, me["friends"]) if c],
                    "incoming": [c for c in map(card, me["incoming"]) if c], "outgoing": [c for c in map(card, me["outgoing"]) if c]}
        if path == "friend":
            other_key = str(data.get("name", "")).strip().lower()
            other = state["users"].get(other_key)
            action = data.get("action")
            if not other or other_key == key:
                return {"ok": False, "error": "Player not found"}
            if action == "request":
                if key in other.get("blocked", []) or other_key in me.get("blocked", []):
                    return {"ok": False, "error": "You can't add this player"}
                if other_key in me["friends"]:
                    return {"ok": False, "error": "You're already friends"}
                if other_key in me["incoming"]:                      # they already asked you -> you're friends now
                    action = "accept"
                elif other_key not in me["outgoing"]:
                    me["outgoing"].append(other_key)
                    other["incoming"].append(key)
                    if other.get("bot"):      # bots answer like people do: minutes, sometimes days
                        state.setdefault("bot_accepts", {})[f"{other_key}|{key}"] = time.time() + random.uniform(300, 4 * 86400)
            if action == "accept" and other_key in me["incoming"]:
                me["incoming"].remove(other_key)
                if key in other["outgoing"]:
                    other["outgoing"].remove(key)
                for a, b in ((me, other_key), (other, key)):
                    if b not in a["friends"]:
                        a["friends"].append(b)
            elif action == "decline" and other_key in me["incoming"]:
                me["incoming"].remove(other_key)
                if key in other["outgoing"]:
                    other["outgoing"].remove(key)
            elif action == "cancel" and other_key in me["outgoing"]:
                me["outgoing"].remove(other_key)
                if key in other["incoming"]:
                    other["incoming"].remove(key)
            elif action == "remove":
                for a, b in ((me, other_key), (other, key)):
                    if b in a["friends"]:
                        a["friends"].remove(b)
            save()
            return {"ok": True}
        if path == "search":
            q = str(data.get("q", "")).strip().lower()
            if len(q) < 1:
                return {"ok": True, "results": []}
            out = []
            for k, u in state["users"].items():
                if q in k and k != key:
                    rel = "friend" if k in me["friends"] else "sent" if k in me["outgoing"] else "incoming" if k in me["incoming"] else "none"
                    out.append({"name": u["name"], "elo": u["elo"], "online": online(k), "relation": rel,
                                "avatar": media_url(u, "avatar")})
            out.sort(key=lambda r: (not r["name"].lower().startswith(q), r["name"].lower()))
            return {"ok": True, "results": out[:15]}
        if path == "games":
            who = str(data.get("name", "")).strip().lower() or key
            u2 = state["users"].get(who)
            if not u2:
                return {"ok": False, "error": "Player not found"}
            out = [dict(h, live=False) for h in u2["history"][:25] if h.get("moves")]
            gid = user_game.get(who)
            g = games.get(gid) if gid else None
            if g and g["status"] == "playing" and color_of(g, key) is None and len(g["moves"]) > 1:
                # someone else's live game can be watched; your own can't (that would be cheating)
                side = color_of(g, who)
                out.insert(0, {"id": g["id"], "moves": list(g["moves"]), "live": True, "color": side, "tc": g["tc"],
                               "opponent": state["users"][g["black" if side == "white" else "white"]]["name"],
                               "result": "playing", "reason": "in progress", "date": "now", "elo_change": 0})
            return {"ok": True, "games": out, "name": u2["name"]}
        if path == "history":
            return {"ok": True, "history": me["history"][:30]}
        if path == "challenge":
            action = data.get("action")
            if action == "send":
                other_key = str(data.get("name", "")).strip().lower()
                tc = data.get("tc")
                if other_key not in me["friends"]:
                    return {"ok": False, "error": "You can only challenge friends"}
                if key in state["users"][other_key].get("blocked", []):
                    return {"ok": False, "error": "You can't challenge this player"}
                if tc not in TIME_CONTROLS:
                    return {"ok": False, "error": "Pick a time control"}
                if other_key in user_game:
                    return {"ok": False, "error": "Your friend is playing a game right now"}
                if not online(other_key):
                    return {"ok": False, "error": "Your friend isn't online right now"}
                for cid, ch in list(challenges.items()):
                    if ch["from"] == key:
                        challenges.pop(cid)
                cid = secrets.token_hex(5)
                challenges[cid] = {"id": cid, "from": key, "to": other_key, "tc": tc, "created": time.time()}
                return {"ok": True, "id": cid}
            ch = challenges.get(str(data.get("id", "")))
            if not ch:
                return {"ok": False, "error": "That challenge has expired"}
            if action == "accept" and ch["to"] == key:
                challenges.pop(ch["id"], None)
                if ch["from"] in user_game or key in user_game:
                    return {"ok": False, "error": "One of you is already in a game"}
                return {"ok": True, "game": new_game(ch["from"], key, ch["tc"])}
            if action in ("decline", "cancel") and key in (ch["to"], ch["from"]):
                challenges.pop(ch["id"], None)
                return {"ok": True}
            return {"ok": False, "error": "Not allowed"}
        return {"ok": False, "error": "Unknown request"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route.startswith("/media/"):
            name = os.path.basename(route)
            path = os.path.join(MEDIA_DIR, name)
            ext = os.path.splitext(name)[1].lower()
            if ext in MEDIA_TYPES and os.path.isfile(path):
                with open(path, "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", MEDIA_TYPES[ext])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
                return
            return self._send(404, {"ok": False})
        if route.startswith("/api/"):
            return self._send(200, {"ok": True, "server": "Chess Studio", "version": 2})
        web_root = os.path.dirname(os.path.abspath(__file__))
        requested = "index.html" if route in ("", "/") else route.lstrip("/")
        # Only expose intended front-end assets; never serve server.py, data, or config files.
        if requested not in {"index.html", "styles.css", "app.js", "chess-engine.js"}:
            requested = "index.html"
        target = os.path.join(web_root, requested)
        try:
            with open(target, "rb") as fh:
                body = fh.read()
            content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith(("text/", "application/javascript")) else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self._send(500, {"ok": False, "error": "Could not read website file"})

    def do_POST(self):
        try:
            limit = 12_000_000 if self.path.endswith(("profile_update", "dm_send")) else 20000
            n = min(int(self.headers.get("Content-Length", 0)), limit)
            data = json.loads(self.rfile.read(n) or b"{}")
            path = self.path.strip("/").split("/")[-1]
            self._send(200, api(path, data if isinstance(data, dict) else {}))
        except Exception as exc:
            self._send(400, {"ok": False, "error": f"Bad request ({exc.__class__.__name__})"})

    def log_message(self, *args):
        pass


def main(port=None):
    load()
    port = int(port or os.environ.get("PORT", 8765))
    threading.Thread(target=tick, daemon=True).start()
    threading.Thread(target=_games_worker, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Chess Studio server running on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
