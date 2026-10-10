from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

WARSAW = ZoneInfo("Europe/Warsaw")
PORD_GDANSK_ID = 43
CATEGORIES = "AM A1 A2 A B1 B C1 C D1 D B+E C1+E C+E D1+E D+E T PT".split()
CENTERS = json.loads(Path(__file__).with_name("centers.json").read_text())
CENTER_IDS = {c["id"] for c in CENTERS}
MIN_INTERVAL_SECONDS = 15 * 60
DEFAULT_INTERVAL_SECONDS = 20 * 60


def now_local():
    return datetime.now(WARSAW)


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date_from: date = Field(default_factory=lambda: now_local().date())
    date_to: date = Field(default_factory=lambda: now_local().date() + timedelta(days=13))
    time_from: time = time(7)
    time_to: time = time(18)
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], min_length=1, max_length=7)

    @model_validator(mode="after")
    def validate_range(self):
        if self.date_to < self.date_from or (self.date_to - self.date_from).days > 59:
            raise ValueError("Zakres dat musi obejmować od 1 do 60 dni.")
        if self.time_from.tzinfo or self.time_to.tzinfo or self.time_from > self.time_to:
            raise ValueError("Podaj rosnący zakres godzin w czasie polskim.")
        if any(d not in range(7) for d in self.weekdays) or len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("Nieprawidłowe dni tygodnia.")
        return self


class MonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    center_id: int = 43
    profile_id: str = Field(default="", max_length=64)
    ranges: list[TimeRange] = Field(default_factory=lambda: [TimeRange()], min_length=1, max_length=10)
    interval_seconds: int = Field(default=DEFAULT_INTERVAL_SECONDS,
                                  ge=MIN_INTERVAL_SECONDS, le=3600)

    @model_validator(mode="before")
    @classmethod
    def migrate_single_range(cls, data):
        if isinstance(data, dict) and "date_from" in data and "ranges" not in data:
            data = dict(data)
            data["ranges"] = [{k: data.pop(k) for k in ("date_from", "date_to", "time_from", "time_to", "weekdays") if k in data}]
        return data

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.center_id not in CENTER_IDS:
            raise ValueError("Wybierz ośrodek z listy.")
        if (max(r.date_to for r in self.ranges) - min(r.date_from for r in self.ranges)).days > 59:
            raise ValueError("Wszystkie przedziały muszą mieścić się w okresie 60 dni.")
        return self

    def start_date(self, today: date) -> date | None:
        starts = [
            max(range_.date_from, today)
            for range_ in self.ranges
            if range_.date_to >= today
        ]
        return min(starts) if starts else None

    def matches(self, slot: dict, now: datetime) -> bool:
        dt = datetime.fromisoformat(slot["start"]).astimezone(WARSAW)
        return (slot["center_id"] == self.center_id and slot["places"] > 0 and dt > now
                and any(r.date_from <= dt.date() <= r.date_to and dt.weekday() in r.weekdays
                        and r.time_from <= dt.time().replace(tzinfo=None) <= r.time_to for r in self.ranges))


def parse_one_center_schedule(data, expected_category: str) -> list[dict]:
    """Normalize all practical exams in a one-center calendar response."""
    if not isinstance(data, Mapping):
        raise ValueError("one_center")
    if "startDatePointerForCalendar" not in data or data["startDatePointerForCalendar"] is None:
        raise ValueError("one_center_calendar_pointer")

    days = data.get("examCollectionForDay")
    if not isinstance(days, list):
        raise ValueError("one_center_days")
    found: dict[str, dict] = {}

    for day in days:
        if not isinstance(day, Mapping) or not isinstance(day.get("date"), str):
            raise ValueError("one_center_day")
        try:
            calendar_date = date.fromisoformat(day["date"])
        except ValueError:
            raise ValueError("one_center_day_date") from None
        collections = day.get("examCollections")
        if not isinstance(collections, list):
            raise ValueError("one_center_collections")
        for item in collections:
            if not isinstance(item, Mapping):
                raise ValueError("one_center_exam")
            exam_type = item.get("examType")
            if not isinstance(exam_type, str):
                raise ValueError("one_center_exam_type")
            if exam_type not in {"Practice", "Theoretical", "Theory"}:
                raise ValueError("one_center_exam_type")
            if exam_type in {"Theoretical", "Theory"}:
                continue
            required = {
                "organizationId", "category", "practiceId", "practiceDateTime",
                "placePracticeAmount",
            }
            if not required.issubset(item):
                raise ValueError("one_center_practice")
            practice_id = item["practiceId"]
            practice_date_time = item["practiceDateTime"]
            if practice_id is None or not str(practice_id).strip() or not practice_date_time:
                continue
            if not isinstance(practice_date_time, str):
                raise ValueError("one_center_datetime")
            if not isinstance(practice_id, (str, int)) or isinstance(practice_id, bool):
                raise ValueError("one_center_practice_id")
            organization_id = item["organizationId"]
            if isinstance(organization_id, bool) or not isinstance(organization_id, int):
                raise ValueError("one_center_organization_id")
            category = item["category"]
            if not isinstance(category, str):
                raise ValueError("one_center_category")
            try:
                if (isinstance(item["placePracticeAmount"], bool)
                        or not isinstance(item["placePracticeAmount"], int)):
                    raise ValueError()
                places = int(item["placePracticeAmount"])
            except (TypeError, ValueError, OverflowError):
                raise ValueError("one_center_places") from None
            try:
                dt = datetime.fromisoformat(practice_date_time)
            except ValueError:
                raise ValueError("one_center_datetime") from None
            dt = dt.replace(tzinfo=WARSAW) if dt.tzinfo is None else dt.astimezone(WARSAW)
            if dt.date() != calendar_date:
                raise ValueError("one_center_day_mismatch")
            if places <= 0:
                continue
            if (organization_id != PORD_GDANSK_ID or category != "B"
                    or expected_category != "B"):
                continue
            practice_id = str(practice_id)
            key = f"{organization_id}:{practice_id}"
            found[key] = {
                "key": key, "center_id": organization_id,
                "center_name": str(item.get("organizationName") or "PORD Gdańsk")[:160],
                "start": dt.isoformat(),
                "places": places, "category": "B", "practice_id": practice_id,
            }
    return sorted(found.values(), key=lambda slot: slot["start"])


def one_center_calendar_dates(data) -> list[date]:
    if not isinstance(data, Mapping):
        raise ValueError("one_center")
    days = data.get("examCollectionForDay")
    if not isinstance(days, list):
        raise ValueError("one_center_days")
    dates = []
    for day in days:
        if not isinstance(day, Mapping) or not isinstance(day.get("date"), str):
            raise ValueError("one_center_day")
        try:
            dates.append(date.fromisoformat(day["date"]))
        except ValueError:
            raise ValueError("one_center_day_date") from None
    return sorted(set(dates))
