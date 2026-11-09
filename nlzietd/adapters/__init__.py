"""Adapters: the only layer that touches the network, the disk or TLS.

Each adapter satisfies one port from `ports.py`. If something in here breaks
because a provider changed, the fix belongs here and nowhere else.
"""
