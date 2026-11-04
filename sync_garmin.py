
import os
import logging
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict

from supabase import create_client
from garmin_personal_api import (
    GarminPersonalApiClient,
    PersonalApiAuthenticator,
)

SYNC_CODE_VERSION = "2026-11-03-v2"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)

logger = logging.getLogger("garmin-sync")


def normalize_date(value, fallback):
    """Extrae una fecha válida en formato YYYY-MM-DD."""
    if not value:
        return fallback

    value = str(value)

    try:
        parsed_date = date.fromisoformat(value[:10])
        return parsed_date.isoformat()
    except (ValueError, TypeError):
        return fallback


def main():
    logger.info("SYNC_CODE_VERSION=%s", SYNC_CODE_VERSION)

    # 1. Validar credenciales
    required_variables = [
        "GARMIN_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
    ]

    missing = [
        name for name in required_variables
        if not os.environ.get(name)
    ]

    if missing:
        raise RuntimeError(
            "Faltan variables de entorno: " + ", ".join(missing)
        )

    api_key = os.environ["GARMIN_API_KEY"]
    supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
    supabase_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    # SUPABASE_URL debe ser la URL raíz del proyecto,
    # sin /rest/v1 ni rutas adicionales.
    if "/rest/v1" in supabase_url:
        raise ValueError(
            "SUPABASE_URL debe terminar en .supabase.co "
            "y no contener /rest/v1."
        )

    db = create_client(supabase_url, supabase_key)

    # 2. Consultar los últimos 7 días, incluido hoy
    today = date.today()
    start = today - timedelta(days=7)

    start_date = start.isoformat()
    end_date = today.isoformat()

    records = []

    def add_record(record_type, record_date, payload):
        """Añade un registro con la clave de conflicto de Supabase."""
        records.append({
            "record_type": record_type,
            "record_date": normalize_date(record_date, end_date),
            "payload": payload,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        })

    # 3. Autenticación con Garmin
    logger.info("Autenticando con Garmin...")

    with PersonalApiAuthenticator() as auth:
        token = auth.obtain_token(api_key)

    if not getattr(token, "access_token", None):
        raise RuntimeError(
            "Garmin no devolvió un token de acceso válido."
        )

    # 4. Consultar los datos
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

                    if record_type == "activities":
                        # Conservar todas las actividades del mismo día
                        # en un único registro por fecha.
                        activities_by_date = defaultdict(list)

                        for item in result:
                            if not isinstance(item, dict):
                                logger.warning(
                                    "Actividad ignorada: formato inesperado"
                                )
                                continue

                            item_date = normalize_date(
                                item.get("startTimeLocal")
                                or item.get("startTimeGMT")
                                or item.get("calendarDate"),
                                end_date,
                            )

                            activities_by_date[item_date].append(item)

                        for activity_date, activities in (
                            activities_by_date.items()
                        ):
                            add_record(
                                "activities",
                                activity_date,
                                {"activities": activities},
                            )

                    else:
                        for item in result:
                            if not isinstance(item, dict):
                                item = {"data": item}

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
                    # La respuesta completa se guarda como un registro.
                    add_record(record_type, end_date, result)

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
                # Permite que otras categorías se consulten aunque una falle.
                logger.exception(
                    "Error consultando %s",
                    record_type,
                )

    # 5. Validar los resultados
    if not records:
        raise RuntimeError(
            "No se obtuvieron datos de Garmin. "
            "Revisa los errores de autenticación y consulta anteriores."
        )

    logger.info("Registros recibidos: %s", len(records))

    # 6. Deduplicar por la clave única usada en Supabase.
    # Si hay varias respuestas del mismo tipo y fecha, conservar
    # el último registro generado para esa combinación.
    unique_records = {}

    for record in records:
        key = (
            record["record_type"],
            record["record_date"],
        )
        unique_records[key] = record

    records = list(unique_records.values())

    # Comprobación adicional: ningún lote debe contener dos claves iguales.
    keys = [
        (record["record_type"], record["record_date"])
        for record in records
    ]

    if len(keys) != len(set(keys)):
        raise RuntimeError(
            "Se detectaron claves duplicadas después de deduplicar."
        )

    logger.info(
        "Registros únicos listos para Supabase: %s",
        len(records),
    )

    # 7. Guardar en Supabase por lotes de 100.
    # Requiere una restricción UNIQUE en
    # (record_type, record_date).
    for offset in range(0, len(records), 100):
        batch = records[offset:offset + 100]

        response = (
            db.table("garmin_records")
            .upsert(
                batch,
                on_conflict="record_type,record_date",
            )
            .execute()
        )

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
