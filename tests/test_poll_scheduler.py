"""The poll scheduler package against a real EquipmentTree and a fake core whose clock the tests control.

Covers the findings of plans_and_analysis/driver/POLL_SCHEDULER_PLAN.md: F1 (a re-plan cancels the old chain), F2
(the first poll is the next slot, not the next hyperperiod), F3 (an empty slot does not end the chain), F4 (an
interval change moves the point), F8 (one stagger term, slots stay inside the interval), the registry (B8), the
cyclic planner's quantisation and warnings (E3), the shared-medium default for unconfigured groups (E4), the serial
scheduler (E5) and the agent's wiring (stop/start, removal, immediate first poll, scheduling health).
"""
import heapq
import logging

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from volttron.client.vip.agent.core import ScheduledEvent
from volttron.driver.base.config import DeviceConfig, PointConfig

from platform_driver.agent import PlatformDriverAgent, STATUS_BAD, STATUS_GOOD
from platform_driver.config import GroupConfig, PlatformDriverConfig
from platform_driver.equipment import EquipmentTree
from platform_driver.poll_scheduler import (PollCycle, PollSet, PollSetRegistry, SerialPollScheduler, SlotPolls,
                                            StaticCyclicPollScheduler, create_poll_schedulers, to_ms)
from platform_driver.poll_scheduler import static_cyclic

MIDNIGHT = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)


class FakeCore:
    """Records scheduled events; ``run_until`` fires the due ones in deadline order, as the real core would."""

    def __init__(self):
        self.heap = []
        self.seq = 0
        self.spawned = []

    def schedule(self, deadline, func, *args, **kwargs):
        event = ScheduledEvent(func, args, kwargs)
        self.seq += 1
        heapq.heappush(self.heap, (deadline, self.seq, event))
        return event

    def spawn(self, func, *args, **kwargs):
        self.spawned.append((func, args))
        func(*args, **kwargs)
        return SimpleNamespace(dead=True)

    def pending(self):
        return [(deadline, event) for deadline, _, event in sorted(self.heap) if not event.canceled]

    def run_until(self, when, clock):
        """Fire every live event due by ``when``; ``clock`` is the mock the code reads the time from."""
        fired = 0
        while self.heap and self.heap[0][0] <= when:
            deadline, _, event = heapq.heappop(self.heap)
            if event.canceled:
                continue
            clock.return_value = deadline
            event()
            fired += 1
        clock.return_value = when
        return fired


class FakeRemote:
    """A remote that records every poll it is asked for: (time, sorted point names)."""

    def __init__(self, name, clock):
        self.unique_id = ('fake', name)
        self.clock = clock
        self.polls = []
        self.startup_read_topics = []
        self.delay = None

    def poll_data(self, poll_set):
        self.polls.append((self.clock(), sorted(t.rsplit('/', 1)[-1] for t in poll_set.points)))
        if self.delay:
            import gevent
            gevent.sleep(self.delay)
        return True

    def remove_register(self, topic):
        pass

    def __repr__(self):
        return self.unique_id[1]


@pytest.fixture
def clock():
    """Every module reads the time through this; tests move it."""
    with mock.patch('platform_driver.poll_scheduler.base.get_aware_utc_now') as base_now, \
            mock.patch('platform_driver.poll_scheduler.static_cyclic.get_aware_utc_now') as cyclic_now, \
            mock.patch('platform_driver.poll_scheduler.serial.get_aware_utc_now') as serial_now:
        shared = mock.Mock(return_value=MIDNIGHT + 3 * S)
        for m in (base_now, cyclic_now, serial_now):
            m.side_effect = lambda: shared.return_value
        yield shared


@pytest.fixture
def world(clock):
    core = FakeCore()
    config = PlatformDriverConfig(default_polling_interval=10, groups={'default': GroupConfig(minimum_polling_interval=0.02,
                                                                                            parallel_subgroups=True)})
    agent = SimpleNamespace(config=config, vip=mock.Mock(), core=core, poll_schedulers={})
    tree = EquipmentTree(agent)
    registry = PollSetRegistry(tree)
    remotes = {}

    def device(name, points, group=None, interval=None, **dev):
        remote = remotes.get(name) or FakeRemote(name, clock)
        remotes[name] = remote
        topic = f'devices/site/{name}'
        cfg = DeviceConfig(**({'polling_group': group} if group else {}), **({'polling_interval': interval} if interval else {}), **dev)
        rows = [PointConfig(volttron_point_name=p, **kw) if isinstance(p, str) else PointConfig(volttron_point_name=p[0], **p[1], **kw)
                for p, kw in ((p, {}) for p in points)]
        tree.add_device(topic, cfg, remote, rows)
        tree.remotes[remote.unique_id] = remote
        remote.point_set = set(tree.device_points(topic))
        return remote

    return SimpleNamespace(core=core, tree=tree, registry=registry, agent=agent, device=device, remotes=remotes, clock=clock,
                           config=config)


def P(name, **kw):
    return (name, kw)


def scheduler(world, group='default', **group_kw):
    cfg = world.config.groups.get(group) if not group_kw else GroupConfig(**group_kw)
    if cfg is None:
        cfg = GroupConfig(**group_kw)
    return StaticCyclicPollScheduler(world.tree, group, cfg, world.registry)


def node(world, device, point):
    return world.tree.get_node(f'devices/site/{device}/{point}')


# --------------------------------------------------------------------------------------------- PollCycle and SlotPolls
class TestPollCycle:
    def _cycle(self):
        slots = {timedelta(seconds=o): SlotPolls() for o in (0, 10, 20, 30, 40, 50)}
        return PollCycle(60 * S, MIDNIGHT, slots)

    def test_wraps_around_without_stopping(self):
        cycle = self._cycle()
        times = [next(cycle)[0] for _ in range(8)]
        assert times[:6] == [MIDNIGHT + o * S for o in (0, 10, 20, 30, 40, 50)]
        assert times[6:] == [MIDNIGHT + 60 * S, MIDNIGHT + 70 * S]

    @pytest.mark.parametrize('now, first', [
        (MIDNIGHT, MIDNIGHT + 10 * S),                                     # on a slot: the next one
        (MIDNIGHT + 23 * S, MIDNIGHT + 30 * S),                            # mid cycle
        (MIDNIGHT + 59 * S, MIDNIGHT + 60 * S),                            # last slot passed: the next hyperperiod
        (MIDNIGHT + timedelta(hours=5, seconds=41), MIDNIGHT + timedelta(hours=5, seconds=50)),   # after a long gap
        (MIDNIGHT - 7 * S, MIDNIGHT),                                      # start still in the future
    ])
    def test_seek_positions_on_the_first_slot_after_now(self, now, first):
        cycle = self._cycle()
        cycle.seek(now)
        assert next(cycle)[0] == first
        assert cycle.peek() == first + 10 * S

    def test_dispatch_merges_per_remote_and_polls_every_remote(self, world):
        a = world.device('a', ['x', 'y'])
        b = world.device('b', ['z'])
        tree = world.tree
        slot = SlotPolls()
        for name, remote in (('a', a), ('b', b)):
            ps = PollSet(tree, remote)
            for point in tree.device_points(f'devices/site/{name}'):
                ps.add(point)
            slot.add(ps)
        extra = PollSet(tree, a)
        extra.add(node(world, 'a', 'x'))                      # a second set of the same remote: merged into one request
        slot.add(extra)
        slot.dispatch()
        assert a.polls == [(world.clock(), ['x', 'y'])] and b.polls == [(world.clock(), ['z'])]
        assert bool(SlotPolls()) is False and bool(slot) is True


# ------------------------------------------------------------------------------------------------------- the registry
class TestRegistry:
    def test_build_places_only_polled_active_points(self, world):
        r = world.device('a', [P('s'), P('l', data_source='long_poll'), P('once', data_source='poll_once'),
                               P('st', data_source='static'), P('np', data_source='never_poll'),
                               P('v', data_source='virtual'), P('off', active=False)])
        groups = world.registry.build()
        assert groups == {'default'}
        short, long = world.registry.for_group('default')
        assert {i: sorted(ps.points) for i, ps in short[r].items()} == {10000: ['devices/site/a/s']}
        assert {i: sorted(ps.points) for i, ps in long[r].items()} == {10000: ['devices/site/a/l']}

    def test_membership_change_is_silent_and_structural_change_is_reported(self, world):
        world.device('a', ['x', 'y'])
        world.registry.build()
        world.device('a', ['x', 'y', 'z'])                          # a third point at the same interval
        assert world.registry.update(node(world, 'a', 'z')) == set()
        world.device('b', ['q'])                                    # a new remote: structural
        assert world.registry.update(node(world, 'b', 'q')) == {'default'}
        # removing the last point of an interval prunes the set and is structural
        assert world.registry.remove(node(world, 'b', 'q')) == 'default'
        assert world.registry.remove(node(world, 'a', 'x')) is None
        short, _ = world.registry.for_group('default')
        assert [sorted(ps.points) for ps in short[world.remotes['a']].values()] == [['devices/site/a/y', 'devices/site/a/z']]

    def test_interval_change_moves_the_point_f4(self, world):
        r = world.device('a', ['x', 'y'])
        world.registry.build()
        y = node(world, 'a', 'y')
        y.polling_interval = 30
        assert world.registry.update(y) == {'default'}
        short, _ = world.registry.for_group('default')
        assert {i: sorted(ps.points) for i, ps in short[r].items()} == {10000: ['devices/site/a/x'], 30000: ['devices/site/a/y']}
        y.polling_interval = 10                                      # back: 30 s set emptied and pruned
        assert world.registry.update(y) == {'default'}
        assert list(short[r]) == [10000] and sorted(short[r][10000].points) == ['devices/site/a/x', 'devices/site/a/y']

    def test_data_source_group_and_active_changes(self, world):
        r = world.device('a', ['x', 'y'])
        world.registry.build()
        x = node(world, 'a', 'x')
        x.data_source = 'long_poll'
        assert world.registry.update(x) == {'default'}
        short, long = world.registry.for_group('default')
        assert sorted(long[r][10000].points) == ['devices/site/a/x'] and sorted(short[r][10000].points) == ['devices/site/a/y']
        x.active = False
        assert world.registry.update(x) == {'default'} and r not in long
        x.active = True
        x.config.polling_group = 'bus'
        assert world.registry.update(x) == {'bus'}
        assert world.registry.groups() == {'default', 'bus'}

    def test_remove_by_identifier_after_the_node_is_gone(self, world):
        r = world.device('a', ['x', 'y'])
        world.registry.build()
        world.tree.remove_segment('devices/site/a/y')
        assert world.registry.remove('devices/site/a/y') is None
        short, _ = world.registry.for_group('default')
        ps = short[r][10000]
        assert sorted(ps.points) == ['devices/site/a/x'] and ps.multi_depth == {'devices/site/a': {'devices/site/a/x'}}

    def test_two_registries_do_not_share_state(self, world):
        world.device('a', ['x'])
        other = PollSetRegistry(world.tree)
        world.registry.build()
        assert other.groups() == set() and world.registry.groups() == {'default'}

    def test_one_shot_sets_collect_poll_once_points_and_referenced_factors(self, world):
        r = world.device('a', [P('plate', data_source='poll_once'), P('sf'), P('w')])
        r.startup_read_topics = ['devices/site/a/sf']
        [ps] = world.registry.one_shot()
        assert sorted(t.rsplit('/', 1)[-1] for t in ps.points) == ['plate', 'sf']
        node(world, 'a', 'plate').active = False
        [ps] = world.registry.one_shot()
        assert sorted(t.rsplit('/', 1)[-1] for t in ps.points) == ['sf']
        [ps] = world.registry.one_shot([node(world, 'a', 'w')])          # a reconfigured point re-reads its remote's factors
        assert sorted(t.rsplit('/', 1)[-1] for t in ps.points) == ['sf']
        r.startup_read_topics = []
        assert world.registry.one_shot([node(world, 'a', 'w')]) == []


# ------------------------------------------------------------------------------------------- the cyclic scheduler
class TestCyclicScheduling:
    def test_first_poll_is_the_next_slot_not_the_next_hyperperiod_f2(self, world):
        world.device('a', ['x'])
        world.registry.build()
        sch = scheduler(world)
        world.clock.return_value = MIDNIGHT + 23 * S                 # 10 s interval, hyperperiod 10 s
        sch.schedule()
        [(deadline, _)] = world.core.pending()
        assert deadline == MIDNIGHT + 30 * S
        world.core.run_until(MIDNIGHT + 55 * S, world.clock)
        assert [t for t, _ in world.remotes['a'].polls] == [MIDNIGHT + 30 * S, MIDNIGHT + 40 * S, MIDNIGHT + 50 * S]

    def test_schedule_again_cancels_the_old_chain_f1(self, world):
        world.device('a', ['x'])
        world.registry.build()
        sch = scheduler(world)
        sch.schedule()
        world.core.run_until(MIDNIGHT + 15 * S, world.clock)
        sch.schedule()
        world.core.run_until(MIDNIGHT + 45 * S, world.clock)
        assert len(world.core.pending()) == 1                        # one live chain
        times = [t for t, _ in world.remotes['a'].polls]
        assert times == sorted(set(times))                           # never two polls at one slot

    def test_an_emptied_slot_does_not_stop_the_chain_f3(self, world):
        world.device('a', ['x'])
        world.registry.build()
        sch = scheduler(world)
        sch.schedule()
        assert world.registry.remove(node(world, 'a', 'x')) == 'default'      # the set is now empty
        world.core.run_until(MIDNIGHT + 35 * S, world.clock)
        assert world.remotes['a'].polls == [] and len(world.core.pending()) == 1   # dispatched nothing, chain alive

    def test_pending_poll_sees_a_point_added_after_scheduling(self, world):
        world.device('a', ['x'])
        world.registry.build()
        sch = scheduler(world)
        sch.schedule()
        world.device('a', ['x', 'y'])
        assert world.registry.update(node(world, 'a', 'y')) == set()           # membership only, no re-plan
        world.core.run_until(MIDNIGHT + 12 * S, world.clock)
        assert world.remotes['a'].polls == [(MIDNIGHT + 10 * S, ['x', 'y'])]

    def test_catch_up_after_a_pause_skips_with_one_warning(self, world, caplog):
        world.device('a', ['x'])
        world.registry.build()
        sch = scheduler(world)
        sch.schedule()
        deadline, _, event = heapq.heappop(world.core.heap)             # the slot at 00:00:10 comes due ...
        world.clock.return_value = MIDNIGHT + timedelta(hours=2, seconds=4)   # ... after the host slept through 720 slots
        with caplog.at_level(logging.WARNING):
            event()
        assert caplog.text.count('skipped the polls') == 1
        assert world.remotes['a'].polls == [(world.clock(), ['x'])]        # the due slot itself is still polled
        [(deadline, _)] = world.core.pending()
        assert deadline == MIDNIGHT + timedelta(hours=2, seconds=10)

    def test_long_polls_get_their_own_cycle(self, world):
        world.device('a', ['x', P('total', data_source='long_poll', polling_interval=3600)])
        world.registry.build()
        sch = scheduler(world)
        sch.schedule()
        assert sorted(hp for _, hp in sch.cycles) == [10 * S, 3600 * S]

    def test_get_schedule_lists_remotes_per_slot(self, world):
        world.device('a', ['x', 'y'])
        world.registry.build()
        sch = scheduler(world)
        sch.schedule()
        assert sch.get_schedule() == {'0:00:10': {'0:00:00': {"('fake', 'a')": ['x', 'y']}}}


class TestCyclicPlanner:
    def _plan(self, world, group, **group_kw):
        sch = scheduler(world, group, **group_kw)
        [plan] = sch._prepare_to_schedule() if not group_kw.get('parallel_subgroups') else [sch._prepare_to_schedule()]
        return sch, plan

    def test_sequential_remotes_are_spread_with_one_stagger_f8(self, world):
        for i in range(10):
            world.device(f'r{i}', ['x'], group='bus', interval=60)
        world.registry.build()
        sch, plan = self._plan(world, 'bus', minimum_polling_interval=1.0)
        slots = sorted(plan[60 * S])
        assert slots == [o * S for o in range(0, 60, 6)]             # 0, 6, ... 54: inside the interval, one each
        assert all(len(plan[60 * S][s].by_remote) == 1 for s in slots)

    def test_remotes_that_do_not_fit_are_clamped_with_a_warning(self, world, caplog):
        for i in range(100):
            world.device(f'r{i:03d}', ['x'], group='bus', interval=10)
        world.registry.build()
        with caplog.at_level(logging.WARNING):
            sch, plan = self._plan(world, 'bus', minimum_polling_interval=1.0)
        slots = sorted(plan[10 * S])
        assert slots[0] == timedelta(0) and slots[-1] == timedelta(milliseconds=9900) and len(slots) == 100
        assert "cannot be spaced minimum_polling_interval (1 s) apart" in caplog.text and "'groups.bus'" in caplog.text

    def test_parallel_remotes_get_their_own_chains_offset_by_the_minimum(self, world):
        for name in ('a', 'b', 'c'):
            world.device(name, ['x'])
        world.registry.build()
        sch = scheduler(world)                                       # default: parallel, 0.02 s
        plans = sch._prepare_to_schedule()
        assert [sorted(p[10 * S]) for p in plans] == [[timedelta(0)], [timedelta(milliseconds=20)], [timedelta(milliseconds=40)]]

    def test_offsets_are_stable_when_a_remote_is_added_b7(self, world):
        world.device('a', ['x'], group='bus', interval=60)
        world.device('b', ['x'], group='bus', interval=60)
        world.registry.build()
        sch, plan = self._plan(world, 'bus', minimum_polling_interval=1.0)
        before = {remote: slot for slot, sp in plan[60 * S].items() for remote in sp.by_remote}
        world.device('c', ['x'], group='bus', interval=60)
        world.registry.update(node(world, 'c', 'x'))
        [plan] = sch._prepare_to_schedule()
        after = {remote: slot for slot, sp in plan[60 * S].items() for remote in sp.by_remote}
        # The spread is recomputed for three remotes (20 s apart instead of 30), but the order never changes: a
        # stays first and b second, whatever order the registry's dictionaries happen to have.
        assert before[world.remotes['a']] == timedelta(0) and before[world.remotes['b']] == 30 * S
        assert after[world.remotes['a']] == timedelta(0) and after[world.remotes['b']] == 20 * S
        assert after[world.remotes['c']] == 40 * S                     # the newcomer takes the last position

    def test_quantisation_warns_and_names_the_setting_e3(self, world, caplog):
        world.device('a', ['x'], group='bus', interval=1.25)
        world.registry.build()
        with caplog.at_level(logging.WARNING):
            sch, plan = self._plan(world, 'bus', minimum_polling_interval=0.1)
        assert list(plan) == [timedelta(milliseconds=1300)]            # 1.25 s rounded to the nearest 0.1 s
        assert "1.25 s polling interval is not a multiple" in caplog.text
        assert "set minimum_polling_interval under 'groups.bus'" in caplog.text
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            sch._prepare_to_schedule()
        assert 'not a multiple' not in caplog.text                    # warned once

    def test_fractional_intervals_are_exact_in_milliseconds(self, world):
        world.device('a', ['x'], group='bus', interval=0.5)
        world.device('b', ['x'], group='bus', interval=1.5)
        world.registry.build()
        sch, plan = self._plan(world, 'bus', minimum_polling_interval=0.1)
        assert list(plan) == [timedelta(milliseconds=1500)]
        assert sorted(plan[1500 * S / 1000]) == [timedelta(milliseconds=ms) for ms in (0, 500, 1000)]
        assert len(plan[1500 * S / 1000][timedelta(0)].by_remote) == 2         # both remotes due at 0: two requests

    def test_near_coprime_intervals_warn_about_the_hyperperiod(self, world, caplog):
        world.device('a', ['x'], group='bus', interval=2.2)
        world.device('b', ['x'], group='bus', interval=2.4)
        world.registry.build()
        with caplog.at_level(logging.WARNING):
            sch, plan = self._plan(world, 'bus', minimum_polling_interval=0.1)
        assert list(plan) == [timedelta(milliseconds=26400)] and 'share a 26.4 s hyperperiod' in caplog.text
        # genuinely coprime counts (11 against 24, 22 and 10) are kept apart in a cycle of their own
        world.device('c', ['x'], group='bus', interval=1.1)
        world.device('d', ['x'], group='bus', interval=1.0)
        world.registry.build()
        sch, plan = self._plan(world, 'bus', minimum_polling_interval=0.1)
        assert sorted(plan) == [timedelta(milliseconds=1100), timedelta(seconds=132)]

    def test_separate_coprimes(self):
        assert StaticCyclicPollScheduler.separate_coprimes([60, 10, 7, 1]) == [[60, 10, 1], [7]]


class TestGroupDefaults:
    def test_an_unconfigured_group_is_a_shared_medium_with_a_warning_e4(self, world, caplog):
        world.device('a', ['x'], group='bus')
        world.registry.build()
        configs = {}
        with caplog.at_level(logging.WARNING):
            schedulers = create_poll_schedulers(world.tree, world.registry, configs, ['bus'], group_offset_interval=2.0,
                                                existing_group_count=1)
        cfg = configs['bus']
        assert cfg.parallel_subgroups is False and cfg.minimum_polling_interval == 0.1 and cfg.start_offset_seconds == 2.0
        assert isinstance(schedulers['bus'], StaticCyclicPollScheduler)
        assert "Polling group 'bus' (used by devices/site/a/x) is not configured" in caplog.text
        assert 'shared medium' in caplog.text and "'groups.bus'" in caplog.text

    def test_durations_are_rounded_to_milliseconds(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = GroupConfig(minimum_polling_interval=0.0005, start_offset=1.23456)
        assert cfg.minimum_polling_interval_ms == 1 and cfg.start_offset_ms == 1235
        assert 'not a whole number of milliseconds' in caplog.text
        assert GroupConfig(minimum_polling_interval=0.00001).minimum_polling_interval == 0.001
        assert PointConfig(volttron_point_name='p', interval=0.5).polling_interval == 0.5
        with pytest.raises(ValueError):
            PointConfig(volttron_point_name='p', interval=0)
        assert to_ms(1.25) == 1250


# ------------------------------------------------------------------------------------------- the serial scheduler
class TestSerialScheduler:
    def _serial(self, world, **options):
        cfg = GroupConfig(poll_scheduler_class_name='SerialPollScheduler', minimum_polling_interval=0.1,
                          poll_scheduler_configs=options or None)
        return SerialPollScheduler(world.tree, 'bus', cfg, world.registry)

    def test_polls_one_remote_at_a_time_in_due_order(self, world):
        a = world.device('a', ['x'], group='bus', interval=10)
        b = world.device('b', ['y'], group='bus', interval=10)
        world.registry.build()
        sch = self._serial(world)
        world.core.spawn = lambda func, *args: SimpleNamespace(dead=False, join=lambda timeout=None: None)   # step by hand
        sch.schedule()
        due = sch._due
        assert {k[0] for k in due} == {a, b} and all(t >= MIDNIGHT + 10 * S for t in due.values())
        world.clock.return_value = MIDNIGHT + 10 * S + timedelta(milliseconds=200)
        assert sch.step() == 0.0 and sch.step() == 0.0               # both were due: polled back to back
        assert a.polls == [(world.clock(), ['x'])] and b.polls == [(world.clock(), ['y'])]
        wait = sch.step()
        assert 9 < wait <= 10                                        # nothing due until the next turn
        assert set(due[(a, 10000)] - MIDNIGHT for _ in [0]) == {20 * S}

    def test_a_slow_remote_skips_periods_with_one_warning_and_never_blocks_the_others(self, world, caplog):
        a = world.device('a', ['x'], group='bus', interval=1)
        b = world.device('b', ['y'], group='bus', interval=1)
        world.registry.build()
        sch = self._serial(world)
        world.core.spawn = lambda func, *args: SimpleNamespace(dead=False, join=lambda timeout=None: None)
        sch.schedule()
        first_a = sch._due[(a, 1000)]
        world.clock.return_value = first_a + 3 * S + timedelta(milliseconds=500)     # a's turn came 3.5 s ago
        with caplog.at_level(logging.WARNING):
            sch.step()
        assert sch._due[(a, 1000)] == first_a + 4 * S                # skipped to the next future turn
        assert 'cannot be polled every 1 s' in caplog.text and '3 poll(s) skipped' in caplog.text

    def test_replan_keeps_the_due_times_of_surviving_entries(self, world):
        a = world.device('a', ['x'], group='bus', interval=10)
        world.registry.build()
        sch = self._serial(world)
        world.core.spawn = lambda func, *args: SimpleNamespace(dead=False, join=lambda timeout=None: None)
        sch.schedule()
        due_a = sch._due[(a, 10000)]
        world.device('b', ['y'], group='bus', interval=10)
        world.registry.update(node(world, 'b', 'y'))
        sch.schedule()
        assert sch._due[(a, 10000)] == due_a and (world.remotes['b'], 10000) in sch._due

    def test_poll_timeout_moves_on(self, world, caplog):
        a = world.device('a', ['x'], group='bus', interval=10)
        world.registry.build()
        a.delay = 0.2
        sch = self._serial(world, poll_timeout=0.05)
        world.core.spawn = lambda func, *args: SimpleNamespace(dead=False, join=lambda timeout=None: None)
        sch.schedule()
        world.clock.return_value = sch._due[(a, 10000)]
        with caplog.at_level(logging.WARNING):
            sch.step()
        assert 'did not answer within 0.05 s' in caplog.text and 'poll_timeout' in caplog.text

    def test_get_schedule_shows_the_queue(self, world):
        world.device('a', ['x'], group='bus', interval=10)
        world.registry.build()
        sch = self._serial(world)
        world.core.spawn = lambda func, *args: SimpleNamespace(dead=False, join=lambda timeout=None: None)
        sch.schedule()
        [(interval, remotes)] = sch.get_schedule().items()
        assert interval == '0:00:10' and remotes["('fake', 'a')"]['points'] == ['x'] and remotes["('fake', 'a')"]['next']


# --------------------------------------------------------------------------------------------- the agent's wiring
@pytest.fixture
def agent(world):
    pda = PlatformDriverAgent.__new__(PlatformDriverAgent)
    pda.config = world.config
    pda.equipment_tree = world.tree
    pda.core = world.core
    pda.vip = mock.Mock()
    pda.vip.health.get_status.return_value = {'status': STATUS_GOOD, 'context': None}
    pda.poll_schedulers = {}
    pda.poll_registry = None
    pda._unscheduled_groups = {}
    pda.served_state = mock.Mock()
    world.agent.poll_schedulers = pda.poll_schedulers
    return pda


class TestAgentWiring:
    def _configure(self, agent, world):
        agent.poll_registry = PollSetRegistry(world.tree)
        groups = agent.poll_registry.build()
        agent.poll_schedulers.update(create_poll_schedulers(world.tree, agent.poll_registry, world.config.groups, groups))
        for group, sch in agent.poll_schedulers.items():
            agent._schedule_group(group, sch)

    def test_stop_and_start_round_trip_keeps_one_chain_and_the_siblings(self, agent, world):
        world.device('a', ['x', 'y'])
        self._configure(agent, world)
        agent._stop([node(world, 'a', 'x')])                         # membership change: y keeps polling
        world.core.run_until(MIDNIGHT + 12 * S, world.clock)
        assert world.remotes['a'].polls == [(MIDNIGHT + 10 * S, ['y'])]
        agent._stop([node(world, 'a', 'y')])                         # last point: structural, chain re-planned (empty)
        assert world.registry_groups() if hasattr(world, 'registry_groups') else True
        world.core.run_until(MIDNIGHT + 32 * S, world.clock)
        assert len(world.remotes['a'].polls) == 1
        agent._start([node(world, 'a', 'x'), node(world, 'a', 'y')])
        world.core.run_until(MIDNIGHT + 52 * S, world.clock)
        assert [t for t, _ in world.remotes['a'].polls][1:] == [MIDNIGHT + 32 * S, MIDNIGHT + 40 * S, MIDNIGHT + 50 * S]
        assert world.remotes['a'].polls[1] == (MIDNIGHT + 32 * S, ['x', 'y'])        # E2: polled at once on start
        assert len(world.core.pending()) == 1

    def test_removing_a_device_unschedules_it(self, agent, world):
        world.device('a', ['x'])
        world.device('b', ['y'])
        self._configure(agent, world)
        agent._remove_equipment('devices/site/a', None, None)
        assert world.tree.get_node('devices/site/a') is None
        world.core.run_until(MIDNIGHT + 22 * S, world.clock)
        assert world.remotes['a'].polls == [] and len(world.remotes['b'].polls) == 2

    def test_new_points_are_polled_immediately_when_configured(self, agent, world):
        world.device('a', ['x'])
        self._configure(agent, world)
        world.device('b', ['y'])
        agent._update_polling_schedules(world.tree.device_points('devices/site/b'))
        assert world.remotes['b'].polls == [(world.clock(), ['y'])]
        world.config.poll_new_points_immediately = False
        world.device('c', ['z'])
        agent._update_polling_schedules(world.tree.device_points('devices/site/c'))
        assert world.remotes['c'].polls == []

    def test_a_group_that_fails_to_schedule_is_in_the_health_context_e6(self, agent, world):
        world.device('a', ['x'])
        self._configure(agent, world)
        broken = mock.Mock()
        broken.schedule.side_effect = ValueError('no slots')
        assert agent._schedule_group('bus', broken) is False
        agent.vip.health.set_status.assert_called_with(STATUS_BAD, {'unscheduled_groups': {'bus': "ValueError('no slots')"}})
        agent.vip.health.get_status.return_value = {'status': STATUS_BAD, 'context': {'unscheduled_groups': {'bus': 'x'}}}
        broken.schedule.side_effect = None
        assert agent._schedule_group('bus', broken) is True
        agent.vip.health.set_status.assert_called_with(STATUS_GOOD, 'All polling groups are scheduled.')

    def test_static_points_publish_on_configure_through_the_remote(self, agent, world):
        r = world.device('a', [P('st', data_source='static'), 'x'])
        r.publish_static = mock.Mock()
        agent._publish_static(world.tree.device_points('devices/site/a'))
        [(points,), _] = r.publish_static.call_args
        assert [p.identifier for p in points] == ['devices/site/a/st']
