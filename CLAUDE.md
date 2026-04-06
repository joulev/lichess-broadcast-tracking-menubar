# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A macOS menu bar app that displays live chess game info from Lichess broadcasts. Uses the same WebSocket protocol as the Lichess browser frontend for real-time updates, and a local Stockfish engine for position evaluation.

## Commands

```sh
# Setup
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# Run (with or without initial URL)
python lichess_menubar.py 'https://lichess.org/broadcast/.../roundId/gameId'
python lichess_menubar.py

# Build .app
pip install py2app
python setup.py py2app
# Output: dist/Lichess Tracker.app
```

No tests or linter configured.

## Architecture

Three modules:

- **`lichess_broadcast.py`** — Reusable broadcast client. Connects via WebSocket and maintains `GameState` for every game in a round. No UI dependencies.
- **`chess_eval.py`** — Stockfish wrapper. Auto-downloads the official binary to `~/Library/Caches/lichess-tracker/` on first run. Exposes `evaluate(board, time_limit, multi_pv)`.
- **`lichess_menubar.py`** — macOS menu bar UI. Subscribes to `BroadcastClient` callbacks and renders `GameState`. No streaming/parsing logic.

### Threading model

- **Main thread**: rumps event loop + 1-second clock ticker (`_tick`)
- **BroadcastClient thread**: WebSocket connection loop with auto-reconnect
- **Ping thread**: sends WebSocket keepalives every 2.5s
- **Eval worker thread**: single long-lived thread, signaled via `_eval_event` on new moves, runs Stockfish with 1s time limit. Discards stale results if position changed during analysis.
- **`self.lock`**: guards all mutable game state in both `BroadcastClient` and `LichessMenuBar`

### Data flow (lichess_broadcast.py)

1. `_fetch_initial_state()` fetches round JSON + round PGN via HTTP. PGN is replayed with python-chess for accurate SAN notation.
2. `_connect_ws()` connects to `wss://socket5.lichess.org/study/{roundId}/socket/v6`
3. WebSocket messages drive state updates:
   - `addNode` → new move (FEN, UCI, SAN, clock)
   - `clock` → live clock update with `relayClocks`
   - `chapters` → full snapshot of all games (FEN, players, clocks, status, thinkTime)
   - `setTags` → PGN header changes (result, player names)
   - `reload`/`resync` → refetch everything via HTTP
4. Callbacks notify the UI: `on_update`, `on_move`, `on_chapters`, `on_game_end`

### Data flow (lichess_menubar.py)

1. Callbacks from `BroadcastClient` update `self._state` and menu labels
2. `_tick()` decrements the active player's clock every second
3. Eval worker runs Stockfish for top 3 engine lines (1s time limit per position)
4. `_refresh_title()` builds: `[white_clock] move (eval) [black_clock ⏱think_time]`

### Thread safety

- AppKit UI updates (`_update_label`) dispatch to main thread via `AppHelper.callAfter()`
- `BroadcastClient` returns `GameState.copy()` from callbacks to avoid shared mutable state
- Eval worker checks if position is still current after engine finishes, discards stale results

### Custom menu rendering

Uses AppKit `NSView`-based menu labels (`_make_menu_label`, `_update_label`) instead of standard rumps menu items to get full-color text (rumps defaults to greyed-out non-interactive items).

### Lichess APIs used

- **WebSocket**: `wss://socket5.lichess.org/study/{roundId}/socket/v6` (same as browser, undocumented)
- **Round JSON**: `GET /api/broadcast/-/-/{roundId}` (initial state)
- **Study PGN**: `GET /api/study/{roundId}.pgn` (accurate move history for SAN)

### Stockfish binary

Auto-downloaded from GitHub releases on first run. Platform-specific:
- Apple Silicon: `stockfish-macos-m1-apple-silicon`
- x86_64: `stockfish-macos-x86-64-bmi2`

Override with `STOCKFISH_PATH` env var.
