"""
Lichess Broadcast Client

Connects to a Lichess broadcast round via WebSocket (the same protocol the
browser uses) and maintains live game state for every game in the round.

Usage:
    client = BroadcastClient(round_id)
    client.on_update = my_callback        # called with (game_id, GameState)
    client.on_chapters = my_chapters_cb   # called with dict[str, GameState]
    client.start()                        # non-blocking, spawns background thread
    ...
    client.stop()
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field

import chess
import chess.pgn
import requests

try:
    from websockets.sync.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
except ImportError:
    raise ImportError("Install websockets: pip install websockets")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class PlayerInfo:
    name: str = "?"
    title: str | None = None
    rating: int | None = None
    fide_id: int | None = None
    fed: str | None = None
    clock: float | None = None  # seconds


@dataclass
class GameState:
    """Live state of a single game in a broadcast round."""
    game_id: str = ""
    name: str = ""                         # e.g. "Carlsen - Nepomniachtchi"
    fen: str = chess.STARTING_FEN
    board: chess.Board = field(default_factory=chess.Board)
    white: PlayerInfo = field(default_factory=PlayerInfo)
    black: PlayerInfo = field(default_factory=PlayerInfo)
    white_clock: float | None = None       # seconds
    black_clock: float | None = None       # seconds
    last_move: str | None = None           # UCI
    move_san: str | None = None            # SAN of the last move
    move_number: int = 0
    ply: int = 0
    status: str = "*"                      # "*", "1-0", "0-1", "½-½"
    think_time: int | None = None          # seconds since last move
    check: str | None = None               # "+" or "#"
    orientation: str = "white"
    opening: str | None = None
    start_time: float | None = None    # round start epoch seconds

    # Move-start timestamp for local think-time tracking
    move_start: float = 0.0

    def is_ongoing(self) -> bool:
        return self.status == "*"

    def turn(self) -> chess.Color:
        return self.board.turn

    def copy(self) -> GameState:
        """Shallow copy with a fresh board object."""
        gs = GameState(
            game_id=self.game_id, name=self.name, fen=self.fen,
            board=self.board.copy(), white=self.white, black=self.black,
            white_clock=self.white_clock, black_clock=self.black_clock,
            last_move=self.last_move, move_san=self.move_san,
            move_number=self.move_number, ply=self.ply, status=self.status,
            think_time=self.think_time, check=self.check,
            orientation=self.orientation, opening=self.opening,
            move_start=self.move_start,
            start_time=self.start_time,
        )
        return gs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SOCKET_DOMAINS = [
    "socket5.lichess.org",
    "socket.lichess.org",
]


def _generate_sri() -> str:
    """Generate a 12-char random socket request identifier."""
    return base64.urlsafe_b64encode(os.urandom(9)).decode().rstrip("=")


def _parse_chapter_preview(ch: dict) -> GameState:
    """Parse a ChapterPreview from the `chapters` WebSocket message."""
    gs = GameState()
    gs.game_id = ch.get("id", "")
    gs.name = ch.get("name", "")

    fen = ch.get("fen")
    if fen:
        gs.fen = fen
        try:
            gs.board = chess.Board(fen)
        except ValueError:
            gs.board = chess.Board()
    else:
        gs.fen = chess.STARTING_FEN
        gs.board = chess.Board()

    players = ch.get("players")
    if players and len(players) >= 2:
        wp, bp = players[0], players[1]
        gs.white = PlayerInfo(
            name=wp.get("name", "?"), title=wp.get("title"),
            rating=wp.get("rating"), fide_id=wp.get("fideId"),
            fed=wp.get("fed"),
            clock=wp.get("clock", 0) / 100 if "clock" in wp else None,
        )
        gs.black = PlayerInfo(
            name=bp.get("name", "?"), title=bp.get("title"),
            rating=bp.get("rating"), fide_id=bp.get("fideId"),
            fed=bp.get("fed"),
            clock=bp.get("clock", 0) / 100 if "clock" in bp else None,
        )
        if gs.white.clock is not None:
            gs.white_clock = gs.white.clock
        if gs.black.clock is not None:
            gs.black_clock = gs.black.clock

    gs.last_move = ch.get("lastMove")
    gs.status = ch.get("status", "*")
    gs.think_time = ch.get("thinkTime")
    gs.check = ch.get("check")
    gs.orientation = ch.get("orientation", "white")

    # Derive SAN from UCI + board position
    if gs.last_move and len(gs.last_move) >= 4:
        gs.move_san = _approximate_san(gs.board, gs.last_move)

    # Derive move_number and ply from FEN
    if gs.board.move_stack:
        gs.ply = len(gs.board.move_stack)
    else:
        # From FEN fullmove number
        gs.move_number = gs.board.fullmove_number
        # Approximate ply from fullmove
        if gs.board.turn == chess.WHITE:
            gs.ply = (gs.board.fullmove_number - 1) * 2
        else:
            gs.ply = (gs.board.fullmove_number - 1) * 2 + 1

    # Move start for think-time tracking
    if gs.think_time is not None and gs.status == "*":
        gs.move_start = time.time() - gs.think_time
    else:
        gs.move_start = time.time()

    return gs


def _approximate_san(board_after: chess.Board, uci: str) -> str | None:
    """Derive approximate SAN from the position after a move and its UCI string.

    This won't include capture notation for pieces or disambiguation,
    but handles piece symbols, pawn captures, castling, promotion, and check.
    Proper SAN arrives later via addNode WebSocket messages.
    """
    try:
        move = chess.Move.from_uci(uci)
        piece = board_after.piece_at(move.to_square)
        if piece is None:
            return None

        dest = chess.square_name(move.to_square)

        # Castling
        if piece.piece_type == chess.KING:
            file_diff = chess.square_file(move.to_square) - chess.square_file(move.from_square)
            if abs(file_diff) > 1:
                return "O-O" if file_diff > 0 else "O-O-O"

        # Promotion
        promo = f"={chess.piece_symbol(move.promotion).upper()}" if move.promotion else ""

        # Check/checkmate
        check = ""
        if board_after.is_checkmate():
            check = "#"
        elif board_after.is_check():
            check = "+"

        # Pawn moves
        if piece.piece_type == chess.PAWN:
            from_file = chess.FILE_NAMES[chess.square_file(move.from_square)]
            to_file = chess.FILE_NAMES[chess.square_file(move.to_square)]
            if from_file != to_file:
                return f"{from_file}x{dest}{promo}{check}"
            return f"{dest}{promo}{check}"

        # Piece moves
        symbol = chess.piece_symbol(piece.piece_type).upper()
        return f"{symbol}{dest}{check}"
    except Exception:
        return None


def _apply_clock_to_state(gs: GameState, white_centis, black_centis):
    """Update clocks on a GameState from centisecond values."""
    if white_centis is not None:
        gs.white_clock = white_centis / 100
        gs.white.clock = gs.white_clock
    if black_centis is not None:
        gs.black_clock = black_centis / 100
        gs.black.clock = gs.black_clock


# ---------------------------------------------------------------------------
# BroadcastClient
# ---------------------------------------------------------------------------

class BroadcastClient:
    """
    Connects to a Lichess broadcast round and maintains live GameState
    for every game (chapter) in the round.

    The client uses the same WebSocket protocol as the Lichess browser
    frontend: /study/{roundId}/socket/v6
    """

    def __init__(self, round_id: str):
        self.round_id = round_id

        # Game states keyed by chapter/game ID
        self.games: dict[str, GameState] = {}
        self.lock = threading.Lock()

        # Callbacks
        self.on_update: callable | None = None       # (game_id, GameState) -> None
        self.on_chapters: callable | None = None      # (dict[str, GameState]) -> None
        self.on_move: callable | None = None          # (game_id, GameState) -> None
        self.on_game_end: callable | None = None      # (game_id, GameState) -> None
        self.on_connected: callable | None = None     # () -> None
        self.on_disconnected: callable | None = None  # () -> None

        # Internal
        self._version: int | None = None
        self._sri = _generate_sri()
        self._thread: threading.Thread | None = None
        self._running = False
        self._ws = None

    # -- public API ---------------------------------------------------------

    def start(self):
        """Start the client in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the client."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def get_game(self, game_id: str) -> GameState | None:
        """Get a copy of the current state of a game."""
        with self.lock:
            gs = self.games.get(game_id)
            return gs.copy() if gs else None

    def get_all_games(self) -> dict[str, GameState]:
        """Get copies of all game states."""
        with self.lock:
            return {gid: gs.copy() for gid, gs in self.games.items()}

    # -- connection loop ----------------------------------------------------

    def _run_loop(self):
        """Reconnection loop — runs until stop() is called."""
        while self._running:
            try:
                self._fetch_initial_state()
                self._connect_ws()
            except Exception as e:
                print(f"[broadcast] {e}", file=sys.stderr, flush=True)
            if self._running:
                if self.on_disconnected:
                    try:
                        self.on_disconnected()
                    except Exception:
                        pass
                # Wait before reconnecting
                for _ in range(30):  # 3 seconds in 0.1s steps
                    if not self._running:
                        return
                    time.sleep(0.1)

    def _fetch_initial_state(self):
        """Fetch the broadcast round data via HTTP to seed game states."""
        # 1. Round JSON for metadata (players, clocks, status, thinkTime)
        resp = requests.get(
            f"https://lichess.org/api/broadcast/-/-/{self.round_id}",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        # Round start time
        starts_at = data.get("round", {}).get("startsAt")
        round_start = starts_at / 1000 if starts_at else None

        # 2. Round PGN for accurate move history and SAN
        pgn_san_map: dict[str, tuple[str, chess.Board, int]] = {}  # gid -> (last_san, board, ply)
        try:
            pgn_resp = requests.get(
                f"https://lichess.org/api/study/{self.round_id}.pgn",
                timeout=10,
            )
            pgn_resp.raise_for_status()
            pgn_text = pgn_resp.text
            sio = io.StringIO(pgn_text)
            while True:
                pgn_game = chess.pgn.read_game(sio)
                if pgn_game is None:
                    break
                # Match to game ID via Site/GameURL header
                site = pgn_game.headers.get("Site", "") or pgn_game.headers.get("GameURL", "")
                gid = None
                for g in data.get("games", []):
                    if g.get("id") and g["id"] in site:
                        gid = g["id"]
                        break
                if not gid:
                    continue
                # Replay moves to get last SAN and full board
                board = pgn_game.board()
                last_san = ""
                ply = 0
                for node in pgn_game.mainline():
                    last_san = board.san(node.move)
                    board.push(node.move)
                    ply += 1
                pgn_san_map[gid] = (last_san, board, ply)
        except Exception as e:
            print(f"[broadcast] pgn fetch: {e}", file=sys.stderr, flush=True)

        # 3. Merge JSON metadata with PGN move data
        with self.lock:
            for game in data.get("games", []):
                gid = game.get("id", "")
                if not gid:
                    continue
                gs = GameState()
                gs.game_id = gid

                # Players
                players = game.get("players", [])
                if len(players) >= 2:
                    wp, bp = players[0], players[1]
                    gs.white = PlayerInfo(
                        name=wp.get("name", "?"), title=wp.get("title"),
                        rating=wp.get("rating"), fide_id=wp.get("fideId"),
                        fed=wp.get("fed"),
                        clock=wp.get("clock", 0) / 100 if "clock" in wp else None,
                    )
                    gs.black = PlayerInfo(
                        name=bp.get("name", "?"), title=bp.get("title"),
                        rating=bp.get("rating"), fide_id=bp.get("fideId"),
                        fed=bp.get("fed"),
                        clock=bp.get("clock", 0) / 100 if "clock" in bp else None,
                    )
                    if gs.white.clock is not None:
                        gs.white_clock = gs.white.clock
                    if gs.black.clock is not None:
                        gs.black_clock = gs.black.clock

                gs.status = game.get("status", "*")
                gs.last_move = game.get("lastMove")
                gs.think_time = game.get("thinkTime")
                gs.name = game.get("name", "")
                gs.start_time = round_start

                # Use PGN data for accurate board + SAN
                if gid in pgn_san_map:
                    last_san, board, ply = pgn_san_map[gid]
                    gs.board = board
                    gs.fen = board.fen()
                    gs.move_san = last_san
                    gs.ply = ply
                    gs.move_number = (ply + 1) // 2
                else:
                    # Fallback to FEN from JSON
                    fen = game.get("fen")
                    if fen:
                        gs.fen = fen
                        try:
                            gs.board = chess.Board(fen)
                        except ValueError:
                            pass
                    gs.move_number = gs.board.fullmove_number
                    if gs.board.turn == chess.WHITE:
                        gs.ply = (gs.board.fullmove_number - 1) * 2
                    else:
                        gs.ply = (gs.board.fullmove_number - 1) * 2 + 1

                # Think time tracking
                if gs.think_time is not None and gs.status == "*":
                    gs.move_start = time.time() - gs.think_time
                    # Adjust active side's clock for elapsed think time
                    if gs.board.turn == chess.WHITE and gs.white_clock is not None:
                        gs.white_clock = max(0, gs.white_clock - gs.think_time)
                    elif gs.board.turn == chess.BLACK and gs.black_clock is not None:
                        gs.black_clock = max(0, gs.black_clock - gs.think_time)
                else:
                    gs.move_start = time.time()

                self.games[gid] = gs

        # Notify
        if self.on_chapters:
            try:
                self.on_chapters(self.get_all_games())
            except Exception:
                pass

    def _connect_ws(self):
        """Connect to the WebSocket and process messages."""
        params = f"sri={self._sri}"
        if self._version is not None:
            params += f"&v={self._version}"

        domain = _SOCKET_DOMAINS[0]
        url = f"wss://{domain}/study/{self.round_id}/socket/v6?{params}"

        self._ws = ws_connect(
            url,
            additional_headers={"Origin": "https://lichess.org"},
            open_timeout=10,
            close_timeout=5,
        )

        try:
            if self.on_connected:
                try:
                    self.on_connected()
                except Exception:
                    pass

            # Start ping thread
            ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
            ping_thread.start()

            while self._running:
                try:
                    raw = self._ws.recv(timeout=15)
                except TimeoutError:
                    # No message in 15s — connection likely dead
                    break

                if not self._running:
                    break

                self._handle_raw(raw)

        except ConnectionClosed:
            pass
        finally:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _ping_loop(self):
        """Send pings to keep the WebSocket alive."""
        while self._running and self._ws:
            try:
                self._ws.send("p")
            except Exception:
                break
            # Ping every 2.5 seconds
            for _ in range(25):
                if not self._running or not self._ws:
                    return
                time.sleep(0.1)

    # -- message handling ---------------------------------------------------

    def _handle_raw(self, raw: str):
        """Parse and dispatch a raw WebSocket message."""
        if raw == "0":
            # Pong — ignore
            return

        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        t = msg.get("t")

        # Handle batch messages
        if t == "batch":
            for sub in msg.get("d", []):
                self._handle_message(sub)
            return

        self._handle_message(msg)

    def _handle_message(self, msg: dict):
        """Handle a single parsed message."""
        # Track version
        v = msg.get("v")
        if v is not None:
            if self._version is not None and v > self._version + 1:
                # Version gap — need to reload
                print(f"[broadcast] version gap: have {self._version}, got {v}",
                      file=sys.stderr, flush=True)
                self._version = v
                self._do_reload()
                return
            self._version = v

        t = msg.get("t")
        d = msg.get("d")

        if t == "addNode":
            self._handle_add_node(d)
        elif t == "clock":
            self._handle_clock(d)
        elif t == "chapters":
            self._handle_chapters(d)
        elif t == "setTags":
            self._handle_set_tags(d)
        elif t == "reload":
            self._do_reload()
        elif t == "resync":
            self._do_reload()
        elif t == "n":
            pass  # lobby pong, ignore
        elif t == "crowd":
            pass  # spectator info, ignore
        elif t == "relaySync":
            pass  # relay sync status, ignore
        elif t == "relayLog":
            pass  # relay log, ignore

    def _handle_add_node(self, d: dict):
        """Handle an addNode message — a new move was played."""
        if not d:
            return

        pos = d.get("p", {})
        chapter_id = pos.get("chapterId", "")
        node = d.get("n", {})

        if not chapter_id or not node:
            return

        fen = node.get("fen")
        uci = node.get("uci")
        san = node.get("san")
        ply = node.get("ply")
        clock_centis = node.get("clock")  # centiseconds

        is_new_move = False

        with self.lock:
            gs = self.games.get(chapter_id)
            if not gs:
                # Unknown chapter — create a minimal state
                gs = GameState(game_id=chapter_id)
                self.games[chapter_id] = gs

            if fen:
                gs.fen = fen
                try:
                    gs.board = chess.Board(fen)
                except ValueError:
                    pass

            if uci:
                gs.last_move = uci
            if san:
                gs.move_san = san

            if ply is not None:
                is_new_move = ply > gs.ply
                gs.ply = ply
                # Derive move number: ply 1 = move 1 (white), ply 2 = move 1 (black), etc.
                gs.move_number = (ply + 1) // 2

            if clock_centis is not None:
                # Clock value is for the side that just moved
                if gs.board.turn == chess.WHITE:
                    # Black just moved (it's now White's turn)
                    gs.black_clock = clock_centis / 100
                    gs.black.clock = gs.black_clock
                else:
                    # White just moved
                    gs.white_clock = clock_centis / 100
                    gs.white.clock = gs.white_clock

            gs.move_start = time.time()
            gs.think_time = 0

            # Check status
            if gs.board.is_checkmate():
                gs.check = "#"
            elif gs.board.is_check():
                gs.check = "+"
            else:
                gs.check = None

        # Notify
        if is_new_move and self.on_move:
            try:
                self.on_move(chapter_id, self.get_game(chapter_id))
            except Exception:
                pass
        if self.on_update:
            try:
                self.on_update(chapter_id, self.get_game(chapter_id))
            except Exception:
                pass

    def _handle_clock(self, d: dict):
        """Handle a clock message — live clock update."""
        if not d:
            return

        pos = d.get("p", {})
        chapter_id = pos.get("chapterId", "")
        relay_clocks = d.get("relayClocks")  # [whiteCentis, blackCentis]

        if not chapter_id:
            return

        with self.lock:
            gs = self.games.get(chapter_id)
            if not gs:
                return

            if relay_clocks and len(relay_clocks) >= 2:
                _apply_clock_to_state(gs, relay_clocks[0], relay_clocks[1])
            else:
                # Single clock value for the node at pos
                c = d.get("c")
                if c is not None:
                    # The clock is for the side that just moved at this position
                    if gs.board.turn == chess.WHITE:
                        gs.black_clock = c / 100
                        gs.black.clock = gs.black_clock
                    else:
                        gs.white_clock = c / 100
                        gs.white.clock = gs.white_clock

        if self.on_update:
            try:
                self.on_update(chapter_id, self.get_game(chapter_id))
            except Exception:
                pass

    def _handle_chapters(self, d):
        """Handle a chapters message — full chapter list with previews."""
        if not d or not isinstance(d, list):
            return

        with self.lock:
            for ch in d:
                gid = ch.get("id", "")
                if not gid:
                    continue
                new_gs = _parse_chapter_preview(ch)
                # Preserve fields that the preview doesn't carry
                old_gs = self.games.get(gid)
                if old_gs:
                    if new_gs.move_san is None and old_gs.move_san:
                        new_gs.move_san = old_gs.move_san
                    if new_gs.opening is None and old_gs.opening:
                        new_gs.opening = old_gs.opening
                    if new_gs.start_time is None and old_gs.start_time:
                        new_gs.start_time = old_gs.start_time
                    # Detect newly ended game
                    if self.on_game_end and old_gs.is_ongoing() and not new_gs.is_ongoing():
                        try:
                            self.on_game_end(gid, new_gs.copy())
                        except Exception:
                            pass
                self.games[gid] = new_gs

        if self.on_chapters:
            try:
                self.on_chapters(self.get_all_games())
            except Exception:
                pass

    def _handle_set_tags(self, d: dict):
        """Handle a setTags message — PGN headers changed."""
        if not d:
            return

        chapter_id = d.get("chapterId", "")
        tags = d.get("tags", [])

        if not chapter_id:
            return

        with self.lock:
            gs = self.games.get(chapter_id)
            if not gs:
                return

            for tag in tags:
                if isinstance(tag, list) and len(tag) >= 2:
                    key, value = tag[0], tag[1]
                    if key == "Result":
                        old_status = gs.status
                        gs.status = value.replace("1/2-1/2", "½-½")
                        if old_status == "*" and gs.status != "*":
                            if self.on_game_end:
                                try:
                                    self.on_game_end(chapter_id, gs.copy())
                                except Exception:
                                    pass
                    elif key == "White":
                        gs.white.name = value
                    elif key == "Black":
                        gs.black.name = value
                    elif key == "WhiteElo":
                        try:
                            gs.white.rating = int(value)
                        except ValueError:
                            pass
                    elif key == "BlackElo":
                        try:
                            gs.black.rating = int(value)
                        except ValueError:
                            pass
                    elif key == "Opening":
                        gs.opening = value

        if self.on_update:
            try:
                self.on_update(chapter_id, self.get_game(chapter_id))
            except Exception:
                pass

    def _do_reload(self):
        """Reload full state from HTTP (triggered by reload/resync/version gap)."""
        try:
            self._fetch_initial_state()
        except Exception as e:
            print(f"[broadcast] reload failed: {e}", file=sys.stderr, flush=True)
