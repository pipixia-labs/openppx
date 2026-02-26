"""Compatibility CLI module for legacy imports and `python -m openheron.cli`."""

from .app.cli import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
