#!/usr/bin/env python3
"""Compatibility entry point for the current named behavior mutation."""
from legacy_mutations import main

if __name__ == "__main__":
    raise SystemExit(main('appset'))
