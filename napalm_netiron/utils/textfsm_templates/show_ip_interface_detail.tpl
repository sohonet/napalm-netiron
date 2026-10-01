Value VlanId (\d+)
Value Members (.+?)
Value ActiveMembers (.+?)
Value PortState (\S+)
Value IpMtu (\d+)

Start
  ^\s+vlan-id: ${VlanId}
  ^\s+members: ${Members}\s*$$
  ^\s+active: ${ActiveMembers}\s*$$
  ^\s+port state: ${PortState}
  ^\s+encapsulation: \S+, mtu: ${IpMtu}
