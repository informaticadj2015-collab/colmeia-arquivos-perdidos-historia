import json
import psycopg
from datetime import datetime, timezone

DATABASE_URL = "dbname=colmeia_db user=servidor"


def constitution_check(
    hive_id: str,
    action: str,
    actor_type: str = "SISTEMA",
    actor_id=None,
    target_type=None,
    target_id=None,
    details=None,
):
    """
    Constitution Guard — primeira camada real de governança.

    Esta função NÃO executa a ação solicitada.
    Ela apenas consulta a Constituição ativa, registra a decisão
    na auditoria e devolve o resultado para quem chamou.
    """

    decision_details = {
        "action": action,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "constitution": "1.0",
    }

    if details:
        decision_details["request"] = details

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT
                        rule_code,
                        title,
                        rule_text,
                        severity,
                        version
                    FROM constitution_rules
                    WHERE hive_id = %s
                      AND active = TRUE
                    ORDER BY rule_code
                    """,
                    (hive_id,),
                )

                rules = cur.fetchall()

                if not rules:
                    decision_details["reason"] = (
                        "Nenhuma regra constitucional ativa encontrada."
                    )

                    cur.execute(
                        """
                        INSERT INTO audit_log (
                            hive_id,
                            actor_type,
                            actor_id,
                            action,
                            target_type,
                            target_id,
                            result,
                            details
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            hive_id,
                            actor_type,
                            actor_id,
                            f"CONSTITUTION_CHECK:{action}",
                            target_type,
                            target_id,
                            "BLOCKED",
                            json.dumps(decision_details, ensure_ascii=False),
                        ),
                    )

                    conn.commit()

                    return {
                        "status": "BLOCKED",
                        "action": action,
                        "reason": "NO_ACTIVE_CONSTITUTION",
                        "rules_checked": 0,
                    }

                # Nesta primeira versão, a existência de uma Constituição
                # ativa é obrigatória. A interpretação específica de cada
                # lei será ligada às operações nas próximas etapas.
                decision_details["rules_checked"] = len(rules)
                decision_details["rule_codes"] = [row[0] for row in rules]

                cur.execute(
                    """
                    INSERT INTO audit_log (
                        hive_id,
                        actor_type,
                        actor_id,
                        action,
                        target_type,
                        target_id,
                        result,
                        details
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        hive_id,
                        actor_type,
                        actor_id,
                        f"CONSTITUTION_CHECK:{action}",
                        target_type,
                        target_id,
                        "ALLOWED",
                        json.dumps(decision_details, ensure_ascii=False),
                    ),
                )

            conn.commit()

        return {
            "status": "ALLOWED",
            "action": action,
            "rules_checked": len(rules),
            "rule_codes": [row[0] for row in rules],
        }

    except Exception as exc:
        return {
            "status": "BLOCKED",
            "action": action,
            "reason": "CONSTITUTION_GUARD_ERROR",
            "error": str(exc),
        }
