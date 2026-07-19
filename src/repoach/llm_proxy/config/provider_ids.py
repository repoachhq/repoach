"""Stable import path for the supported provider-id set.

The single source is :data:`providers.catalog.PROVIDER_DESCRIPTORS`;
``SUPPORTED_PROVIDER_IDS`` is derived there as ``tuple(PROVIDER_DESCRIPTORS)``.
This module re-exports it so ``config`` and ``routing`` keep a leaf import
path that does not pull the provider registry (which imports ``Settings``).
"""

from __future__ import annotations

from repoach.llm_proxy.providers.catalog import SUPPORTED_PROVIDER_IDS

__all__ = ["SUPPORTED_PROVIDER_IDS"]
