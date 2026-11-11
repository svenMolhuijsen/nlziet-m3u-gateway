"""HTTP edge. Translates requests into service calls and back.

No business logic here, by rule. When something looks like a decision - "is
this channel known", "how long is a ticket good for" - it belongs in
services. This layer is allowed to make exactly one kind of decision:
how to render a failure as a status code.
"""
