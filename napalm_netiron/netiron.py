"""
NAPALM Brocade/Foundry netiron IOS Handler.

Note this port is based on the Cisco IOS handler.  The following copyright is from the napalm project:

# Copyright 2015 Spotify AB. All rights reserved.
#
# The contents of this file are licensed under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

Additionally, some code was taken from https://github.com/ckishimo/napalm-extreme-netiron/tree/master. Author
Carles Kishimoto carles.kishimoto@gmail.com contributed the following which have been modified as needed:
 - get_bgp_neighbors
 - get_environment
 - get_mac_address_table

 A note on interface names

 NetIron is inconsistent in how it names interfaces in command output. For consistency the handler should use the interface names reported by ifDescription, i.e.:

 GigabitEthernet1/1
 Ve16
 Tunnel1
 Ethernetmgmt1
 Loopback1

"""


from __future__ import print_function
from __future__ import unicode_literals

import re
import os
import uuid
import socket
import tempfile
import logging
import sys
import io
from threading import Thread
import socket

from netmiko import ConnectHandler, redispatch
from napalm.base.base import NetworkDriver
from napalm.base.exceptions import (
    ReplaceConfigException,
    MergeConfigException,
    ConnectionClosedException,
    CommandErrorException,
)

from netaddr import IPAddress, IPNetwork
import napalm.base.helpers
from napalm.base.helpers import textfsm_extractor

import time
import tftpy


# Easier to store these as constants
HOUR_SECONDS = 3600
DAY_SECONDS = 24 * HOUR_SECONDS
WEEK_SECONDS = 7 * DAY_SECONDS
YEAR_SECONDS = 365 * DAY_SECONDS

# STD REGEX PATTERNS
IP_ADDR_REGEX = r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"
IPV4_ADDR_REGEX = IP_ADDR_REGEX
IPV6_ADDR_REGEX_1 = r"::"
IPV6_ADDR_REGEX_2 = r"[0-9a-fA-F:]{1,39}::[0-9a-fA-F:]{1,39}"
IPV6_ADDR_REGEX_3 = (
    r"[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}:"
    "[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}"
)
# Should validate IPv6 address using an IP address library after matching with this regex
IPV6_ADDR_REGEX = "(?:{}|{}|{})".format(IPV6_ADDR_REGEX_1, IPV6_ADDR_REGEX_2, IPV6_ADDR_REGEX_3)

MAC_REGEX = r"[a-fA-F0-9]{4}\.[a-fA-F0-9]{4}\.[a-fA-F0-9]{4}"
VLAN_REGEX = r"\d{1,4}"
RE_IPADDR = re.compile(r"{}".format(IP_ADDR_REGEX))
RE_IPADDR_STRIP = re.compile(r"({})\n".format(IP_ADDR_REGEX))
RE_MAC = re.compile(r"{}".format(MAC_REGEX))

# Period needed for 32-bit AS Numbers
ASN_REGEX = r"[\d\.]+"

"""
Per netiron 5.9 docs:
maxttl value parameter is the maximum TTL (hops) value: Possible value is 1 - 255. The default is 30 seconds.
minttl value parameter is the minimum TTL (hops) value: Possible value is 1 - 255. The default is 1 second.
timeout value parameter specifies the possible values. Possible value range is 1 - 120. Default value is 2 seconds.

Use these defaults
"""
TRACEROUTE_TTL = 30
TRACEROUTE_SOURCE = ""
TRACEROUTE_TIMEOUT = 2
TRACEROUTE_NULL_HOST_NAME = "*"
TRACEROUTE_NULL_IP_ADDRESS = "*"
TRACEROUTE_VRF = ""

"""
Per netiron 5.9 docs:

- count num parameter specifies how many ping packets the device sends. 1-4294967296 . default is 1.
- timeout msec parameter specifies how many milliseconds the device waits for a reply from the pinged device.
    1 - 4294967296 milliseconds. The default is 5000 (5 seconds).
- ttl num parameter specifies the maximum number of hops. You can specify a TTL from 1 - 255. The default is 64.
- size byte parameter specifies the size of the ICMP data portion of the packet. This is the payload and does not
    include the header. 0 - 9170. The default is 16.
"""
PING_SOURCE = ""
PING_TTL = 64
PING_TIMEOUT = 2
PING_SIZE = 16
PING_COUNT = 1
PING_VRF = ""


SUPPORTED_ROUTING_PROTOCOLS = ["bgp"]

logger = logging.getLogger(__name__)


class NetIronDriver(NetworkDriver):
    """NAPALM Brocade/Foundry netiron Handler."""

    def __init__(self, hostname, username, password, timeout=60, **optional_args):
        """NAPALM Brocade/Foundry netiron Handler."""

        if optional_args is None:
            optional_args = {}

        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout

        # default to MLX for now
        self.family = "MLX"

        # tmp path
        self._tmp_working_path = optional_args.get("tmp_working_path", "/tmp")

        # uuid
        self._uuid = optional_args.get("uuid", uuid.uuid4())

        # support optional SSH proxy
        self._use_proxy = optional_args.pop("use_proxy", None)

        # Control automatic toggling of 'file prompt quiet' for file operations
        self.auto_file_prompt = optional_args.get("auto_file_prompt", True)

        # it appears that in some cases, devices that may be impacted by network delay, incrementing
        # the netmiko delay factor will help -- I found increasing the delay may help long-running
        # commands like 'show ip bgp neighbors'
        #
        # however, setting the delay global will slow down simple processing such as authentication
        # or finding the prompt -- these commands seem to work best with a delay factor of 1
        self._show_command_delay_factor = optional_args.pop("show_command_delay_factor", 1)

        # Netmiko possible arguments
        netmiko_argument_map = {
            "port": None,
            "secret": "",
            "verbose": False,
            "keepalive": 30,
            "global_delay_factor": 1,
            "use_keys": False,
            "key_file": None,
            "ssh_strict": False,
            "system_host_keys": False,
            "alt_host_keys": False,
            "alt_key_file": "",
            "ssh_config_file": None,
            "allow_agent": False,
        }

        # Build dict of any optional Netmiko args
        self.netmiko_optional_args = {}
        for k, v in netmiko_argument_map.items():
            try:
                self.netmiko_optional_args[k] = optional_args[k]
            except KeyError:
                pass

        self.port = optional_args.get("port", 22)
        self.device = None
        self.merge_candidate = False

        self.profile = ["netiron"]

        # Cached command output
        self.show_int = None
        self.show_int_brief_wide = None
        self.show_vlan = None
        self.show_running_config_lag = None
        self.show_mpls_config = None

        # Cached interface number to name dict
        self.interface_map = None

    def open(self):
        """Open a connection to the device."""
        device_type = "brocade_netiron"

        if self._use_proxy:
            logger.info("{0}: using SSH proxy {1}".format(self.hostname, self._use_proxy))

            self.device = ConnectHandler(
                device_type="terminal_server",
                host=self._use_proxy,
                username=self.username,
                password=self.password,
                **self.netmiko_optional_args,
            )
            logger.debug("{0}: proxy prompt: ", self.hostname, self.device.find_prompt())

            _cmd = "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null  -t -l {0} {1}\n".format(
                self.username, self.hostname
            )
            logger.debug("{0}: proxy cmd: {1}".format(self.hostname, _cmd))
            self.device.write_channel(_cmd)
            time.sleep(1)

            for t in range(0, 4):
                _output = self.device.read_channel()
                # print('output: [{0}]'.format(_output))
                if "ssword" in _output:
                    self.device.write_channel(self.password + "\n")
                    time.sleep(2)
                    _output = self.device.read_channel()
                    break
                time.sleep(1)

            redispatch(self.device, device_type=device_type)
            # print('device: {0}'.format(self.device))
        else:
            self.device = ConnectHandler(
                device_type=device_type,
                host=self.hostname,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                conn_timeout=self.timeout,
                **self.netmiko_optional_args,
            )

        # ensure in enable mode
        self.device.enable()

    def close(self):
        """Close the connection to the device."""
        self.device.disconnect()

    def _send_command(self, command):
        """Wrapper for self.device.send.command().

        If command is a list will iterate through commands until valid command.
        """
        try:
            output = ""
            if isinstance(command, list):
                for cmd in command:
                    output = self.device.send_command(cmd)
                    if "% Invalid" not in output:
                        break
            else:
                output = self.device.send_command(command)
            return self._send_command_postprocess(output)
        except (socket.error, EOFError) as e:
            raise ConnectionClosedException(str(e))

    # NAPALM FUNCTIONS

    def is_alive(self):
        """Returns a flag with the state of the connection."""
        null = chr(0)
        _status = False

        if self.device:
            # SSH
            try:
                # Try sending ASCII null byte to maintain the connection alive
                self.device.write_channel(null)
                _status = self.device.remote_conn.transport.is_active()
            except (socket.error, EOFError):
                # If unable to send, we can tell for sure that the connection is unusable
                pass
        return {"is_alive": _status}

    def load_replace_candidate(self, filename=None, config=None):
        """
        In our case we do NOT ever want to perform a replacement
        """
        raise NotImplementedError("config replacement is not supported on Brocade devices")

    def load_merge_candidate(self, filename=None, config=None):
        """
        Create merge candidate. This only stores the config in self.merge_candidate
        """

        if filename and config:
            raise MergeConfigException("Cannot specify both filename and config")

        if filename:
            with open(filename, "r") as stream:
                self.merge_candidate = stream.read()

        if config:
            self.merge_candidate = config

    def compare_config(self):
        """Not supported on Brocade"""
        raise NotImplemented("config compare is not supported on Brocade devices")

    def commit_config(self, message=""):
        """
        Send self.merge_candidate to running-config via tftp
        """

        if not self.merge_candidate:
            raise MergeConfigException("No merge candidate loaded")

        with tempfile.TemporaryDirectory() as temp_dir:
            # Setup TFTP server
            tftp_server = tftpy.TftpServer(tftproot=temp_dir, dyn_file_func=self._tftp_handler(self.merge_candidate))
            tftp_thread = Thread(target=tftp_server.listen)
            tftp_thread.daemon = True
            tftp_thread.start()

            result = self._send_command(["copy tftp running-config " + self._get_ipaddress() + " merge_candidate"])
            logger.info(result)

            tftp_server.stop()
            tftp_thread.join()

    def discard_config(self):
        """Discard loaded candidate configurations."""
        self.merge_candidate = False

    def get_optics(self):
        """
        Not implemented

        Brocade will likely require "show media" followed by "show optic <slot>" for each slot

        Optionally use snmp snIfOpticalMonitoringInfoTable - ifIndex to optical parameters table
        :return:
        """
        """
        command = 'show interfaces transceiver'
        output = self._send_command(command)

        # Check if router supports the command
        if '% Invalid input' in output:
            return {}

        # Formatting data into return data structure
        optics_detail = {}

        try:
            split_output = re.split(r'^---------.*$', output, flags=re.M)[1]
        except IndexError:
            return {}

        split_output = split_output.strip()

        for optics_entry in split_output.splitlines():
            # Example, Te1/0/1      34.6       3.29      -2.0      -3.5
            try:
                split_list = optics_entry.split()
            except ValueError:
                return {}

            int_brief = split_list[0]
            output_power = split_list[3]
            input_power = split_list[4]

            port = canonical_interface_name(int_brief)

            port_detail = {}

            port_detail['physical_channels'] = {}
            port_detail['physical_channels']['channel'] = []

            # If interface is shutdown it returns "N/A" as output power.
            # Converting that to -100.0 float
            try:
                float(output_power)
            except ValueError:
                output_power = -100.0

            # Defaulting avg, min, max values to -100.0 since device does not
            # return these values
            optic_states = {
                'index': 0,
                'state': {
                    'input_power': {
                        'instant': (float(input_power) if 'input_power' else -100.0),
                        'avg': -100.0,
                        'min': -100.0,
                        'max': -100.0
                    },
                    'output_power': {
                        'instant': (float(output_power) if 'output_power' else -100.0),
                        'avg': -100.0,
                        'min': -100.0,
                        'max': -100.0
                    },
                    'laser_bias_current': {
                        'instant': 0.0,
                        'avg': 0.0,
                        'min': 0.0,
                        'max': 0.0
                    }
                }
            }

            port_detail['physical_channels']['channel'].append(optic_states)
            optics_detail[port] = port_detail

        return optics_detail
        """
        raise NotImplementedError

    def get_facts(self):
        """get_facts method."""
        uptime = -1
        vendor = "Brocade"
        model = None
        hostname = None
        version = "netiron"
        serial = None

        command = "show version"
        lines = self.device.send_command_timing(command, delay_factor=self._show_command_delay_factor)
        for line in lines.splitlines():
            r1 = re.match(r"^(System|Chassis):\s+(.*)\s+\(Serial #:\s+(\S+),(.*)", line)
            if r1:
                model = r1.group(2)
                serial = r1.group(3)

            r2 = re.match(r"^IronWare : Version\s+(\S+)\s+Copyright \(c\)\s+(.*)", line)
            if r2:
                version = r2.group(1)
                vendor = r2.group(2)

        command = "show uptime"
        lines = self.device.send_command_timing(command, delay_factor=self._show_command_delay_factor)
        for line in lines.splitlines():
            # Get the uptime from the Active MP module
            r1 = re.match(
                r"\s+Active MP(.*)Uptime\s+(\d+)\s+days"
                r"\s+(\d+)\s+hours"
                r"\s+(\d+)\s+minutes"
                r"\s+(\d+)\s+seconds",
                line,
            )
            if r1:
                days = int(r1.group(2))
                hours = int(r1.group(3))
                minutes = int(r1.group(4))
                seconds = int(r1.group(5))
                uptime = seconds + minutes * 60 + hours * 3600 + days * 86400

        # Infer hostname from the prompt
        hostname = self.device.base_prompt.replace("SSH@", "")

        facts = {
            "uptime": float(uptime),
            "vendor": str(vendor),
            "model": str(model),
            "hostname": str(hostname),
            # FIXME: fqdn
            "fqdn": str("Unknown"),
            "os_version": str(version),
            "serial_number": str(serial),
            "interface_list": [],
        }

        # Get interfaces
        if not self.show_int_brief_wide or "pytest" in sys.modules:
            self.show_int_brief_wide = self.device.send_command_timing(
                "show int brief wide", delay_factor=self._show_command_delay_factor
            )
        info = textfsm_extractor(self, "show_interface_brief_wide", self.show_int_brief_wide)
        for interface in info:
            port = self.standardize_interface_name(interface["port"])
            facts["interface_list"].append(port)

        # Add lags to interfaces
        lags = self.get_lags()
        facts["interface_list"] += list(lags.keys())

        return facts

    def get_interfaces(self):
        """get_interfaces method."""
        if not self.show_int or "pytest" in sys.modules:
            self.show_int = self.device.send_command_timing(
                "show interface", delay_factor=self._show_command_delay_factor
            )
        info = textfsm_extractor(self, "show_interface", self.show_int)

        show_mpls_interface = self.device.send_command_timing(
            "show mpls interface brief", delay_factor=self._show_command_delay_factor
        )
        mpls_info = textfsm_extractor(self, "show_mpls_interface_brief", show_mpls_interface)

        vlans = self.get_vlans()

        result = {}
        for interface in info:
            port = self.standardize_interface_name(interface["port"])

            # Convert speeds to MB/s
            speed = interface["speed"]
            SPEED_REG = r"^(?P<number>\d+)(?P<unit>\S+)$"
            speed_m = re.match(SPEED_REG, speed)
            if speed_m:
                if speed_m.group("unit") in ["M", "Mbit"]:
                    speed = int(int(speed_m.group("number")))
                elif speed_m.group("unit") in ["G", "Gbit"]:
                    speed = int(int(speed_m.group("number")) * 10e2)
            if speed == "unknown" or not speed:
                speed = 0

            result[port] = {
                "is_up": interface["link"].lower() == "up",
                "is_enabled": interface["link"].lower() != "disabled",
                "description": interface["name"],
                "last_flapped": float(-1),
                "speed": float(speed),
                "mac_address": interface["mac"],
                "mtu": int(interface["mtu"]),
                "mpls_enabled": False,
            }

            # Add ve_children to VEs
            if re.match(r"^Ve", port):
                # Get list of interfaces with the same VLAN as the Ve, excluding the Ve.
                # These are the physical interfaces that form the Ve.
                result[port]["ve_children"] = []
                for vlan, data in vlans.items():
                    if port in data["interfaces"]:
                        for interface in data["interfaces"]:
                            if interface != port:
                                result[port]["ve_children"].append(interface)

        # Add MPLS Enabled value
        for mpls_interface in mpls_info:
            if mpls_interface["ldp"].lower() == "yes":
                port = self.standardize_interface_name(mpls_interface["interface"])
                if result.get(port):
                    result[port]["mpls_enabled"] = True

        # Get lags
        lags = self.get_lags()
        result.update(lags)

        # Remove extra keys to make tests pass
        if "pytest" in sys.modules:
            return self._delete_keys_from_dict(result, ["children", "type", "mpls_enabled", "ve_children"])

        return result

    def get_interfaces_ip(self):
        """get_interfaces_ip method."""
        interfaces = {}

        output = self.device.send_command_timing(
            "show running-config interface", delay_factor=self._show_command_delay_factor
        )
        info = textfsm_extractor(self, "show_running_config_interface", output)

        for intf in info:
            port = self.standardize_interface_name(intf["interface"] + intf["interfacenum"])

            if port not in interfaces:
                interfaces[port] = {
                    "ipv4": {},
                    "ipv6": {},
                }

            if intf["ipv4address"]:
                ipaddress, prefix = intf["ipv4address"].split("/")
                interfaces[port]["ipv4"][ipaddress] = {"prefix_length": int(prefix)}
            if intf["ipv6address"]:
                ipaddress, prefix = intf["ipv6address"].split("/")
                interfaces[port]["ipv6"][ipaddress] = {"prefix_length": int(prefix)}
            if intf["vrfname"]:
                interfaces[port]["vrf"] = intf["vrfname"]
            if intf["interfaceacl"]:
                interfaces[port]["interfaceacl"] = intf["interfaceacl"]

        return interfaces

    def get_interfaces_vlans(self):
        """return dict as documented at https://github.com/napalm-automation/napalm/issues/919#issuecomment-485905491"""

        if not self.show_int or "pytest" in sys.modules:
            self.show_int = self.device.send_command_timing(
                "show interface", delay_factor=self._show_command_delay_factor
            )
        info = textfsm_extractor(self, "show_interface", self.show_int)

        result = {}

        # Create interfaces structure and correct mode
        for interface in info:
            intf = self.standardize_interface_name(interface["port"])
            if interface["tag"] == "untagged" or re.match(r"^ve", interface["port"].lower()):
                mode = "access"
            else:
                mode = "trunk"
            result[intf] = {
                "mode": mode,
                "access-vlan": -1,
                "trunk-vlans": [],
                "native-vlan": -1,
                "tagged-native-vlan": False,
            }

        # Add lags
        for lag in self.get_lags().keys():
            result[lag] = {
                "mode": "trunk",
                "access-vlan": -1,
                "trunk-vlans": [],
                "native-vlan": -1,
                "tagged-native-vlan": False,
            }

        if not self.show_vlan or "pytest" in sys.modules:
            self.show_vlan = self.device.send_command("show vlan", read_timeout=self.timeout)
        info = textfsm_extractor(self, "show_vlan", self.show_vlan)

        # Assign VLANs to interfaces
        for vlan in info:
            access_ports = self.interface_list_conversion(vlan["ve"], "", vlan["untaggedports"])
            trunk_ports = self.interface_list_conversion("", vlan["taggedports"], "")

            for port in access_ports:
                if int(vlan["vlan"]) <= 4094:
                    result[port]["access-vlan"] = vlan["vlan"]

            for port in trunk_ports:
                if int(vlan["vlan"]) <= 4094:
                    result[port]["trunk-vlans"].append(vlan["vlan"])

        # Add ports with VLANs from VLLs
        if not self.show_mpls_config:
            self.show_mpls_config = self.device.send_command("show mpls config")
        info = textfsm_extractor(self, "show_mpls_config", self.show_mpls_config)
        for vll in info:
            interface = self.standardize_interface_name(vll["interface"])
            # Ignore VLLs with no interface
            if interface:
                result[interface]["trunk-vlans"].append(vll["vlan"])

        # Set native vlan for tagged ports
        for port, data in result.items():
            if data["trunk-vlans"] and data["access-vlan"]:
                result[port]["native-vlan"] = data["access-vlan"]
                result[port]["access-vlan"] = -1

        return result

    def get_vlans(self):
        if not self.show_vlan or "pytest" in sys.modules:
            self.show_vlan = self.device.send_command("show vlan", read_timeout=self.timeout)
        info = textfsm_extractor(self, "show_vlan", self.show_vlan)

        result = {}
        for vlan in info:
            result[vlan["vlan"]] = {
                "name": vlan["name"],
                "interfaces": self.interface_list_conversion(vlan["ve"], vlan["taggedports"], vlan["untaggedports"]),
            }

        # Add ports with VLANs from VLLs
        if not self.show_mpls_config or "pytest" in sys.modules:
            self.show_mpls_config = self.device.send_command("show mpls config")
        info = textfsm_extractor(self, "show_mpls_config", self.show_mpls_config)
        for vll in info:
            if vll["vlan"] not in result:
                result[vll["vlan"]] = {
                    "name": vll["name"],
                    "interfaces": [],
                }

            result[vll["vlan"]]["interfaces"].append(self.standardize_interface_name(vll["interface"]))

        return result

    def get_lldp_neighbors(self):
        """
            Returns a dictionary where the keys are local ports and the value is a list of \
            dictionaries with the following information:
                * hostname
                * port
            """
        my_dict = {}
        shw_int_neg = self.device.send_command("show lldp neighbors detail")
        info = textfsm_extractor(self, "show_lldp_neighbors_detail", shw_int_neg)

        for result in info:
            if result["remoteportid"]:
                port = result["remoteportid"]
            else:
                port = result["remoteportdescription"]
            local_port = self.standardize_interface_name(result["port"])

            if local_port not in my_dict.keys():
                my_dict[local_port] = []
            my_dict[local_port].append(
                {"hostname": re.sub('"', "", result["remotesystemname"]), "port": re.sub('"', "", port)}
            )

        return my_dict

    def get_bgp_route(self, prefix):
        """
        Execute show ip[v6] bgp route <prefix> and return the output in a dictionary format

        {
          "ip_version": 4,
          "prefix": "47.186.1.43",
          "routes": [
            {
              "index": "1",
              "local_pref": "320",
              "med": "0",
              "next_hop": "74.43.96.220",
              "prefix": "47.184.0.0/14",
              "status": "BE",
              "weight": "0"
            },
            ...

        :param prefix: IPv6 or IPv6 prefix in CIDR notation
        :return: dictionary of route info on success or error message on error/no route found
        """
        _prefix_net = IPNetwork(prefix)
        if not _prefix_net:
            raise ValueError("prefix must be a valid prefix")

        command = "show ip{0} bgp route {1}".format("" if _prefix_net.version == 4 else "v6", prefix)
        _lines = self.device.send_command(command)

        _routes = list()
        _last_update = None
        _num_paths_installed = None

        # if no routes found; simply return error
        r1 = re.search(r"None of the BGP4 routes match the display condition", _lines, re.MULTILINE)
        if r1:
            return {"error": "No matching BGP routes found"}

        for line in _lines.splitlines():
            r2 = re.match(r"^\s*AS_PATH:\s+(?P<path>(.*))", line)
            if r2 and r1:
                _routes.append(
                    {
                        "index": r1.group("index"),
                        "prefix": r1.group("prefix"),
                        "next_hop": r1.group("next_hop"),
                        "med": r1.group("med"),
                        "local_pref": r1.group("local_pref"),
                        "weight": r1.group("weight"),
                        "status": r1.group("status"),
                        "best": True if "B" in r1.group("status") else False,
                        "as_path": r2.group("path").split(),
                    }
                )
                r1 = None
                continue

            r1 = re.match(
                r"^(?P<index>(\d+))\s+(?P<prefix>\S+)\s+(?P<next_hop>\S+)"
                r"\s+(?P<med>\d+)\s+(?P<local_pref>\d+)\s+(?P<weight>\d+)\s+(?P<status>\S+)",
                line,
            )
            if r1:
                continue

            r3 = re.match(r"^\s+Last update.*table:\s+(?P<last_update>(\S+)),\s+(?P<paths>\d+)\s+", line)
            if r3:
                _last_update = r3.group("last_update")
                _num_paths_installed = r3.group("paths")
                continue

        return {
            "success": {
                "prefix": prefix,
                "ip_version": _prefix_net.version,
                "routes": _routes,
                "routing_table": {"last_update": _last_update, "paths_installed": _num_paths_installed},
            }
        }

    def get_route_to(self, destination="", protocol=""):
        """
        Returns a dictionary of dictionaries containing details of all available routes to a
        destination.

        Note that currently only routing protocol 'bgp' is supported.

        :param destination: The destination prefix to be used when filtering the routes.
        :param protocol: (optional) Retrieve the routes only for a specific protocol.

        Each inner dictionary contains the following fields:

            * protocol (string)
            * current_active (True/False)
            * last_active (True/False)
            * age (int)
            * next_hop (string)
            * outgoing_interface (string)
            * selected_next_hop (True/False)
            * preference (int)
            * inactive_reason (string)
            * routing_table (string)
            * protocol_attributes (dictionary)

        protocol_attributes is a dictionary with protocol-specific information, as follows:

        - BGP
            * local_as (int)
            * remote_as (int)
            * peer_id (string)
            * as_path (string)
            * communities (list)
            * local_preference (int)
            * preference2 (int)
            * metric (int)
            * metric2 (int)
        - ISIS:
            * level (int)

        Example::

            {
                "1.0.0.0/24": [
                    {
                        "protocol"          : u"BGP",
                        "inactive_reason"   : u"Local Preference",
                        "last_active"       : False,
                        "age"               : 105219,
                        "next_hop"          : u"172.17.17.17",
                        "selected_next_hop" : True,
                        "preference"        : 170,
                        "current_active"    : False,
                        "outgoing_interface": u"ae9.0",
                        "routing_table"     : "inet.0",
                        "protocol_attributes": {
                            "local_as"          : 13335,
                            "as_path"           : u"2914 8403 54113 I",
                            "communities"       : [
                                u"2914:1234",
                                u"2914:5678",
                                u"8403:1717",
                                u"54113:9999"
                            ],
                            "preference2"       : -101,
                            "remote_as"         : 2914,
                            "local_preference"  : 100
                        }
                    }
                ]
            }
        """

        protocol = protocol.lower()
        if protocol != "bgp":
            raise ValueError("unsupported routing protocol: {0}".format(protocol))

        _prefix_net = IPNetwork(destination)
        if not _prefix_net:
            raise ValueError("prefix must be a valid prefix")

        command = "show ip{0} bgp route {1}".format("" if _prefix_net.version == 4 else "v6", destination)
        logger.info(command)
        _lines = self.device.send_command(command)
        logger.info(_lines)

        _routes = list()
        _last_update = None
        _num_paths_installed = None

        # if no routes found; simply return error
        r1 = re.search(r"None of the BGP4 routes match the display condition", _lines, re.MULTILINE)
        if r1:
            return {"error": "No matching BGP routes found"}

        _r1v6 = None
        _previous_line = None
        for line in _lines.splitlines():
            if _r1v6:
                # v6 is rendered differently than v4 -- v6 splits prefix info into (2) lines
                # if _rlv6 is True then the 1st line containing index, prefix and next_hop was matched
                # on the previous line
                #
                # join previous line and current line and perform r1 match
                line = "{0} {1}".format(_previous_line, line)
                r1 = re.match(
                    r"^(?P<index>(\d+))\s+(?P<prefix>\S+)\s+(?P<next_hop>\S+)"
                    r"\s+(?P<med>\d+)\s+(?P<local_pref>\d+)\s+(?P<weight>\d+)\s+(?P<status>\S+)",
                    line,
                )
                _r1v6 = None

            _previous_line = line

            r2 = re.match(r"^\s*AS_PATH:\s+(?P<path>(.*))", line)
            if r2 and r1:
                _status = r1.group("status")
                _active = True if "B" in _status else False
                _routes.append(
                    {
                        "protocol": "eBGP" if "E" in _status else "iBGP",
                        "inactive_reason": "n/a",
                        "age": 0,
                        "routing_table": "default",
                        "next_hop": r1.group("next_hop"),
                        "outgoing_interface": None,
                        "preference": 20 if "E" in _status else 200,
                        "current_active": _active,
                        "selected_next_hop": _active,
                        "protocol_attributes": {
                            "local_preference": r1.group("local_pref"),
                            "remote_as": "n/a",
                            "communities": [],
                            "preference2": 0,
                            "metric": napalm.base.helpers.convert(int, r1.group("med"), 0),
                            "weight": napalm.base.helpers.convert(int, r1.group("weight"), 0),
                            "status": r1.group("status"),
                            "local_as": 22822,
                            "as_path": r2.group("path").split(),
                            "remote_address": r1.group("next_hop"),
                        },
                    }
                )
                r1 = None
                _r1v6 = None
                continue

            r1 = re.match(
                r"^(?P<index>(\d+))\s+(?P<prefix>\S+)\s+(?P<next_hop>\S+)"
                r"\s+(?P<med>\d+)\s+(?P<local_pref>\d+)\s+(?P<weight>\d+)\s+(?P<status>\S+)",
                line,
            )

            if not r1 and _prefix_net.version == 6:
                # brocade renders differently for v6 than it does for v4 -- this is likely due to the length
                # difference between a v4 and v6 address
                # brocade renders v6 prefix over (2) lines.  The 1st line has prefix and next-hop where
                # the 2nd line has MED, LocPref...
                #        Prefix             Next Hop        MED        LocPrf     Weight Status
                # 1      2001:200:900::/40  2001:de8:8::2907:1
                #                                           0          320        0      BI
                #
                # if r1 didn't match try matching just index, prefix and next_hop
                _r1v6 = re.match(r"^(?P<index>(\d+))\s+(?P<prefix>\S+)\s+(?P<next_hop>\S+)", line)

        return {destination: _routes}

    def get_bgp_neighbors(self):
        """
        Retrieve BGP neighbors.

        FIXME: No VRF support
        :return: dict()
        """
        bgp_data = dict()
        bgp_data["global"] = dict()
        bgp_data["global"]["peers"] = dict()

        lines_summary = ""
        lines_neighbors = ""

        _stat_errors = dict()

        # retrieve both v4 and v6 BGP summary and neighbors
        for v in [4, 6]:
            command = "show ip{0} bgp summary".format("" if v == 4 else "v6")
            _lines = self.device.send_command(command)
            lines_summary += _lines + "\n" if _lines else ""

            command = "show ip{0} bgp neighbors".format("" if v == 4 else "v6")
            _lines = self.device.send_command(command)
            lines_neighbors += _lines + "\n" if _lines else ""

        local_as = 0
        for line in lines_summary.splitlines():
            r1 = re.match(
                r"^\s+Router ID:\s+(?P<router_id>({}))\s+"
                r"Local AS Number:\s+(?P<local_as>({}))".format(IPV4_ADDR_REGEX, ASN_REGEX),
                line,
            )
            if r1:
                # FIXME: Use AS numbers check: napalm.base.helpers.as_number
                router_id = r1.group("router_id")
                local_as = r1.group("local_as")
                # FIXME check the router_id looks like an ipv4 address
                # router_id = napalm.base.helpers.ip(router_id, version=4)
                bgp_data["global"]["router_id"] = router_id
                continue

            # Neighbor Address  AS#         State   Time          Rt:Accepted Filtered Sent     ToSend
            # 12.12.12.12       513         ESTAB   587d7h24m    0           0        255      0
            # NOTE: uptime is not always a single string!
            r2 = re.match(
                r"^\s+(?P<remote_addr>({}|{}))\s+(?P<remote_as>({}))\s+(?P<state>\S+)\s+"
                r"(?P<uptime>.+)"
                r"\s\s+(?P<accepted_prefixes>\d+)"
                r"\s+(?P<filtered_prefixes>\d+)"
                r"\s+(?P<sent_prefixes>\d+)"
                r"\s+(?P<tosend_prefixes>\d+)".format(IPV4_ADDR_REGEX, IPV6_ADDR_REGEX, ASN_REGEX),
                line,
            )
            if r2:
                remote_addr = napalm.base.helpers.IPAddress(r2.group("remote_addr"))

                afi = "ipv4" if remote_addr.version == 4 else "ipv6"
                received_prefixes = int(r2.group("accepted_prefixes")) + int(r2.group("filtered_prefixes"))
                bgp_data["global"]["peers"][str(remote_addr)] = {
                    "local_as": local_as,
                    "remote_as": r2.group("remote_as"),
                    "address_family": {
                        afi: {
                            "received_prefixes": received_prefixes,
                            "accepted_prefixes": r2.group("accepted_prefixes"),
                            "filtered_prefixes": r2.group("filtered_prefixes"),
                            "sent_prefixes": r2.group("sent_prefixes"),
                            "to_send_prefixes": r2.group("tosend_prefixes"),
                        }
                    },
                }
                continue

            # There is a case where brocade's formatting doesn't account for overruns and numbers are displayed
            # without a space between fields:
            # 2607:f4e8::26             22822       ESTAB   349d16h40m    1466        1191838648268     0
            # in this case just grab the 1st (4) fields and add the remote_addr to the _stats_error dict
            r2 = re.match(
                r"^\s+(?P<remote_addr>({}|{})\s+(?P<remote_as>({}))\s+(?P<state>\S+)\s+"
                r"(?P<uptime>.+)\s".format(IPV4_ADDR_REGEX, IPV6_ADDR_REGEX, ASN_REGEX),
                line,
            )
            if r2:
                logger.info("brocade overflow bug: line: {}".format(line))
                logger.info(r2.group())
                try:
                    remote_addr = napalm.base.helpers.IPAddress(r2.group("remote_addr"))
                    bgp_data["global"]["peers"][str(remote_addr)] = {
                        "local_as": local_as,
                        "remote_as": r2.group("remote_as"),
                        "address_family": self.__get_bgp_route_stats__(remote_addr),
                    }
                except Exception as ex:
                    logger.warn("unable to process overflow bug line: {}".format(ex))

        # pprint.pprint(bgp_data)

        current = ""
        for line in lines_neighbors.splitlines():
            r1 = re.match(
                r"^\d+\s+IP Address:\s+(?P<remote_addr>\S+),"
                r"\s+AS:\s+(?P<remote_as>({}))"
                r"\s+\((IBGP|EBGP)\), RouterID:\s+(?P<remote_id>({})),"
                r"\s+VRF:\s+(?P<vrf_name>\S+)".format(ASN_REGEX, IPV4_ADDR_REGEX),
                line,
            )
            if r1:
                remote_addr = r1.group("remote_addr")

                if remote_addr not in bgp_data["global"]["peers"]:
                    print("{0} not found".format(remote_addr))
                    continue

                # if remote_addr in bgp_data['global']['peers']:
                #    raise ValueError('%s already exists'.format(remote_addr))

                # pprint.pprint(remote_addr)
                remote_id = r1.group("remote_id")
                bgp_data["global"]["peers"][remote_addr]["remote_as"] = r1.group("remote_as")
                bgp_data["global"]["peers"][remote_addr]["remote_id"] = remote_id
                current = remote_addr

            r2 = re.match(r"\s+Description:\s+(.*)", line)
            if r2:
                description = r2.group(1)
                # pprint.pprint(description)
                bgp_data["global"]["peers"][current]["description"] = description

            # line:    State: ESTABLISHED, Time: 587d7h24m52s, KeepAliveTime: 10, HoldTime: 30
            r3 = re.match(
                r"\s+State:\s+(\S+),\s+Time:\s+(\S+)," r"\s+KeepAliveTime:\s+(\d+)," r"\s+HoldTime:\s+(\d+)", line
            )
            if r3:
                state = r3.group(1)

                bgp_data["global"]["peers"][current]["state"] = state
                bgp_data["global"]["peers"][current]["is_up"] = True if "ESTABLISHED" in state else False
                bgp_data["global"]["peers"][current]["is_enabled"] = False if "ADMIN_SHUTDOWN" in state else True
                bgp_data["global"]["peers"][current]["uptime"] = r3.group(2)

        return bgp_data

    def get_bgp_neighbors_detail(self, neighbor_address=""):
        """
        This code is based on the napalm.eos.get_bgp_neighbors_detail with a few variations to address
        netiron specifics

        Note that VRF support is not implemented
        :param neighbor_address: neighbor address (defaults to all if not specified)
        :return dictionary of neighbor data keyed by AS
        """

        def __process_bgp_summary_data__(lines_summary):
            """
            Process BGP summary data
            Args:
                lines_summary (str):

            Returns:
                bgp_data (dict):
            """

            bgp_data = dict()
            bgp_data["global"] = dict()
            bgp_data["global"]["peers"] = dict()

            local_as = 0
            for line in lines_summary.splitlines():
                r1 = re.match(
                    r"^\s+Router ID:\s+(?P<router_id>({}))\s+"
                    r"Local AS Number:\s+(?P<local_as>({}))".format(IPV4_ADDR_REGEX, ASN_REGEX),
                    line,
                )
                if r1:
                    # FIXME: Use AS numbers check: napalm.base.helpers.as_number
                    router_id = r1.group("router_id")
                    local_as = r1.group("local_as")
                    # FIXME check the router_id looks like an ipv4 address
                    # router_id = napalm.base.helpers.ip(router_id, version=4)
                    bgp_data["global"]["router_id"] = router_id
                    continue

                # Neighbor Address  AS#         State   Time          Rt:Accepted Filtered Sent     ToSend
                # 12.12.12.12       513         ESTAB   587d7h24m    0           0        255      0
                # NOTE: uptime is not always a single string!
                r2 = re.match(
                    r"^\s+(?P<remote_addr>({}|{}))\s+(?P<remote_as>({}))\s+(?P<state>\S+)\s+"
                    r"(?P<uptime>.+)"
                    r"\s\s+(?P<accepted_prefixes>\d+)"
                    r"\s+(?P<filtered_prefixes>\d+)"
                    r"\s+(?P<sent_prefixes>\d+)"
                    r"\s+(?P<tosend_prefixes>\d+)".format(IPV4_ADDR_REGEX, IPV6_ADDR_REGEX, ASN_REGEX),
                    line,
                )
                if r2:
                    remote_addr = napalm.base.helpers.IPAddress(r2.group("remote_addr"))

                    afi = "ipv4" if remote_addr.version == 4 else "ipv6"
                    received_prefixes = int(r2.group("accepted_prefixes")) + int(r2.group("filtered_prefixes"))
                    bgp_data["global"]["peers"][str(remote_addr)] = {
                        "local_as": local_as,
                        "remote_as": r2.group("remote_as"),
                        "address_family": {
                            afi: {
                                "received_prefixes": received_prefixes,
                                "accepted_prefixes": r2.group("accepted_prefixes"),
                                "filtered_prefixes": r2.group("filtered_prefixes"),
                                "sent_prefixes": r2.group("sent_prefixes"),
                                "to_send_prefixes": r2.group("tosend_prefixes"),
                            }
                        },
                    }
                    continue

                # There is a case where brocade's formatting doesn't account for overruns and numbers are displayed
                # without a space between fields:
                # 2607:f4e8::26             22822       ESTAB   349d16h40m    1466        1191838648268     0
                # in this case just grab the 1st (4) fields and add the remote_addr to the _stats_error dict
                r2 = re.match(
                    r"^\s+(?P<remote_addr>({}|{}))\s+(?P<remote_as>({}))\s+(?P<state>\S+)\s+"
                    r"(?P<uptime>.+)\s".format(IPV4_ADDR_REGEX, IPV6_ADDR_REGEX, ASN_REGEX),
                    line,
                )
                if r2:
                    logger.info("brocade overflow bug: line: {}".format(line))
                    logger.info(r2.group())
                    try:
                        remote_addr = napalm.base.helpers.IPAddress(r2.group("remote_addr"))
                        bgp_data["global"]["peers"][str(remote_addr)] = {
                            "local_as": local_as,
                            "remote_as": r2.group("remote_as"),
                            "address_family": self.__get_bgp_route_stats__(remote_addr),
                        }
                    except Exception as ex:
                        logger.warn("unable to process overflow bug line: {}".format(ex))

            return bgp_data

        def _parse_per_peer_bgp_detail(peer_output):
            """This function parses the raw data per peer and returns a
            json structure per peer.
            """

            int_fields = [
                "local_as",
                "remote_as",
                "local_port",
                "remote_port",
                "local_port",
                "input_messages",
                "output_messages",
                "input_updates",
                "output_updates",
                "messages_queued_out",
                "holdtime",
                "configured_holdtime",
                "keepalive",
                "configured_keepalive",
                "advertised_prefix_count",
                "received_prefix_count",
            ]

            peer_details = []

            # Using preset template to extract peer info
            _peer_info = napalm_base.helpers.textfsm_extractor(self, "bgp_detail", peer_output)

            for item in _peer_info:
                # Determining a few other fields in the final peer_info
                item["up"] = True if item["connection_state"] == "ESTABLISHED" else False
                item["local_address_configured"] = True if item["local_address"] else False
                item["multihop"] = True if item["multihop"] == "yes" else False
                item["remove_private_as"] = True if item["remove_private_as"] == "yes" else False

                # TODO: The below fields need to be retrieved
                # Currently defaulting their values to False or 0
                item["multipath"] = False
                item["suppress_4byte_as"] = False
                item["local_as_prepend"] = False
                item["flap_count"] = 0
                item["active_prefix_count"] = 0
                item["suppressed_prefix_count"] = 0

                # Converting certain fields into int
                for key in int_fields:
                    if key in item:
                        item[key] = napalm_base.helpers.convert(int, item[key], 0)

                # process maps and lists
                for f in ["route_map", "filter_list", "prefix_list"]:
                    _val = item.get(f)
                    if _val is not None:
                        r = _val.split()
                        if r:
                            # print 'r: ', r
                            # print len(r)
                            _name = "policy" if f == "route_map" else f
                            if len(r) >= 2:
                                item["{0}_{1}".format("import" if "in" in r[0] else "export", _name)] = str(r[1])

                            if len(r) == 4:
                                item["{0}_{1}".format("import" if "in" in r[2] else "export", _name)] = str(r[3])

                        # remove raw data from item
                        item.pop(f, None)

                # Conforming with the datatypes defined by the base class
                item["description"] = str(item.get("description", ""))
                item["peer_group"] = str(item.get("peer_group", ""))
                item["remote_address"] = napalm_base.helpers.ip(item["remote_address"])
                item["previous_connection_state"] = str(item["previous_connection_state"])
                item["connection_state"] = str(item["connection_state"])
                item["routing_table"] = str(item["routing_table"])
                item["router_id"] = napalm_base.helpers.ip(item["router_id"])
                item["local_address"] = napalm_base.helpers.convert(napalm_base.helpers.ip, item["local_address"])

                peer_details.append(item)

            return peer_details

        def _append(bgp_dict, peer_info):
            remote_as = peer_info["remote_as"]
            vrf_name = peer_info["routing_table"]

            if vrf_name not in bgp_dict.keys():
                bgp_dict[vrf_name] = {}
            if remote_as not in bgp_dict[vrf_name].keys():
                bgp_dict[vrf_name][remote_as] = []

            bgp_dict[vrf_name][remote_as].append(peer_info)

        _peer_ver = None
        bgp_summary = [list(), list()]
        raw_output = [list(), list()]
        bgp_detail_info = dict()

        # used to hold Address Family specific peer info
        _peer_info_af = [list(), list()]

        if not neighbor_address:
            """
            raw_output[0] = self.device.send_command(
                'show ip bgp neighbors', delay_factor=self._show_command_delay_factor)
            raw_output[1] = self.device.send_command(
                'show ipv6 bgp neighbors', delay_factor=self._show_command_delay_factor)
            """
            bgp_summary[0] = __process_bgp_summary_data__(
                self.device.send_command("show ip bgp summary", delay_factor=self._show_command_delay_factor)
            )
            bgp_summary[1] = __process_bgp_summary_data__(
                self.device.send_command("show ipv6 bgp summary", delay_factor=self._show_command_delay_factor)
            )

            # Using preset template to extract peer info
            _peer_info_af[0] = _parse_per_peer_bgp_detail(
                self.device.send_command("show ip bgp neighbors", delay_factor=self._show_command_delay_factor)
            )
            _peer_info_af[1] = _parse_per_peer_bgp_detail(
                self.device.send_command("show ipv6 bgp neighbors", delay_factor=self._show_command_delay_factor)
            )

        else:
            try:
                _peer_ver = IPAddress(neighbor_address).version
            except Exception as e:
                raise e

            _ver = "" if _peer_ver == 4 else "v6"

            if _peer_ver == 4:
                """
                raw_output[0] = self.device.send_command(
                    'show ip bgp neighbors {}'.format(neighbor_address),
                    delay_factor=self._show_command_delay_factor)
                """
                bgp_summary[0] = __process_bgp_summary_data__(
                    self.device.send_command("show ip bgp summary", delay_factor=self._show_command_delay_factor)
                )
                _peer_info_af[0] = _parse_per_peer_bgp_detail(
                    self.device.send_command(
                        "show ip bgp neighbors {}".format(neighbor_address),
                        delay_factor=self._show_command_delay_factor,
                    )
                )
            else:
                """
                raw_output[1] = self.device.send_command(
                    'show ipv6 bgp neighbors {}'.format(neighbor_address),
                    delay_factor=self._show_command_delay_factor)
                """
                bgp_summary[1] = __process_bgp_summary_data__(
                    self.device.send_command("show ipv6 bgp summary", delay_factor=self._show_command_delay_factor)
                )
                _peer_info_af[1] = _parse_per_peer_bgp_detail(
                    self.device.send_command(
                        "show ipv6 bgp neighbors {}".format(neighbor_address),
                        delay_factor=self._show_command_delay_factor,
                    )
                )

        for i, info in enumerate(_peer_info_af):
            for peer_info in info:
                _peer_remote_addr = peer_info.get("remote_address")

                try:
                    _bgp_summary = bgp_summary[i]["global"]["peers"].get(_peer_remote_addr)
                    if _bgp_summary:
                        peer_info["local_as"] = _bgp_summary["local_as"]

                        _afi_info = _bgp_summary["address_family"].get("ipv4" if i == 0 else "ipv6")
                        if _afi_info:
                            peer_info["suppressed_prefix_count"] = int(_afi_info.get("filtered_prefixes", 0))
                            peer_info["advertised_prefix_count"] = int(_afi_info.get("sent_prefixes", 0))
                            peer_info["accepted_prefix_count"] = int(_afi_info.get("accepted_prefixes", 0))
                except:
                    pass

                _append(bgp_detail_info, peer_info)

        return bgp_detail_info

    def get_interfaces_counters(self):
        """get_interfaces_counters method."""
        cmd = "show statistics"
        lines = self.device.send_command(cmd)
        lines = lines.split("\n")

        counters = {}
        for line in lines:
            port_block = re.match(r"\s*PORT (\S+) Counters:.*", line)
            if port_block:
                interface = port_block.group(1)
                counters.setdefault(interface, {})
            elif len(line) == 0:
                continue
            else:
                octets = re.match(r"\s+InOctets\s+(\d+)\s+OutOctets\s+(\d+)\.*", line)
                if octets:
                    counters[interface]["rx_octets"] = octets.group(1)
                    counters[interface]["tx_octets"] = octets.group(2)
                    continue

                packets = re.match(r"\s+InUnicastPkts\s+(\d+)\s+OutUnicastPkts\s+(\d+)\.*", line)
                if packets:
                    counters[interface]["rx_unicast_packets"] = packets.group(1)
                    counters[interface]["tx_unicast_packets"] = packets.group(2)
                    continue

                broadcast = re.match(r"\s+InBroadcastPkts\s+(\d+)\s+OutBroadcastPkts\s+(\d+)\.*", line)
                if broadcast:
                    counters[interface]["rx_broadcast_packets"] = broadcast.group(1)
                    counters[interface]["tx_broadcast_packets"] = broadcast.group(2)
                    continue

                multicast = re.match(r"\s+InMulticastPkts\s+(\d+)\s+OutMulticastPkts\s+(\d+)\.*", line)
                if multicast:
                    counters[interface]["rx_multicast_packets"] = multicast.group(1)
                    counters[interface]["tx_multicast_packets"] = multicast.group(2)
                    continue

                error = re.match(r"\s+InErrors\s+(\d+)\s+OutErrors\s+(\d+)\.*", line)
                if error:
                    counters[interface]["rx_errors"] = error.group(1)
                    counters[interface]["tx_errors"] = error.group(2)
                    continue

                discard = re.match(r"\s+InDiscards\s+(\d+)\s+OutDiscards\s+(\d+)\.*", line)
                if discard:
                    counters[interface]["rx_discards"] = discard.group(1)
                    counters[interface]["tx_discards"] = discard.group(2)

        return counters

    def get_environment(self):
        """
        Note this only partially implemented.  Currently only
        Returns a dictionary where:

            * fans is a dictionary of dictionaries where the key is the location and the values:
                 * status (True/False) - True if it's ok, false if it's broken
            * temperature is a dict of dictionaries where the key is the location and the values:
                 * temperature (float) - Temperature in celsius the sensor is reporting.
                 * is_alert (True/False) - True if the temperature is above the alert threshold
                 * is_critical (True/False) - True if the temp is above the critical threshold
            * power is a dictionary of dictionaries where the key is the PSU id and the values:
                 * status (True/False) - True if it's ok, false if it's broken
                 * capacity (float) - Capacity in W that the power supply can support
                 * output (float) - Watts drawn by the system
            * cpu is a dictionary of dictionaries where the key is the ID and the values
                 * %usage
            * memory is a dictionary with:
                 * available_ram (int) - Total amount of RAM installed in the device
                 * used_ram (int) - RAM in use in the device
        """
        # todo: add cpu, memory
        environment = {
            "memory": {"used_ram": 0, "available_ram": 0},
            "temperature": {},
            "cpu": [{"%usage": 0.0}],
            "power": {},
            "fans": {},
            "memory_detail": {},
            "cpu_detail": {},
        }

        lines = self.device.send_command("show cpu-utilization average all 300 | include idle")
        for line in lines.split("\n"):
            r1 = re.match(r"^idle\s+.*(\d+)$", line)
            if r1:
                environment["cpu"][0]["%usage"] = 100 - int(r1.group(1))

        _data = napalm.base.helpers.textfsm_extractor(
            self, "show_cpu_lp", self.device.send_command("show cpu-utilization lp")
        )
        if _data:
            for d in _data:
                _slot = d.get("slot")
                _pct = napalm.base.helpers.convert(int, d.get("util"), 0)
                if _slot:
                    environment["cpu_detail"]["LP{}".format(_slot)] = {"%usage": _pct}

        # process memory
        _data = napalm.base.helpers.textfsm_extractor(self, "show_memory", self.device.send_command("show memory"))
        # print(json.dumps(_data, indent=2))
        if _data:
            for d in _data:
                _name = d.get("name")
                _module = d.get("module")
                _state = d.get("state")
                _avail = napalm.base.helpers.convert(int, d.get("avail_ram"), 0)
                _total = napalm.base.helpers.convert(int, d.get("total_ram"), 0)
                _used = _avail / _total if _avail > 0 else 0
                _pct = d.get("avail_ram_pct")

                if _name and _module:
                    environment["memory_detail"][_module] = {"used_ram": _used, "available_ram": _avail}

                    if "MP" in _module and _state and _state == "active":
                        environment["memory"] = {"available_ram": _avail, "used_ram": _avail}

        # todo replace with 'show chassis' tpl
        command = "show chassis"
        lines = self.device.send_command(command)
        _data = napalm.base.helpers.textfsm_extractor(self, "show_chassis", lines)

        _chassis_modules = {"TEMP": "temperature", "FAN": "fans", "POWER": "power"}
        if _data:
            for d in _data:
                _module = d.get("module")
                _mod_name = _chassis_modules.get(_module)
                if not _mod_name:
                    continue

                _name = d.get("name")
                _status = d.get("status")
                if _module and _name:
                    if _module == "TEMP":
                        environment[_mod_name][_name] = {"temperature": d.get("temp", "0")}
                    elif _module == "FAN":
                        environment[_mod_name][_name] = {"status": _status, "speed": d.get("speed", "")}
                    elif _module == "POWER":
                        environment[_mod_name][_name] = {
                            "status": _status,
                            "capacity": d.get("value", "N/A"),
                            "output": "N/A",
                        }

        return environment

    def get_arp_table(self, vrf=""):
        """
        Returns a list of dictionaries having the following set of keys:
            * interface (string)
            * mac (string)
            * ip (string)
            * age (float)

        'vrf' of null-string will default to all VRFs. Specific 'vrf' will return the ARP table
        entries for that VRFs (including potentially 'default' or 'global').

        In all cases the same data structure is returned and no reference to the VRF that was used
        is included in the output.

        Example::

            [
                {
                    'interface' : 'MgmtEth0/RSP0/CPU0/0',
                    'mac'       : '5C:5E:AB:DA:3C:F0',
                    'ip'        : '172.17.17.1',
                    'age'       : 1454496274.84
                },
                {
                    'interface' : 'MgmtEth0/RSP0/CPU0/0',
                    'mac'       : '5C:5E:AB:DA:3C:FF',
                    'ip'        : '172.17.17.2',
                    'age'       : 1435641582.49
                }
            ]

        """
        arp_table = list()

        arp_cmd = "show arp {}".format(vrf)
        output = self.device.send_command(arp_cmd)
        output = output.split("\n")
        output = output[7:]

        for line in output:
            fields = line.split()

            if len(fields) == 6:
                num, address, mac, typ, age, interface = fields
                try:
                    if age == "None":
                        age = 0
                    age = float(age)
                except ValueError:
                    logger.warn("Unable to convert age value to float: {}".format(age))

                # Do not include 'Pending' entries
                if typ == "Dynamic" or typ == "Static":
                    entry = {"interface": interface, "mac": napalm.base.helpers.mac(mac), "ip": address, "age": age}
                    arp_table.append(entry)

        return arp_table

    def cli(self, commands):
        """
        Execute a list of commands and return the output in a dictionary format using the command
        as the key.

        Example input:
        ['show clock', 'show calendar']

        Output example:
        {   'show calendar': u'22:02:01 UTC Thu Feb 18 2016',
            'show clock': u'*22:01:51.165 UTC Thu Feb 18 2016'}

        """
        cli_output = dict()
        if type(commands) is not list:
            raise TypeError("Please enter a valid list of commands!")

        for command in commands:
            output = self._send_command(command)
            if "Invalid input detected" in output:
                raise ValueError('Unable to execute command "{}"'.format(command))
            cli_output.setdefault(command, {})
            cli_output[command] = output

        return cli_output

    def get_ntp_servers(self):
        """
        Returns the NTP servers configuration as dictionary.
        The keys of the dictionary represent the IP Addresses of the servers.
        Inner dictionaries do not have yet any available keys.

        Example::

            {
                '192.168.0.1': {},
                '17.72.148.53': {},
                '37.187.56.220': {},
                '162.158.20.18': {}
            }

        """
        _ntp_servers = {}

        # as a quick implementation; call get_ntp_stats to get a list of ntp servers
        _ntp_info = self.get_ntp_stats()
        if _ntp_info:
            for n in _ntp_info:
                _ntp_servers[n.get("remote")] = {}

        return _ntp_servers

    def get_ntp_stats(self):
        """
        Note this was copied from the ios driver. Need to revisit type.

        Returns a list of NTP synchronization statistics.

            * remote (string)
            * referenceid (string)
            * synchronized (True/False)
            * stratum (int)
            * type (string)
            * when (string)
            * hostpoll (int)
            * reachability (int)
            * delay (float)
            * offset (float)
            * jitter (float)

        Example::

            [
                {
                    'remote'        : u'188.114.101.4',
                    'referenceid'   : u'188.114.100.1',
                    'synchronized'  : True,
                    'stratum'       : 4,
                    'type'          : u'-',
                    'when'          : u'107',
                    'hostpoll'      : 256,
                    'reachability'  : 377,
                    'delay'         : 164.228,
                    'offset'        : -13.866,
                    'jitter'        : 2.695
                }
            ]
        """
        ntp_stats = []

        command = "show ntp associations"
        output = self._send_command(command)

        for line in output.splitlines():
            # Skip first two lines and last line of command output
            if line == "" or "address" in line or "sys.peer" in line:
                continue

            if "%NTP is not enabled" in line:
                return []

            elif len(line.split()) == 9:
                address, ref_clock, st, when, poll, reach, delay, offset, disp = line.split()
                address_regex = re.match(r"(\W*)([0-9.*]*)", address)
            try:
                ntp_stats.append(
                    {
                        "remote": str(address_regex.group(2)),
                        "synchronized": ("*" in address_regex.group(1)),
                        "referenceid": str(ref_clock),
                        "stratum": int(st),
                        "type": "-",
                        "when": str(when),
                        "hostpoll": int(poll),
                        "reachability": int(reach),
                        "delay": float(delay),
                        "offset": float(offset),
                        "jitter": float(disp),
                    }
                )
            except Exception:
                continue

        return ntp_stats

    def get_mac_address_table(self):
        """get_mac_address_table method."""
        cmd = "show mac-address"
        lines = self.device.send_command(cmd)
        lines = lines.split("\n")

        mac_address_table = []
        # Headers may change whether there are static entries, is MLX or is CER
        for line in lines:
            fields = line.split()

            r1 = re.match(r"(\S+)\s+(\S+)\s+(Static|\d+)\s+(\d+).*", line)
            if r1:
                vlan = -1
                age = 0
                if self.family == "MLX":
                    if len(fields) == 4:
                        mac_address, port, age, vlan = fields
                else:
                    if len(fields) == 5:
                        mac_address, port, age, vlan, esi = fields

                is_static = bool("Static" in age)
                mac_address = napalm.base.helpers.mac(mac_address)

                entry = {
                    "mac": mac_address,
                    "interface": str(port),
                    "vlan": int(vlan),
                    "active": bool(1),
                    "static": is_static,
                    "moves": None,
                    "last_move": None,
                }
                mac_address_table.append(entry)

        return mac_address_table

    def get_probes_config(self):
        raise NotImplementedError

    def get_snmp_information(self, decrypt=False):
        """
        Retrieves SNMP configuration. Note this is partially implemented in that only SNMP v2c is supported. There
        is no particular support for v3.

        Brocade Notes:
        - no support for chassis-id
        - communities are encrypted; requires config -> enable password-display in order to view communities
          in-the-clear
        - additional setting retrieval should be supported

        Example Output:

        {   'chassis_id': u'unknown',
        'community': {   u'private': {   'acl': u'12', 'mode': u'rw'},
                         u'public': {   'acl': u'11', 'mode': u'ro'},
                         u'public_named_acl': {   'acl': u'ALLOW-SNMP-ACL',
                                                  'mode': u'ro'},
                         u'public_no_acl': {   'acl': u'N/A', 'mode': u'ro'}},
        'contact': u'Joe Smith',
        'location': u'123 Anytown USA Rack 404'}

        :param decrypt: if True community strings are decrypted otherwise Brocade renders dots
        :return: dict of dicts
        """

        try:
            # enable password-display in order to display community strings decrypted/in-the-clear
            if decrypt:
                self.device.send_config_set(["enable password-display"])

            # default values
            snmp_dict = {"chassis_id": "unknown", "community": {}, "contact": "unknown", "location": "unknown"}
            command = "show run | include snmp-server"
            output = self._send_command(command)
            for line in output.splitlines():
                fields = line.split()
                if "snmp-server community" in line:
                    name = fields[2]
                    if "community" not in snmp_dict.keys():
                        snmp_dict.update({"community": {}})
                    snmp_dict["community"].update({name: {}})
                    try:
                        snmp_dict["community"][name].update({"mode": fields[3].lower()})
                    except IndexError:
                        snmp_dict["community"][name].update({"mode": "N/A"})
                    try:
                        snmp_dict["community"][name].update({"acl": fields[4]})
                    except IndexError:
                        snmp_dict["community"][name].update({"acl": "N/A"})
                elif "snmp-server location" in line:
                    snmp_dict["location"] = " ".join(fields[2:])
                elif "snmp-server contact" in line:
                    snmp_dict["contact"] = " ".join(fields[2:])
                elif "snmp-server chassis-id" in line:
                    snmp_dict["chassis_id"] = " ".join(fields[2:])
                else:
                    # add any other snmp-server configuration
                    if len(fields) > 2:
                        snmp_dict[fields[1]] = " ".join(fields[2:])

        finally:
            # disable password-display before exiting
            if decrypt:
                self.device.send_config_set(["no enable password-display"])

        return snmp_dict

    def get_users(self):
        """
        Returns a dictionary with the configured users.
        The keys of the main dictionary represents the username.
        The values represent the details of the user,
        represented by the following keys:

            * level (int)
            * password (str)
            * sshkeys (list)

        *Note: need to revisit sshkeys -- I'm not sure exactly what Brocade supports


        The level is an integer between 0 and 15, where 0 is the
        lowest access and 15 represents full access to the device.
        """
        _users = {}
        _output = self._send_command("show users")
        for l in _output.split("\n"):
            _info = l.split()

            if _info and len(_info) == 4 and not re.search(r"^(Username|=======)", _info[0]):
                _users[_info[0]] = {"password": _info[1], "sshkeys": [], "level": _info[3]}
        return _users

    def ping(
        self, destination, source=PING_SOURCE, ttl=PING_TTL, timeout=50, size=PING_SIZE, count=PING_COUNT, vrf=PING_VRF
    ):
        """
        Execute ping on the device and returns a dictionary with the result.  This is a direct port
        of the Cisco IOS code; modified to support Brocade netiron

        Note that a timeout=50 is the minimum supported timeout for Brocades

        Output dictionary has one of following keys:
            * success
            * error
        In case of success, inner dictionary will have the following keys:
            * probes_sent (int)
            * packet_loss (int)
            * rtt_min (float)
            * rtt_max (float)
            * rtt_avg (float)
            * rtt_stddev (float)
            * results (list)
        'results' is a list of dictionaries with the following keys:
            * ip_address (str)
            * rtt (float)
        """
        ping_dict = {}

        _ip = IPAddress(destination)
        if not _ip:
            raise ValueError("destination must be a valid IP Address")

        if timeout < 50:
            timeout = 50

        # vrf needs to be right after the ping command
        # ipv6 addresses require an additional parameter
        command = "ping {vrf} {family} {destination} timeout {timeout} size {size} count {count} ".format(
            vrf="vrf " + vrf if vrf else "",
            family="ipv6" if _ip.version == 6 else "",
            destination=destination,
            timeout=timeout,
            size=size,
            count=count,
        )

        # apply a source-ip
        if source != "":
            command += " source-ip {}".format(source)

        # logger.info(command)

        output = self._send_command(command)
        if "No reply from remote host" in output:
            ping_dict["error"] = "No reply from remote host"
        elif "Sending" in output:
            ping_dict["success"] = {
                "probes_sent": 0,
                "packet_loss": 0,
                "rtt_min": 0.0,
                "rtt_max": 0.0,
                "rtt_avg": 0.0,
                "rtt_stddev": 0.0,
                "results": [],
            }

            _probe_results = list()
            for line in output.splitlines():
                fields = line.split()
                if "Success rate is 0" in line:
                    sent_and_received = re.search(r"\((\d*)/(\d*)\)", fields[5])
                    probes_sent = int(sent_and_received.groups()[0])
                    probes_received = int(sent_and_received.groups()[1])
                    ping_dict["success"]["probes_sent"] = probes_sent
                    ping_dict["success"]["packet_loss"] = probes_sent - probes_received
                elif "Success rate is" in line:
                    # brocade jams min/avg/max and values together as opposed to Cisco which uses spaces
                    # Success rate is 100 percent (3/3), round-trip min/avg/max=24/26/29 ms.
                    sent_and_received = re.search(r"\((\d*)/(\d*)\)", fields[5])
                    probes_sent = int(sent_and_received.groups()[0])
                    probes_received = int(sent_and_received.groups()[1])
                    min_avg_max = re.search(r"(\d*)/(\d*)/(\d*)", fields[7])
                    ping_dict["success"]["probes_sent"] = probes_sent
                    ping_dict["success"]["packet_loss"] = probes_sent - probes_received
                    ping_dict["success"].update(
                        {
                            "rtt_min": float(min_avg_max.groups()[0]),
                            "rtt_avg": float(min_avg_max.groups()[1]),
                            "rtt_max": float(min_avg_max.groups()[2]),
                        }
                    )
                    results_array = []

                    # modified original cisco code to use values from 'Reply from' results. If no value
                    # is found default to 0.0 per the original code
                    for i in range(probes_received):
                        results_array.append(
                            {
                                "ip_address": str(destination),
                                "rtt": _probe_results[i] if len(_probe_results) > i else 0.0,
                            }
                        )
                    ping_dict["success"].update({"results": results_array})

                elif "Reply from " in line:
                    # grab the time results and append a list
                    r = re.search(r"^Reply from .* time=(\d+)", line)
                    if r:
                        _probe_results.append(r.groups()[0])

        return ping_dict

    def traceroute(
        self, destination, source=TRACEROUTE_SOURCE, ttl=TRACEROUTE_TTL, timeout=TRACEROUTE_TIMEOUT, vrf=TRACEROUTE_VRF
    ):
        """
        Executes traceroute on the device and returns a dictionary with the result.

        :param destination: Host or IP Address of the destination
        :param source: Use a specific IP Address to execute the traceroute
        :param ttl: Maximum number of hops -> int (0-255)
        :param timeout: Number of seconds to wait for response -> int (1-3600)

        Output dictionary has one of the following keys:

            * success
            * error

        In case of success, the keys of the dictionary represent the hop ID, while values are
        dictionaries containing the probes results:
            * rtt (float)
            * ip_address (str)
            * host_name (str)
        """
        _ip = IPAddress(destination)
        if not _ip:
            raise ValueError("destination must be a valid IP Address")

        # perform a ping to verify if the destination will respond -- this speeds up processing -- if a
        # destination is inaccessible a traceroute will consume a lot of time; only to fail.  Where a ping
        # is relatively quick.
        #
        # note that brocade doesn't support v6 source traceroute so do NOT specify a source for
        # v6 ping check
        _res = self.ping(destination, source=source if _ip.version == 4 else "")
        if "error" in _res:
            return _res

        # vrf needs to be right after the traceroute command
        # ipv6 addresses require an additional parameter
        command = "traceroute {vrf} {family} {destination} ".format(
            vrf="vrf " + vrf if vrf else "", family="ipv6" if _ip.version == 6 else "", destination=destination
        )

        if source != "" and _ip.version == 4:
            command += " source-ip {}".format(source)
        if ttl:
            if isinstance(ttl, int) and 0 <= timeout <= 255:
                command += " maxttl {}".format(str(ttl))
        if timeout:
            # Timeout should be an integer between 1 and 3600
            if isinstance(timeout, int) and 1 <= timeout <= 3600:
                command += " timeout {}".format(str(timeout))

        logger.info(command)

        # Calculation to leave enough time for traceroute to complete assumes send_command
        # delay of .2 seconds.
        max_loops = (5 * ttl * timeout) + 150
        if max_loops < 500:  # Make sure max_loops isn't set artificially low
            max_loops = 500
        output = self.device.send_command(command, max_loops=max_loops)

        if "Not authorized to execute this command" in output:
            raise ValueError("Permissions Error: {0}: Not authorized to execute this command.".format(self.username))

        # Prepare return dict
        traceroute_dict = dict()
        if re.search("Unrecognized host or address", output):
            traceroute_dict["error"] = "unknown host %s" % destination
            return traceroute_dict
        else:
            traceroute_dict["success"] = dict()

        results = dict()
        # Find all hops
        hops = re.findall(r"\n\s+[0-9]{1,3}\s+.*", output)
        for h in hops:
            # lets try simply splitting the string vs. regex
            v = h.strip().split()
            if not v or len(v) < 8:
                logger.warn("expected at least 7 hop results: {0}:{1}".format(h, hops))
                continue

            _hop = v[0]
            _ip_address = ""
            _host = "?"
            _p1 = _p2 = _p3 = "*"

            if v[1] != "*":
                if len(v) == 9:
                    _ip_address = re.sub(r"[\[\]]", "", v[8])
                _host = v[7]
                _p1 = v[1]
                _p2 = v[3]
                _p3 = v[5]

            results[_hop] = dict()
            results[_hop]["probes"] = dict()
            results[_hop]["probes"][1] = {"rtt": _p1, "ip_address": _ip_address, "host_name": _host}
            results[_hop]["probes"][2] = {"rtt": _p2, "ip_address": _ip_address, "host_name": _host}
            results[_hop]["probes"][3] = {"rtt": _p3, "ip_address": _ip_address, "host_name": _host}

        traceroute_dict["success"] = results
        return traceroute_dict

    def get_network_instances(self, name=""):
        instances = {}

        show_vrf_detail = self.device.send_command("show vrf detail")
        vrf_detail = textfsm_extractor(self, "show_vrf_detail", show_vrf_detail)

        show_ip_interface = self.device.send_command("show ip interface")
        ip_interface = textfsm_extractor(self, "show_ip_interface", show_ip_interface)

        instances["default"] = {
            "name": "default",
            "type": "DEFAULT_INSTANCE",
            "state": {"route_distinguisher": ""},
            "interfaces": {"interface": {}},
        }

        for vrf in vrf_detail:
            instances[vrf["name"]] = {
                "name": vrf["name"],
                "type": "L3VRF",
                "state": {"route_distinguisher": vrf["rd"]},
                "interfaces": {"interface": {}},
            }

        for interface in ip_interface:
            intf = self.standardize_interface_name(interface["interfacetype"] + interface["interfacenum"])

            vrf = interface["vrf"]
            if vrf == "default-vrf":
                vrf = "default"

            instances[vrf]["interfaces"]["interface"][intf] = {}

        return instances if not name else instances[name]

    def get_static_routes(self):
        routes = []

        show_running_config = self.device.send_command_timing(
            "show running-config", delay_factor=self._show_command_delay_factor
        )
        static_routes_detail = textfsm_extractor(self, "static_route_details", show_running_config)

        vrf_static_routes_details = textfsm_extractor(self, "vrf_static_route_details", show_running_config)

        for route in static_routes_detail:
            route["vrf"] = None
            routes.append(route)

        for route in vrf_static_routes_details:
            routes.append(route)

        return routes

    def get_config(self, retrieve="all"):
        """Implementation of get_config for netiron.

        Returns the startup or/and running configuration as dictionary.
        The keys of the dictionary represent the type of configuration
        (startup or running). The candidate is always empty string,
        since netiron does not support candidate configuration.
        """

        configs = {
            "startup": "",
            "running": "",
            "candidate": "",
        }

        if retrieve in ("startup", "all"):
            command = "show configuration"
            output = self.device.send_command_timing(command)
            configs["startup"] = output

        if retrieve in ("running", "all"):
            command = "show running-config"
            output = self.device.send_command_timing(command)
            configs["running"] = output

        return configs

    def get_ipv6_neighbors_table(self):
        """
        Get IPv6 neighbors table information.
        Return a list of dictionaries having the following set of keys:
            * interface (string)
            * mac (string)
            * ip (string)
            * age (float) in seconds
            * state (string)
        For example::
            [
                {
                    'interface' : 'MgmtEth0/RSP0/CPU0/0',
                    'mac'       : '5c:5e:ab:da:3c:f0',
                    'ip'        : '2001:db8:1:1::1',
                    'age'       : 1454496274.84,
                    'state'     : 'REACH'
                },
                {
                    'interface': 'MgmtEth0/RSP0/CPU0/0',
                    'mac'       : '66:0e:94:96:e0:ff',
                    'ip'        : '2001:db8:1:1::2',
                    'age'       : 1435641582.49,
                    'state'     : 'STALE'
                }
            ]
        """

        ipv6_neighbors_table = []
        command = "show ipv6 neighbors"
        output = self._send_command(command)

        ipv6_neighbors = ""
        fields = re.split(r"^IPv6\s+Address.*Interface$", output, flags=(re.M | re.I))
        if len(fields) == 2:
            ipv6_neighbors = fields[1].strip()
        for entry in ipv6_neighbors.splitlines():
            # typical format of an entry in the IOS IPv6 neighbors table:
            # 2002:FFFF:233::1 0 2894.0fed.be30  REACH Fa3/1/2.233
            ip, age, mac, state, interface = entry.split()
            mac = "" if mac == "-" else napalm.base.helpers.mac(mac)
            ip = napalm.base.helpers.ip(ip)
            ipv6_neighbors_table.append(
                {"interface": interface, "mac": mac, "ip": ip, "age": float(age), "state": state}
            )
        return ipv6_neighbors_table

    ###################
    # PRIVATE METHODS #
    ###################

    def _tftp_handler(self, merge_candidate):
        """tftp handler. return merge candidate no matter what is requested."""

        def _handler(fn, raddress=None, rport=None):
            if fn == "merge_candidate":
                return io.StringIO(merge_candidate)

        return _handler

    def _get_ipaddress(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip

    @staticmethod
    def _send_command_postprocess(output):
        """
        Cleanup actions on send_command() for NAPALM getters.

        Remove "Load for five sec; one minute if in output"
        Remove "Time source is"
        """
        output = re.sub(r"^Load for five secs.*$", "", output, flags=re.M)
        output = re.sub(r"^Time source is .*$", "", output, flags=re.M)
        return output.strip()

    @staticmethod
    def __parse_port_change__(last_str):
        r1 = re.match(r"(\d+) days (\d+):(\d+):(\d+)", last_str)
        if r1:
            days = int(r1.group(1))
            hours = int(r1.group(2))
            mins = int(r1.group(3))
            secs = int(r1.group(4))

            return float(secs + (mins * 60) + (hours * 60 * 60) + (days * 24 * 60 * 60))
        else:
            return float(-1.0)

    def _delete_keys_from_dict(self, dict_del, lst_keys):
        for k in lst_keys:
            try:
                del dict_del[k]
            except KeyError:
                pass
        for v in dict_del.values():
            if isinstance(v, dict):
                self._delete_keys_from_dict(v, lst_keys)

        return dict_del

    def _get_interface_detail(self, port):
        description = None
        mac = None
        if port == "mgmt1":
            command = "show interface management1"
        else:
            command = "show interface ethernet {}".format(port)
        output = self.device.send_command(command, delay_factor=self._show_command_delay_factor)
        output = output.split("\n")

        last_flap = "0.0"
        speed = "0"
        for line in output:
            # Port state change is only supported from >5.9? (no support in 5.7b)
            r0 = re.match(r"\s+Port state change time: \S+\s+\d+\s+\S+\s+\((.*) ago\)", line)
            if r0:
                last_flap = self.__class__.__parse_port_change__(r0.group(1))
            r1 = re.match(r"\s+No port name", line)
            if r1:
                description = ""
            r2 = re.match(r"\s+Port name is (.*)", line)
            if r2:
                description = r2.group(1)
            r3 = re.match(r"\s+Hardware is \S+, address is (\S+) (.+)", line)
            if r3:
                mac = r3.group(1)
            # Empty modules may not report the speed
            # Configured fiber speed auto, configured copper speed auto
            # actual unknown, configured fiber duplex fdx, configured copper duplex fdx, actual unknown
            r4 = re.match(r"\s+Configured speed (\S+),.+", line)
            if r4:
                speed = r4.group(1)
                if "auto" in speed:
                    speed = -1
                else:
                    r = re.match(r"(\d+)([M|G])bit", speed)
                    if r:
                        speed = r.group(1)
                        if r.group(2) == "M":
                            speed = int(speed) * 1000
                        elif r.group(2) == "G":
                            speed = int(speed) * 1000000

        return [last_flap, description, speed, mac]

    def _get_interface_map(self):
        """Return dict mapping ethernet port numbers to full interface name, ie

        {
            "1/1": "GigabitEthernet1/1",
            ...
        }
        """

        if not self.show_int or "pytest" in sys.modules:
            self.show_int = self.device.send_command_timing(
                "show interface", delay_factor=self._show_command_delay_factor
            )
        info = textfsm_extractor(self, "show_interface", self.show_int)

        result = {}
        for interface in info:
            if "ethernet" in interface["port"].lower() and "mgmt" not in interface["port"].lower():
                ifnum = re.sub(r".*(\d+/\d+)", "\\1", interface["port"])
                result[ifnum] = interface["port"]

        return result

    def standardize_interface_name(self, port):
        if not self.interface_map or "pytest" in sys.modules:
            self.interface_map = self._get_interface_map()

        port = str(port).strip()

        # Convert lbX to LoopbackX
        port = re.sub(r"^lb(\d+)$", "Loopback\\1", port)
        # Convert loopbackX to LoopbackX
        port = re.sub(r"^loopback(\d+)$", "Loopback\\1", port)
        # Convert tnX to tunnelX
        port = re.sub(r"^tn(\d+)$", "Tunnel\\1", port)
        # Conver gre-tnlX to TunnelX
        port = re.sub(r"^gre-tnl(\d+)$", "Tunnel\\1", port)
        # Convert veX to VeX
        port = re.sub(r"^ve(\d+)$", "Ve\\1", port)
        # Convert mgmt1 to Ethernetmgmt1
        if port in ["mgmt1", "management1"]:
            port = "Ethernetmgmt1"
        # Convert 1/1 or ethernet1/1 to ethernet1/1
        if re.match(r".*\d+/\d+", port):
            ifnum = re.sub(r".*(\d+/\d+)", "\\1", port)
            port = self.interface_map[ifnum]

        return port

    def get_lags(self):
        result = {}

        if not self.show_running_config_lag or "pytest" in sys.modules:
            self.show_running_config_lag = self.device.send_command_timing(
                "show running-config lag", delay_factor=self._show_command_delay_factor
            )
        info = textfsm_extractor(self, "show_running_config_lag", self.show_running_config_lag)
        for lag in info:
            port = "lag{}".format(lag["id"])
            result[port] = {
                "is_up": True,
                "is_enabled": True,
                "description": lag["name"],
                "last_flapped": float(-1),
                "speed": float(0),
                "mac_address": "",
                "mtu": 0,
                "children": self.interfaces_to_list(lag["ports"]),
            }

        return result

    def interface_list_conversion(self, ve, taggedports, untaggedports):
        interfaces = []
        if ve and ve != "NONE":
            interfaces.append("Ve{}".format(ve))
        if taggedports:
            interfaces.extend(self.interfaces_to_list(taggedports))
        if untaggedports:
            interfaces.extend(self.interfaces_to_list(untaggedports))
        return interfaces

    def interfaces_to_list(self, interfaces_string):
        """Convert string like 'ethe 2/1 ethe 2/4 to 2/5' or 'e 2/1 to 2/4' to list of interfaces"""
        interfaces = []

        if "ethernet" in interfaces_string:
            split_string = "ethernet"
        elif "ethe" in interfaces_string:
            split_string = "ethe"
        else:
            split_string = "e"

        sections = interfaces_string.split(split_string)
        if "" in sections:
            sections.remove("")  # Remove empty list items
        for section in sections:
            section = section.strip()  # Remove leading/trailing spaces

            # Process sections like 2/4 to 2/6
            if "to" in section:
                start_intf, end_intf = section.split(" to ")
                slot, num = start_intf.split("/")
                slot, end_num = end_intf.split("/")
                num = int(num)
                end_num = int(end_num)

                while num <= end_num:
                    intf_name = "{}/{}".format(slot, num)
                    interfaces.append(self.standardize_interface_name(intf_name))
                    num += 1

            # Individual ports like '2/1'
            else:
                interfaces.append(self.standardize_interface_name(section))

        return interfaces

    def __get_bgp_route_stats__(self, remote_addr):
        afi = "ipv4" if remote_addr.version == 4 else "ipv6"
        command = "show ip{0} bgp neighbors {1} routes-summary".format(
            "" if remote_addr.version == 4 else "v6", str(remote_addr)
        )
        _lines = self.device.send_command(command, delay_factor=self._show_command_delay_factor)
        _lines += _lines + "\n" if _lines else ""

        _stats = {
            "received_prefixes": -1,
            "accepted_prefixes": -1,
            "filtered_prefixes": -1,
            "sent_prefixes": -1,
            "to_send_prefixes": -1,
        }

        for line in _lines.splitlines():
            r1 = re.match(
                r"^Routes Accepted/Installed:\s*(?P<accepted_prefixes>\d+),\s+"
                r"Filtered/Kept:\s*(?P<filtered_kept>\d+),\s+"
                r"Filtered:\s*(?P<filtered_prefixes>\d+)",
                line,
            )
            if r1:
                _received_prefixes = int(r1.group("accepted_prefixes")) + int(r1.group("filtered_prefixes"))
                _stats["received_prefixes"] = _received_prefixes
                _stats["accepted_prefixes"] = r1.group("accepted_prefixes")
                _stats["filtered_prefixes"] = r1.group("filtered_prefixes")

            r2 = re.match(
                r"^Routes Advertised:\s*(?P<sent_prefixes>\d+),\s+"
                r"To be Sent:\s*(?P<to_be_sent>\d+),\s+"
                r"To be Withdrawn:\s*(?P<to_be_withdrawn>\d+)",
                line,
            )
            if r2:
                _stats["sent_prefixes"] = r2.group("sent_prefixes")
                _stats["to_send_prefixes"] = r2.group("to_be_sent")

        return {afi: _stats}
