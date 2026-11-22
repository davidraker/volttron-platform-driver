"""The vctl driver plugin: typed set values and the readable health report."""
from volttron.plugins.vctl.driver.parser import _HealthText, _typed


def test_set_values_are_typed_when_they_parse_as_json():
    assert _typed('1') == 1 and _typed('2.5') == 2.5 and _typed('true') is True and _typed('null') is None
    assert _typed('{"devices/a/b/P": 3}') == {'devices/a/b/P': 3}
    assert _typed('commissioned 2026-11-12') == 'commissioned 2026-11-12'
    assert _typed('"7"') == '7'


def test_health_text_summarises_point_errors_and_protocol():
    report = {'devices/demo/inverter': {
        'remote': "('tcp', '127.0.0.1', 15502, 1)", 'status': 'BAD', 'polls': 12, 'poll_failures': 2,
        'consecutive_failures': 2, 'writes': 1, 'write_failures': 0, 'last_success': '2026-11-13T00:45:20.023Z',
        'last_failure': '2026-11-13T00:46:10.020Z', 'last_poll_duration': 0.0208,
        'last_error': 'Request starting at 40072 failed:   Not connected', 'last_write_error': None,
        'point_errors': {f'devices/demo/inverter/P{i}': 'Not connected' for i in range(40)},
        'events': [{'time': '2026-11-13T00:46:00Z', 'event': 'Modbus Proxy lost'}],
        'protocol': {'protocol': 'modbus', 'transport': 'tcp', 'unit_id': 1, 'connected': False,
                     'unit': {'requests': 575, 'failures': 2, 'last_error': None, 'last_success': 1794530912.1}, 'refused_single_writes': []},
        'devices': ['devices/demo/inverter']}}
    text = str(_HealthText(report))
    lines = text.splitlines()
    assert lines[0] == "devices/demo/inverter  remote ('tcp', '127.0.0.1', 15502, 1)"
    assert 'status BAD   polls 12 (failures 2, consecutive 2)   writes 1 (failures 0)' in text
    assert 'last error: Request starting at 40072 failed: Not connected' in text
    assert 'point errors (40): P0: Not connected; P1: Not connected; P2: Not connected, +37 more' in text
    assert 'event 2026-11-13T00:46:00Z  Modbus Proxy lost' in text
    assert 'protocol: protocol modbus, transport tcp, unit_id 1, connected False' in text
    assert 'unit: requests 575, failures 2, last_success 2026-11-13T00:48:32Z' in text and 'last_error' not in text.split('unit:')[1]
    assert 'refused_single_writes' not in text and len(lines) < 12


import argparse

import pytest


@pytest.mark.parametrize("args", [["--json", "driver", "health"], ["driver", "health", "--json"]])
def test_health_json_on_either_side_of_the_command(args):
    """vctl's --json is top-level; health's own --json has no default, so a value given before the command survives
    under plain argparse, which copies a command's defaults over the top-level values (volttron-core#374)."""
    from volttron.plugins.vctl.driver.parser import build_driver_commands

    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", default=False)
    driver = parser.add_subparsers(dest="command").add_parser("driver")
    subs = driver.add_subparsers(dest="driver_command", required=True)
    build_driver_commands(subs, lambda name, **kw: subs.add_parser(name, **kw), print_results=True)
    assert parser.parse_args(args).json is True
    assert parser.parse_args(["driver", "health"]).json is False
