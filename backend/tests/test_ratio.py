import pytest

from nigrani.config import RuleCfg
from nigrani.format import build_event, fmt_value


@pytest.mark.parametrize("value, expected", [(0, "0.0%"), (0.125, "12.5%"), (0.8, "80.0%"), (1, "100.0%"), (1.25, "125.0%"), (-0.25, "-25.0%"), (None, "—")])
def test_ratio_format(value, expected):
    assert fmt_value(value, "ratio") == expected


def test_ratio_rule_and_notification_keep_raw_thresholds():
    rule = RuleCfg(id="ratio", name="Disk in use", expr="disk_in_use", unit="ratio", warn=0.8, crit=0.9)
    assert rule.warn == 0.8 and rule.crit == 0.9
    event = build_event("firing", severity="warning", rule_name=rule.name, series="", value=0.85,
                        threshold=rule.warn, unit=rule.unit, summary="", started_at=0, now=1, environment="test")
    assert event["value"] == 0.85 and event["threshold"] == 0.8
    assert event["value_text"] == "85.0%"
    assert event["threshold_text"] == "80.0%"
    assert "Current: 85.0% (threshold 80.0%)" in event["body"]
    assert fmt_value(12.5, "percent") == "12.5%"
