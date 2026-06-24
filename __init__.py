"""Spike-chat platform plugin for Hermes Agent."""

from .adapter import SpikeChatAdapter, register  # noqa: F401

__all__ = ["SpikeChatAdapter", "register"]
