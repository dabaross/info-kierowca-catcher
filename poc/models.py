from __future__ import annotations

import json
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

    def windows(self, today: date):
        days = sorted({r.date_from + timedelta(days=i) for r in self.ranges
                       for i in range((r.date_to-r.date_from).days+1)
                       if r.date_from + timedelta(days=i) >= today})
        result = []
        while days:
            start = days[0]
            end = min(start + timedelta(days=19), days[-1])
            result.append((start, end))
            days = [d for d in days if d > end]
        return result

    def matches(self, slot: dict, now: datetime) -> bool:
        dt = datetime.fromisoformat(slot["start"]).astimezone(WARSAW)
        return (slot["center_id"] == self.center_id and slot["places"] > 0 and dt > now
                and any(r.date_from <= dt.date() <= r.date_to and dt.weekday() in r.weekdays
                        and r.time_from <= dt.time().replace(tzinfo=None) <= r.time_to for r in self.ranges))


def parse_schedule(data, expected_category: str) -> list[dict]:
    """Normalize the confirmed multi-center response without hiding schema changes."""
    if not isinstance(data, list):
        raise ValueError("schedule_centers")
    found = {}
    expected_category = expected_category.upper()
    for center in data:
        if not isinstance(center, dict) or "wordId" not in center:
            raise ValueError("schedule_center")
        try:
            center_id = int(center["wordId"])
        except (TypeError, ValueError, OverflowError):
            raise ValueError("schedule_center_id") from None
        if center_id != PORD_GDANSK_ID:
            continue
        if ("wordName" not in center or "examCollectionForDay" not in center
                or not isinstance(center["examCollectionForDay"], list)):
            raise ValueError("schedule_center")
        center_name = str(center["wordName"])
        for item in center["examCollectionForDay"]:
            if not isinstance(item, dict):
                raise ValueError("schedule_item")
            organization_id = item.get("organizationId")
            if organization_id is not None:
                if isinstance(organization_id, bool):
                    raise ValueError("schedule_organization_id")
                try:
                    organization_id = int(organization_id)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError("schedule_organization_id") from None
                if organization_id != PORD_GDANSK_ID:
                    continue
            if "examType" not in item:
                raise ValueError("schedule_item")
            if item["examType"] != "Practice":
                continue
            required = {"practiceId", "practiceDateTime", "placePracticeAmount", "category"}
            if not required.issubset(item):
                raise ValueError("schedule_practice")
            practice_id = item["practiceId"]
            practice_date_time = item["practiceDateTime"]
            if practice_id is None or not str(practice_id).strip() or not practice_date_time:
                continue
            if not isinstance(practice_date_time, str):
                raise ValueError("schedule_datetime")
            try:
                places = int(item["placePracticeAmount"] or 0)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("schedule_places") from None
            if places <= 0:
                continue
            if not isinstance(item["category"], str):
                raise ValueError("schedule_category")
            item_category = item["category"].upper()
            if item_category != expected_category:
                continue
            dt = datetime.fromisoformat(practice_date_time)
            dt = dt.replace(tzinfo=WARSAW) if dt.tzinfo is None else dt.astimezone(WARSAW)
            practice_id = str(practice_id)
            key = f"{center_id}:{practice_id}"
            found[key] = {"key": key, "center_id": center_id,
                          "center_name": center_name[:160], "start": dt.isoformat(),
                          "places": places, "category": item_category,
                          "practice_id": practice_id}
    return sorted(found.values(), key=lambda s: s["start"])
