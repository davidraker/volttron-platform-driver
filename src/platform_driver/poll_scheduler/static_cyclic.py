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
"""The cyclic poll scheduler: every (remote, interval) of a group is laid out once into the slots of a hyperperiod,
anchored to midnight, and polled from a chain of scheduled callbacks that walks those slots forever.

Planning runs only on a structural change (``schedule()``); each poll is O(1). Intervals are quantised to the group's
``minimum_polling_interval`` (the unit of the hyperperiod arithmetic) with a warning naming the setting when that
changes an interval. Remotes of a sequential group are spread across the interval with one stagger term,
``position * step``, where ``step = max(minimum_polling_interval, interval / n)``; when ``n * step`` does not fit the
interval the planner warns and clamps the step so no slot falls outside the interval. Remotes of a parallel group
(``parallel_subgroups``) each get their own chain, offset ``position * minimum_polling_interval`` from each other.
"""
from __future__ import annotations

import logging

from collections import defaultdict
from datetime import datetime, timedelta
from math import gcd, lcm

from .base import (DriverAgent, PollCycle, PollScheduler, PollSet, SlotPolls, get_aware_utc_now, ms_timedelta)

_log = logging.getLogger(__name__)

#: A hyperperiod longer than this many times the longest interval in it is reported: near-coprime intervals make them.
HYPERPERIOD_WARNING_FACTOR = 10


class StaticCyclicPollScheduler(PollScheduler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cycles: dict[tuple[int, timedelta], PollCycle] = {}        # (plan index, hyperperiod) -> cycle
        self._remote_order: list[DriverAgent] = []                     # first seen first; never reordered (B7)
        self._quantisation_warned: set[int] = set()

    # ---- planning --------------------------------------------------------------------------------------------------
    @property
    def unit_ms(self) -> int:
        return max(1, self.group_config.minimum_polling_interval_ms)

    def _position(self, remote: DriverAgent) -> int:
        """A remote's stable position in this group: appended when first seen, so existing remotes keep their offsets
        when another arrives."""
        try:
            return self._remote_order.index(remote)
        except ValueError:
            self._remote_order.append(remote)
            return len(self._remote_order) - 1

    def _quantise(self, interval_ms: int) -> int:
        """The interval as a whole number of the group's minimum polling intervals (at least one)."""
        unit = self.unit_ms
        quantised = max(1, int(interval_ms / unit + 0.5)) * unit            # halves round up
        if quantised != interval_ms and interval_ms not in self._quantisation_warned:
            self._quantisation_warned.add(interval_ms)
            _log.warning(f"Polling group {self.group!r}: a {interval_ms / 1000:g} s polling interval is not a multiple"
                         f" of the group's minimum_polling_interval ({unit / 1000:g} s), so it is polled every"
                         f" {quantised / 1000:g} s. To keep the interval exact, set minimum_polling_interval under"
                         f" 'groups.{self.group}' in the Platform Driver configuration to a value that divides it.")
        return quantised

    @staticmethod
    def separate_coprimes(counts: list[int]) -> list[list[int]]:
        """Partition interval counts so that no set holds two coprime members (which would multiply the hyperperiod).
        Ones go with everything."""
        separated: list[list[int]] = []
        unseparated = sorted(set(counts), reverse=True)
        while unseparated:
            first = unseparated.pop(0)
            together, apart = [first], []
            for count in unseparated:
                if gcd(first, count) == 1 and first != 1 and count != 1:
                    apart.append(count)
                else:
                    together.append(count)
            unseparated = apart
            separated.append(together)
        return separated

    def _find_slots(self, input_dict: dict[int, dict[DriverAgent, list[PollSet]]], parallel_remote: DriverAgent | None
                    ) -> dict[timedelta, dict[timedelta, SlotPolls]]:
        """Lay out ``{interval_ms: {remote: [PollSet]}}`` into ``{hyperperiod: {offset: SlotPolls}}``."""
        unit = self.unit_ms
        quantised: dict[int, int] = {interval: self._quantise(interval) for interval in input_dict}
        # Several raw intervals may quantise to one.
        by_interval: dict[int, dict[DriverAgent, list[PollSet]]] = defaultdict(lambda: defaultdict(list))
        for raw, remotes in input_dict.items():
            for remote, sets in remotes.items():
                by_interval[quantised[raw]][remote].extend(sets)
        plan: dict[timedelta, dict[timedelta, SlotPolls]] = defaultdict(lambda: defaultdict(SlotPolls))
        for counts in self.separate_coprimes([interval // unit for interval in by_interval]):
            hyperperiod = lcm(*counts) * unit
            longest = max(counts) * unit
            if hyperperiod > HYPERPERIOD_WARNING_FACTOR * longest:
                _log.warning(f"Polling group {self.group!r}: intervals {[c * unit / 1000 for c in counts]} s share a"
                             f" {hyperperiod / 1000:g} s hyperperiod. Intervals that are multiples of each other, or of"
                             f" minimum_polling_interval under 'groups.{self.group}', keep the schedule short.")
            for count in counts:
                interval = count * unit
                remotes = sorted(by_interval[interval], key=self._position)
                offsets = self._offsets(interval, remotes, parallel_remote)
                for remote, offset in zip(remotes, offsets):
                    for i in range(hyperperiod // interval):
                        slot = plan[ms_timedelta(hyperperiod)][ms_timedelta(interval * i + offset)]
                        for poll_set in by_interval[interval][remote]:
                            slot.add(poll_set)
        return {hp: dict(sorted(slots.items())) for hp, slots in plan.items()}

    def _offsets(self, interval: int, remotes: list[DriverAgent], parallel_remote: DriverAgent | None) -> list[int]:
        """Where each remote's polls sit within the interval, in ms. One stagger term (B7)."""
        unit = self.unit_ms
        if parallel_remote is not None:                 # this plan holds one remote; its chain is offset from the others'
            return [self._position(parallel_remote) * unit] * len(remotes)
        n = len(remotes)
        step = max(unit, interval // n)
        if n * step > interval:                         # cannot keep minimum_polling_interval between remotes (F8)
            step = interval // n
            _log.warning(f"Polling group {self.group!r}: {n} remotes polled every {interval / 1000:g} s cannot be"
                         f" spaced minimum_polling_interval ({unit / 1000:g} s) apart; they are spaced"
                         f" {step / 1000:g} s apart instead. Poll less often, lower minimum_polling_interval under"
                         f" 'groups.{self.group}', or use the SerialPollScheduler for a slow shared medium.")
        return [position * step for position in range(n)]

    def _prepare_to_schedule(self) -> list[dict[timedelta, dict[timedelta, SlotPolls]]]:
        """One plan for a sequential group; one plan per remote for a parallel group; the long-poll sets always get
        plans of their own so their long intervals never stretch the regular hyperperiod."""
        plans = []
        for group_poll_sets in self.poll_sets():
            if not group_poll_sets:
                continue
            if self.group_config.parallel_subgroups:
                for remote in sorted(group_poll_sets, key=self._position):
                    input_dict: dict[int, dict[DriverAgent, list[PollSet]]] = defaultdict(lambda: defaultdict(list))
                    for interval, poll_set in group_poll_sets[remote].items():
                        input_dict[interval][remote].append(poll_set)
                    plans.append(self._find_slots(input_dict, remote))
            else:
                input_dict = defaultdict(lambda: defaultdict(list))
                for remote in sorted(group_poll_sets, key=self._position):
                    for interval, poll_set in group_poll_sets[remote].items():
                        input_dict[interval][remote].append(poll_set)
                plans.append(self._find_slots(input_dict, None))
        return plans

    # ---- scheduling -----------------------------------------------------------------------------------------------
    @staticmethod
    def cycle_origin(now: datetime, hyperperiod: timedelta) -> datetime:
        """The start of the hyperperiod ``now`` falls in, counted from midnight: every slot time is
        midnight + k * interval + offset, so a re-plan keeps every surviving interval's phase."""
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight + ((now - midnight) // hyperperiod) * hyperperiod

    def _schedule_polling(self, plans: list[dict[timedelta, dict[timedelta, SlotPolls]]]):
        self.cycles = {}
        now = get_aware_utc_now()
        for index, plan in enumerate(plans):
            for hyperperiod, slots in plan.items():
                origin = self.cycle_origin(now, hyperperiod) + self.group_config.start_offset
                cycle = PollCycle(hyperperiod, origin, slots)
                cycle.seek(now)
                start, polls = next(cycle)
                self.start_all_datetime = max(self.start_all_datetime, origin + hyperperiod)
                key = (index, hyperperiod)
                self.cycles[key] = cycle
                _log.info(f'Scheduled polling for {self.group}--{hyperperiod} starts at {start.time()} (datetime: {start})')
                self.pollers[key] = self._schedule(start, self._operate_polling, key, cycle, polls)

    def _operate_polling(self, key, cycle: PollCycle, slot_polls: SlotPolls):
        """One slot fires: schedule the next (always, an empty slot included) and dispatch this one."""
        if key not in self.pollers:                 # cancelled while the event was due: a replaced plan's chain
            return
        start, next_polls = next(cycle)
        now = get_aware_utc_now()
        if start <= now:
            cycle.seek(now)
            skipped_from = start
            start, next_polls = next(cycle)
            _log.warning(f'Polling group {self.group!r}: skipped the polls from {skipped_from} to {now} to catch up'
                         f' with the current time.')
        self.pollers[key] = self._schedule(start, self._operate_polling, key, cycle, next_polls)
        slot_polls.dispatch()

    # ---- reporting ------------------------------------------------------------------------------------------------
    def get_schedule(self, full_topics: bool = False) -> dict:
        """``{hyperperiod: {offset: {remote: [points]}}}`` for every planned cycle."""
        schedule: dict = defaultdict(lambda: defaultdict(dict))
        for (index, hyperperiod), cycle in self.cycles.items():
            for offset, slot_polls in zip(cycle.offsets, cycle.entries):
                for remote, poll_set in slot_polls.merged().items():
                    names = sorted(poll_set.points) if full_topics else sorted(p.split('/')[-1] for p in poll_set.points)
                    schedule[str(hyperperiod)][str(offset)][str(remote.unique_id)] = names
        return {hp: dict(slots) for hp, slots in schedule.items()}
