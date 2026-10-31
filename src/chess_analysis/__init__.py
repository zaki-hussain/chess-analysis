"""Download and analyse a chess.com player's games."""

from .dataset import load_games, load_moves, my_games

__all__ = ["load_games", "load_moves", "my_games"]
