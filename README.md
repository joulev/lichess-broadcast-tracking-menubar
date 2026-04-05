# Lichess Broadcast Tracking Menu Bar

A macOS menu bar app that displays live chess game information from [Lichess broadcasts](https://lichess.org/broadcast). Watch the FIDE Candidates Tournament, or any chess tournaments, from your menu bar.

<img width="466" height="281" alt="image" src="https://github.com/user-attachments/assets/5a6df5c7-469f-468e-9958-ed4291429fd4" />

## Features

- Live clocks with per-second countdown
- Current move with move number
- Engine evaluation from Lichess cloud eval
- Think time for the active player
- Top 3 engine lines in the dropdown
- Player names, ratings, and opening name
- Sound notification on new moves
- Paste any broadcast game URL to switch games

## Menu bar format

```
[white_clock] move (eval) [black_clock ⏱think_time]
```

## Setup

Requires Python 3.10+.

```sh
git clone https://github.com/joulev/lichess-broadcast-tracking-menubar.git
cd lichess-broadcast-tracking-menubar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

### Run from terminal

```sh
source .venv/bin/activate
python lichess_menubar.py 'https://lichess.org/broadcast/..../roundId/gameId'
```

Or launch without a URL and paste one via the menu:

```sh
python lichess_menubar.py
```

### Build as .app

```sh
source .venv/bin/activate
pip install py2app
python setup.py py2app
```

The app is created at `dist/Lichess Tracker.app`. Double-click to launch, then paste a broadcast game URL via the dropdown menu.

To install to Applications:

```sh
cp -R "dist/Lichess Tracker.app" /Applications/
```
