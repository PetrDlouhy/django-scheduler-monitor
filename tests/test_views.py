import json

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from scheduler_monitor.models import LaneOverride


@pytest.fixture
def staff_client(client, db):
    user = User.objects.create_user("ops", password="x", is_staff=True)
    client.force_login(user)
    return client


def test_anonymous_is_redirected_to_login(client, db):
    resp = client.get(reverse("scheduler_monitor:dashboard"))
    assert resp.status_code == 302
    assert "login" in resp["Location"]


@pytest.mark.parametrize("name", ["api_data", "api_older", "api_output", "api_merges"])
def test_api_endpoints_return_json_403_when_not_staff(client, db, name):
    # not a redirect-to-HTML — the dashboard's fetch().json() would choke on
    # "<!DOCTYPE" and show a mystery error; a JSON 403 lets it prompt re-login.
    resp = client.get(reverse(f"scheduler_monitor:{name}"))
    assert resp.status_code == 403
    assert resp["Content-Type"] == "application/json"
    assert resp.json() == {"ok": False, "error": "auth", "detail": "staff login required"}


def test_api_forbidden_for_authenticated_non_staff(client, db):
    User.objects.create_user("plain", password="x", is_staff=False)
    client.login(username="plain", password="x")
    resp = client.get(reverse("scheduler_monitor:api_data"))
    assert resp.status_code == 403


def test_dashboard_renders(staff_client):
    resp = staff_client.get(reverse("scheduler_monitor:dashboard"))
    assert resp.status_code == 200
    assert b"Scheduler Monitor" in resp.content
    assert reverse("scheduler_monitor:api_data").encode() in resp.content


def test_api_data_demo_mode(staff_client):
    resp = staff_client.get(reverse("scheduler_monitor:api_data"))
    data = resp.json()
    assert data["ok"] and data["demo"]
    assert len(data["runs"]) > 50
    run = data["runs"][0]
    for field in ("dyno", "job_label", "group_label", "outcome", "cadence", "start"):
        assert field in run
    outcomes = {r["outcome"] for r in data["runs"]}
    assert {"success", "running", "timed_out", "stopped"} <= outcomes


def test_api_data_rejects_bad_range(staff_client):
    resp = staff_client.get(reverse("scheduler_monitor:api_data"), {"lookback": "3d; drop"})
    assert resp.status_code == 400


def test_api_output_demo_mode(staff_client):
    resp = staff_client.get(reverse("scheduler_monitor:api_output"),
                            {"dyno": "scheduler.1234", "start": "2026-07-01T00:00:00Z"})
    data = resp.json()
    assert data["ok"] and data["count"] > 3
    assert all("t" in line and "m" in line for line in data["lines"])


def test_api_output_rejects_apl_injection(staff_client):
    resp = staff_client.get(reverse("scheduler_monitor:api_output"),
                            {"dyno": 'x" or 1==1 | project *', "start": "now-1h"})
    assert resp.status_code == 400


def test_merges_roundtrip(staff_client):
    url = reverse("scheduler_monitor:api_merges")
    payload = {"merges": [{"name": "rollup", "members": ["rollup --days N", "rollup"]}],
               "splits": ["export --id=N"]}
    resp = staff_client.post(url, json.dumps(payload), content_type="application/json")
    assert resp.json()["ok"]
    assert LaneOverride.objects.count() == 3

    data = staff_client.get(url).json()
    assert data["splits"] == ["export --id=N"]
    assert data["merges"] == [{"name": "rollup",
                               "members": ["rollup --days N", "rollup"]}]

    # posting a new state replaces the old one
    staff_client.post(url, json.dumps({"merges": [], "splits": []}),
                      content_type="application/json")
    assert LaneOverride.objects.count() == 0
