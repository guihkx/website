import datetime
import json
from collections import defaultdict
from urllib.parse import urlparse, urlunparse

import orjson
import requests

from app import utils

from . import config, db, search

StatsType = dict[str, dict[str, list[int]]]
POPULAR_DAYS_NUM = 7

FIRST_STATS_DATE = datetime.date(2018, 4, 29)


def _get_stats_for_date(date: datetime.date, session: requests.Session):
    stats_json_url = urlparse(
        config.settings.stats_baseurl + date.strftime("/%Y/%m/%d.json")
    )
    if stats_json_url.scheme == "file":
        try:
            with open(stats_json_url.path) as stats_file:
                stats = json.load(stats_file)
        except FileNotFoundError:
            return None
        return stats
    redis_key = f"stats:date:{date.isoformat()}"
    stats_txt = db.redis_conn.get(redis_key)
    if stats_txt is None:
        response = session.get(urlunparse(stats_json_url))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        stats = response.json()
        expire = (
            datetime.timedelta(hours=4)
            if date > datetime.date.today() + datetime.timedelta(days=-7)
            else None
        )
        db.redis_conn.set(redis_key, orjson.dumps(stats), ex=expire)
    else:
        stats = orjson.loads(stats_txt)
    return stats


def _get_stats_for_period(sdate: datetime.date, edate: datetime.date):
    totals: StatsType = {}
    with requests.Session() as session:
        for i in range((edate - sdate).days + 1):
            date = sdate + datetime.timedelta(days=i)
            stats = _get_stats_for_date(date, session)

            if stats is None or "refs" not in stats or stats["refs"] is None:
                continue
            for app_id, app_stats in stats["refs"].items():
                app_id_without_architecture = _remove_architecture_from_id(app_id)
                if app_id_without_architecture not in totals:
                    totals[app_id_without_architecture] = {}
                app_totals = totals[app_id_without_architecture]
                for arch, downloads in app_stats.items():
                    if arch not in app_totals:
                        app_totals[arch] = [0, 0, 0]
                    app_totals[arch][0] += downloads[0]
                    app_totals[arch][1] += downloads[1]
                    app_totals[arch][2] += downloads[0] - downloads[1]
    return totals


def _get_app_stats_per_day() -> dict[str, dict[str, int]]:
    # Skip last two days as flathub-stats publishes partial statistics
    edate = datetime.date.today() - datetime.timedelta(days=2)
    sdate = FIRST_STATS_DATE

    app_stats_per_day: dict[str, dict[str, int]] = {}

    with requests.Session() as session:
        for i in range((edate - sdate).days + 1):
            date = sdate + datetime.timedelta(days=i)
            stats = _get_stats_for_date(date, session)

            if stats is not None and "refs" in stats and stats["refs"] is not None:
                for app_id, app_stats in stats["refs"].items():
                    app_id_without_architecture = _remove_architecture_from_id(app_id)
                    if app_id_without_architecture not in app_stats_per_day:
                        app_stats_per_day[app_id_without_architecture] = {}
                    app_stats_per_day[app_id_without_architecture][
                        date.isoformat()
                    ] = sum([i[0] - i[1] for i in app_stats.values()])
    return app_stats_per_day


def _get_stats(app_count: int) -> dict[str, dict[str, int]]:
    edate = datetime.date.today()
    sdate = FIRST_STATS_DATE

    downloads_per_day: dict[str, int] = {}
    delta_downloads_per_day: dict[str, int] = {}
    updates_per_day: dict[str, int] = {}
    totals_country: dict[str, int] = {}
    with requests.Session() as session:
        for i in range((edate - sdate).days + 1):
            date = sdate + datetime.timedelta(days=i)
            stats = _get_stats_for_date(date, session)

            if (
                stats is not None
                and "downloads" in stats
                and stats["downloads"] is not None
            ):
                downloads_per_day[date.isoformat()] = stats["downloads"]

            if (
                stats is not None
                and "updates" in stats
                and stats["updates"] is not None
            ):
                updates_per_day[date.isoformat()] = stats["updates"]

            if (
                stats is not None
                and "delta_downloads" in stats
                and stats["delta_downloads"] is not None
            ):
                delta_downloads_per_day[date.isoformat()] = stats["delta_downloads"]

            if (
                stats is not None
                and "countries" in stats
                and stats["countries"] is not None
            ):
                for country, downloads in stats["countries"].items():
                    if country not in totals_country:
                        totals_country[country] = 0
                    totals_country[country] = totals_country[country] + downloads

    totals = search.search_apps_post(search.SearchQuery(query="", filters=None))

    return {
        "countries": totals_country,
        "downloads_per_day": downloads_per_day,
        "updates_per_day": updates_per_day,
        "delta_downloads_per_day": delta_downloads_per_day,
        "downloads": sum(downloads_per_day.values()),
        "number_of_apps": app_count,
        "category_totals": totals["facetDistribution"]["main_categories"],
    }


def _sort_key(
    app_stats: dict[str, list[int]], for_arches: list[str] | None = None
) -> int:
    new_dls = 0
    for arch, dls in app_stats.items():
        if for_arches is not None and arch not in for_arches:
            continue
        new_dls += dls[2]
    return new_dls


def _is_app(app_id: str) -> bool:
    return "/" not in app_id


def _remove_architecture_from_id(app_id: str) -> str:
    return app_id.split("/")[0]


def get_installs_by_ids(ids: list[str]):
    result = defaultdict()
    for app_id in ids:
        app_stats = db.get_json_key(f"app_stats:{app_id}")
        if app_stats is None:
            continue

        app_stats["id"] = app_id
        result[app_id] = app_stats
    return result


def get_popular(days: int | None):
    edate = datetime.date.today()

    if days is None:
        sdate = FIRST_STATS_DATE
    else:
        sdate = edate - datetime.timedelta(days=days - 1)

    redis_key = f"popular:{sdate}-{edate}"

    if popular := db.get_json_key(redis_key):
        return popular

    stats = _get_stats_for_period(sdate, edate)
    sorted_apps = sorted(
        filter(lambda a: _is_app(a[0]), stats.items()),
        key=lambda a: _sort_key(a[1]),
        reverse=True,
    )

    popular = [k for k, v in sorted_apps]
    db.redis_conn.set(redis_key, orjson.dumps(popular), ex=60 * 60)
    return popular


def update(all_app_ids: list):
    stats_apps_dict = defaultdict(lambda: {})

    edate = datetime.date.today()
    sdate = datetime.date(2018, 4, 29)

    stats_total = _get_stats_for_period(sdate, edate)
    stats_dict = _get_stats(len(all_app_ids))

    app_stats_per_day = _get_app_stats_per_day()

    for app_id, dict in stats_total.items():
        # Index 0 is install and update count index 1 would be the update count
        # Index 2 is the install count
        stats_apps_dict[app_id]["installs_total"] = sum([i[2] for i in dict.values()])

        if app_id in app_stats_per_day:
            stats_apps_dict[app_id]["installs_per_day"] = app_stats_per_day[app_id]

    sdate_30_days = edate - datetime.timedelta(days=30 - 1)
    stats_30_days = _get_stats_for_period(sdate_30_days, edate)

    stats_installs: list = []
    for app_id, dict in stats_30_days.items():
        # Index 0 is install and update count index 1 would be the update count
        # Index 2 is the install count
        installs_last_month = sum([i[2] for i in dict.values()])
        stats_apps_dict[app_id]["installs_last_month"] = installs_last_month
        if app_id in all_app_ids:
            if not db.is_appid_for_frontend(app_id):
                continue

            stats_installs.append(
                {
                    "id": utils.get_clean_app_id(app_id),
                    "installs_last_month": installs_last_month,
                }
            )
    search.create_or_update_apps(stats_installs)

    sdate_7_days = edate - datetime.timedelta(days=7 - 1)
    stats_7_days = _get_stats_for_period(sdate_7_days, edate)

    for app_id, dict in stats_7_days.items():
        # Index 0 is install and update count index 1 would be the update count
        # Index 2 is the install count
        stats_apps_dict[app_id]["installs_last_7_days"] = sum(
            [i[2] for i in dict.values()]
        )

    # Make sure the Apps has all Keys
    for app_id in stats_apps_dict:
        stats_apps_dict[app_id]["installs_total"] = stats_apps_dict[app_id].get(
            "installs_total", 0
        )
        stats_apps_dict[app_id]["installs_last_month"] = stats_apps_dict[app_id].get(
            "installs_last_month", 0
        )
        stats_apps_dict[app_id]["installs_last_7_days"] = stats_apps_dict[app_id].get(
            "installs_last_7_days", 0
        )
        stats_apps_dict[app_id]["installs_per_day"] = stats_apps_dict[app_id].get(
            "installs_per_day", {}
        )

    new_id: str
    old_id_list: list[str]
    for new_id, old_id_list in db.get_json_key("eol_rebase").items():
        if new_id not in stats_apps_dict:
            stats_apps_dict[new_id] = {
                "installs_total": 0,
                "installs_last_month": 0,
                "installs_last_7_days": 0,
                "installs_per_day": {},
            }

        for old_id in old_id_list:
            old_id = old_id.removesuffix(":stable")

            if old_id not in stats_apps_dict:
                continue

            stats_apps_dict[new_id]["installs_total"] += stats_apps_dict[old_id][
                "installs_total"
            ]
            stats_apps_dict[new_id]["installs_last_month"] += stats_apps_dict[old_id][
                "installs_last_month"
            ]
            stats_apps_dict[new_id]["installs_last_7_days"] += stats_apps_dict[old_id][
                "installs_last_7_days"
            ]

            for day, count in stats_apps_dict[old_id]["installs_per_day"].items():
                if day in stats_apps_dict[new_id]["installs_per_day"]:
                    stats_apps_dict[new_id]["installs_per_day"][day] += count
                else:
                    stats_apps_dict[new_id]["installs_per_day"][day] = count

            sorted_days = {}
            for day in sorted(stats_apps_dict[new_id]["installs_per_day"]):
                sorted_days[day] = stats_apps_dict[new_id]["installs_per_day"][day]
            stats_apps_dict[new_id]["installs_per_day"] = sorted_days

    db.redis_conn.set("stats", orjson.dumps(stats_dict))
    db.redis_conn.mset(
        {
            f"app_stats:{app_id}": orjson.dumps(stats_apps_dict[app_id])
            for app_id in stats_apps_dict
        }
    )
