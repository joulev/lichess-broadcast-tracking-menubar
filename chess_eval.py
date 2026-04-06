"""
Stockfish-based position evaluation.

Auto-downloads the official Stockfish binary on first use and caches it
in ~/Library/Caches/lichess-tracker/. No system install required.

Uses python-chess's UCI engine interface for multi-PV analysis.

Usage:
    results = evaluate(board, depth=18, multi_pv=3)
    # results: list of AnalysisResult with score, pv, depth
"""

from __future__ import annotations

import os
import platform
import stat
import sys
import tarfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.engine

# ---------------------------------------------------------------------------
# Stockfish binary management
# ---------------------------------------------------------------------------

_CACHE_DIR = Path.home() / "Library" / "Caches" / "lichess-tracker"

# Official Stockfish release URLs
_RELEASES = {
    ("Darwin", "arm64"): (
        "https://github.com/official-stockfish/Stockfish/releases/latest/download/"
        "stockfish-macos-m1-apple-silicon.tar",
        "stockfish",
    ),
    ("Darwin", "x86_64"): (
        "https://github.com/official-stockfish/Stockfish/releases/latest/download/"
        "stockfish-macos-x86-64-bmi2.tar",
        "stockfish",
    ),
}


def _get_platform_key() -> tuple[str, str]:
    system = platform.system()
    machine = platform.machine()
    # Normalize
    if machine in ("aarch64", "arm64"):
        machine = "arm64"
    return (system, machine)


def get_stockfish_path() -> Path:
    """Return path to the Stockfish binary, downloading if needed."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    binary = _CACHE_DIR / "stockfish"
    if binary.exists() and os.access(binary, os.X_OK):
        return binary

    key = _get_platform_key()
    if key not in _RELEASES:
        raise RuntimeError(
            f"No Stockfish binary available for {key[0]} {key[1]}. "
            "Install Stockfish manually and set STOCKFISH_PATH env var."
        )

    url, _name = _RELEASES[key]
    print(f"[eval] Downloading Stockfish from {url}...", file=sys.stderr, flush=True)

    tar_path = _CACHE_DIR / "stockfish.tar"
    urllib.request.urlretrieve(url, tar_path)

    # Extract — find the largest file (the binary)
    with tarfile.open(tar_path) as tf:
        binary_member = max(
            (m for m in tf.getmembers() if m.isfile()),
            key=lambda m: m.size,
        )
        binary_member.name = "stockfish"
        tf.extract(binary_member, _CACHE_DIR)

    tar_path.unlink()

    # Make executable
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    print(f"[eval] Stockfish cached at {binary}", file=sys.stderr, flush=True)
    return binary


# ---------------------------------------------------------------------------
# Engine wrapper
# ---------------------------------------------------------------------------

@dataclass
class PVInfo:
    score_cp: int | None = None
    score_mate: int | None = None
    pv: list[chess.Move] = field(default_factory=list)
    depth: int = 0


class StockfishEval:
    """Manages a Stockfish process for position evaluation."""

    def __init__(self):
        self._engine: chess.engine.SimpleEngine | None = None
        self._path: Path | None = None
        self.engine_name: str = "Stockfish"

    def _ensure_engine(self):
        if self._engine is not None:
            return
        # Allow override via env var
        env_path = os.environ.get("STOCKFISH_PATH")
        if env_path:
            self._path = Path(env_path)
        else:
            self._path = get_stockfish_path()
        self._engine = chess.engine.SimpleEngine.popen_uci(str(self._path))
        self.engine_name = self._engine.id.get("name", "Stockfish")
        # Low resource usage for a menu bar app
        self._engine.configure({"Threads": 1, "Hash": 16})

    def evaluate(self, board: chess.Board, depth: int | None = None,
                 time_limit: float | None = None,
                 multi_pv: int = 3) -> list[PVInfo]:
        """Evaluate a position. Returns up to multi_pv lines."""
        self._ensure_engine()

        if time_limit is not None:
            limit = chess.engine.Limit(time=time_limit)
        elif depth is not None:
            limit = chess.engine.Limit(depth=depth)
        else:
            limit = chess.engine.Limit(time=1.0)

        try:
            results = self._engine.analyse(board, limit, multipv=multi_pv)
        except chess.engine.EngineTerminatedError:
            self._engine = None
            self._ensure_engine()
            results = self._engine.analyse(board, limit, multipv=multi_pv)

        if not isinstance(results, list):
            results = [results]

        pvs: list[PVInfo] = []
        for info in results:
            pv_info = PVInfo(depth=info.get("depth", 0))
            score = info.get("score")
            if score:
                white_score = score.white()
                if white_score.is_mate():
                    pv_info.score_mate = white_score.mate()
                else:
                    pv_info.score_cp = white_score.score()
            pv_info.pv = list(info.get("pv", []))
            pvs.append(pv_info)

        return pvs

    def quit(self):
        if self._engine:
            try:
                self._engine.quit()
            except Exception:
                pass
            self._engine = None


# Module-level instance
_eval = StockfishEval()


def evaluate(board: chess.Board, depth: int | None = None,
             time_limit: float | None = None,
             multi_pv: int = 3) -> list[PVInfo]:
    """Module-level convenience function."""
    return _eval.evaluate(board, depth=depth, time_limit=time_limit, multi_pv=multi_pv)


def engine_name() -> str:
    """Return the engine name (e.g. 'Stockfish 18')."""
    return _eval.engine_name


def quit():
    """Clean up the engine process."""
    _eval.quit()
