# -*- coding: utf-8 -*- {{{
# ===----------------------------------------------------------------------===
#
#                 Installable Component of Eclipse VOLTTRON
#
# ===----------------------------------------------------------------------===
#
# Copyright 2025 Battelle Memorial Institute
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
"""The values of served points, kept across platform driver restarts in the agent's own configuration store.

A device in a server role holds values that exist nowhere else once both the platform driver and its proxy are gone:
what the platform wrote for the remote peer to read, and what the peer wrote. Both reach the equipment tree through
``DriverAgent.publish_push``, which calls :meth:`EquipmentTree.record_pushed_value`; this store notes the device as dirty
and, a little later, writes the device's ``data_source: server`` points as one entry, ``state/<device topic>``, with
``vip.config.set(..., trigger_callback=False)``. The ``state/`` prefix matches none of the agent's configuration
callbacks (``config``, ``devices/*``), so the entry is state kept in the configuration mechanism, inspectable with
``vctl config get``, never hand-edited while the driver runs.

Writes are coalesced (each ``set`` is an RPC that rewrites the agent's whole store document): a device is flushed at most
once per ``served_state_flush_interval`` seconds, and on shutdown. When a device is configured, its entry is read into
the tree before the device registers with its proxy, so the registration carries the values to the proxy
(``ProxyBackedInterface.tree_values``). When a device is removed, its entry is deleted.
"""
import logging

from typing import TYPE_CHECKING, Any

from volttron.driver.base.config import DataSource

if TYPE_CHECKING:
    from .equipment import DeviceNode, PointNode

_log = logging.getLogger(__name__)


class ServedStateStore:
    PREFIX = 'state/'

    def __init__(self, agent, flush_interval: float = 5.0):
        self.agent = agent
        self.flush_interval = flush_interval
        self.dirty: set[str] = set()
        self.forgotten: set[str] = set()         # devices whose entries are deleted at the next flush
        self._flusher = None

    @classmethod
    def entry_name(cls, device_topic: str) -> str:
        return f'{cls.PREFIX}{device_topic}'

    # ---- recording --------------------------------------------------------------------------------------------
    def record(self, point: 'PointNode'):
        """Note that a served point's value changed; its device is written at the next flush."""
        if getattr(point, 'data_source', None) is not DataSource.SERVER:
            return
        try:
            device_topic = self.agent.equipment_tree.get_device_node(point.identifier).identifier
        except Exception as e:                                           # a point with no device above it
            _log.debug(f'Not recording {point.identifier}: {e}')
            return
        self.dirty.add(device_topic)
        self._schedule_flush(self.flush_interval)

    def _schedule_flush(self, delay: float):
        if self._flusher is None:
            self._flusher = self.agent.core.spawn_later(delay, self.flush)

    def flush(self) -> int:
        """Write every dirty device's served values now, and delete the entries of forgotten devices. Returns the number
        of entries written. Runs from its own greenlet: the configuration store refuses changes made from inside a
        configuration callback, which is where devices are configured and removed."""
        self._flusher = None
        forgotten, self.forgotten = self.forgotten, set()
        if forgotten:
            try:
                existing = set(self.agent.vip.config.list())
            except Exception as e:
                _log.warning(f'Unable to list the configuration store; saved served values are not deleted yet: {e}')
                self.forgotten |= forgotten
                existing = set()
                forgotten = set()
            for device_topic in sorted(forgotten):
                if self.entry_name(device_topic) not in existing:      # nothing was ever saved: nothing to delete
                    continue
                try:
                    self.agent.vip.config.delete(self.entry_name(device_topic), trigger_callback=False)
                except Exception as e:
                    _log.warning(f'Unable to delete the saved served values of {device_topic}: {e}')
                    self.forgotten.add(device_topic)
        dirty, self.dirty = self.dirty, set()
        written = 0
        for device_topic in sorted(dirty):
            values = self.served_values(device_topic)
            if values is None:
                continue
            try:
                self.agent.vip.config.set(self.entry_name(device_topic), values, trigger_callback=False)
                written += 1
            except Exception as e:
                _log.warning(f'Unable to save the served values of {device_topic}: {e}')
                self.dirty.add(device_topic)
        return written

    def served_values(self, device_topic: str) -> dict[str, Any] | None:
        """``{point name: last value}`` of the device's served points that have a value; None if the device is gone."""
        tree = self.agent.equipment_tree
        device = tree.get_node(device_topic)
        if device is None or not getattr(device, 'is_device', False):
            return None
        return {point.identifier.rsplit('/', 1)[-1]: point.last_value
                for point in tree.device_points(device_topic)
                if point.data_source is DataSource.SERVER and point.last_updated is not None}

    # ---- loading and forgetting -------------------------------------------------------------------------------
    def load_into(self, device: 'DeviceNode') -> int:
        """Set the stored values on the device's served points that have none yet. Returns how many were set."""
        try:
            stored = self.agent.vip.config.get(self.entry_name(device.identifier))
        except KeyError:
            return 0
        except Exception as e:
            _log.warning(f'Unable to read the saved served values of {device.identifier}: {e}')
            return 0
        if not isinstance(stored, dict):
            _log.warning(f'Ignoring the saved served values of {device.identifier}: not a mapping ({stored!r}).')
            return 0
        loaded = 0
        for point in self.agent.equipment_tree.device_points(device.identifier):
            name = point.identifier.rsplit('/', 1)[-1]
            if point.data_source is DataSource.SERVER and point.last_updated is None and name in stored:
                point.last_value = stored[name]
                loaded += 1
        if loaded:
            _log.info(f'Restored {loaded} served value(s) of {device.identifier} from the configuration store.')
        return loaded

    def forget(self, device_topic: str):
        """Drop the device's entry (the device is being removed). The delete happens at the next flush, scheduled now,
        because forget is called from the configuration callback that removes the device."""
        self.dirty.discard(device_topic)
        self.forgotten.add(device_topic)
        self._schedule_flush(0)
