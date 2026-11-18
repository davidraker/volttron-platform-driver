"""Poll schedulers of the Platform Driver.

``platform_driver.poll_scheduler`` is the module name configurations give as ``poll_scheduler_module``; the classes
live in :mod:`.base`, :mod:`.static_cyclic` and :mod:`.serial` and are re-exported here.
"""
from .base import (PollCycle, PollScheduler, PollSet, PollSetRegistry, SlotPolls, create_poll_schedulers, ms_timedelta,
                   to_ms)
from .static_cyclic import StaticCyclicPollScheduler
from .serial import SerialPollScheduler

__all__ = ['PollCycle', 'PollScheduler', 'PollSet', 'PollSetRegistry', 'SerialPollScheduler', 'SlotPolls',
           'StaticCyclicPollScheduler', 'create_poll_schedulers', 'ms_timedelta', 'to_ms']
