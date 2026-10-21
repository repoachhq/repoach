"""Repoach — the self-forging software factory."""

from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("repoach")
except _metadata.PackageNotFoundError:
    __version__ = "0+unknown"
