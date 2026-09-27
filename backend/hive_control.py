import json
import psycopg


def change_hive_state(
    database_url: str,
    hive_id: str,
    new_state: str,
    actor_type: str = "HUMANO",
    actor_id: str | None = None,
):
    allowed_states = {"ATIVA", "PAUSADA", "DORMINDO", "EMERGENCIA", "CONTIDA"}

    if new_state not in allowed_states:
        raise ValueError(f"Estado de Colmeia inválido: {new_state}")

    if actor_type != "HUMANO":
        raise PermissionError("Somente HUMANO pode alterar o estado da Colmeia.")

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, identifier, state::text
                FROM hive
                WHERE id = %s
                FOR UPDATE
                """,
                (hive_id,),
            )
            hive = cur.fetchone()

            if not hive:
                raise LookupError("Colmeia não encontrada.")

            db_hive_id, name, identifier, current_state = hive

            if current_state == new_state:
                return {
                    "status": "NO_CHANGE",
                    "hive_id": str(db_hive_id),
                    "identifier": identifier,
                    "previous_state": current_state,
                    "state": new_state,
                }

            cur.execute(
                """
                UPDATE hive
                SET state = %s::hive_state,
                    updated_at = now()
                WHERE id = %s
                """,
                (new_state, db_hive_id),
            )

            details = {
                "from": current_state,
                "to": new_state,
                "actor_type": actor_type,
            }

            if actor_id:
                details["actor_id"] = actor_id

            action = f"HIVE_{new_state}"

            cur.execute(
                """
                INSERT INTO audit_log
                    (hive_id, actor_type, actor_id, action,
                     target_type, target_id, result, details)
                VALUES
                    (%s, %s, %s, %s,
                     'HIVE', %s, 'SUCCESS', %s::jsonb)
                """,
                (
                    db_hive_id,
                    actor_type,
                    actor_id,
                    action,
                    db_hive_id,
                    json.dumps(details),
                ),
            )

            cur.execute(
                """
                INSERT INTO events
                    (hive_id, event_type, severity, message, data)
                VALUES
                    (%s, 'HIVE_STATE_CHANGED', 'INFO',
                     %s, %s::jsonb)
                """,
                (
                    db_hive_id,
                    f"Estado da Colmeia alterado de {current_state} para {new_state} pelo Mestre/Apicultor.",
                    json.dumps(
                        {
                            "from": current_state,
                            "to": new_state,
                            "actor_type": actor_type,
                            "actor_id": actor_id,
                            "hive_identifier": identifier,
                        }
                    ),
                ),
            )

            return {
                "status": "CHANGED",
                "hive_id": str(db_hive_id),
                "name": name,
                "identifier": identifier,
                "previous_state": current_state,
                "state": new_state,
            }
