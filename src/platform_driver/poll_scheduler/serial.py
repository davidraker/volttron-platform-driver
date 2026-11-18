# -*- coding: utf-8 -*- {{{
# ===----------------------------------------------------------------------===
#
#                 Installable Component of Eclipse VOLTTRON
#
# ===----------------------------------------------------------------------===
#
# Copyright 2022 Battelle Memorial Institute
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# ===----------------------------------------------------------------------===
# }}}
"""The serial poll scheduler: for a group whose remotes share a slow medium (a serial bus, a slow gateway).

Polls are never overlapped: one greenlet per group polls the (remote, interval) whose turn is due and waits for the
answer (or its timeout) before the next. Each (remote, interval) keeps its own due time, advanced by its interval after
every poll, so a fast remote is never held for a slow one beyond its turn in the queue, and a remote that cannot keep
up skips periods rather than piling them up. When a full pass takes longer than the shortest interval the scheduler
warns once per plan. Due times survive a re-plan, so adding a remote does not restart the others' timing.

Options, under the group's ``poll_scheduler_configs``: ``poll_timeout`` (seconds a poll may take before the
scheduler moves on, default 30) and ``idle_sleep`` (the longest the loop sleeps when nothing is due, default 1.0).
"""
from __future__ import annotations

import logging

from datetime import datetime, timedelta

import gevent

from .base import DriverAgent, PollScheduler, PollSet, get_aware_utc_now, ms_timedelta

_log = logging.getLogger(__name__)

Key = tuple[DriverAgent, int]           # (remote, interval in ms)


class SerialPollScheduler(PollScheduler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        options = dict(self.group_config.poll_scheduler_configs or {})
        self.poll_timeout: float = float(options.get('poll_timeout', 30.0))
        self.idle_sleep: float = float(options.get('idle_sleep', 1.0))
        self._sets: dict[Key, PollSet] = {}
        self._due: dict[Key, datetime] = {}
        self._greenlet = None
        self._wake = gevent.event.Event()
        self._stopping = False
        self._pass_warned = False
        self._skipped_warned: set[Key] = set()

    # ---- planning --------------------------------------------------------------------------------------------------
    def _prepare_to_schedule(self) -> dict[Key, PollSet]:
        short, long = self.poll_sets()
        return {(remote, interval): poll_set
                for sets in (short, long) for remote, by_interval in sets.items() for interval, poll_set in by_interval.items()}

    def _first_due(self, now: datetime, interval_ms: int, position: int) -> datetime:
        """A new entry's first turn: the next multiple of its interval from midnight, plus the group's start offset and
        a small stagger by position so new entries do not all land on one instant."""
        interval = ms_timedelta(interval_ms)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        next_boundary = midnight + ((now - midnight) // interval + 1) * interval
        return next_boundary + self.group_config.start_offset + position * ms_timedelta(self.group_config.minimum_polling_interval_ms)

    def _schedule_polling(self, plan: dict[Key, PollSet]):
        now = get_aware_utc_now()
        self._sets = plan
        self._due = {key: self._due.get(key) or self._first_due(now, key[1], i) for i, key in enumerate(plan)}
        self._pass_warned = False
        self._skipped_warned.clear()
        if plan:
            self.start_all_datetime = max(self.start_all_datetime, max(self._due.values()) + ms_timedelta(max(k[1] for k in plan)))
            if self._greenlet is None or self._greenlet.dead:
                self._stopping = False
                self._greenlet = self.data_model.agent.core.spawn(self._run)
            self._wake.set()
        else:
            self._stop_loop()

    def cancel(self):
        """Stop the loop; due times are kept so a following ``schedule()`` keeps every surviving entry's timing."""
        super().cancel()
        self._stop_loop()

    def _stop_loop(self):
        self._stopping = True
        self._wake.set()
        greenlet, self._greenlet = self._greenlet, None
        join = getattr(greenlet, 'join', None)
        if greenlet is not None and not greenlet.dead and greenlet is not gevent.getcurrent() and callable(join):
            join(timeout=self.poll_timeout + 1)

    # ---- the loop ----------------------------------------------------------------------------------------------------
    def _run(self):
        while not self._stopping:
            wait = self.step()
            if self._stopping:
                break
            if wait > 0:
                self._wake.wait(min(wait, self.idle_sleep))
                self._wake.clear()

    def step(self, now: datetime | None = None) -> float:
        """Poll the entry whose turn is due (the earliest due first). Returns seconds until the next turn, 0 when
        another entry is already due."""
        now = now or get_aware_utc_now()
        if not self._due:
            return self.idle_sleep
        key, due = min(self._due.items(), key=lambda item: item[1])
        if due > now:
            return (due - now).total_seconds()
        remote, interval_ms = key
        poll_set = self._sets.get(key)
        if poll_set:
            self._poll(remote, poll_set)
        finished = get_aware_utc_now() if now is None else max(now, get_aware_utc_now()) if self._greenlet else now
        interval = ms_timedelta(interval_ms)
        next_due = due + interval
        if next_due <= finished:                        # this entry fell behind: skip to its next future turn
            missed = (finished - due) // interval
            next_due = due + (missed + 1) * interval
            if key not in self._skipped_warned:
                self._skipped_warned.add(key)
                _log.warning(f"Polling group {self.group!r} (serial): {remote.unique_id} cannot be polled every"
                             f" {interval_ms / 1000:g} s on this medium; {missed} poll(s) skipped. Poll it less often,"
                             f" or give its remotes a group of their own.")
        self._due[key] = next_due
        return 0.0

    def _poll(self, remote: DriverAgent, poll_set: PollSet):
        try:
            with gevent.Timeout(self.poll_timeout):
                remote.poll_data(poll_set)
        except gevent.Timeout:
            _log.warning(f"Polling group {self.group!r} (serial): {remote.unique_id} did not answer within"
                         f" {self.poll_timeout:g} s (poll_timeout under 'groups.{self.group}.poll_scheduler_configs');"
                         f" moving on to the next remote.")
        except Exception as e:
            _log.error(f"Polling group {self.group!r} (serial): polling {remote.unique_id} raised {e!r}.")

    # ---- reporting ------------------------------------------------------------------------------------------------
    def get_schedule(self, full_topics: bool = False) -> dict:
        """``{interval: {remote: {'points': [...], 'next': time}}}``: the serial queue has no fixed slots."""
        schedule: dict = {}
        for (remote, interval_ms), poll_set in self._sets.items():
            names = sorted(poll_set.points) if full_topics else sorted(p.split('/')[-1] for p in poll_set.points)
            due = self._due.get((remote, interval_ms))
            schedule.setdefault(str(ms_timedelta(interval_ms)), {})[str(remote.unique_id)] = {
                'points': names, 'next': due.isoformat() if due else None}
        return schedule
