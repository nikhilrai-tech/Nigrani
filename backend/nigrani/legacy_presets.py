"""Fingerprints of every preset rule exactly as v0.1.0 shipped it (default host settings).

Databases from v0.1.0 have no record of what a preset rule looked like when it was seeded. A rule row whose fingerprint
is in this set has never been edited, so a later fix to the preset can safely replace it; anything else was edited by
the user and is left alone. Generated from the v0.1.0 tag with `fingerprint.fingerprint`; it never needs to grow.
"""
SHIPPED_IN_0_1_0: frozenset[str] = frozenset({
    "a8bc4c91ca7785d2",  # celery-failures
    "e23a22af80b60bcc",  # celery-queue
    "50f5708c6f5a466a",  # celery-workers
    "57eb49fdf7e7c2af",  # container-restarts
    "d8eb832142b2a94a",  # django-5xx
    "5100b5b9fe075f22",  # django-latency
    "e52a3137336f534b",  # host-cpu
    "2a4eae2df2fb28f6",  # host-disk
    "d481b40828e2b129",  # host-disk-forecast
    "b3ede1b5b9be756c",  # host-memory
    "3cd8a61c8d6c6eab",  # log-errors
    "13a597bae3d8f617",  # mqtt-no-clients
    "2c920a0e39f2bea5",  # pg-connections
    "70881a9a016e6a9d",  # pg-down
    "2700cd0e04f6ba78",  # rds-cpu
    "653568ddec628290",  # rds-storage
    "a6b944ac05b83357",  # redis-down
    "2db60247f09221d4",  # redis-memory
    "234aa81a9086ab11",  # target-down
})
