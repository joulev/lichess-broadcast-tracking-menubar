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

import re
import sys
import threading
import time
from datetime import datetime

import chess
import rumps

from chess_eval import evaluate as engine_eval, engine_name, PVInfo
from lichess_broadcast import BroadcastClient, GameState


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


def fmt_clock(seconds: float | None) -> str:
    """Format seconds as H:MM:SS or M:SS."""
    if seconds is None:
        return "–:––"
    s = max(0, int(seconds))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_score(pv: PVInfo) -> str:
    """Format a PVInfo score as a string."""
    if pv.score_mate is not None:
        return f"#{pv.score_mate}"
    if pv.score_cp is not None:
        return f"{pv.score_cp / 100:+.1f}"
    return "?"


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
    """Update the text of a label view, resizing to fit. Thread-safe."""
    from PyObjCTools import AppHelper
    def _do():
        tf.setStringValue_(text)
        tf.sizeToFit()
        f = tf.frame()
        container.setFrameSize_((f.size.width + 14 + 14, container.frame().size.height))
    AppHelper.callAfter(_do)


def _play_move_sound():
    """Play a short system sound to indicate a new move."""
    try:
        from AppKit import NSSound
        sound = NSSound.soundNamed_("Tink")
        if sound:
            sound.play()
    except Exception:
        pass


def _play_game_end_sound():
    """Play a distinct system sound to indicate a game has finished."""
    try:
        from AppKit import NSSound
        sound = NSSound.soundNamed_("Glass")
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
        self._client: BroadcastClient | None = None

        # --- game state (guarded by self.lock) --------------------------
        self.lock = threading.Lock()
        self._state: GameState | None = None
        self.evaluation = None        # str like "+0.3" or "#5"
        self.eval_lines: list[str] = []
        self._first_move_seen = False
        self._last_eval_fen = None       # avoid re-fetching same position
        self._all_games: dict[str, GameState] = {}  # all games in round

        # --- eval worker thread -------------------------------------------
        self._eval_event = threading.Event()
        self._eval_thread = threading.Thread(target=self._eval_worker, daemon=True)
        self._eval_thread.start()

        # --- menu items (custom views — non-interactive, full color) --
        self._mi_white, self._lbl_white, self._ctr_white = _make_menu_label("White: -")
        self._mi_black, self._lbl_black, self._ctr_black = _make_menu_label("Black: -")
        self._mi_eval, self._lbl_eval, self._ctr_eval = _make_menu_label("Eval: -")
        self._mi_pv1, self._lbl_pv1, self._ctr_pv1 = _make_menu_label("  -")
        self._mi_pv2, self._lbl_pv2, self._ctr_pv2 = _make_menu_label("  -")
        self._mi_pv3, self._lbl_pv3, self._ctr_pv3 = _make_menu_label("  -")
        self._mi_open = rumps.MenuItem("View on Lichess", callback=self._on_open)
        self._mi_change = rumps.MenuItem("Change Game…")
        self._mi_paste_url = rumps.MenuItem("Paste Game URL…", callback=self._on_paste_url)
        self._mi_change["Paste Game URL…"] = self._mi_paste_url
        self._game_items = []  # prevent GC of dynamic menu items
        self.menu = [
            self._mi_white, self._mi_black, None,
            self._mi_eval, self._mi_pv1, self._mi_pv2, self._mi_pv3, None,
            self._mi_open, self._mi_change, None,
        ]

    # ── URL handling ──────────────────────────────────────────────

    def set_url(self, url: str) -> bool:
        rid, gid = parse_url(url)
        if not rid or not gid:
            return False
        self._stop_client()
        with self.lock:
            self.round_id, self.game_id = rid, gid
            self.game_url = url.strip()
            self._state = None
            self.evaluation = None
            self.eval_lines = []
            self._first_move_seen = False
            self._last_eval_fen = None
            self._all_games = {}
        self.title = "♟ Loading…"
        _update_label(self._lbl_eval, self._ctr_eval, "Eval: -")
        _update_label(self._lbl_pv1, self._ctr_pv1, "  -")
        _update_label(self._lbl_pv2, self._ctr_pv2, "  -")
        _update_label(self._lbl_pv3, self._ctr_pv3, "  -")
        self._rebuild_game_submenu()
        self._start_client()
        return True

    def _on_open(self, _):
        if self.game_url:
            import webbrowser
            webbrowser.open(self.game_url)

    def _on_paste_url(self, _):
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

    def _on_select_game(self, game_id):
        """Switch to a different game in the same round without reconnecting."""
        if game_id == self.game_id:
            return

        # Get state from client (outside our lock to avoid deadlock)
        state = self._client.get_game(game_id) if self._client else None

        with self.lock:
            self.game_id = game_id
            self.game_url = f"https://lichess.org/broadcast/-/-/{self.round_id}/{game_id}"
            self.evaluation = None
            self.eval_lines = []
            self._last_eval_fen = None
            self._first_move_seen = True
            if state:
                self._state = state

        _update_label(self._lbl_eval, self._ctr_eval, "Eval: -")
        _update_label(self._lbl_pv1, self._ctr_pv1, "  -")
        _update_label(self._lbl_pv2, self._ctr_pv2, "  -")
        _update_label(self._lbl_pv3, self._ctr_pv3, "  -")

        if state:
            self._update_menu_labels(state)
            self._eval_event.set()

        self._rebuild_game_submenu()

    # ── Client lifecycle ──────────────────────────────────────────

    def _start_client(self):
        client = BroadcastClient(self.round_id)
        client.on_update = self._on_game_update
        client.on_move = self._on_move
        client.on_chapters = self._on_chapters_update
        client.on_game_end = self._on_game_end
        self._client = client
        client.start()

    def _stop_client(self):
        if self._client:
            self._client.stop()
            self._client = None

    # ── Callbacks from BroadcastClient ────────────────────────────

    def _on_game_update(self, game_id: str, state: GameState):
        """Called when any game state changes (clock, tags, etc.)."""
        if game_id != self.game_id:
            return
        with self.lock:
            self._state = state
        self._update_menu_labels(state)

    def _on_move(self, game_id: str, state: GameState):
        """Called when a new move is played."""
        if game_id != self.game_id:
            return
        with self.lock:
            if self._first_move_seen:
                _play_move_sound()
            else:
                self._first_move_seen = True
        # Signal eval worker to process new position
        self._eval_event.set()

    def _on_game_end(self, game_id: str, state: GameState):
        """Called when a game finishes."""
        with self.lock:
            self._all_games[game_id] = state
        if game_id == self.game_id:
            _play_game_end_sound()
        self._rebuild_game_submenu()

    def _on_chapters_update(self, games: dict[str, GameState]):
        """Called when the chapter list is updated (all games)."""
        with self.lock:
            self._all_games = games

        gid = self.game_id
        if gid and gid in games:
            state = games[gid]
            with self.lock:
                self._state = state
                if not self._first_move_seen and state.ply > 0:
                    self._first_move_seen = True
            self._update_menu_labels(state)
            # Signal eval worker
            self._eval_event.set()

        self._rebuild_game_submenu()

    # ── Clock ticker (main thread, 1 s) ──────────────────────────

    @rumps.timer(1)
    def _tick(self, _):
        with self.lock:
            state = self._state
            if not state:
                return

            # Tick clocks locally
            has_moves = state.move_san or state.last_move
            if state.is_ongoing() and has_moves:
                if state.board.turn == chess.WHITE:
                    if state.white_clock is not None:
                        state.white_clock = max(0, state.white_clock - 1)
                else:
                    if state.black_clock is not None:
                        state.black_clock = max(0, state.black_clock - 1)

            self._refresh_title(state)

    # ── Menu label updates ────────────────────────────────────────

    def _update_menu_labels(self, state: GameState):
        """Update dropdown menu labels from game state."""
        wr = f" ({state.white.rating})" if state.white.rating else ""
        br = f" ({state.black.rating})" if state.black.rating else ""
        wt = f" {state.white.title}" if state.white.title else ""
        bt = f" {state.black.title}" if state.black.title else ""
        _update_label(self._lbl_white, self._ctr_white, f"⬜{wt} {state.white.name}{wr}")
        _update_label(self._lbl_black, self._ctr_black, f"⬛{bt} {state.black.name}{br}")

    # ── Game submenu ─────────────────────────────────────────────

    def _rebuild_game_submenu(self):
        """Rebuild the game list in the Change Game submenu."""
        from AppKit import NSMenuItem
        from PyObjCTools import AppHelper

        with self.lock:
            games = dict(self._all_games)
            current_gid = self.game_id

        def _do():
            ns_sub = self._mi_change._menuitem.submenu()
            if not ns_sub:
                return

            # Keep only "Paste Game URL…" (index 0)
            while ns_sub.numberOfItems() > 1:
                ns_sub.removeItemAtIndex_(ns_sub.numberOfItems() - 1)

            if not games:
                self._game_items = []
                return

            # Add separator
            ns_sub.addItem_(NSMenuItem.separatorItem())

            # Add a rumps MenuItem for each game
            items = []
            for gid, state in games.items():
                wt = f"{state.white.title} " if state.white.title else ""
                bt = f"{state.black.title} " if state.black.title else ""
                label = f"{wt}{state.white.name} — {bt}{state.black.name}"
                if not state.is_ongoing():
                    label += f"  {state.status}"
                mi = rumps.MenuItem(label, callback=lambda _, g=gid: self._on_select_game(g))
                if gid == current_gid:
                    mi._menuitem.setState_(1)  # checkmark
                ns_sub.addItem_(mi._menuitem)
                items.append(mi)

            self._game_items = items  # prevent GC

        AppHelper.callAfter(_do)

    # ── Stockfish eval ───────────────────────────────────────────

    def _eval_worker(self):
        """Single background thread that evaluates the latest position."""
        while True:
            self._eval_event.wait()
            self._eval_event.clear()

            # Grab the latest position
            with self.lock:
                state = self._state
                if not state or not state.is_ongoing() or state.ply == 0:
                    continue
                fen = state.fen
                if fen == self._last_eval_fen:
                    continue
                board_copy = state.board.copy()

            try:
                pvs = engine_eval(board_copy, time_limit=1.0, multi_pv=3)
                if not pvs:
                    continue

                # Discard if a newer position arrived during analysis
                with self.lock:
                    if self._state and self._state.fen != fen:
                        continue

                main_ev = fmt_score(pvs[0])
                lines = []
                for pv in pvs:
                    ev = fmt_score(pv)
                    san = self._pv_to_san(board_copy, pv.pv[:8])
                    lines.append(f"({ev}) {san}  [d{pv.depth}]")

                with self.lock:
                    self._last_eval_fen = fen
                    self.evaluation = main_ev
                    self.eval_lines = lines

                depth = pvs[0].depth
                _update_label(self._lbl_eval, self._ctr_eval,
                              f"Eval: {main_ev} — {engine_name()}, d{depth}")
                lcs = [
                    (self._lbl_pv1, self._ctr_pv1),
                    (self._lbl_pv2, self._ctr_pv2),
                    (self._lbl_pv3, self._ctr_pv3),
                ]
                for i, (lbl, ctr) in enumerate(lcs):
                    _update_label(lbl, ctr, lines[i] if i < len(lines) else "-")

            except Exception as e:
                print(f"[eval] {e}", file=sys.stderr, flush=True)

    @staticmethod
    def _pv_to_san(board: chess.Board, pv: list[chess.Move]) -> str:
        b = board.copy()
        parts = []
        for i, mv in enumerate(pv):
            try:
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

    def _refresh_title(self, state: GameState):
        """Build the menu-bar string. Must hold self.lock."""
        if not state:
            return

        wc = fmt_clock(state.white_clock)
        bc = fmt_clock(state.black_clock)

        # Determine the move text: prefer SAN, fall back to UCI
        move_text = state.move_san or state.last_move

        if not move_text:
            if state.start_time:
                start_str = datetime.fromtimestamp(state.start_time).strftime("%-H:%M")
                self.title = f"[{wc}] Start at {start_str} [{bc}]"
            else:
                self.title = f"[{wc}] Not started [{bc}]"
            return

        # "35.Rd2" or "35...Kf7"
        if state.board.turn == chess.WHITE:   # Black moved last
            ms = f"{state.move_number}...{move_text}"
        else:                                 # White moved last
            ms = f"{state.move_number}.{move_text}"

        ev = f" ({self.evaluation})" if self.evaluation else ""

        if not state.is_ongoing():
            self.title = f"[{wc}] {ms} {state.status}{ev} [{bc}]"
        else:
            think = fmt_clock(time.time() - state.move_start)
            if state.board.turn == chess.WHITE:
                self.title = f"[{wc} ⏱{think}] {ms}{ev} [{bc}]"
            else:
                self.title = f"[{wc}] {ms}{ev} [{bc} ⏱{think}]"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Hide Dock icon when running as a script (py2app uses LSUIElement instead)
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    app = LichessMenuBar()
    if len(sys.argv) > 1:
        if not app.set_url(sys.argv[1]):
            print(f"Invalid URL: {sys.argv[1]}", file=sys.stderr)
            sys.exit(1)
    app.run()


if __name__ == "__main__":
    main()
