"""Remote health through the platform driver (the health RPC, the agent's own status and alerts), comment rows,
unknown-column warnings, and registry rows that fail validation or register creation."""
import logging

from types import SimpleNamespace
from unittest import mock

import pytest

from pydantic import Field

from volttron.driver.base.config import DeviceConfig, PointConfig
from volttron.driver.base.health import RemoteHealth

from platform_driver.agent import PlatformDriverAgent, STATUS_BAD, STATUS_GOOD
from platform_driver.config import PlatformDriverConfig
from platform_driver.equipment import EquipmentTree

from tests.fixtures import base_PDA  # noqa: F401

DEVICE = 'devices/campus/b/meter'


class ToyPointConfig(PointConfig):
    type: str = Field(default='int', alias='Type')


class ToyInterface:
    REGISTER_CONFIG_CLASS = ToyPointConfig
    LEGACY_COLUMN_HINTS = {'precision': 'use round(n) in the Transform column'}

    def prepare_registry_config(self, rows, remote_config=None):
        return rows


@pytest.fixture
def tree():
    agent = SimpleNamespace(config=PlatformDriverConfig(default_polling_interval=5), vip=mock.Mock(), core=mock.Mock(),
                            poll_schedulers={})
    return EquipmentTree(agent)


class TestRows:
    def test_comment_rows_are_skipped(self, base_PDA):
        contents = {'driver_type': 'fake', 'registry_config': [
            {'Volttron Point Name': '# the meter block', 'Point Address': ''},
            {'Volttron Point Name': 'Volts', 'Point Address': '1'},
            {'Volttron Point Name': '  #Amps', 'Point Address': '2'}]}
        _, dev_config, rows = base_PDA._separate_equipment_configs(contents)
        assert [r['Volttron Point Name'] for r in rows] == ['Volts']

    def test_unknown_columns_are_warned_about_with_hints(self, caplog):
        rows = [{'Volttron Point Name': 'V', 'Type': 'int', 'Precision': '2', 'Notes': 'x', 'Colour': 'red'}]
        with caplog.at_level(logging.WARNING):
            PlatformDriverAgent._warn_unknown_columns(DEVICE, ToyInterface(), rows)
        assert 'Colour' in caplog.text and 'Precision (use round(n) in the Transform column)' in caplog.text
        assert 'Notes' not in caplog.text.split('not used by')[1]

    def test_invalid_rows_are_dropped_not_fatal(self, caplog):
        rows = [{'Volttron Point Name': 'Good'}, {'Volttron Point Name': 'Bad', 'Data Source': 'sometimes'}, {'Type': 'int'}]
        with caplog.at_level(logging.ERROR):
            validated = PlatformDriverAgent._validate_rows(DEVICE, ToyInterface(), rows)
        assert [v.volttron_point_name for v in validated] == ['Good']
        assert "registry row 'Bad' is not configured" in caplog.text

    def test_update_equipment_drops_a_row_whose_register_fails(self, tree, caplog):
        remote = mock.Mock(name='remote', unique_id=('toy', 'm'))
        tree.add_device(DEVICE, DeviceConfig(), remote, [PointConfig(volttron_point_name='A')])
        remote.add_register.side_effect = lambda cfg, nid: (_ for _ in ()).throw(ValueError('bad type')) if cfg.volttron_point_name == 'B' else None
        with caplog.at_level(logging.ERROR):
            tree.update_equipment(DEVICE, DeviceConfig(), remote, [PointConfig(volttron_point_name='A'), PointConfig(volttron_point_name='B')])
        assert tree.get_node(f'{DEVICE}/B') is None and tree.get_node(f'{DEVICE}/A') is not None
        assert f'point {DEVICE}/B is not configured: bad type' in caplog.text


class TestHealth:
    def _remote(self, status='GOOD', unique_id='r1'):
        remote = mock.Mock(unique_id=unique_id)
        remote.health = RemoteHealth(unique_id)
        remote.health.status = status
        remote.health_report.return_value = {'remote': unique_id, 'status': status}
        return remote

    def test_health_rpc_reports_every_remote_or_the_selected_ones(self, base_PDA, tree):
        base_PDA.equipment_tree = tree
        r1, r2 = self._remote(), self._remote('BAD', 'r2')
        tree.add_device(DEVICE, DeviceConfig(), r1, [PointConfig(volttron_point_name='A')])
        tree.add_device('devices/campus/b/other', DeviceConfig(), r2, [PointConfig(volttron_point_name='Z')])
        report = base_PDA.health()
        assert report == {DEVICE: {'remote': 'r1', 'status': 'GOOD'}, 'devices/campus/b/other': {'remote': 'r2', 'status': 'BAD'}}
        assert list(base_PDA.health(topic=f'{DEVICE}/A')) == [DEVICE]

    def test_status_rpc_describes_points(self, base_PDA, tree):
        base_PDA.equipment_tree = tree
        remote = self._remote()
        remote.health.point_errors = {f'{DEVICE}/A': 'timeout'}
        tree.add_device(DEVICE, DeviceConfig(), remote, [PointConfig(volttron_point_name='A')])
        status = base_PDA.status(topic=f'{DEVICE}/A')
        entry = status[f'{DEVICE}/A']
        assert entry['active'] is True and entry['last_value'] is None and entry['stale'] is True and entry['error'] == 'timeout'

    def test_remote_failures_go_into_the_agent_context_and_alerts(self, base_PDA):
        base_PDA.vip.health.get_status.return_value = {'status': STATUS_GOOD, 'context': None}
        health = RemoteHealth(('modbus', '10.0.0.4', 502, 1), on_change=base_PDA._remote_health_changed)
        health.record_poll({}, {'devices/x/A': 'No response'})
        base_PDA.vip.health.set_status.assert_called_with(STATUS_GOOD, {'failing_remotes': {"('modbus', '10.0.0.4', 502, 1)": 'No response'}})
        alert_key, alert = base_PDA.vip.health.send_alert.call_args.args
        assert alert_key == "remote/('modbus', '10.0.0.4', 502, 1)" and alert.status == STATUS_BAD
        assert alert.context['health']['consecutive_failures'] == 1
        health.record_poll({'devices/x/A': 1}, {})
        base_PDA.vip.health.set_status.assert_called_with(STATUS_GOOD, 'All remotes are answering.')
        assert base_PDA.vip.health.send_alert.call_args.args[1].status == STATUS_GOOD

    def test_an_agent_already_bad_keeps_its_status(self, base_PDA):
        base_PDA.vip.health.get_status.return_value = {'status': STATUS_BAD, 'context': 'config error'}
        health = RemoteHealth('r', on_change=base_PDA._remote_health_changed)
        health.record_poll({}, {})
        base_PDA.vip.health.set_status.assert_not_called()
        base_PDA.vip.health.send_alert.assert_called_once()


class TestUpdateFinalizesSetup:
    def test_a_registry_update_runs_finalize_setup_again(self, base_PDA):
        """A proxy-backed interface declares its point table at finalize_setup, so an update must call it again."""
        remote = mock.Mock(unique_id='r1')
        remote.interface.prepare_registry_config.side_effect = lambda rows, cfg: rows
        remote.interface.REGISTER_CONFIG_CLASS = PointConfig
        remote.interface.LEGACY_COLUMN_HINTS = {}
        base_PDA._get_or_create_remote = mock.Mock(return_value=remote)
        base_PDA._update_polling_schedules = mock.Mock()
        base_PDA.equipment_tree.update_equipment.return_value = True
        base_PDA._update_equipment(DEVICE, 'UPDATE', {'driver_type': 'fake', 'driver_config': {},
                                                      'registry_config': [{'Volttron Point Name': 'A'}]})
        remote.interface.finalize_setup.assert_called_once_with(initial_setup=False)

    def test_an_unchanged_update_does_not_finalize(self, base_PDA):
        remote = mock.Mock(unique_id='r1')
        remote.interface.prepare_registry_config.side_effect = lambda rows, cfg: rows
        remote.interface.REGISTER_CONFIG_CLASS = PointConfig
        remote.interface.LEGACY_COLUMN_HINTS = {}
        base_PDA._get_or_create_remote = mock.Mock(return_value=remote)
        base_PDA.equipment_tree.update_equipment.return_value = False
        base_PDA._update_equipment(DEVICE, 'UPDATE', {'driver_type': 'fake', 'driver_config': {},
                                                      'registry_config': [{'Volttron Point Name': 'A'}]})
        remote.interface.finalize_setup.assert_not_called()
