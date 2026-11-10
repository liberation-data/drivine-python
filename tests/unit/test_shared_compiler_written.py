"""How drivine-python writes a statement's parameters for the shared compiler: temporals tagged at any depth."""

from datetime import date, datetime, time, timedelta, timezone

from drivine.cypher.shared_compiler import written

ZONE = timezone(timedelta(hours=10))


def test_each_python_temporal_is_tagged_with_the_modules_kind():
    assert written(datetime(2026, 11, 11, 9, 30, tzinfo=ZONE)) == {
        "$temporal": "DATE_TIME",
        "text": "2026-11-11T09:30:00+10:00",
    }
    assert written(datetime(2026, 11, 11, 9, 30)) == {"$temporal": "LOCAL_DATE_TIME", "text": "2026-11-11T09:30:00"}
    assert written(date(2026, 11, 11)) == {"$temporal": "DATE", "text": "2026-11-11"}
    assert written(time(9, 30, tzinfo=ZONE)) == {"$temporal": "TIME", "text": "09:30:00+10:00"}
    assert written(time(9, 30)) == {"$temporal": "LOCAL_TIME", "text": "09:30:00"}
    assert written(timedelta(days=1, seconds=5))["$temporal"] == "DURATION"


def test_a_temporal_is_tagged_at_any_depth_of_lists_and_maps():
    day = date(2026, 11, 11)
    tagged = {"$temporal": "DATE", "text": "2026-11-11"}
    assert written({"a": [day, {"b": (day,)}], "c": {"d": {day}}}) == {
        "a": [tagged, {"b": [tagged]}],
        "c": {"d": [tagged]},
    }
    # Beside a text that reads as one, the temporal is tagged and the text is left a text.
    assert written(["2026-11-11", day]) == ["2026-11-11", tagged]
    # In a map with a key starting with `$`, which is wrapped so it isn't read as a tag, its values are tagged still.
    assert written({"$k": day}) == {"$map": {"$k": tagged}}
