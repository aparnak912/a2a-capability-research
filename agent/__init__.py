"""Shared agent behavior. This package does not import A2A or a cloud SDK."""

from agent.work import WorkEvent, iter_work

__all__ = ["WorkEvent", "iter_work"]
