# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A macOS menu bar app that displays live chess game info from Lichess broadcasts. Single-file Python application (~490 lines) using rumps for the menu bar UI.

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

Everything lives in `lichess_menubar.py`. The app is a single class `LichessMenuBar(rumps.App)`.

### Threading model

- **Main thread**: rumps event loop + 1-second clock ticker (`_tick`)
- **Background stream thread**: streams PGN from Lichess, one thread at a time. Graceful cancellation via a generation counter (`_gen`) — when a new stream starts, the old thread detects its generation is stale and exits.
- **Daemon threads**: short-lived threads for cloud eval fetches (`_fetch_eval`)
- **`self.lock`**: guards all mutable game state (board, clocks, players, eval)

### Data flow

1. `_stream_once()` connects to `lichess.org/api/stream/broadcast/round/{rid}.pgn`, buffers lines until a complete PGN game block arrives
2. `_ingest()` parses the PGN, extracts board state, player info, clocks, and detects new moves (triggers sound)
3. `_sync_clocks_once()` makes a one-time JSON API call to get precise live clock values on first load
4. `_fetch_eval()` queries Lichess cloud eval API with the current FEN for top 3 engine lines
5. `_refresh_title()` builds the menu bar string: `[white_clock] move (eval) [black_clock ⏱think_time]`

### Custom menu rendering

Uses AppKit `NSView`-based menu labels (`_make_menu_label`, `_update_label`) instead of standard rumps menu items to get full-color text (rumps defaults to greyed-out non-interactive items).

### Lichess APIs used

- **PGN stream**: `GET /api/stream/broadcast/round/{roundId}.pgn` (NDJSON-style streaming)
- **Round JSON**: `GET /api/broadcast/-/-/{roundId}` (clock sync)
- **Cloud eval**: `GET /api/cloud-eval?fen=...&multiPv=3`
