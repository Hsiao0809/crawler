#!/usr/bin/env python3
"""Convenience entry point. Use `python main.py track @evachien.chien`."""
import sys

from threads_tracker.cli import main

if __name__ == "__main__":
    sys.exit(main())
