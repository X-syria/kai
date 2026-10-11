"""Durable operational alerts: the record, the notification intent, the lifecycle.

An alert is no longer a webhook POST that happens to accompany a failure. It is a
row, written in the same transaction as the state change that raised it, and every
question about an incident is answered from that row rather than from a receiver's
inbox. See :mod:`kai.alerts.models` for what the row means, :mod:`kai.alerts.service`
for the atomicity and delivery contract, and :mod:`kai.observability.alerting` for the
classification and the wire format that leaves the process.
"""

__all__ = ["models", "service"]
