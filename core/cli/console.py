"""Shared Rich Console instance.

All Live / Progress / RichHandler / ad-hoc prints in the pipeline must
write through this single Console so cursor tracking stays consistent.
See Rich docs on Live + logging integration.
"""
from rich.console import Console

CONSOLE = Console()
