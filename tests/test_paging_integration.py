#!/usr/bin/env python3
"""
Test paging system integration with PBX core
"""

from typing import Any

from pbx.features.paging import PagingSystem


def test_paging_system_initialization() -> bool:
    """Test paging system initialization"""

    # Create a mock config object with proper structure
    class MockConfig:
        def get(self, key: str, default: Any = None) -> Any:
            config_map = {
                "features.paging.enabled": True,
                "features.paging.prefix": "7",
                "features.paging.all_call_extension": "700",
                "features.paging.zones": [
                    {
                        "extension": "701",
                        "name": "Zone 1 - Office",
                        "description": "Main office area",
                        "dac_device": "test-device-1",
                    },
                    {
                        "extension": "702",
                        "name": "Zone 2 - Warehouse",
                        "description": "Warehouse area",
                        "dac_device": "test-device-1",
                    },
                ],
                "features.paging.dac_type": "sip_gateway",
                "features.paging.dac_devices": [
                    {
                        "device_id": "test-device-1",
                        "device_type": "cisco_vg224",
                        "sip_uri": "sip:paging@192.168.1.100:5060",
                        "ip_address": "192.168.1.100",
                        "port": 5060,
                    }
                ],
            }
            return config_map.get(key, default)

    config = MockConfig()
    paging = PagingSystem(config)

    assert paging.enabled, "Paging should be enabled"
    assert paging.paging_prefix == "7", "Paging prefix should be '7'"
    assert paging.all_call_extension == "700", "All-call extension should be '700'"
    assert len(paging.zones) == 2, "Should have 2 zones"
    assert len(paging.dac_devices) == 1, "Should have 1 DAC device"

    return True


def test_paging_extension_detection() -> bool:
    """Test paging extension detection"""

    class MockConfig:
        def get(self, key: str, default: Any = None) -> Any:
            config_map = {
                "features.paging.enabled": True,
                "features.paging.prefix": "7",
                "features.paging.all_call_extension": "700",
                "features.paging.zones": [{"extension": "701", "name": "Zone 1"}],
                "features.paging.dac_type": "sip_gateway",
                "features.paging.dac_devices": [],
            }
            return config_map.get(key, default)

    config = MockConfig()
    paging = PagingSystem(config)

    # Test paging extensions
    assert paging.is_paging_extension("700"), "700 should be paging extension"
    assert paging.is_paging_extension("701"), "701 should be paging extension"
    assert paging.is_paging_extension("702"), "702 should be paging extension"

    # Test non-paging extensions
    assert paging.is_paging_extension("1001") is False, "1001 should not be paging extension"
    assert paging.is_paging_extension("8001") is False, "8001 should not be paging extension"

    return True


def test_zone_management() -> bool:
    """Test zone management"""

    class MockConfig:
        def get(self, key: str, default: Any = None) -> Any:
            config_map = {
                "features.paging.enabled": True,
                "features.paging.prefix": "7",
                "features.paging.all_call_extension": "700",
                "features.paging.zones": [],
                "features.paging.dac_type": "sip_gateway",
                "features.paging.dac_devices": [],
            }
            return config_map.get(key, default)

    config = MockConfig()
    paging = PagingSystem(config)

    # Add a zone
    success = paging.add_zone(
        extension="701",
        name="Test Zone",
        description="Test zone description",
        dac_device="test-device",
    )
    assert success, "Should successfully add zone"
    assert len(paging.zones) == 1, "Should have 1 zone"

    # Get zone by extension
    zone = paging.get_zone_for_extension("701")
    assert zone is not None, "Should find zone"
    assert zone["name"] == "Test Zone", "Zone name should match"

    # Try to add duplicate zone
    success = paging.add_zone(extension="701", name="Duplicate Zone", description="Should fail")
    assert success is False, "Should not add duplicate zone"

    # Remove zone
    success = paging.remove_zone("701")
    assert success, "Should successfully remove zone"
    assert len(paging.zones) == 0, "Should have 0 zones"

    # Try to remove non-existent zone
    success = paging.remove_zone("999")
    assert success is False, "Should not remove non-existent zone"

    return True


def test_page_initiation() -> bool:
    """Test page initiation"""

    class MockConfig:
        def get(self, key: str, default: Any = None) -> Any:
            config_map = {
                "features.paging.enabled": True,
                "features.paging.prefix": "7",
                "features.paging.all_call_extension": "700",
                "features.paging.zones": [
                    {"extension": "701", "name": "Zone 1", "dac_device": "test-device"}
                ],
                "features.paging.dac_type": "sip_gateway",
                "features.paging.dac_devices": [],
            }
            return config_map.get(key, default)

    config = MockConfig()
    paging = PagingSystem(config)

    # Initiate a page to specific zone
    page_id = paging.initiate_page("1001", "701")
    assert page_id is not None, "Should return page ID"
    assert page_id.startswith("page-"), "Page ID should start with 'page-'"

    # Check active pages
    active_pages = paging.get_active_pages()
    assert len(active_pages) == 1, "Should have 1 active page"
    assert active_pages[0]["from_extension"] == "1001", "From extension should match"
    assert active_pages[0]["to_extension"] == "701", "To extension should match"

    # Get page info
    page_info = paging.get_page_info(page_id)
    assert page_info is not None, "Should get page info"
    assert page_info["zone_names"] == "Zone 1", "Zone name should match"

    # End the page
    success = paging.end_page(page_id)
    assert success, "Should successfully end page"
    assert len(paging.get_active_pages()) == 0, "Should have 0 active pages"

    return True


def test_all_call_paging() -> bool:
    """Test all-call paging"""

    class MockConfig:
        def get(self, key: str, default: Any = None) -> Any:
            config_map = {
                "features.paging.enabled": True,
                "features.paging.prefix": "7",
                "features.paging.all_call_extension": "700",
                "features.paging.zones": [
                    {"extension": "701", "name": "Zone 1"},
                    {"extension": "702", "name": "Zone 2"},
                    {"extension": "703", "name": "Zone 3"},
                ],
                "features.paging.dac_type": "sip_gateway",
                "features.paging.dac_devices": [],
            }
            return config_map.get(key, default)

    config = MockConfig()
    paging = PagingSystem(config)

    # Initiate all-call page
    page_id = paging.initiate_page("1001", "700")
    assert page_id is not None, "Should return page ID for all-call"

    # Get page info
    page_info = paging.get_page_info(page_id)
    assert page_info is not None, "Should get page info"
    assert page_info["zone_names"] == "All Zones", "Should be all zones"
    assert len(page_info["zones"]) == 3, "Should include all 3 zones"

    # End the page
    paging.end_page(page_id)

    return True


def test_dac_device_configuration() -> bool:
    """Test DAC device configuration"""

    class MockConfig:
        def get(self, key: str, default: Any = None) -> Any:
            config_map = {
                "features.paging.enabled": True,
                "features.paging.prefix": "7",
                "features.paging.all_call_extension": "700",
                "features.paging.zones": [],
                "features.paging.dac_type": "sip_gateway",
                "features.paging.dac_devices": [],
            }
            return config_map.get(key, default)

    config = MockConfig()
    paging = PagingSystem(config)

    # Configure a DAC device
    success = paging.configure_dac_device(
        device_id="gateway-1",
        device_type="cisco_vg224",
        sip_uri="sip:paging@192.168.1.100",
        ip_address="192.168.1.100",
        port=5060,
    )
    assert success, "Should successfully configure device"
    assert len(paging.dac_devices) == 1, "Should have 1 device"

    # Get devices
    devices = paging.get_dac_devices()
    assert len(devices) == 1, "Should return 1 device"
    assert devices[0]["device_id"] == "gateway-1", "Device ID should match"
    assert devices[0]["device_type"] == "cisco_vg224", "Device type should match"

    # Try to add duplicate device
    success = paging.configure_dac_device(
        device_id="gateway-1", device_type="grandstream_ht802", ip_address="192.168.1.101"
    )
    assert success is False, "Should not add duplicate device"

    return True


def test_paging_disabled() -> bool:
    """Test paging system when disabled"""

    class MockConfig:
        def get(self, key: str, default: Any = None) -> Any:
            config_map: dict[str, Any] = {"features.paging.enabled": False}
            return config_map.get(key, default)

    config = MockConfig()
    paging = PagingSystem(config)

    assert paging.enabled is False, "Paging should be disabled"
    assert paging.is_paging_extension("700") is False, "Should not detect paging extensions"
    assert paging.initiate_page("1001", "700") is None, "Should not initiate page"
    assert len(paging.get_zones()) == 0, "Should return empty zones"

    return True


def test_dac_device_removal() -> bool:
    """Removing a DAC device should also unlink zones that referenced it"""

    class MockConfig:
        def get(self, key: str, default: Any = None) -> Any:
            config_map: dict[str, Any] = {
                "features.paging.enabled": True,
                "features.paging.zones": [
                    {"extension": "701", "name": "Office", "dac_device": "gw-1"},
                    {"extension": "702", "name": "Warehouse", "dac_device": "gw-2"},
                ],
                "features.paging.dac_devices": [
                    {"device_id": "gw-1", "device_type": "cisco_vg224"},
                    {"device_id": "gw-2", "device_type": "cisco_vg224"},
                ],
            }
            return config_map.get(key, default)

    paging = PagingSystem(MockConfig())

    assert paging.remove_dac_device("gw-1") is True, "Should remove a configured device"
    assert len(paging.get_dac_devices()) == 1, "Should have 1 device left"

    # The zone survives, but no longer points at a device that is gone -- a
    # dangling reference would answer pages and silently route no audio.
    office = paging.get_zone_for_extension("701")
    assert office is not None, "Zone should still exist"
    assert office["dac_device"] is None, "Zone's device reference should be cleared"

    # Zones on other devices are untouched.
    warehouse = paging.get_zone_for_extension("702")
    assert warehouse is not None and warehouse["dac_device"] == "gw-2", "Other zone unaffected"

    assert paging.remove_dac_device("nope") is False, "Unknown device should report failure"

    return True


def test_dac_device_removal_when_disabled() -> bool:
    """Device removal should be refused while paging is disabled"""

    class MockConfig:
        def get(self, key: str, default: Any = None) -> Any:
            config_map: dict[str, Any] = {"features.paging.enabled": False}
            return config_map.get(key, default)

    paging = PagingSystem(MockConfig())
    assert paging.remove_dac_device("gw-1") is False, "Should refuse while disabled"

    return True


def test_get_status_reports_enabled_state() -> bool:
    """get_status should report meaningfully in both enabled and disabled states"""

    class EnabledConfig:
        def get(self, key: str, default: Any = None) -> Any:
            config_map: dict[str, Any] = {
                "features.paging.enabled": True,
                "features.paging.prefix": "7",
                "features.paging.all_call_extension": "700",
                "features.paging.max_duration": 90,
                "features.paging.zones": [
                    {"extension": "701", "name": "Office", "dac_device": "gw-1"}
                ],
                "features.paging.dac_devices": [
                    {"device_id": "gw-1", "device_type": "cisco_vg224"}
                ],
            }
            return config_map.get(key, default)

    status = PagingSystem(EnabledConfig()).get_status()
    assert status["enabled"] is True, "Should report enabled"
    assert status["all_call_extension"] == "700", "Should report all-call extension"
    assert status["max_duration"] == 90, "Should report max duration"
    assert status["zone_count"] == 1, "Should count zones"
    assert status["dac_device_count"] == 1, "Should count devices"
    assert status["active_page_count"] == 0, "Should start with no active pages"

    class DisabledConfig:
        def get(self, key: str, default: Any = None) -> Any:
            config_map: dict[str, Any] = {"features.paging.enabled": False}
            return config_map.get(key, default)

    # Unlike the other getters, this one still answers when disabled -- that is
    # the whole point: it is how a client tells "off" from "on but empty".
    disabled_status = PagingSystem(DisabledConfig()).get_status()
    assert disabled_status["enabled"] is False, "Should report disabled"
    assert disabled_status["zone_count"] == 0, "Should report no zones when disabled"

    return True
