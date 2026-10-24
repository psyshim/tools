"""The Device Agent: the single local executor. Owns workspace access, tool execution, effect journal and private state.

It speaks only a typed, token-authenticated channel to the coding service (server.py). It never accepts an absolute path, a
raw shell command or a workspace root from the wire: workspaces exist only as grants in its own configuration file (config.py).
"""
