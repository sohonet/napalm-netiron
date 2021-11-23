Value Filldown Interface (\S+)
Value Filldown InterfaceNum (\S+)
Value VrfName (\S+)
Value Ipv4address (\S+)
Value Ipv6address (\S+)

Start
  ^interface ${Interface} ${InterfaceNum}
  ^\s+vrf forwarding ${VrfName} -> Record
  ^\s+ip address ${Ipv4address} -> Record
  ^\s+ipv6 address ${Ipv6address} -> Record
  ^! -> Clearall
