"""Services: orchestration. These may depend on `ports`, never on `adapters`.

A service answers "what should happen", not "how do I talk to the network".
If you find an import of `adapters.*` in this package, the layering has been
broken and the fix is usually a missing method on a port.
"""
