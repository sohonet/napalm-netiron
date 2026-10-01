Value SystemId ([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})
Value Interface (\S+(?:\s+\d\S*)?)
Value Snpa (\S+)
Value State (\S+)
Value HoldTime (\d+)
Value Type (\S+)
Value Priority (\d+)
Value StateChangeTime (.+?)
Value Protocol (\S+)

Start
  ^${SystemId}\s+${Interface}\s+${Snpa}\s+${State}\s+${HoldTime}\s+${Type}\s+${Priority}\s+${StateChangeTime}\s+${Protocol}\s*$$ -> Record
