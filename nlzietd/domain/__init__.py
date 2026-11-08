"""Domain values.

Frozen dataclasses with no I/O, no HTTP, no filesystem, and no imports from
elsewhere in this package. That rule is what keeps the layer graph acyclic:
everything may import this module, this module imports nothing.
"""
