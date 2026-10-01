Value NeighborAddress ([0-9a-fA-F.:]+)
Value State (\S+)
Value Interface (\S+(?:\s+\d\S*)?)
Value Holddown (\d+)
Value Interval (\d+)
Value RemoteRx ([YN])
Value Hop ([SM])

Start
  ^${NeighborAddress}\s+${State}\s+(?:${Interface}\s+)?${Holddown}\s+${Interval}\s+${RemoteRx}/${Hop}\s*$$ -> Record
