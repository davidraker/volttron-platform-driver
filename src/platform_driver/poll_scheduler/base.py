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
"""The pieces every poll scheduler is built from.

* :class:`PollSet`: the live set of points one remote polls at one interval, with the publication topics the
  results go to. Mutated in place as points come and go, so a poll that is already scheduled sees the change.
* :class:`SlotPolls`: what one slot of a cyclic plan dispatches: the poll sets of every remote due in that slot,
  merged per remote at fire time (one request per remote).
* :class:`PollCycle`: the slots of one hyperperiod, iterated in O(1) per poll and re-positioned with ``seek``.
* :class:`PollSetRegistry`: owned by the agent, it holds every poll set by (group, remote, interval) with a reverse
  index by point, decides from a point's ``data_source`` whether and where it is scheduled, and reports which groups a
  change made structural (an interval or remote appearing or disappearing) so only those are re-planned.
* :class:`PollScheduler`: the abstract scheduler of one group; ``schedule()`` replaces the group's pending polls.

Intervals are seconds in configuration and integer milliseconds here (:func:`to_ms`), so the hyperperiod arithmetic
is exact.
"""
from __future__ import annotations

import abc
import importlib
import logging

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from functools import reduce
from importlib.metadata import distribution, PackageNotFoundError
from operator import or_
from typing import Any, Iterable
from weakref import WeakValueDictionary

try:
    distribution('volttron-core')
    # noinspection PyProtectedMember
    from volttron.client.vip.agent.core import ScheduledEvent
    from volttron.utils import get_aware_utc_now
except PackageNotFoundError:
    # noinspection PyProtectedMember
    from volttron.platform.vip.agent.core import ScheduledEvent
    from volttron.platform.agent.utils import get_aware_utc_now

from volttron.driver.base.config import DataSource
from volttron.driver.base.driver import DriverAgent

from ..config import GroupConfig
from ..equipment import EquipmentTree, PointNode

_log = logging.getLogger(__name__)


def to_ms(seconds: float) -> int:
    """Whole milliseconds for a duration in seconds (the unit the planners compute in)."""
    return int(round(float(seconds) * 1000))


def ms_timedelta(ms: int) -> timedelta:
    return timedelta(milliseconds=ms)


class PollSet:
    """The points one remote polls at one interval, with the topics their results are published to.

    One PollSet exists per (group, remote, interval); the registry mutates it as points are added and removed, and
    the slot that holds it dispatches whatever it contains at fire time.
    """

    def __init__(self, data_model: EquipmentTree, remote: DriverAgent,
                 points: WeakValueDictionary[str, PointNode] = None,
                 single_depth: set[str] = None, single_breadth: set[tuple[str, str]] = None,
                 multi_depth: dict[str, set[str]] = None, multi_breadth: dict[str, set[str]] = None):
        self.data_model: EquipmentTree = data_model
        self.points: WeakValueDictionary[str, PointNode] = points if points else WeakValueDictionary()
        self.single_depth: set[str] = single_depth if single_depth else set()
        self.single_breadth: set[tuple[str, str]] = single_breadth if single_breadth else set()
        self.multi_depth: dict[str, set[str]] = multi_depth if multi_depth else defaultdict(set)
        self.multi_breadth: dict[str, set[str]] = multi_breadth if multi_breadth else defaultdict(set)
        self.remote = remote

    def add(self, item: PointNode):
        self.points[item.identifier] = item
        self._add_to_publish_setup(item)

    def remove(self, item: PointNode | str) -> bool:
        """Remove a point (or its identifier, for a point whose node is already gone). False when it was not here."""
        identifier = item if isinstance(item, str) else item.identifier
        present = self.points.pop(identifier, None) is not None
        self._remove_from_publish_setup(identifier)
        return present

    def _add_to_publish_setup(self, point: PointNode):
        point_depth, point_breadth = self.data_model.get_point_topics(point.identifier)
        device_depth, device_breadth = self.data_model.get_device_topics(point.identifier)
        if self.data_model.is_published_single_depth(point.identifier):
            self.single_depth.add(point_depth)
        if self.data_model.is_published_single_breadth(point.identifier):
            self.single_breadth.add((point_depth, point_breadth))
        if self.data_model.is_published_multi_depth(point.identifier):
            self.multi_depth[device_depth].add(point_depth)
        if self.data_model.is_published_multi_breadth(point.identifier):
            self.multi_breadth[device_breadth].add(point.identifier)

    def _remove_from_publish_setup(self, identifier: str):
        point_depth, point_breadth = self.data_model.get_point_topics(identifier)
        self.single_depth.discard(point_depth)
        self.single_breadth.discard((point_depth, point_breadth))
        # The device topics come from the tree; after a device is removed they are found by searching instead.
        try:
            device_depth, device_breadth = self.data_model.get_device_topics(identifier)
            devices = [(device_depth, device_breadth)]
        except Exception:
            devices = [(d, b) for d in list(self.multi_depth) for b in list(self.multi_breadth)] or \
                      [(d, None) for d in list(self.multi_depth)] + [(None, b) for b in list(self.multi_breadth)]
        for device_depth, device_breadth in devices:
            if device_depth in self.multi_depth:
                self.multi_depth[device_depth].discard(point_depth)
                if not self.multi_depth[device_depth]:
                    self.multi_depth.pop(device_depth, None)
            if device_breadth in self.multi_breadth:
                self.multi_breadth[device_breadth].discard(identifier)
                if not self.multi_breadth[device_breadth]:
                    self.multi_breadth.pop(device_breadth, None)

    def __or__(self, other: 'PollSet') -> 'PollSet':
        if self.data_model is not other.data_model:
            raise ValueError(f'Cannot combine PollSets based on different data models: {self.data_model}, {other.data_model}.')
        if self.remote is not other.remote:
            raise ValueError(f'Cannot combine PollSets of different remotes: {self.remote.unique_id}, {other.remote.unique_id}.')
        return PollSet(
            self.data_model, self.remote,
            points=self.points | other.points,
            single_depth=self.single_depth | other.single_depth,
            single_breadth=self.single_breadth | other.single_breadth,
            multi_depth={k: self.multi_depth.get(k, set()) | other.multi_depth.get(k, set())
                         for k in self.multi_depth.keys() | other.multi_depth.keys()},
            multi_breadth={k: self.multi_breadth.get(k, set()) | other.multi_breadth.get(k, set())
                           for k in self.multi_breadth.keys() | other.multi_breadth.keys()})

    def __bool__(self):
        return bool(self.points)

    def __len__(self):
        return len(self.points)

    def __repr__(self):
        return f'PollSet({getattr(self.remote, "unique_id", self.remote)}, {sorted(self.points)})'


class SlotPolls:
    """The poll sets due in one slot, by remote. Built at plan time from the live sets; merged per remote only at
    fire time, so the poll always reflects the current membership and several remotes in one slot each get their own
    request (dispatched back-to-back in the callback greenlet)."""
    __slots__ = ('by_remote',)

    def __init__(self):
        self.by_remote: dict[DriverAgent, list[PollSet]] = {}

    def add(self, poll_set: PollSet):
        self.by_remote.setdefault(poll_set.remote, []).append(poll_set)

    def merged(self) -> dict[DriverAgent, PollSet]:
        return {remote: sets[0] if len(sets) == 1 else reduce(or_, sets) for remote, sets in self.by_remote.items()}

    def dispatch(self):
        for remote, poll_set in self.merged().items():
            if poll_set:
                remote.poll_data(poll_set)

    def __bool__(self):
        return any(poll_set for sets in self.by_remote.values() for poll_set in sets)

    def __repr__(self):
        return f'SlotPolls({ {getattr(r, "unique_id", r): len(ps) for r, ps in self.merged().items()} })'


class PollCycle:
    """The slots of one hyperperiod as an endless, O(1) iterator of ``(time, SlotPolls)``.

    ``start`` is the beginning of the current hyperperiod (anchored to midnight by the scheduler); ``seek(now)``
    repositions to the first slot after ``now`` in O(log slots), jumping whole hyperperiods first.
    """
    __slots__ = ('hyperperiod', 'start', 'offsets', 'entries', 'index')

    def __init__(self, hyperperiod: timedelta, start: datetime, slots: dict[timedelta, SlotPolls]):
        if not slots:
            raise ValueError('A poll cycle needs at least one slot.')
        items = sorted(slots.items())
        self.hyperperiod = hyperperiod
        self.start = start
        self.offsets: tuple[timedelta, ...] = tuple(offset for offset, _ in items)
        self.entries: tuple[SlotPolls, ...] = tuple(entry for _, entry in items)
        self.index = 0

    def __iter__(self):
        return self

    def __next__(self) -> tuple[datetime, SlotPolls]:
        i = self.index
        if i == len(self.offsets):
            i = 0
            self.start += self.hyperperiod
        self.index = i + 1
        return self.start + self.offsets[i], self.entries[i]

    def seek(self, now: datetime):
        """Position so that the next slot returned is the first one strictly after ``now``."""
        elapsed = now - self.start
        if elapsed >= self.hyperperiod:
            self.start += (elapsed // self.hyperperiod) * self.hyperperiod
        elif elapsed < timedelta(0):
            self.index = 0
            return
        self.index = bisect_right(self.offsets, now - self.start)

    def peek(self) -> datetime:
        """The time of the slot ``__next__`` would return, without advancing."""
        if self.index == len(self.offsets):
            return self.start + self.hyperperiod + self.offsets[0]
        return self.start + self.offsets[self.index]


class PollSetRegistry:
    """Every poll set of the agent by (group, remote, interval), plus a reverse index by point.

    ``update(point)`` places a point where its current ``data_source``, group, remote, interval and ``active`` flag
    say it belongs, moving it when that changed, and reports the groups whose structure changed (a (remote, interval)
    created or emptied); a membership change inside an existing set is silent, because the live PollSet already
    carries it to the pending poll. ``remove`` works by identifier, so a point can be unscheduled after its node left
    the tree. Nothing here is class-level: two agents in one process do not share state.
    """

    SHORT, LONG = 'short', 'long'

    def __init__(self, data_model: EquipmentTree):
        self.data_model = data_model
        self.short: dict[str, dict[DriverAgent, dict[int, PollSet]]] = defaultdict(dict)   # group -> remote -> ms -> set
        self.long: dict[str, dict[DriverAgent, dict[int, PollSet]]] = defaultdict(dict)
        self._index: dict[str, tuple[str, str, DriverAgent, int]] = {}                   # point -> (which, group, remote, ms)

    # ---- building -------------------------------------------------------------------------------------------------
    def build(self) -> set[str]:
        """Index every point of the model from scratch; returns the groups that have polls."""
        self.short.clear()
        self.long.clear()
        self._index.clear()
        for point in list(self.data_model.points()):
            self.update(point)
        return self.groups()

    def groups(self) -> set[str]:
        return {g for g, remotes in self.short.items() if remotes} | {g for g, remotes in self.long.items() if remotes}

    def for_group(self, group: str) -> tuple[dict[DriverAgent, dict[int, PollSet]], dict[DriverAgent, dict[int, PollSet]]]:
        """The group's short- and long-poll sets, ``{remote: {interval_ms: PollSet}}`` (live objects)."""
        return self.short.get(group, {}), self.long.get(group, {})

    def location(self, point: PointNode) -> tuple[str, str, DriverAgent, int] | None:
        """Where the point belongs now, or None when it is not polled cyclically (data source, inactive, no remote)."""
        source = point.data_source
        if not isinstance(source, DataSource) or not source.scheduled:
            return None
        if not self.data_model.is_active(point.identifier):
            return None
        try:
            remote = self.data_model.get_remote(point.identifier)
        except Exception:                                   # a point without a device above it
            return None
        if remote is None:
            return None
        which = self.LONG if source is DataSource.LONG_POLL else self.SHORT
        return (which, self.data_model.get_group(point.identifier), remote,
                to_ms(self.data_model.get_polling_interval(point.identifier)))

    def scheduled_at(self, point: PointNode | str) -> tuple[str, str, DriverAgent, int] | None:
        return self._index.get(point if isinstance(point, str) else point.identifier)

    # ---- changes --------------------------------------------------------------------------------------------------
    def update(self, point: PointNode) -> set[str]:
        """Place the point where it belongs now. Returns the groups whose structure changed."""
        groups: set[str] = set()
        new = self.location(point)
        old = self._index.get(point.identifier)
        if old is not None and old != new:
            if (pruned := self._remove_at(point.identifier, old)) is not None:
                groups.add(pruned)
            del self._index[point.identifier]
        if new is not None:
            which, group, remote, interval = new
            remotes = self._sets(which)[group]
            structural = remote not in remotes or interval not in remotes[remote]
            remotes.setdefault(remote, {}).setdefault(interval, PollSet(self.data_model, remote)).add(point)
            self._index[point.identifier] = new
            if structural:
                groups.add(group)
        return groups

    def update_many(self, points: Iterable[PointNode]) -> tuple[set[str], list[PointNode]]:
        """Update several points. Returns the groups to re-plan and the points that were not scheduled before."""
        groups: set[str] = set()
        newly_scheduled: list[PointNode] = []
        for point in points:
            was = point.identifier in self._index
            groups |= self.update(point)
            if not was and point.identifier in self._index:
                newly_scheduled.append(point)
        return groups, newly_scheduled

    def remove(self, point: PointNode | str) -> str | None:
        """Unschedule a point (by node or identifier). Returns the group when a (remote, interval) emptied."""
        identifier = point if isinstance(point, str) else point.identifier
        old = self._index.pop(identifier, None)
        if old is None:
            return None
        return self._remove_at(identifier, old)

    def remove_many(self, points: Iterable[PointNode | str]) -> set[str]:
        groups = set()
        for point in points:
            if (group := self.remove(point)) is not None:
                groups.add(group)
        return groups

    def _sets(self, which: str):
        return self.long if which == self.LONG else self.short

    def _remove_at(self, identifier: str, location: tuple[str, str, DriverAgent, int]) -> str | None:
        which, group, remote, interval = location
        sets = self._sets(which)
        by_interval = sets.get(group, {}).get(remote)
        poll_set = by_interval.get(interval) if by_interval else None
        if poll_set is None:
            return None
        poll_set.remove(identifier)
        if poll_set.points:
            return None
        del by_interval[interval]
        if not by_interval:
            del sets[group][remote]
        if not sets[group]:
            del sets[group]
        return group

    # ---- one-shot reads -------------------------------------------------------------------------------------------
    def one_shot(self, points: Iterable[PointNode] | None = None) -> list[PollSet]:
        """One PollSet per remote of the active ``POLL_ONCE`` points among ``points`` (all points when None), plus the
        polled points that other points' transforms read (a scale factor), so they have a last value before the first
        cyclic poll of anything depending on them. Nothing here touches the cyclic sets."""
        data_model = self.data_model
        points = list(data_model.points() if points is None else points)
        by_remote: dict[DriverAgent, PollSet] = {}
        remotes: dict[int, DriverAgent] = {}
        for point in points:
            try:
                remote = data_model.get_remote(point.identifier)
            except Exception:                                   # a point without a device above it
                continue
            if remote is None:
                continue
            remotes[id(remote)] = remote
            if point.data_source is DataSource.POLL_ONCE and data_model.is_active(point.identifier):
                by_remote.setdefault(remote, PollSet(data_model, remote)).add(point)
        for remote in remotes.values():
            topics = getattr(remote, 'startup_read_topics', ())
            if not isinstance(topics, (list, tuple, set, frozenset)):
                continue
            for topic in topics:
                node = data_model.get_node(topic)
                if (node is not None and getattr(node, 'is_point', False) and data_model.is_active(topic)
                        and getattr(node.data_source, 'polled', False)):
                    by_remote.setdefault(remote, PollSet(data_model, remote)).add(node)
        return list(by_remote.values())


class PollScheduler(abc.ABC):
    """The scheduler of one polling group. ``schedule()`` replaces whatever the group had pending: it cancels the
    pending polls, plans from the registry's current sets and schedules the first slot after now, so a structural
    change takes effect at the next slot and never leaves an old chain running."""

    def __init__(self, data_model: EquipmentTree, group: str, group_config: GroupConfig, registry: PollSetRegistry,
                 **kwargs):
        self.data_model: EquipmentTree = data_model
        self.group: str = group
        self.group_config: GroupConfig = group_config
        self.registry = registry
        self.start_all_datetime: datetime = get_aware_utc_now()
        self.pollers: dict[Any, ScheduledEvent] = {}

    def schedule(self):
        self.cancel()
        plan = self._prepare_to_schedule()
        self._schedule_polling(plan)

    def cancel(self):
        """Cancel every pending poll of this group. A cancelled event stays in the core's heap until due, then does
        nothing."""
        for event in self.pollers.values():
            event.cancel()
        self.pollers.clear()

    def poll_sets(self) -> tuple[dict[DriverAgent, dict[int, PollSet]], dict[DriverAgent, dict[int, PollSet]]]:
        return self.registry.for_group(self.group)

    def _schedule(self, deadline: datetime, function, *args) -> ScheduledEvent:
        return self.data_model.agent.core.schedule(deadline, function, *args)

    @abc.abstractmethod
    def _prepare_to_schedule(self) -> Any:
        """Plan from the registry's current sets; returns what ``_schedule_polling`` needs."""

    @abc.abstractmethod
    def _schedule_polling(self, plan: Any):
        """Schedule the first poll(s) of the plan."""

    @abc.abstractmethod
    def get_schedule(self, full_topics: bool = False) -> dict:
        """The planned schedule, for ``get_poll_schedule``."""

    @staticmethod
    def load_class(group_config: GroupConfig) -> type['PollScheduler']:
        module = importlib.import_module(group_config.poll_scheduler_module)
        return getattr(module, group_config.poll_scheduler_class_name)


def create_poll_schedulers(data_model: EquipmentTree, registry: PollSetRegistry, group_configs: dict[str, GroupConfig],
                           groups: Iterable[str], group_offset_interval: float = 0.0, existing_group_count: int = 0
                           ) -> dict[str, PollScheduler]:
    """A scheduler per group. A group no configuration names is taken to be a shared medium: it gets the default
    ``GroupConfig`` (sequential, 0.1 s minimum spacing), staggered by ``group_offset_interval`` times its index, and a
    warning says so; the synthesised configuration is kept in ``group_configs`` so the group is treated the same way
    for the rest of the run."""
    schedulers = {}
    for i, group in enumerate(sorted(groups)):
        group_config = group_configs.get(group)
        if group_config is None:
            group_config = GroupConfig(start_offset=group_offset_interval * (i + existing_group_count))
            group_configs[group] = group_config
            sample = next((p.identifier for p in data_model.points() if data_model.get_group(p.identifier) == group), '?')
            _log.warning(f"Polling group {group!r} (used by {sample}) is not configured under 'groups' in the Platform"
                         f" Driver configuration. It is treated as a shared medium: remotes polled one after another,"
                         f" at least {group_config.minimum_polling_interval:g} s apart, starting"
                         f" {group_config.start_offset_seconds:g} s after the cycle boundary. Add a 'groups.{group}'"
                         f" entry to choose otherwise.")
        schedulers[group] = PollScheduler.load_class(group_config)(data_model, group, group_config, registry)
    return schedulers
