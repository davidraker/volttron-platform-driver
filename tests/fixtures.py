import argparse
import json
import pathlib
import pytest
import os

from volttron.driver.base.interfaces import BaseInterface
from volttron.driver.base.driver import DriverAgent  # TODO: This should import real DriverAgent from base driver and/or a better mock?
from platform_driver.agent import PlatformDriverAgent
from volttron.types.server_config import ServerConfig
from unittest.mock import MagicMock, Mock, patch

@pytest.fixture
def driver_agent():
    # Lightweight mock that emulates the DriverAgent API used in tests
    m = MagicMock()
    config_mock = MagicMock()
    config_mock.driver_type = 'TestInterface'
    config_mock.group = 'remotes/0'
    config_mock.get = MagicMock(side_effect=lambda key, default=None: getattr(config_mock, key, default))
    m.config = config_mock
    m.core = MagicMock()
    m.core.unique_id = ('some', 'unique', 'id')
    m.interface = MagicMock()
    m.point_set = set()
    m.get_multiple_points = MagicMock(return_value=({}, {}))
    m.set_multiple_points = MagicMock(return_value={})
    return m

@pytest.fixture
def equipped_driver_service(driver_service):
    pds = driver_service
    pds.config_version = 2
    pds._get_or_create_remote = lambda x, y: driver_agent

    topic = 'devices/Foo/Bar/Baz'
    contents = {'driver_type': DummyInterface}
    pds._configure_new_equipment(topic, 'NEW', contents)

@pytest.fixture
def driver_service():
    # Set up mock ServerConfig:
    parser = argparse.ArgumentParser()
    parser.set_defaults(volttron_publickey='DEADBEEF')
    opts = parser.parse_args([])
    server_config = ServerConfig()
    server_config.opts = opts

    # Instantiate PlatformDriverAgent while mocking credential/core builders to avoid file access
    with patch('volttron.client.decorators.get_core_builder') as mock_get_core_builder, \
            patch('volttron.types.auth.auth_credentials.CredentialsFactory.load_credentials_from_file') as mock_load_credentials, \
            patch('platform_driver.reservations.ReservationManager.save_state') as mock_save_state, \
            patch('platform_driver.reservations.ReservationManager.load_state') as mock_load_state, \
            patch('volttron.driver.base.interfaces.BaseInterface.get_interface_subclass') as mock_get_interface:
        mock_core = MagicMock()
        mock_core._annotations = {"__rpc__.exports": set()}
        mock_core.periodic = MagicMock()
        mock_core.schedule = MagicMock()
        mock_core.connected = False
        mock_core.identity = "test_identity"
        mock_get_core_builder.return_value.build.return_value = mock_core

        mock_credentials = MagicMock()
        mock_credentials.identity = "test_identity"
        mock_credentials.publickey = "test_public_key"
        mock_load_credentials.return_value = mock_credentials

        # Mock reservation manager state operations to prevent pickle/bytes encoding errors
        mock_save_state.return_value = None
        mock_load_state.return_value = None

        # Mock interface loading to prevent ModuleNotFoundError
        def mock_interface_loader(driver_type, module=None):
            if driver_type in ['TestInterface', 'UnknownInterface']:
                return DummyInterface
            else:
                raise ValueError(f"Interface {driver_type} not found")
        mock_get_interface.side_effect = mock_interface_loader

        # Call using keyword to match PlatformDriverAgent signature
        pds = PlatformDriverAgent(server_config=server_config)
        assert isinstance(pds, PlatformDriverAgent)
        # Keep interface class (not instance) so _get_configured_interface can use INTERFACE_CONFIG_CLASS
        pds.interface_classes = {'TestInterface': DummyInterface}

        # Minimal VIP mock used by configure_main
        vip = MagicMock()
        vip.pubsub.list.return_value.get.return_value = []
        vip.pubsub.subscribe = MagicMock()
        vip.config.get = MagicMock()
        vip.config.list = MagicMock(return_value=[])
        vip.config.set_default = MagicMock()
        vip.config.subscribe = MagicMock()
        vip.health = MagicMock()
        vip.rpc = MagicMock()
        pds.vip = vip
        pds.core = mock_core

        return pds


class DummyInterface(BaseInterface):
    INTERFACE_CONFIG_CLASS = MagicMock  # Add this to prevent AttributeError during config conversion

    def configure(self, config_dict, registry_config_str):
        pass

    def get_point(self, point_name, **kwargs):
        pass

    def set_point(self, point_name, value, **kwargs):
        pass

    def scrape_all(self):
        pass

    def revert_all(self, **kwargs):
        pass

    def revert_point(self, point_name, **kwargs):
        pass

    def create_register(self, register_definition):
        r = MagicMock()
        r.point_name = getattr(register_definition, 'volttron_point_name', getattr(register_definition, 'Point Name', 'pt'))
        r.register_type = 'byte'
        r.python_type = float
        r.get_units.return_value = getattr(register_definition, 'units', '')
        return r

    def get_multiple_points(self, topics, **kwargs):
        return {}, {}

    @classmethod
    def unique_remote_id(cls, equipment_name, config, **kwargs):
        return 'some', 'unique', 'id'

@pytest.fixture(autouse=True)
def set_agent_identity():
    os.environ["AGENT_VIP_IDENTITY"] = "test_identity"


@pytest.fixture
def base_PDA():
    # Set the required environment variable for AGENT_VIP_IDENTITY
    os.environ["AGENT_VIP_IDENTITY"] = "test_identity"

    with patch('volttron.client.decorators.get_core_builder') as mock_get_core_builder, \
            patch('volttron.types.auth.auth_credentials.CredentialsFactory.load_credentials_from_file') as mock_load_credentials, \
            patch('platform_driver.reservations.ReservationManager.save_state') as mock_save_state, \
            patch('platform_driver.reservations.ReservationManager.load_state') as mock_load_state:
        # Mock core with necessary attributes
        mock_core = MagicMock()
        mock_core._annotations = {"__rpc__.exports": set()}
        mock_get_core_builder.return_value.build.return_value = mock_core

        # Mock credentials to bypass file loading
        mock_credentials = MagicMock()
        mock_credentials.identity = "test_identity"
        mock_credentials.publickey = "test_public_key"
        mock_load_credentials.return_value = mock_credentials

        # Mock reservation manager state operations
        mock_save_state.return_value = None
        mock_load_state.return_value = None

        # Initialize the agent and add frequently used mocks
        PDA = PlatformDriverAgent()
        PDA.core = mock_core
        PDA.vip = MagicMock()
        PDA.vip.pubsub.publish = MagicMock()
        PDA._push_result_topic_pair = Mock()
        PDA.equipment_tree = MagicMock()
        PDA.equipment_tree.root = "root"

        return PDA
