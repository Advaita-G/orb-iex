"""Research platform. Read-only by construction: it may import the GET-only data client, never the broker.

tests/unit/test_quantlab_isolation.py enforces that nothing under quantlab can reach an order-writing path.
"""
