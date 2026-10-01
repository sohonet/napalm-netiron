"""Tests for getters."""

from napalm.base.test.getters import BaseTestGetters, wrap_test_cases


import pytest


# Interface passed to the interface detail getters for each test case in mocked_data
# Both forms are accepted, the long form as returned by the getters and the short form used in the commands
INTERFACE_DETAIL_TEST_CASES = {
    "lag_primary": "10GigabitEthernet2/5",
    "lag_secondary_down": "ethernet 2/6",
    "untagged_port": "10GigabitEthernet2/1",
    "tagged_port": "ethernet 2/3",
    "ve": "Ve13",
}

IP_INTERFACE_DETAIL_TEST_CASES = {
    "routed_port": "10GigabitEthernet2/5",
    "ve": "ve 4",
    "ve_lag": "Ve95",
}


@pytest.mark.usefixtures("set_device_parameters")
class TestGetter(BaseTestGetters):
    """Test get_* methods."""

    # Skip test_method_signatures - we have additional getters
    def test_method_signatures(self):
        return True

    # Unsupported functions
    def test_get_interfaces_counters(self):
        return True

    def test_get_environment(self):
        return True

    def test_get_arp_table_with_vrf(self):
        return True

    def test_get_ntp_peers(self):
        return True

    def test_get_ntp_servers(self):
        return True

    def test_get_ntp_stats(self):
        return True

    def test_get_users(self):
        return True

    def test_get_config(self):
        return True

    def test_get_config_filtered(self):
        return True

    def test_get_config_sanitized(self):
        return True

    def test_get_lldp_neighbors_detail(self):
        return True

    def test_get_bgp_neighbors_detail(self):
        return True

    def test_get_ipv6_neighbors_table(self):
        return True

    def test_get_route_to(self):
        return True

    def test_get_route_to_longer(self):
        return True

    def test_get_snmp_information(self):
        return True

    def test_ping(self):
        return True

    def test_traceroute(self):
        return True

    def test_get_mac_address_table(self):
        return True

    def test_get_bgp_neighbors(self):
        return True

    # Additional getters
    @wrap_test_cases
    def test_get_isis_neighbors(self, test_case):
        return self.device.get_isis_neighbors()

    @wrap_test_cases
    def test_get_bfd_neighbors(self, test_case):
        return self.device.get_bfd_neighbors()

    @wrap_test_cases
    def test_get_ldp_sessions(self, test_case):
        return self.device.get_ldp_sessions()

    @wrap_test_cases
    def test_get_interface_detail(self, test_case):
        return self.device.get_interface_detail(INTERFACE_DETAIL_TEST_CASES[test_case])

    @wrap_test_cases
    def test_get_ip_interface_detail(self, test_case):
        return self.device.get_ip_interface_detail(IP_INTERFACE_DETAIL_TEST_CASES[test_case])

    @pytest.mark.parametrize(
        "interface", ["ethernet 2/5; reload", "10GigabitEthernet2/5 | include x", "ve", "Ve13a", "loopback 1", ""]
    )
    def test_get_interface_detail_invalid_interface(self, interface):
        with pytest.raises(ValueError):
            self.device.get_interface_detail(interface)
        with pytest.raises(ValueError):
            self.device.get_ip_interface_detail(interface)
