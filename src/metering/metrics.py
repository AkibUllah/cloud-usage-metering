"""Metric registry.

Two kinds of metric exist:

* **gauge** - the sample describes a level that held for ``[start, end)``: 1 instance, 50 GB of
  volume, 1 floating IP. Rated quantity = level x hours, so a 50 GB volume for 30 minutes is
  25 GB-hours.
* **delta** - the sample is a total accumulated over ``[start, end)``: 12.5 GB of egress, 4,000
  object-storage requests. Rated quantity = the total, split across buckets by time overlap.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase


class MetricKind(str, Enum):
    GAUGE = "gauge"
    DELTA = "delta"


@dataclass(frozen=True)
class MetricSpec:
    pattern: str
    kind: MetricKind
    unit: str  # unit of the sample quantity
    rated_unit: str  # unit of the rated quantity
    description: str

    def matches(self, name: str) -> bool:
        return fnmatchcase(name, self.pattern)


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        "compute.instance.*",
        MetricKind.GAUGE,
        "instance",
        "instance-hour",
        "Running instance of a given flavor (e.g. compute.instance.m1.small)",
    ),
    MetricSpec("compute.vcpu", MetricKind.GAUGE, "vcpu", "vcpu-hour", "Allocated vCPUs"),
    MetricSpec("compute.ram.gb", MetricKind.GAUGE, "GB", "GB-hour", "Allocated RAM"),
    MetricSpec("storage.volume.gb", MetricKind.GAUGE, "GB", "GB-hour", "Block volume capacity"),
    MetricSpec("storage.snapshot.gb", MetricKind.GAUGE, "GB", "GB-hour", "Volume snapshot size"),
    MetricSpec("storage.object.gb", MetricKind.GAUGE, "GB", "GB-hour", "Object storage used"),
    MetricSpec("network.floating_ip", MetricKind.GAUGE, "ip", "ip-hour", "Allocated floating IP"),
    MetricSpec("network.loadbalancer", MetricKind.GAUGE, "lb", "lb-hour", "Load balancer"),
    MetricSpec(
        "network.bandwidth.egress.gb", MetricKind.DELTA, "GB", "GB", "Outbound internet traffic"
    ),
    MetricSpec(
        "network.bandwidth.ingress.gb", MetricKind.DELTA, "GB", "GB", "Inbound internet traffic"
    ),
    MetricSpec(
        "storage.object.requests", MetricKind.DELTA, "request", "request", "Object API requests"
    ),
)


def resolve_metric(name: str) -> MetricSpec | None:
    for spec in METRICS:
        if spec.matches(name):
            return spec
    return None


def is_known_metric(name: str) -> bool:
    return resolve_metric(name) is not None
