import json
import os
import tempfile
from types import SimpleNamespace

import pytest

os.environ["WORKOUT_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["SESSION_SECRET"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402
from fit_tool.fit_file import FitFile  # noqa: E402
from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage  # noqa: E402

from app import llm, main  # noqa: E402
from app.exporters import to_fit, to_zwo  # noqa: E402
from app.main import app  # noqa: E402
from app.workout import SPORTS, WorkoutError, stats, validate  # noqa: E402
from app.importers import ImportError_, from_zwo  # noqa: E402

THRESHOLD = {
    "name": "4x8 Threshold",
    "sport": "cycling",
    "description": "Classic threshold session",
    "steps": [
        {"type": "warmup", "duration_s": 600, "intensity": 0.5, "intensity_end": 0.75},
        {"type": "repeat", "count": 4, "steps": [
            {"type": "steady", "duration_s": 480, "intensity": 0.97, "label": "Threshold"},
            {"type": "rest", "duration_s": 180, "intensity": 0.55},
        ]},
        {"type": "cooldown", "duration_s": 600, "intensity": 0.6, "intensity_end": 0.4},
    ],
}

RUN = {
    "name": "6x400",
    "sport": "running",
    "description": "",
    "steps": [
        {"type": "warmup", "duration_s": 600, "intensity": 0.75, "intensity_end": 0.85},
        {"type": "repeat", "count": 6, "steps": [
            {"type": "steady", "distance_m": 400, "intensity": 1.1},
            {"type": "rest", "duration_s": 90, "intensity": 0.6},
        ]},
    ],
}


# --- workout format -----------------------------------------------------------

def test_stats_duration():
    assert stats(validate(THRESHOLD))["duration_s"] == 600 + 4 * 660 + 600


def test_validation_rejects_bad_steps():
    bad = json.loads(json.dumps(THRESHOLD))
    bad["steps"][0]["intensity"] = 9
    with pytest.raises(WorkoutError):
        validate(bad)
    with pytest.raises(WorkoutError, match="distance_m"):
        validate({**THRESHOLD, "steps": [{"type": "steady", "distance_m": 1000, "intensity": 1}]})


# --- exporters --------------------------------------------------------------------

def test_zwo_uses_intervals_block():
    xml = to_zwo(validate(THRESHOLD)).decode()
    assert "<sportType>bike</sportType>" in xml
    assert 'IntervalsT Repeat="4" OnDuration="480" OffDuration="180" OnPower="0.97" OffPower="0.55"' in xml


def test_zwo_running_converts_distance_to_time():
    xml = to_zwo(validate(RUN, SPORTS), 270).decode()
    assert "<sportType>run</sportType>" in xml
    # 400 m at 110% of 4:30/km threshold speed ~= 98 s
    assert 'SteadyState Duration="98" Power="1.1"' in xml


def _fit_steps(data: bytes, tmp_path):
    path = tmp_path / "w.fit"
    path.write_bytes(data)
    return [r.message for r in FitFile.from_file(str(path)).records if isinstance(r.message, WorkoutStepMessage)]


def test_fit_cycling_repeat_and_power(tmp_path):
    steps = _fit_steps(to_fit(validate(THRESHOLD)), tmp_path)
    assert len(steps) == 5
    assert (steps[1].custom_target_power_low, steps[1].custom_target_power_high) == (95, 99)
    assert steps[3].duration_step == 1 and steps[3].target_repeat_steps == 4


def test_fit_running_speed_targets(tmp_path):
    steps = _fit_steps(to_fit(validate(RUN, SPORTS), 270), tmp_path)
    assert steps[1].duration_distance == 400
    assert 3.9 < steps[1].custom_target_speed_low < steps[1].custom_target_speed_high < 4.2


# --- API with a fake Claude --------------------------------------------------------------

class Block(SimpleNamespace):
    def model_dump(self, exclude_none=False):
        return dict(vars(self))


class FakeClient:
    def __init__(self, workout):
        self.calls = []
        self.workout = workout
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        last = kwargs["messages"][-1]["content"]
        if isinstance(last, list) and last[0]["type"] == "tool_result":
            return SimpleNamespace(stop_reason="end_turn", content=[Block(type="text", text="Here is your workout.")])
        return SimpleNamespace(stop_reason="tool_use", content=[
            Block(type="tool_use", id="tu_1", name="set_workout", input=self.workout),
        ])


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient(THRESHOLD)
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda: fake)
    for name in ("APP_MODE", "ADMIN_EMAILS", "REQUIRE_INVITE", "DAILY_MESSAGE_LIMIT", "MONTHLY_BUDGET_USD"):
        monkeypatch.delenv(name, raising=False)
    main.security.reset_rate_limits()
    with TestClient(app) as c:
        c.fake = fake
        yield c


def test_accounts_and_chat_flow(client):
    assert client.get("/api/me").status_code == 401
    r = client.post("/api/register", json={"email": "Rider@Example.com", "password": "longenough"})
    assert r.status_code == 200 and r.json()["email"] == "rider@example.com"
    client.put("/api/me/profile", json={"ftp": 250, "threshold_pace_s_per_km": 270, "max_hr": None})

    wid = client.post("/api/workouts").json()["id"]
    view = client.post(f"/api/workouts/{wid}/chat", json={"message": "4x8 threshold"}).json()
    assert view["reply"] == "Here is your workout."
    assert view["workout"]["name"] == "4x8 Threshold"
    assert [m["role"] for m in view["transcript"]] == ["user", "assistant"]
    assert view["transcript"][0]["text"] == "4x8 threshold"
    context = client.fake.calls[0]["messages"][0]["content"][0]["text"]
    assert "FTP 250 W" in context and "Current workout" not in context

    # A follow-up doesn't resend the workout: Claude already has it from its own tool call.
    client.post(f"/api/workouts/{wid}/chat", json={"message": "longer"})
    assert "Current workout" not in client.fake.calls[-2]["messages"][-1]["content"][0]["text"]

    assert client.get(f"/api/workouts/{wid}/export/zwo").status_code == 200
    fit = client.get(f"/api/workouts/{wid}/export/fit")
    assert fit.status_code == 200 and fit.headers["content-disposition"].endswith('4x8_Threshold.fit"')

    # A second account can't see the first one's workouts.
    client.post("/api/logout")
    client.post("/api/register", json={"email": "other@example.com", "password": "longenough"})
    assert client.get(f"/api/workouts/{wid}").status_code == 404


def test_login_rejects_wrong_password(client):
    client.post("/api/register", json={"email": "a@b.co", "password": "longenough"})
    client.post("/api/logout")
    assert client.post("/api/login", json={"email": "a@b.co", "password": "nope-nope"}).status_code == 401
    assert client.post("/api/login", json={"email": "a@b.co", "password": "longenough"}).status_code == 200


# --- send to Zwift ---------------------------------------------------------------

def test_send_to_zwift(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ZWIFT_WORKOUTS_DIR", str(tmp_path))
    client.post("/api/register", json={"email": "z@example.com", "password": "longenough"})
    assert client.get("/api/integrations").json()["zwift"]["available"] is True

    wid = client.post("/api/workouts").json()["id"]
    assert client.post(f"/api/workouts/{wid}/send/zwift").status_code == 400  # empty workout
    client.post(f"/api/workouts/{wid}/chat", json={"message": "4x8"})

    # Someone else's file with the same name is kept; ours gets a suffix.
    (tmp_path / "4x8_Threshold.zwo").write_text("<workout_file><author>Me</author></workout_file>")
    r = client.post(f"/api/workouts/{wid}/send/zwift").json()
    assert r["filename"] == "4x8_Threshold_1.zwo"
    assert "Me" in (tmp_path / "4x8_Threshold.zwo").read_text()

    # Sending again replaces our own earlier export instead of piling up copies.
    assert client.post(f"/api/workouts/{wid}/send/zwift").json()["filename"] == "4x8_Threshold_1.zwo"
    assert len(list(tmp_path.glob("*.zwo"))) == 2


def test_send_to_zwift_without_folder(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ZWIFT_WORKOUTS_DIR", str(tmp_path / "missing"))
    client.post("/api/register", json={"email": "nz@example.com", "password": "longenough"})
    assert client.get("/api/integrations").json()["zwift"]["available"] is False
    wid = client.post("/api/workouts").json()["id"]
    client.post(f"/api/workouts/{wid}/chat", json={"message": "4x8"})
    r = client.post(f"/api/workouts/{wid}/send/zwift")
    assert r.status_code == 400 and "Zwift workouts folder" in r.json()["detail"]


# --- import -------------------------------------------------------------------------

ZWIFT_FILE = b"""<workout_file>
    <author>Someone</author>
    <name>K3 Intervals</name>
    <description>Low cadence work</description>
    <sportType>bike</sportType>
    <tags><tag name="Strength"/></tags>
    <workout>
        <Warmup Duration="600" PowerLow="0.40449998" PowerHigh="0.70449996" pace="0"/>
        <IntervalsT Repeat="3" OnDuration="600" OffDuration="300" OnPower="0.90449995" OffPower="0.58449996" Cadence="55" CadenceResting="87">
            <textevent timeoffset="10" message="Low cadence, stay seated"/>
        </IntervalsT>
        <SteadyState Duration="900" Power="0.6" pace="0">
            <textevent timeoffset="0" message="Easy spin"/>
            <textevent timeoffset="300" message="Halfway"/>
        </SteadyState>
        <FreeRide Duration="300" FlatRoad="0"/>
        <Cooldown Duration="300" PowerLow="0.58" PowerHigh="0.40"/>
    </workout>
</workout_file>"""


def test_from_zwo_reads_blocks_and_messages():
    w = from_zwo(ZWIFT_FILE)
    assert w["name"] == "K3 Intervals" and w["sport"] == "cycling" and w["tags"] == ["Strength"]
    rep = w["steps"][1]
    assert rep["count"] == 3 and rep["steps"][0]["cadence"] == 55 and rep["steps"][1]["cadence"] == 87
    assert rep["text_events"] == [{"offset_s": 10, "message": "Low cadence, stay seated"}]
    assert w["steps"][2]["text_events"][1] == {"offset_s": 300, "message": "Halfway"}
    assert w["steps"][3] == {"type": "free", "duration_s": 300}
    assert stats(w)["duration_s"] == 600 + 3 * 900 + 900 + 300 + 300
    # Exporting and importing again gives the same workout.
    assert from_zwo(to_zwo(w)) == w


def test_from_zwo_rejects_non_workouts():
    with pytest.raises(ImportError_):
        from_zwo(b"<html></html>")
    with pytest.raises(ImportError_):
        from_zwo(b'<!DOCTYPE x [<!ENTITY a "aaaa">]><workout_file/>')


def test_import_edit_and_send_back_with_backup(client, tmp_path, monkeypatch):
    zwift_dir = tmp_path / "zwift"
    zwift_dir.mkdir()
    (zwift_dir / "K3.zwo").write_bytes(ZWIFT_FILE)
    monkeypatch.setenv("ZWIFT_WORKOUTS_DIR", str(zwift_dir))
    monkeypatch.setattr(main, "ZWIFT_BACKUPS", tmp_path / "backups")
    client.post("/api/register", json={"email": "imp@example.com", "password": "longenough"})

    listing = client.get("/api/zwift/workouts").json()
    assert listing[0]["filename"] == "K3.zwo" and listing[0]["name"] == "K3 Intervals"
    assert client.post("/api/import/zwift", json={"filename": "../secret.zwo"}).status_code == 400

    view = client.post("/api/import/zwift", json={"filename": "K3.zwo"}).json()
    assert view["source_filename"] == "K3.zwo" and view["workout"]["name"] == "K3 Intervals"

    # The first chat turn about an imported workout includes it, so Claude can edit it.
    client.post(f"/api/workouts/{view['id']}/chat", json={"message": "make it 4 reps"})
    assert '"K3 Intervals"' in client.fake.calls[0]["messages"][0]["content"][0]["text"]

    r = client.post(f"/api/workouts/{view['id']}/send/zwift").json()
    assert r["replaced"] is True and r["filename"] == "K3.zwo"
    assert b"4x8 Threshold" in (zwift_dir / "K3.zwo").read_bytes()  # the fake Claude's edit
    backups = list((tmp_path / "backups").glob("K3.*.zwo"))
    assert len(backups) == 1 and backups[0].read_bytes() == ZWIFT_FILE
    assert len(list(zwift_dir.glob("*.zwo"))) == 1


def test_import_uploaded_file(client):
    client.post("/api/register", json={"email": "up@example.com", "password": "longenough"})
    r = client.post("/api/import/file", files={"file": ("k3.zwo", ZWIFT_FILE, "application/xml")})
    assert r.status_code == 200 and r.json()["source_filename"] is None
    bad = client.post("/api/import/file", files={"file": ("k3.zwo", b"not xml", "application/xml")})
    assert bad.status_code == 400
    other = client.post("/api/import/file", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert other.status_code == 400 and ".fit" in other.json()["detail"]


# --- running is switched off until v2 ----------------------------------------------------

def test_running_is_disabled(client):
    with pytest.raises(WorkoutError, match="running"):
        validate(RUN)
    with pytest.raises(ImportError_, match="Running isn't supported"):
        from_zwo(to_zwo(validate(RUN, SPORTS), 270))

    client.post("/api/register", json={"email": "run@example.com", "password": "longenough"})
    user_id = main.db.get_user_by_email("run@example.com")["id"]
    main.db.create_workout(user_id, validate(RUN, SPORTS))  # e.g. created before running was switched off
    draft = client.post("/api/workouts").json()["id"]
    assert [w["id"] for w in client.get("/api/workouts").json()] == [draft]


# --- Garmin .fit import ----------------------------------------------------------------

from fit_tool.fit_file_builder import FitFileBuilder  # noqa: E402
from fit_tool.profile.messages.file_id_message import FileIdMessage  # noqa: E402
from fit_tool.profile.messages.workout_message import WorkoutMessage  # noqa: E402
from fit_tool.profile.profile_type import (  # noqa: E402
    FileType, Intensity, Sport, WorkoutStepDuration, WorkoutStepTarget,
)

from app.importers import from_fit  # noqa: E402


def _garmin_fit(steps, sport=Sport.CYCLING, name="Garmin Workout"):
    """Build a workout .fit file the way Garmin Connect does (watt targets, zones, open steps)."""
    fid = FileIdMessage()
    fid.type = FileType.WORKOUT
    fid.manufacturer = 1
    fid.product = 0
    wkt = WorkoutMessage()
    wkt.workout_name = name
    wkt.sport = sport
    wkt.num_valid_steps = len(steps)
    msgs = []
    for i, (intensity, dur, target, low, high, repeat) in enumerate(steps):
        m = WorkoutStepMessage()
        m.message_index = i
        if repeat:
            m.duration_type = WorkoutStepDuration.REPEAT_UNTIL_STEPS_CMPLT
            m.duration_step = repeat[0]
            m.target_type = WorkoutStepTarget.OPEN
            m.target_repeat_steps = repeat[1]
        else:
            m.intensity = intensity
            if dur is None:
                m.duration_type = WorkoutStepDuration.OPEN
            else:
                m.duration_type = WorkoutStepDuration.TIME
                m.duration_time = float(dur)
            m.target_type = target
            if target == WorkoutStepTarget.POWER and low is not None and high is None:
                m.target_value = low  # power zone
            elif target == WorkoutStepTarget.POWER:
                m.target_value = 0
                m.custom_target_power_low = low
                m.custom_target_power_high = high
            elif target == WorkoutStepTarget.HEART_RATE:
                m.target_value = low  # HR zone
            else:
                m.target_value = 0
        msgs.append(m)
    b = FitFileBuilder(auto_define=True, min_string_size=50)
    b.add(fid)
    b.add(wkt)
    b.add_all(msgs)
    return b.build().to_bytes()


GARMIN = [
    (Intensity.WARMUP, 600, WorkoutStepTarget.POWER, 2, None, None),             # power zone 2
    (Intensity.ACTIVE, 300, WorkoutStepTarget.POWER, 1250, 1270, None),          # 250-270 W
    (Intensity.RECOVERY, 120, WorkoutStepTarget.HEART_RATE, 2, None, None),      # HR zone 2
    (None, None, None, None, None, (1, 4)),                                       # repeat steps 1-2 4x
    (Intensity.COOLDOWN, None, WorkoutStepTarget.OPEN, None, None, None),        # until lap button
]


def test_from_fit_garmin_style():
    w = from_fit(_garmin_fit(GARMIN), ftp=260)
    assert w["name"] == "Garmin Workout"
    warm, rep, cool = w["steps"]
    assert warm["intensity"] == 0.65 and warm["label"] == "Power zone 2"
    assert rep["count"] == 4 and rep["steps"][0]["intensity"] == round((250 / 260 + 270 / 260) / 2, 3)
    assert rep["steps"][1]["label"] == "HR zone 2"
    assert cool["type"] == "free" and cool["label"] == "Until lap button"


def test_from_fit_errors():
    with pytest.raises(ImportError_, match="Set your FTP"):
        from_fit(_garmin_fit(GARMIN), ftp=None)
    with pytest.raises(ImportError_, match="running"):
        from_fit(_garmin_fit(GARMIN, sport=Sport.RUNNING), ftp=260)
    with pytest.raises(ImportError_, match="isn't a valid .fit"):
        from_fit(b"\x0e\x10" + b"\x00" * 6 + b".FIT" + b"garbage", ftp=260)


def test_fit_round_trip_and_upload(client):
    w = validate(THRESHOLD)
    assert stats(from_fit(to_fit(w), ftp=None)) == stats(w)
    client.post("/api/register", json={"email": "fit@example.com", "password": "longenough"})
    r = client.post("/api/import/file", files={"file": ("THRESH.FIT", to_fit(w), "application/octet-stream")})
    assert r.status_code == 200 and r.json()["workout"]["name"] == "4x8 Threshold"


# --- hosting: mode, invites, limits, security ---------------------------------------------

def test_hosted_mode_disables_zwift_folder(client, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_MODE", "hosted")
    monkeypatch.setenv("REQUIRE_INVITE", "0")
    monkeypatch.setenv("ZWIFT_WORKOUTS_DIR", str(tmp_path))
    client.post("/api/register", json={"email": "h@example.com", "password": "longenough"})
    assert client.get("/api/integrations").json()["zwift"]["available"] is False
    assert client.get("/api/config").json()["hosted"] is True
    wid = client.post("/api/workouts").json()["id"]
    client.post(f"/api/workouts/{wid}/chat", json={"message": "4x8"})
    r = client.post(f"/api/workouts/{wid}/send/zwift")
    assert r.status_code == 400 and "Download" in r.json()["detail"]
    assert not list(tmp_path.iterdir())
    assert client.get(f"/api/workouts/{wid}/export/zwo").status_code == 200


def test_invites(client, monkeypatch):
    monkeypatch.setenv("APP_MODE", "hosted")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    assert client.get("/api/config").json()["invites_required"] is True

    no_code = client.post("/api/register", json={"email": "x@example.com", "password": "longenough"})
    assert no_code.status_code == 400 and "invite" in no_code.json()["detail"]

    admin = client.post("/api/register", json={"email": "boss@example.com", "password": "longenough"})
    assert admin.json()["is_admin"] is True
    code = client.post("/api/admin/invites", json={"note": "Anna"}).json()["code"]
    client.post("/api/logout")

    wrong = client.post("/api/register", json={"email": "anna@example.com", "password": "longenough", "invite_code": "NOPE-NOPE"})
    assert wrong.status_code == 400
    ok = client.post("/api/register", json={"email": "anna@example.com", "password": "longenough", "invite_code": code.lower()})
    assert ok.status_code == 200 and ok.json()["is_admin"] is False
    assert client.get("/api/admin/invites").status_code == 403
    client.post("/api/logout")

    reused = client.post("/api/register", json={"email": "ben@example.com", "password": "longenough", "invite_code": code})
    assert reused.status_code == 400 and "already been used" in reused.json()["detail"]
    assert main.db.get_user_by_email("ben@example.com") is None  # nothing half-created

    client.post("/api/login", json={"email": "boss@example.com", "password": "longenough"})
    invite = client.get("/api/admin/invites").json()[0]
    assert invite["used_by_email"] == "anna@example.com" and invite["note"] == "Anna"


class _Usage(SimpleNamespace):
    pass


def test_daily_limit_and_budget(client, monkeypatch):
    monkeypatch.setenv("DAILY_MESSAGE_LIMIT", "2")
    client.post("/api/register", json={"email": "lim@example.com", "password": "longenough"})
    wid = client.post("/api/workouts").json()["id"]

    # Every fake response used 1M input + 1M output tokens: $0.60 per call on Haiku.
    original = client.fake._create
    def with_usage(**kw):
        r = original(**kw)
        r.usage = _Usage(input_tokens=1_000_000, output_tokens=1_000_000)
        return r
    client.fake.beta.messages.create = with_usage
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-5-5")

    first = client.post(f"/api/workouts/{wid}/chat", json={"message": "a"}).json()
    assert first["messages_left_today"] == 1
    client.post(f"/api/workouts/{wid}/chat", json={"message": "b"})
    blocked = client.post(f"/api/workouts/{wid}/chat", json={"message": "c"})
    assert blocked.status_code == 429 and "2 chat messages" in blocked.json()["detail"]

    # Two turns x two API calls x $0.60 = $2.40 spent this month.
    monkeypatch.setenv("DAILY_MESSAGE_LIMIT", "100")
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "2")
    paused = client.post(f"/api/workouts/{wid}/chat", json={"message": "d"})
    assert paused.status_code == 503 and "budget" in paused.json()["detail"]


def test_login_rate_limit(client):
    for _ in range(10):
        client.post("/api/login", json={"email": "nobody@example.com", "password": "wrong-wrong"})
    r = client.post("/api/login", json={"email": "nobody@example.com", "password": "wrong-wrong"})
    assert r.status_code == 429


def test_security_headers_and_cross_site_block(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    evil = client.post("/api/login", json={"email": "a@b.co", "password": "x"}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    same = client.post("/api/login", json={"email": "nobody2@example.com", "password": "longenough"}, headers={"Origin": "http://testserver"})
    assert same.status_code == 401


# --- Garmin readiness -------------------------------------------------------------------

from datetime import date as _date, timedelta as _td  # noqa: E402

from app import garmin, readiness  # noqa: E402


class FakeGarmin:
    """Answers like the Personal API (shapes from Garmin's OpenAPI spec)."""

    def __init__(self, today, days=40, bad_last_night=False):
        self.today, self.days, self.bad = today, days, bad_last_night
        self.calls = []

    def _range(self, lo, hi):
        d = max(lo, self.today - _td(days=self.days - 1))
        while d <= hi:
            yield d
            d += _td(days=1)

    def get_daily_hrv_summaries(self, lo, hi):
        self.calls.append(("hrv", lo, hi))
        out = []
        for d in self._range(lo, hi):
            last = 38 if (self.bad and d == self.today) else 60 + (d.toordinal() % 5)
            out.append({"calendarDate": d.isoformat(), "lastNightAvg": last, "weeklyAvg": 61,
                        "baseline": {"lowUpper": 48, "balancedLow": 55, "balancedUpper": 70}, "status": "BALANCED"})
        return {"hrvSummaries": out}

    def get_daily_sleep_summaries(self, lo, hi):
        return [{"calendarDate": d.isoformat(), "sleepTimeSeconds": (5 if self.bad and d == self.today else 7.5) * 3600,
                 "deepSleepSeconds": 5400, "lightSleepSeconds": 14400, "remSleepSeconds": 6000, "awakeSleepSeconds": 900,
                 "sleepScores": {"overall": {"value": 45 if self.bad and d == self.today else 82, "qualifierKey": "GOOD"}}}
                for d in self._range(lo, hi)]

    def get_daily_summaries(self, lo, hi):
        return {"data": [{"calendarDate": d.isoformat(),
                          "heartRate": {"restingValue": 56 if self.bad and d == self.today else 48},
                          "bodyBattery": {"maxValue": 30 if self.bad and d == self.today else 85, "minValue": 20},
                          "stress": {"avgLevel": -1}} for d in self._range(lo, hi)]}

    def search_activities(self, start, limit, start_date, end_date):
        acts = [{"activityId": 1000 + d.toordinal(), "activityName": "Ride", "startTimeLocal": f"{d} 07:00:00",
                 "activityType": {"typeKey": "virtual_ride"}, "duration": 3600, "trainingStressScore": 60,
                 "activityTrainingLoad": 80, "normPower": 230}
                for d in self._range(start_date, end_date) if d.toordinal() % 2 == 0]
        return acts[start:start + limit]


@pytest.fixture
def garmin_owner(client, monkeypatch):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "test-key-not-real")
    monkeypatch.setenv("GARMIN_OWNER_EMAIL", "owner@example.com")
    creds = {"email": "owner@example.com", "password": "longenough"}
    if client.post("/api/register", json=creds).status_code != 200:
        client.post("/api/login", json=creds)
    return main.db.get_user_by_email("owner@example.com")


def test_garmin_sync_and_readiness(client, garmin_owner, monkeypatch):
    today = _date.today()
    fake = FakeGarmin(today)
    monkeypatch.setattr(garmin, "_client", lambda: fake)
    assert client.get("/api/me").json()["garmin"] is True

    data = client.get("/api/readiness").json()
    assert data["sync_error"] is None and data["is_today"] is True
    t = data["today"]
    assert t["score"] >= 75 and t["verdict"]["key"] == "ready"
    hrv = next(c for c in t["components"] if c["key"] == "hrv")
    assert "within your usual range (55–70)" in hrv["note"]
    assert t["load"]["chronic"] > 0 and t["load"]["ratio"] is not None
    assert len(data["trend"]) == 30 and data["trend"][-1]["sleep_hours"] == 7.5
    assert data["trend"][-1]["load"] in (0, 80)
    assert data["activities"] and data["activities"][0]["training_load"] == 80

    # A second visit within 3 hours doesn't call Garmin again; refresh=1 does, and only re-reads recent days.
    n = len(fake.calls)
    client.get("/api/readiness")
    assert len(fake.calls) == n
    client.get("/api/readiness?refresh=1")
    assert fake.calls[-1][1] >= today - _td(days=garmin.RESYNC_DAYS)


def test_readiness_drops_after_a_bad_night(client, garmin_owner, monkeypatch):
    monkeypatch.setattr(garmin, "_client", lambda: FakeGarmin(_date.today(), bad_last_night=True))
    t = client.get("/api/readiness?refresh=1").json()["today"]
    assert t["score"] < 55 and t["verdict"]["key"] in ("easy", "recover")
    rhr = next(c for c in t["components"] if c["key"] == "resting_hr")
    assert "8 bpm above" in rhr["note"]


def test_garmin_only_for_owner_and_errors_are_shown(client, garmin_owner, monkeypatch):
    def broken():
        raise garmin.GarminError("Garmin rejected the API key. It may have expired: create a new one in Garmin Connect.")
    monkeypatch.setattr(garmin, "_client", broken)
    r = client.get("/api/readiness?refresh=1").json()
    assert "expired" in r["sync_error"]

    client.post("/api/logout")
    client.post("/api/register", json={"email": "notowner@example.com", "password": "longenough"})
    assert client.get("/api/me").json()["garmin"] is False
    assert client.get("/api/readiness").status_code == 404


def test_public_pages_and_app_route(client):
    assert "Describe the vibe" in client.get("/").text
    assert 'id="auth-form"' in client.get("/app").text
    for page in ("privacy", "terms", "imprint"):
        r = client.get(f"/{page}")
        assert r.status_code == 200 and "<h1>" in r.text
    assert client.get("/nope").status_code == 404
    assert client.get("/healthz").json() == {"ok": True}  # not shadowed by "/{page}"


def test_send_to_zwift_still_recognizes_files_from_before_the_rename(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ZWIFT_WORKOUTS_DIR", str(tmp_path))
    (tmp_path / "4x8_Threshold.zwo").write_text("<workout_file><author>Workout Builder</author></workout_file>")
    from app import zwift
    assert zwift.save(b"<workout_file><author>VibeGrind</author></workout_file>", "4x8_Threshold.zwo").name == "4x8_Threshold.zwo"


def test_page_and_static_files_are_revalidated(client):
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/app").headers["cache-control"] == "no-cache"
    r = client.get("/static/app.js")
    assert r.headers["cache-control"] == "no-cache" and r.headers.get("etag")
    assert client.get("/healthz").headers.get("cache-control") != "no-cache"


def test_garmin_status_explains_why_it_is_off(client, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "zed@example.com, me@example.com")
    monkeypatch.delenv("GARMIN_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("GARMIN_PERSONAL_API_KEY", raising=False)
    creds = {"email": "me@example.com", "password": "longenough"}
    if client.post("/api/register", json=creds).status_code != 200:
        client.post("/api/login", json=creds)
    assert "isn't set" in client.get("/api/me").json()["garmin_status"]
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "k")
    me = client.get("/api/me").json()
    assert me["garmin"] is False and "several addresses" in me["garmin_status"]  # no silent pick
    monkeypatch.setenv("ADMIN_EMAILS", "me@example.com")
    me = client.get("/api/me").json()
    assert me["garmin"] is True and me["garmin_status"].startswith("On")


def _last_user_turn(client):
    """Content blocks of the latest user text turn sent to (fake) Claude."""
    msgs = client.fake.calls[-1]["messages"]
    return next(m["content"] for m in reversed(msgs) if m["role"] == "user" and m["content"][0]["type"] == "text")


def test_chat_gets_readiness_for_the_garmin_owner_only(client, garmin_owner, monkeypatch):
    monkeypatch.setattr(garmin, "_client", lambda: FakeGarmin(_date.today(), bad_last_night=True))
    client.get("/api/readiness?refresh=1")
    wid = client.post("/api/workouts").json()["id"]
    client.post(f"/api/workouts/{wid}/chat", json={"message": "What should I ride today?"})
    context = _last_user_turn(client)[0]["text"]
    assert "Readiness today:" in context and "HRV (" in context and "ratio" in context
    assert "Activities in the last 7 days:" in context and "virtual_ride, 60 min, load 80" in context
    assert _last_user_turn(client)[1]["text"] == "What should I ride today?"

    client.post("/api/logout")
    client.post("/api/register", json={"email": "friend2@example.com", "password": "longenough"})
    wid = client.post("/api/workouts").json()["id"]
    client.post(f"/api/workouts/{wid}/chat", json={"message": "What should I ride today?"})
    assert "Readiness" not in _last_user_turn(client)[0]["text"]


# --- backup and restore -------------------------------------------------------------------

from app import backup  # noqa: E402

SQLITE_HEADER = b"SQLite format 3\x00"


def _as(client, email, monkeypatch=None):
    creds = {"email": email, "password": "longenough"}
    if client.post("/api/register", json=creds).status_code != 200:
        client.post("/api/login", json=creds)


def test_backup_is_admin_only_and_a_real_database(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_EMAILS", "backup-admin@example.com")
    _as(client, "plain@example.com")
    assert client.get("/api/admin/backup").status_code == 403
    client.post("/api/logout")
    _as(client, "backup-admin@example.com")
    r = client.get("/api/admin/backup")
    assert r.status_code == 200 and r.content.startswith(SQLITE_HEADER)
    assert "vibegrind-backup-" in r.headers["content-disposition"]
    f = tmp_path / "b.sqlite3"
    f.write_bytes(r.content)
    assert backup.validate_backup(f)["users"] >= 2


def test_restore_guards(client, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "backup-admin@example.com")
    _as(client, "backup-admin@example.com")
    data = client.get("/api/admin/backup").content
    upload = {"file": ("b.sqlite3", data, "application/octet-stream")}

    off = client.post("/api/admin/restore", files=upload)
    assert off.status_code == 400 and "switched off" in off.json()["detail"]

    monkeypatch.setenv("ALLOW_RESTORE", "1")
    full = client.post("/api/admin/restore", files=upload)  # the test database has data
    assert full.status_code == 400 and "already has data" in full.json()["detail"]


def test_restore_into_a_fresh_server(client, monkeypatch, tmp_path):
    # Take a backup of the (busy) test database first.
    monkeypatch.setenv("ADMIN_EMAILS", "backup-admin@example.com")
    _as(client, "backup-admin@example.com")
    data = client.get("/api/admin/backup").content
    with backup.sqlite3.connect(f"file:{main.db.DB_PATH}?mode=ro", uri=True) as c:
        users_before = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    client.post("/api/logout")

    # A brand-new server: empty database, only the admin account created to run the restore.
    monkeypatch.setattr(main.db, "DB_PATH", tmp_path / "fresh.db")
    main.db.init_db()
    monkeypatch.setenv("ALLOW_RESTORE", "1")
    client.post("/api/register", json={"email": "backup-admin@example.com", "password": "fresh-password"})
    assert client.get("/api/admin/restore").json() == {"allowed": True, "target_empty": True}

    bad = client.post("/api/admin/restore", files={"file": ("x.sqlite3", b"not a database at all", "application/octet-stream")})
    assert bad.status_code == 400 and "isn't a VibeGrind database" in bad.json()["detail"]

    ok = client.post("/api/admin/restore", files={"file": ("b.sqlite3", data, "application/octet-stream")})
    assert ok.status_code == 200 and ok.json()["users"] == users_before
    assert client.get("/api/me").status_code == 401  # signed out after restore
    # The old account and password work again; the throwaway password doesn't.
    assert client.post("/api/login", json={"email": "backup-admin@example.com", "password": "fresh-password"}).status_code == 401
    assert client.post("/api/login", json={"email": "backup-admin@example.com", "password": "longenough"}).status_code == 200
    # A second restore is blocked: the server has data now.
    again = client.post("/api/admin/restore", files={"file": ("b.sqlite3", data, "application/octet-stream")})
    assert again.status_code == 400


# --- one official address -------------------------------------------------------------------

def test_secondary_addresses_redirect_to_the_canonical_host(client, monkeypatch):
    monkeypatch.setenv("CANONICAL_HOST", "vibegrind.app")
    r = client.get("/app?x=1", headers={"host": "vibegrind.onrender.com"}, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "https://vibegrind.app/app?x=1"
    r = client.get("/privacy", headers={"host": "www.vibegrind.app"}, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "https://vibegrind.app/privacy"
    r = client.post("/api/login", json={}, headers={"host": "vibegrind.onrender.com"}, follow_redirects=False)
    assert r.status_code == 308  # keeps POST and its body
    # Served normally: the canonical host, Render's health check, and unknown/internal hosts.
    assert client.get("/healthz", headers={"host": "vibegrind.onrender.com"}).status_code == 200
    assert client.get("/", headers={"host": "vibegrind.app"}, follow_redirects=False).status_code == 200
    assert client.get("/", headers={"host": "10.0.0.5:10000"}, follow_redirects=False).status_code == 200


def test_no_redirect_without_canonical_host(client, monkeypatch):
    monkeypatch.delenv("CANONICAL_HOST", raising=False)
    assert client.get("/", headers={"host": "vibegrind.onrender.com"}, follow_redirects=False).status_code == 200
