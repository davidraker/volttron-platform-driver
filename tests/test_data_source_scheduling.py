"""How a point's data_source decides whether, where and how often it is polled, and when it counts as stale."""
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import pytest

from volttron.driver.base.config import DataSource, DeviceConfig, PointConfig
from volttron.utils import get_aware_utc_now

from platform_driver.config import GroupConfig, PlatformDriverConfig
from platform_driver.equipment import EquipmentTree
from platform_driver.poll_scheduler import PollSetRegistry, StaticCyclicPollScheduler

DEVICE = 'devices/campus/b/der'
KINDS = ('short_poll', 'long_poll', 'poll_once', 'static', 'never_poll', 'server')


def _point(name, data_source, **kwargs):
    return PointConfig(volttron_point_name=name, data_source=data_source, **kwargs)


@pytest.fixture
def tree():
    agent = SimpleNamespace(config=PlatformDriverConfig(default_polling_interval=5), vip=mock.Mock(), core=mock.Mock(),
                            poll_schedulers={})
    return EquipmentTree(agent)


@pytest.fixture
def registry(tree):
    return PollSetRegistry(tree)


@pytest.fixture
def remote(tree):
    """One device with one point of every data_source kind, plus a long-poll point at a long interval."""
    remote = mock.Mock(name='remote', unique_id=('toy', 'h'))
    points = [_point(k, k) for k in KINDS] + [_point('total', 'long_poll', polling_interval=3600)]
    tree.add_device(DEVICE, DeviceConfig(), remote, points)
    remote.point_set = set(tree.device_points(DEVICE))
    tree.remotes[remote.unique_id] = remote
    return remote


def topics(poll_set):
    return sorted(t.rsplit('/', 1)[-1] for t in poll_set.points)


class TestBuildPollSets:
    def test_only_short_and_long_poll_points_are_scheduled(self, tree, remote, registry):
        assert registry.build() == {'default'}
        short, long = registry.for_group('default')
        assert {i: topics(ps) for i, ps in short[remote].items()} == {5000: ['short_poll']}
        assert {i: topics(ps) for i, ps in long[remote].items()} == {5000: ['long_poll'], 3600000: ['total']}

    def test_poll_once_points_are_collected_per_remote(self, tree, remote, registry):
        other = mock.Mock(name='other', unique_id=('toy', 'h2'))
        tree.add_device(f'{DEVICE}2', DeviceConfig(), other, [_point('plate', 'poll_once'), _point('x', 'short_poll')])
        [a, b] = sorted(registry.one_shot(), key=lambda ps: str(ps.remote.unique_id))
        assert (a.remote, topics(a)) == (remote, ['poll_once']) and (b.remote, topics(b)) == (other, ['plate'])
        tree.get_node(f'{DEVICE}/poll_once').active = False
        assert [ps.remote for ps in registry.one_shot()] == [other]
        assert registry.one_shot([tree.get_node(f'{DEVICE}/short_poll')]) == []

    def test_inactive_points_are_left_out(self, tree, remote, registry):
        tree.get_node(f'{DEVICE}/short_poll').active = False
        registry.build()
        short, long = registry.for_group('default')
        assert not short and 'default' in registry.groups() and remote in long

    def test_groups_with_only_long_polls_still_get_a_scheduler(self, tree, remote, registry):
        from platform_driver.poll_scheduler import create_poll_schedulers
        tree.get_node(f'{DEVICE}/short_poll').active = False
        schedulers = create_poll_schedulers(tree, registry, {}, registry.build())
        assert list(schedulers) == ['default']


class TestIncrementalChanges:
    def test_unscheduled_kinds_are_not_added_and_remove_quietly(self, tree, remote, registry):
        for kind in ('poll_once', 'static', 'never_poll', 'server'):
            point = tree.get_node(f'{DEVICE}/{kind}')
            assert registry.update(point) == set()
            assert registry.remove(point) is None                                   # no KeyError
        assert registry.groups() == set()

    def test_long_poll_points_are_added_to_and_removed_from_their_own_sets(self, tree, remote, registry):
        total = tree.get_node(f'{DEVICE}/total')
        assert registry.update(total) == {'default'}
        short, long = registry.for_group('default')
        assert topics(long[remote][3600000]) == ['total'] and not short
        assert registry.remove(total) == 'default'
        assert registry.groups() == set()


class TestSeparateSchedules:
    def test_long_polls_get_their_own_slot_plan(self, tree, remote, registry):
        registry.build()
        scheduler = StaticCyclicPollScheduler(tree, 'default', GroupConfig(minimum_polling_interval=1.0), registry)
        regular, long_plan = scheduler._prepare_to_schedule()
        # The regular schedule keeps a 5 s hyperperiod; the long-poll plan has its own (5 s and 3600 s intervals).
        assert [sorted(regular), sorted(long_plan)] == [[timedelta(seconds=5)], [timedelta(seconds=3600)]]
        assert topics(regular[timedelta(seconds=5)][timedelta(seconds=0)].merged()[remote]) == ['short_poll']
        long_slots = long_plan[timedelta(seconds=3600)]
        assert len(long_slots) == 720                                            # the 5 s long-poll point's slots
        assert topics(long_slots[timedelta(seconds=0)].merged()[remote]) == ['long_poll', 'total']


class TestStaleness:
    @pytest.mark.parametrize('kind, stale', [('short_poll', True), ('long_poll', True), ('poll_once', False),
                                             ('static', False), ('never_poll', False), ('server', False)])
    def test_without_a_configured_timeout_only_scheduled_points_go_stale(self, tree, remote, kind, stale):
        node = tree.get_node(f'{DEVICE}/{kind}')
        node.last_value = 1
        node.data['last_updated'] = get_aware_utc_now() - timedelta(seconds=100)     # 20 intervals ago
        assert tree.is_stale(node.identifier) is stale

    @pytest.mark.parametrize('kind', ('poll_once', 'never_poll', 'server'))
    def test_a_configured_timeout_applies_to_unpolled_points(self, tree, remote, kind):
        node = tree.get_node(f'{DEVICE}/{kind}')
        node.config.stale_timeout_configured = 60
        node.last_value = 1
        assert tree.is_stale(node.identifier) is False
        node.data['last_updated'] = get_aware_utc_now() - timedelta(seconds=61)
        assert tree.is_stale(node.identifier) is True

    def test_static_points_never_go_stale_and_unset_points_are_stale(self, tree, remote):
        static = tree.get_node(f'{DEVICE}/static')
        assert tree.is_stale(static.identifier) is True                              # never updated at all
        static.config.stale_timeout_configured = 1
        static.last_value = 3
        static.data['last_updated'] = get_aware_utc_now() - timedelta(days=1)
        assert tree.is_stale(static.identifier) is False
