#!/usr/bin/env python3
"""
Lichess Broadcast Menu Bar Tracker

Displays live chess game information in the macOS menu bar.
Format: [white_clock] move (eval) [black_clock]

Usage:
    source .venv/bin/activate
    python lichess_menubar.py [URL]

    URL: Lichess broadcast game URL (optional — can paste later via menu)
         e.g. https://lichess.org/broadcast/.../roundId/gameId
"""

import io
import re
import sys
import threading
import time

import chess
import chess.pgn
import requests
import rumps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_url(url: str):
    """Extract (round_id, game_id) from a Lichess broadcast game URL."""
    m = re.match(
        r"https?://lichess\.org/broadcast/[^/]+/[^/]+/([A-Za-z0-9]+)/([A-Za-z0-9]+)",
        url.strip(),
    )
    return (m.group(1), m.group(2)) if m else (None, None)


def fmt_clock(seconds: float) -> str:
    """Format seconds as H:MM:SS or M:SS."""
    s = max(0, int(seconds))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_eval(cp=None, mate=None):
    """Format centipawns or mate score. Returns None when nothing available."""
    if mate is not None:
        return f"#{mate}"
    if cp is not None:
        return f"{cp / 100:+.1f}"
    return None


RESULT_TOKENS = {"*", "1-0", "0-1", "1/2-1/2"}


def _make_menu_label(text):
    """Create a non-interactive menu item with a custom NSView (no greying)."""
    from AppKit import NSView, NSTextField, NSFont
    mi = rumps.MenuItem("")
    tf = NSTextField.labelWithString_(text)
    tf.setFont_(NSFont.menuFontOfSize_(0))
    tf.sizeToFit()
    f = tf.frame()
    h = f.size.height + 6
    w = f.size.width + 14 + 14  # left + right padding
    container = NSView.alloc().initWithFrame_(((0, 0), (w, h)))
    tf.setFrameOrigin_((14, 3))
    container.addSubview_(tf)
    mi._menuitem.setView_(container)
    return mi, tf, container


def _update_label(tf, container, text):
    """Update the text of a label view, resizing to fit."""
    tf.setStringValue_(text)
    tf.sizeToFit()
    f = tf.frame()
    container.setFrameSize_((f.size.width + 14 + 14, container.frame().size.height))


def _play_move_sound():
    """Play a short system sound to indicate a new move."""
    try:
        from AppKit import NSSound
        sound = NSSound.soundNamed_("Tink")
        if sound:
            sound.play()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class LichessMenuBar(rumps.App):

    def __init__(self):
        super().__init__("Lichess Tracker")
        self.title = "♟ Paste a game URL"

        # --- connection -------------------------------------------------
        self.round_id = None
        self.game_id = None
        self.game_url = ""

        # --- game state (guarded by self.lock) --------------------------
        self.lock = threading.Lock()
        self.board = chess.Board()
        self.white_name = self.black_name = "?"
        self.white_elo = self.black_elo = "?"
        self.white_clock = self.black_clock = 0.0
        self.move_number = 0
        self.move_san = ""
        self.result = "*"
        self.evaluation = None        # str like "+0.3" or "#5"
        self.eval_lines: list[str] = []
        self.half_moves = 0           # used to detect new moves
        self.clock_anchor = time.time()
        self.move_start = time.time() # when the current move started
        self.active = False           # True once we have data

        # --- streaming --------------------------------------------------
        self._stream_thread = None
        self._generation = 0  # incremented on each URL change

        # --- menu items (custom views — non-interactive, full color) --
        self._mi_white, self._lbl_white, self._ctr_white = _make_menu_label("White: -")
        self._mi_black, self._lbl_black, self._ctr_black = _make_menu_label("Black: -")
        self._mi_opening, self._lbl_opening, self._ctr_opening = _make_menu_label("Opening: -")
        self._mi_eval, self._lbl_eval, self._ctr_eval = _make_menu_label("Eval: -")
        self._mi_pv1, self._lbl_pv1, self._ctr_pv1 = _make_menu_label("  -")
        self._mi_pv2, self._lbl_pv2, self._ctr_pv2 = _make_menu_label("  -")
        self._mi_pv3, self._lbl_pv3, self._ctr_pv3 = _make_menu_label("  -")
        self._mi_change = rumps.MenuItem("Change Game…", callback=self._on_change)
        self.menu = [
            self._mi_white, self._mi_black, self._mi_opening, None,
            self._mi_eval, self._mi_pv1, self._mi_pv2, self._mi_pv3, None,
            self._mi_change, None,
        ]

    # ── URL handling ──────────────────────────────────────────────

    def set_url(self, url: str) -> bool:
        rid, gid = parse_url(url)
        if not rid or not gid:
            return False
        # Stop existing stream
        self._stop_stream()
        with self.lock:
            self.round_id, self.game_id = rid, gid
            self.game_url = url.strip()
            self.active = False
            self.half_moves = 0
            self.evaluation = None
            self.eval_lines = []
        self.title = "♟ Loading…"
        self._start_stream()
        return True

    def _on_change(self, _):
        w = rumps.Window(
            "Paste a Lichess broadcast game URL:",
            title="Change Game",
            default_text=self.game_url,
            ok="Watch",
            cancel="Cancel",
            dimensions=(420, 24),
        )
        r = w.run()
        if r.clicked and r.text.strip():
            if not self.set_url(r.text.strip()):
                rumps.alert(
                    "Invalid URL",
                    "Expected format:\nhttps://lichess.org/broadcast/…/roundId/gameId",
                )

    # ── Clock ticker (main thread, 1 s) ──────────────────────────

    @rumps.timer(1)
    def _tick(self, _):
        with self.lock:
            if self.active and self.result == "*" and self.move_san:
                if self.board.turn == chess.WHITE:
                    self.white_clock = max(0, self.white_clock - 1)
                else:
                    self.black_clock = max(0, self.black_clock - 1)
            self._refresh_title()

    # ── Streaming (background thread) ─────────────────────────────

    def _start_stream(self):
        self._generation += 1
        gen = self._generation
        # Capture IDs now so the thread uses a consistent snapshot
        rid = self.round_id
        gid = self.game_id
        self._stream_thread = threading.Thread(
            target=self._stream_loop, args=(gen, rid, gid), daemon=True,
        )
        self._stream_thread.start()

    def _stop_stream(self):
        self._generation += 1  # invalidates any running thread

    def _stream_loop(self, gen: int, rid: str, gid: str):
        """Connect to streaming endpoint, reconnect on errors."""
        while self._generation == gen:
            try:
                self._stream_once(gen, rid, gid)
            except Exception as e:
                print(f"[stream] {e}", file=sys.stderr, flush=True)
            # Wait before reconnecting (unless generation changed)
            for _ in range(30):  # 3 seconds in 0.1s steps
                if self._generation != gen:
                    return
                time.sleep(0.1)

    def _stream_once(self, gen: int, rid: str, gid: str):
        """Single streaming session."""
        url = f"https://lichess.org/api/stream/broadcast/round/{rid}.pgn"
        # Read timeout 30s — Lichess sends keep-alive every ~7s,
        # so 30s with no data means the connection is dead.
        with requests.get(url, stream=True, timeout=(10, 30)) as resp:
            resp.raise_for_status()
            resp.encoding = "utf-8"
            buf = ""
            for line in resp.iter_lines(decode_unicode=True):
                if self._generation != gen:
                    return
                if line:
                    buf += line + "\n"
                else:
                    # Empty line — check if we have a complete game
                    stripped = buf.strip()
                    if stripped and any(
                        stripped.endswith(tok) for tok in RESULT_TOKENS
                    ):
                        self._process_pgn_block(buf, gen, gid)
                        buf = ""
                    else:
                        buf += "\n"

    def _process_pgn_block(self, text: str, gen: int, gid: str):
        """Parse a PGN block and ingest if it matches our game."""
        if self._generation != gen:
            return
        sio = io.StringIO(text)
        while True:
            game = chess.pgn.read_game(sio)
            if game is None:
                break
            gurl = game.headers.get("GameURL", "") or game.headers.get("Site", "")
            if gid and gid in gurl:
                self._ingest(game, gen)
                # Fetch eval in a short-lived thread (non-blocking)
                if self._generation == gen:
                    threading.Thread(target=self._fetch_eval, daemon=True).start()
                return

    # ── PGN ingestion ─────────────────────────────────────────────

    def _ingest(self, game, gen: int):
        board = game.board()
        w_clk = b_clk = 0.0
        last_san = ""
        pgn_eval = None
        hm = 0

        for node in game.mainline():
            san = board.san(node.move)
            board.push(node.move)
            last_san = san
            hm += 1

            clk = node.clock()
            if clk is not None:
                if board.turn == chess.BLACK:   # White just moved
                    w_clk = clk
                else:                           # Black just moved
                    b_clk = clk

            ev = node.eval()
            if ev is not None:
                s = ev.white()
                if s.is_mate():
                    pgn_eval = f"#{s.mate()}"
                else:
                    pgn_eval = f"{s.score() / 100:+.1f}"

        opening = game.headers.get("Opening", "")

        with self.lock:
            if self._generation != gen:
                return  # stale thread, discard
            self.white_name = game.headers.get("White", "?")
            self.black_name = game.headers.get("Black", "?")
            self.white_elo = game.headers.get("WhiteElo", "?")
            self.black_elo = game.headers.get("BlackElo", "?")
            self.result = game.headers.get("Result", "*")
            self.board = board
            self.move_san = last_san
            self._opening = opening

            # Move number
            if board.turn == chess.BLACK:   # White moved last
                self.move_number = board.fullmove_number
            else:                           # Black moved last
                self.move_number = board.fullmove_number - 1

            # Reset clocks when a new move arrives
            first_load = self.half_moves == 0
            new_move = hm != self.half_moves
            if new_move:
                self.white_clock = w_clk
                self.black_clock = b_clk
                self.move_start = time.time()
                self.half_moves = hm

            if pgn_eval is not None:
                self.evaluation = pgn_eval

            self.active = True

        # Update dropdown items
        _update_label(self._lbl_white, self._ctr_white, f"⬜ {self.white_name} ({self.white_elo})")
        _update_label(self._lbl_black, self._ctr_black, f"⬛ {self.black_name} ({self.black_elo})")
        _update_label(self._lbl_opening, self._ctr_opening, f"Opening: {opening}" if opening else "Opening: -")

        # On first load, sync clocks from JSON to get live values
        if first_load:
            self._sync_clocks_once(gen)
        elif new_move:
            _play_move_sound()

    def _sync_clocks_once(self, gen: int):
        """One-time clock sync from JSON API to get live values."""
        try:
            rid = self.round_id
            gid = self.game_id
            if self._generation != gen:
                return
            resp = requests.get(
                f"https://lichess.org/api/broadcast/-/-/{rid}", timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            for game in data.get("games", []):
                if game.get("id") != gid:
                    continue
                players = game.get("players", [])
                if len(players) < 2:
                    return
                w_clock = players[0].get("clock", 0) / 100
                b_clock = players[1].get("clock", 0) / 100
                think_time = game.get("thinkTime") or 0
                fen = game.get("fen", "")
                status = game.get("status", "*")
                if status == "*":
                    if " b " in fen:
                        b_clock = max(0, b_clock - think_time)
                    else:
                        w_clock = max(0, w_clock - think_time)
                with self.lock:
                    if self._generation != gen:
                        return
                    self.white_clock = w_clock
                    self.black_clock = b_clock
                    self.move_start = time.time() - think_time
                return
        except Exception as e:
            print(f"[clock-sync] {e}", file=sys.stderr, flush=True)

    # ── Cloud eval ────────────────────────────────────────────────

    def _fetch_eval(self):
        with self.lock:
            if self.result != "*":
                return
            fen = self.board.fen()
            board_copy = self.board.copy()

        try:
            r = requests.get(
                "https://lichess.org/api/cloud-eval",
                params={"fen": fen, "multiPv": 3},
                timeout=5,
            )
            if r.status_code != 200:
                return
            data = r.json()
            pvs = data.get("pvs", [])
            if not pvs:
                return

            main_ev = fmt_eval(pvs[0].get("cp"), pvs[0].get("mate"))
            lines = []
            for pv in pvs[:3]:
                ev = fmt_eval(pv.get("cp"), pv.get("mate")) or "?"
                san = self._uci_to_san(board_copy, pv.get("moves", ""))
                depth = data.get("depth", "?")
                lines.append(f"({ev}) {san}  [d{depth}]")

            with self.lock:
                if main_ev:
                    self.evaluation = main_ev
                self.eval_lines = lines

            # Update menu
            _update_label(self._lbl_eval, self._ctr_eval, f"Eval: {main_ev or '-'}")
            lcs = [
                (self._lbl_pv1, self._ctr_pv1),
                (self._lbl_pv2, self._ctr_pv2),
                (self._lbl_pv3, self._ctr_pv3),
            ]
            for i, (lbl, ctr) in enumerate(lcs):
                _update_label(lbl, ctr, lines[i] if i < len(lines) else "-")

        except Exception:
            pass

    @staticmethod
    def _uci_to_san(board: chess.Board, uci_str: str) -> str:
        b = board.copy()
        parts = []
        for i, tok in enumerate(uci_str.split()[:8]):
            try:
                mv = chess.Move.from_uci(tok)
                san = b.san(mv)
                if b.turn == chess.WHITE:
                    parts.append(f"{b.fullmove_number}. {san}")
                elif i == 0:
                    parts.append(f"{b.fullmove_number}… {san}")
                else:
                    parts.append(san)
                b.push(mv)
            except Exception:
                break
        return " ".join(parts)

    # ── Display ───────────────────────────────────────────────────

    def _refresh_title(self):
        """Build the menu-bar string. Must hold self.lock."""
        if not self.active or not self.move_san:
            return

        wc = fmt_clock(self.white_clock)
        bc = fmt_clock(self.black_clock)

        # "35.Rd2" or "35...Kf7"
        if self.board.turn == chess.WHITE:   # Black moved last
            ms = f"{self.move_number}...{self.move_san}"
        else:                                # White moved last
            ms = f"{self.move_number}.{self.move_san}"

        ev = f" ({self.evaluation})" if self.evaluation else ""

        if self.result != "*":
            self.title = f"[{wc}] {ms} {self.result}{ev} [{bc}]"
        else:
            think = fmt_clock(time.time() - self.move_start)
            # Put timer inside the active player's clock
            if self.board.turn == chess.WHITE:
                self.title = f"[{wc} ⏱{think}] {ms}{ev} [{bc}]"
            else:
                self.title = f"[{wc}] {ms}{ev} [{bc} ⏱{think}]"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    app = LichessMenuBar()
    if len(sys.argv) > 1:
        if not app.set_url(sys.argv[1]):
            print(f"Invalid URL: {sys.argv[1]}", file=sys.stderr)
            sys.exit(1)
    app.run()


if __name__ == "__main__":
    main()
