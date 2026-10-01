Value Required Name (\S+)
Value LinkStatus (.+?)
Value LineProtocolStatus (\S+)
Value Description (.*?)
Value UntaggedVlan (\d+)
Value TrunkPorts ([\d/,\-]+)
Value TrunkRole (primary|secondary)
Value IpMtu (\d+)
Value Mtu (\d+)

Start
  ^${Name} is ${LinkStatus}, line protocol is ${LineProtocolStatus}\s*$$
  ^\s+Port name is ${Description}\s*$$
  ^.*VLAN ${UntaggedVlan} \(untagged\)
  ^\s+Member of active trunk ports ${TrunkPorts}, ${TrunkRole} port
  ^.*IP MTU ${IpMtu} bytes
  ^.*MTU ${Mtu} bytes
