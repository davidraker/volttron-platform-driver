"""The platform driver's side of point transforms: the startup read of referenced points, and registers configured and
removed through the remote so a point's transform follows its configuration."""
from types import SimpleNamespace
from unittest import mock

import pytest

from volttron.driver.base.config import DeviceConfig, PointConfig

from platform_driver.config import PlatformDriverConfig
from platform_driver.equipment import EquipmentTree
from platform_driver.poll_scheduler import PollScheduler

DEVICE = 'devices/campus/b/inverter'


def _point(name, data_source='short_poll', **kwargs):
    return PointConfig(volttron_point_name=name, data_source=data_source, **kwargs)


@pytest.fixture(autouse=True)
def clean_class_state():
    PollScheduler.poll_sets.clear()
    PollScheduler.long_poll_sets.clear()
    yield
    PollScheduler.poll_sets.clear()
    PollScheduler.long_poll_sets.clear()


@pytest.fixture
def tree():
    agent = SimpleNamespace(config=PlatformDriverConfig(default_polling_interval=5), vip=mock.Mock(), core=mock.Mock(),
                            poll_schedulers={})
    return EquipmentTree(agent)


@pytest.fixture
def remote(tree):
    """A device whose W point scales by W_SF (polled) and whose V point scales by V_SF (never polled), with a nameplate
    POLL_ONCE point and a STATIC one."""
    remote = mock.Mock(name='remote', unique_id=('toy', 'inv'))
    points = [_point('W', transform='sunssf(W_SF)'), _point('W_SF'), _point('V', transform='sunssf(V_SF)'),
              _point('V_SF', 'never_poll'), _point('Model', 'poll_once'), _point('Label', 'static')]
    tree.add_device(DEVICE, DeviceConfig(), remote, points)
    remote.point_set = set(tree.device_points(DEVICE))
    remote.startup_read_topics = [f'{DEVICE}/W_SF']         # what DriverAgent reports: the polled references
    tree.remotes[remote.unique_id] = remote
    return remote


def names(poll_set):
    return sorted(t.rsplit('/', 1)[-1] for t in poll_set.points)


class TestStartupRead:
    def test_referenced_polled_points_join_the_one_shot_read(self, tree, remote):
        sets = PollScheduler.one_shot_poll_sets(tree)
        assert len(sets) == 1 and names(sets[0]) == ['Model', 'W_SF']

    def test_never_poll_reference_is_not_read(self, tree, remote):
        remote.startup_read_topics = [f'{DEVICE}/W_SF', f'{DEVICE}/V_SF']
        assert names(PollScheduler.one_shot_poll_sets(tree)[0]) == ['Model', 'W_SF']

    def test_reconfiguring_any_point_of_the_remote_reads_its_factors(self, tree, remote):
        sets = PollScheduler.one_shot_poll_sets(tree, [tree.get_node(f'{DEVICE}/W')])
        assert names(sets[0]) == ['W_SF']

    def test_a_remote_without_the_attribute_is_unaffected(self, tree, remote):
        del remote.startup_read_topics
        assert names(PollScheduler.one_shot_poll_sets(tree)[0]) == ['Model']

    def test_the_factors_schedule_is_unchanged(self, tree, remote):
        PollScheduler._build_poll_sets(tree)
        assert names(PollScheduler.poll_sets['default'][remote][5]) == ['V', 'W', 'W_SF']


class TestRegisterLifecycle:
    def test_update_equipment_configures_points_through_the_remote(self, tree, remote):
        new = _point('Total', 'virtual', transform='sum_of(W, V)')
        changed = _point('W', transform='sunssf(V_SF)')
        tree.update_equipment(DEVICE, DeviceConfig(), remote,
                              [_point('W_SF'), _point('V', transform='sunssf(V_SF)'), _point('V_SF', 'never_poll'),
                               _point('Model', 'poll_once'),
                               _point('Label', 'static'), changed, new])
        calls = {c.args[0].volttron_point_name: c.args for c in remote.add_register.call_args_list}
        assert set(calls) == {'Total', 'W'} and calls['Total'][1] == DEVICE and calls['W'][0].transform == 'sunssf(V_SF)'
        remote.interface.create_register.assert_not_called()

    def test_removing_a_point_forgets_its_register_and_transform(self, tree, remote):
        tree.remove_segment(f'{DEVICE}/W')
        remote.remove_register.assert_called_once_with(f'{DEVICE}/W')
        assert tree.get_node(f'{DEVICE}/W') is None
