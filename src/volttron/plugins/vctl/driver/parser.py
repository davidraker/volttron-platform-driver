#!/usr/bin/env python
# PYTHON_ARGCOMPLETE_OK
"""Platform Driver commands: ``vctl driver <command>`` (this module is a vctl plugin) and the standalone ``vdrv``
script (``vdrv driver <command>``), both over the driver's RPC interface.

Importing this module has no side effects: vctl imports every module under ``volttron.plugins.vctl`` on every
invocation, so the connection to the platform is made only when a driver command runs. Under vctl the command reuses
vctl's own control connection with its peer switched to the driver; standalone, one is built on first use.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pprint import pprint

PEER = 'platform.driver'
_log = logging.getLogger(os.path.basename(sys.argv[0]) if __name__ == "__main__" else __name__)
_standalone_connection = None


def _modular() -> bool:
    from importlib.metadata import PackageNotFoundError, distribution
    try:
        distribution('volttron-core')
        return True
    except PackageNotFoundError:
        return False


def connection(opts=None):
    """The control connection to the driver: vctl's own (``opts.connection``) when the command runs under vctl, else one
    built on first use. Raises ValueError when the platform's control credentials are absent (platform not running)."""
    global _standalone_connection
    existing = getattr(opts, 'connection', None)
    if existing is not None:
        existing.peer = PEER
        return existing
    if _standalone_connection is None:
        if _modular():
            from volttron.client.commands.connection import ControlConnection
            from volttron.utils import ClientContext as cc
            _standalone_connection = ControlConnection(cc.get_address(), peer=PEER)
        else:                                                   # classic platform
            from volttron.platform import get_address
            from volttron.platform.control.control_connection import ControlConnection
            _standalone_connection = ControlConnection(get_address(), peer=PEER)
    return _standalone_connection


def _parse_json_config(stream):
    if _modular():
        from volttron.utils import parse_json_config
    else:
        from volttron.platform.agent.utils import parse_json_config
    return parse_json_config(stream)


def _require_driver(opts):
    conn = connection(opts)
    if PEER not in conn.server.vip.peerlist().get(timeout=10):
        sys.exit('Platform Driver Agent must be running to use driver subcommands.')    # message to stderr, exit status 1
    return conn


# ---------------------------------------------------------------- the commands (each returns the RPC result)
def driver_get(opts):
    return _require_driver(opts).call('get', topic=opts.topic, regex=opts.regex)


def driver_set(opts):
    return _require_driver(opts).call('set', value=opts.value, topic=opts.topic, regex=opts.regex,
                                      confirm_values=opts.confirm_values, map_points=opts.map_points)


def driver_last(opts):
    return _require_driver(opts).call('last', topic=opts.topic, regex=opts.regex, value=opts.value, updated=opts.updated)


def driver_revert(opts):
    return _require_driver(opts).call('revert', topic=opts.topic, regex=opts.regex)


def driver_start(opts):
    return _require_driver(opts).call('start', topic=opts.topic, regex=opts.regex)


def driver_stop(opts):
    return _require_driver(opts).call('stop', topic=opts.topic, regex=opts.regex)


def driver_enable(opts):
    return _require_driver(opts).call('enable', topic=opts.topic, regex=opts.regex)


def driver_disable(opts):
    return _require_driver(opts).call('disable', topic=opts.topic, regex=opts.regex)


def driver_status(opts):
    return _require_driver(opts).call('status', topic=opts.topic, regex=opts.regex)


def driver_node_add(opts):
    if not opts.config and opts.config_path:
        with open(opts.config_path) as f:
            config = _parse_json_config(f)
    else:
        config = opts.config
    return _require_driver(opts).call('add_node', node_topic=opts.node_topic, config=config, update_schedule=opts.update_schedule)


def driver_node_remove(opts):
    return _require_driver(opts).call('remove_node', topic=opts.topic)


def driver_interface_install(opts):
    return _require_driver(opts).call('add_interface', interface_name=opts.name)


def driver_interface_list(opts):
    return _require_driver(opts).call('list_interfaces')


def driver_interface_remove(opts):
    return _require_driver(opts).call('remove_interface', interface_name=opts.name)


def driver_list_topics(opts):
    return _require_driver(opts).call('list_topics', topic=opts.topic, regex=opts.regex, active=opts.active, enabled=opts.enabled)


def topic_completer(prefix, parsed_args=None, **kwargs):
    try:
        return connection(parsed_args).call('list_topics', topic=prefix or None)
    except Exception:                 # no platform: no completions
        return []


# ---------------------------------------------------------------- the parser, shared by vctl and vdrv
def _printing(func):
    def run(opts):
        pprint(func(opts))
    run.__name__ = func.__name__
    return run


def _selection(parser, topic_help='Topic or pattern to select equipment.'):
    parser.add_argument('topic', help=topic_help).completer = topic_completer
    parser.add_argument('-r', '--regex', help='Regex pattern to modify equipment selection.')
    parser.add_argument('--tag', help='Tagging expression to select equipment.')    # no -t: vctl uses it for --timeout


def build_driver_commands(subparsers, add_parser, print_results: bool):
    """Add the driver subcommands to ``subparsers`` through ``add_parser(name, help=...)`` (vctl's registering
    function, or ``subparsers.add_parser`` for vdrv). With ``print_results`` each command prints its result, as vctl
    expects of a command function; vdrv prints the returned value itself."""
    wrap = _printing if print_results else (lambda f: f)
    bool_opt = argparse.BooleanOptionalAction

    p = add_parser('get', help='Query data from equipment.'); _selection(p); p.set_defaults(func=wrap(driver_get))
    p = add_parser('set', help='Set data on equipment.'); _selection(p)
    p.add_argument('value', help='Value to which to set the selected points.')
    p.add_argument('--confirm-values', action='store_true', help='Query points after setting to confirm the value was set.')
    p.add_argument('--map-points', action='store_true', help='Take value to be a dictionary of {point: value} mappings.')
    p.set_defaults(func=wrap(driver_set))
    p = add_parser('last', help='Query last known values and/or the corresponding datetime of points on equipment.'); _selection(p)
    p.add_argument('-v', '--value', action=bool_opt, default=True, help='Return the value.')
    p.add_argument('-u', '--updated', action=bool_opt, default=True, help='Return the datetime of the last known value.')
    p.set_defaults(func=wrap(driver_last))
    p = add_parser('revert', help='Reset any commands set on equipment.'); _selection(p)
    p.add_argument('-c', '--confirm-values', action='store_true', help='Query points after reversion to confirm values were reset.')
    p.set_defaults(func=wrap(driver_revert))
    for name, func, text in (('start', driver_start, 'Start polling for a given topic.'), ('stop', driver_stop, 'Stop polling for a given topic.'),
                             ('enable', driver_enable, 'Set polling to start automatically for topic.'),
                             ('disable', driver_disable, 'Set polling not to start automatically for topic.'),
                             ('status', driver_status, 'Provide status of running drivers.')):
        p = add_parser(name, help=text); _selection(p); p.set_defaults(func=wrap(func))

    node = add_parser('node', help='Add and remove equipment from the Equipment Tree.')
    node_subs = node.add_subparsers(title='subcommands', metavar='', dest='node_command')
    p = node_subs.add_parser('add', help='Add a node to the Equipment Tree.')
    p.add_argument('node_topic', help='Topic for the new equipment node.')
    p.add_argument('-c', '--config', help='Configuration (JSON) for the new equipment node.')
    p.add_argument('-p', '--config-path', help='Path of a configuration file for the new equipment node.')
    p.add_argument('-u', '--update-schedule', action=bool_opt, default=True, help='Run the Poll Scheduler after adding the node.')
    p.set_defaults(func=wrap(driver_node_add))
    p = node_subs.add_parser('remove', help='Remove a node from the Equipment Tree.')
    p.add_argument('topic', help='Topic of the equipment node to remove.')           # was added to the "add" parser by mistake
    p.set_defaults(func=wrap(driver_node_remove))

    interface = add_parser('interface', help='Add and remove interfaces.')
    iface_subs = interface.add_subparsers(title='subcommands', metavar='', dest='interface_command')
    p = iface_subs.add_parser('install', help='Install a new driver interface.')
    p.add_argument('name', help='Name of the driver interface to install.'); p.set_defaults(func=wrap(driver_interface_install))
    p = iface_subs.add_parser('list', help='List installed driver interfaces.'); p.set_defaults(func=wrap(driver_interface_list))
    p = iface_subs.add_parser('remove', help='Remove an interface from the driver.')
    p.add_argument('name', help='Name of the driver interface to uninstall.'); p.set_defaults(func=wrap(driver_interface_remove))

    p = add_parser('list', help='List configured equipment topics.')
    p.add_argument('topic', nargs='?', default='devices', help='Topic or pattern to select equipment (default devices).').completer = topic_completer
    p.add_argument('-r', '--regex', help='Regex pattern to modify equipment selection.')
    p.add_argument('--tag', help='Tagging expression to select equipment.')
    p.add_argument('-a', '--active', action='store_true', help='Only topics which are actively polled.')
    p.add_argument('-e', '--enabled', action='store_true', help='Only topics enabled for polling.')
    p.set_defaults(func=wrap(driver_list_topics))


# ---------------------------------------------------------------- vctl plugin (modular platform)
try:
    from volttron.client.decorators import vctl_subparser
    from volttron.types.factories import ControlParser
except ImportError:                          # classic platform: no plugin API; vdrv still works
    vctl_subparser = None

if vctl_subparser is not None:
    @vctl_subparser
    class DriverVCtlParser(ControlParser):
        """``vctl driver …``: the Platform Driver's commands."""

        class Meta:
            name = 'driver'

        def configure(self, ctx):
            driver = ctx.register_command('driver', help='Platform Driver commands')
            # required: a bare "vctl driver" errors with the usage instead of needing a parent default, which argparse
            # would apply over the subcommand's func (set_defaults of a parent wins over the subparser's).
            subs = driver.add_subparsers(title='subcommands', metavar='', dest='driver_command', required=True)
            build_driver_commands(subs, lambda name, **kw: ctx.register_subcommand(subs, name, **kw), print_results=True)


# ---------------------------------------------------------------- vdrv
def main(argv=None):
    parser = argparse.ArgumentParser(prog='vdrv', description='Platform Driver commands (the same as vctl driver).')
    subparsers = parser.add_subparsers(dest='command')
    driver = subparsers.add_parser('driver', help='Driver commands')
    driver_subs = driver.add_subparsers(title='subcommands', metavar='', dest='driver_command', required=True)
    build_driver_commands(driver_subs, driver_subs.add_parser, print_results=False)
    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass
    opts = parser.parse_args(argv)
    if not hasattr(opts, 'func'):
        parser.print_help()
        return 0
    try:
        pprint(opts.func(opts))
    except ValueError as e:                 # no control credentials: the platform is not running in this VOLTTRON_HOME
        sys.stderr.write(f'{e}\nIs the platform running, and is VOLTTRON_HOME ({os.environ.get("VOLTTRON_HOME", "unset")}) the right home?\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
