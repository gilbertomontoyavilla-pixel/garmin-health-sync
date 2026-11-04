
import os
import logging
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict

from supabase import create_client
from garmin_personal_api import (
    GarminPersonalApiClient,
    PersonalApiAuthenticator,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)

logger = logging.getLogger("garmin-sync")


def normalize_date(value, fallback):
    """Extrae una fecha YYYY-MM-DD de un valor Garmin."""
    if not value:
        return fallback

    value = str(value)

    if len(value) >= 10:
        candidate = value[:10]
        try:
            date.fromisoformat(candidate)
            return candidate
        except ValueError:
            pass

    return fallback


def main():
    # 1. Leer credenciales
    api_key = os.environ["GARMIN_API_KEY"]
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    db = create_client(supabase_url, supabase_key)

    # 2. Definir periodo de consulta
    today = date.today()
    start = today - timedelta(days=7)

    start_date = start.isoformat()
    end_date = today.isoformat()

    records = []

    # 3. Preparar función para almacenar registros
    def add_record(record_type, record_date, payload):
        records.append({
            "record_type": record_type,
            "record_date": str(record_date),
            "payload": payload,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        })

    # 4. Autenticación Garmin
    logger.info("Autenticando con Garmin...")

    with PersonalApiAuthenticator() as auth:
        token = auth.obtain_token(api_key)

    # 5. Consultar datos Garmin
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

                    # Agrupar actividades por día para no
                    # perder varias actividades de una misma fecha.
                    if record_type == "activities":
                        activities_by_date = defaultdict(list)

                        for item in result:
                            item_date = normalize_date(
                                item.get("startTimeLocal")
                                or item.get("startTimeGMT")
                                or item.get("calendarDate"),
                                end_date,
                            )

                            activities_by_date[item_date].append(item)

                        for activity_date, activities in activities_by_date.items():
                            add_record(
                                record_type,
                                activity_date,
                                {"activities": activities},
                            )

                    else:
                        for item in result:
                            item_date = normalize_date(
                                item.get("calendarDate")
                                or item.get("startTimeLocal")
                                or item.get("startTimeGMT"),
                                end_date,
                            )

                            add_record(
                                record_type,
                                item_date,
                                item,
                            )

                elif isinstance(result, dict):
                    add_record(
                        record_type,
                        end_date,
                        result,
                    )

                else:
                    add_record(
                        record_type,
                        end_date,
                        {"data": result},
                    )

                logger.info(
                    "%s: consulta completada",
                    record_type,
                )

            except Exception:
                logger.exception(
                    "Error consultando %s",
                    record_type,
                )

    # 6. Verificar que se obtuvieron datos
    if not records:
        raise RuntimeError(
            "No se obtuvieron datos de Garmin. "
            "Revisa el registro de ejecución."
        )

    logger.info(
        "Registros recibidos: %s",
        len(records),
    )

    # 7. Eliminar duplicados antes de guardar
    unique_records = {}

    for record in records:
        key = (
            record["record_type"],
            record["record_date"],
        )

        unique_records[key] = record

    records = list(unique_records.values())

    logger.info(
        "Registros después de deduplicar: %s",
        len(records),
    )

    # 8. Guardar en Supabase por lotes de 100
    for offset in range(0, len(records), 100):
        batch = records[offset:offset + 100]

        db.table("garmin_records").upsert(
            batch,
            on_conflict="record_type,record_date",
        ).execute()

        logger.info(
            "Lote guardado: %s de %s",
            min(offset + len(batch), len(records)),
            len(records),
        )

    logger.info(
        "Sincronización finalizada correctamente: %s registros",
        len(records),
    )


if __name__ == "__main__":
    main()
