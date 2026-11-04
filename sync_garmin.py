
import os
import json
import logging
from datetime import date, timedelta

from supabase import create_client
from garmin_personal_api import (
    GarminPersonalApiClient,
    PersonalApiAuthenticator,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("garmin-sync")


def main():
    api_key = os.environ["GARMIN_API_KEY"]
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    db = create_client(supabase_url, supabase_key)

    today = date.today()
    start = today - timedelta(days=7)
    start_date = start.isoformat()
    end_date = today.isoformat()

    records = []

    def add_record(record_type, record_date, payload):
        records.append({
            "record_type": record_type,
            "record_date": str(record_date),
            "payload": payload,
            "synced_at": __import__("datetime").datetime.now().isoformat(),
        })

    with PersonalApiAuthenticator() as auth:
        token = auth.obtain_token(api_key)

    with GarminPersonalApiClient(token.access_token) as garmin:
        requests_to_make = [
            (
                "daily_summaries",
                lambda: garmin.get_daily_summaries(
                    start_date, end_date
                ),
            ),
            (
                "steps",
                lambda: garmin.get_daily_steps(
                    start_date, end_date
                ),
            ),
            (
                "stress",
                lambda: garmin.get_daily_stress(
                    start_date, end_date
                ),
            ),
            (
                "hrv",
                lambda: garmin.get_daily_hrv_summaries(
                    start_date, end_date
                ),
            ),
            (
                "sleep",
                lambda: garmin.get_daily_sleep_summaries(
                    start_date, end_date
                ),
            ),
            (
                "body_battery",
                lambda: garmin.get_daily_body_battery(
                    start_date, end_date
                ),
            ),
            (
                "activities",
                lambda: garmin.search_activities(
                    start_date=start_date,
                    end_date=end_date,
                    start=0,
                    limit=100,
                ),
            ),
        ]

        for record_type, request in requests_to_make:
            try:
                result = request()

                if result is None:
                    logger.info("%s: sin datos", record_type)
                    continue

                if isinstance(result, list):
                    for item in result:
                        item_date = (
    item.get("activityId")
    or item.get("startTimeLocal")
    or item.get("startTimeGMT")
    or item.get("calendarDate")
    or end_date
)
                        add_record(record_type, item_date, item)

                elif isinstance(result, dict):
                    add_record(record_type, end_date, result)

                else:
                    add_record(
                        record_type,
                        end_date,
                        {"data": result},
                    )

                logger.info("%s: consulta completada", record_type)

            except Exception:
                logger.exception(
                    "Error consultando %s", record_type
                )

    if not records:
        raise RuntimeError(
            "No se obtuvieron datos. Revisa el registro de ejecución."
        )

    # Guarda los datos en lotes pequeños.
    for offset in range(0, len(records), 100):
        batch = records[offset:offset + 100]
        db.table("garmin_records").upsert(
            batch,
            on_conflict="record_type,record_date",
        ).execute()

    logger.info(
        "Sincronización finalizada: %s registros procesados",
        len(records),
    )


if __name__ == "__main__":
    main()
