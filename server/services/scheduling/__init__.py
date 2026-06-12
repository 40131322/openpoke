"""Scheduling intent detection for incoming emails."""

from .detector import SchedulingContext, detect_scheduling_intent

__all__ = ["SchedulingContext", "detect_scheduling_intent"]
