Value Required PeerLdpId ([\d.]+)
Value PeerLabelSpace (\d+)
Value LocalLdpId ([\d.]+)
Value State (\S+)
Value Adjacency (\S+)
Value Role (\S+)
Value UpTime (.+?)
Value Interfaces (.+?)

Start
  ^Peer LDP ID: -> Continue.Record
  ^Peer LDP ID: ${PeerLdpId}:${PeerLabelSpace}, Local LDP ID: ${LocalLdpId}:\d+, State: ${State}\s*$$
  ^\s+Adj: ${Adjacency}, Role: ${Role},
  ^\s+Up time: ${UpTime}\s*$$
  ^\s+Neighboring interfaces: ${Interfaces}\s*$$
