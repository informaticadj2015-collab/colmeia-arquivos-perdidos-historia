from fastapi import FastAPI, Header
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
import httpx
from pathlib import Path
import json
import psycopg
from datetime import datetime, timezone
from backend.hive_control import change_hive_state

app = FastAPI(title="Colmeia Central")

WORKER_ID = "WORKER-001"
WORKER_URL = "http://192.168.88.113:5100/health"
WORKER_MISSION_URL = "http://192.168.88.113:5100/mission/receive"
SECRET_FILE = Path("/home/servidor/colmeia/config/worker.secret")
WORKER_SECRET = SECRET_FILE.read_text().strip()
DATABASE_URL = "dbname=colmeia_db user=servidor"


@app.get("/hive/state")
def hive_state():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, identifier, state::text, updated_at
                FROM hive
                WHERE identifier = %s
                """,
                ("COLMEIA-001",)
            )
            row = cur.fetchone()

    if not row:
        return {
            "status": "HIVE_NOT_FOUND"
        }

    hive_id, name, identifier, state, updated_at = row

    return {
        "status": "OK",
        "hive": {
            "id": str(hive_id),
            "name": name,
            "identifier": identifier,
            "state": state,
            "updated_at": updated_at.isoformat()
        }
    }


@app.post("/hive/state/{new_state}")
def set_hive_state(new_state: str):
    try:
        result = change_hive_state(
            DATABASE_URL,
            "71196fed-e0f9-4994-8bd3-13b4e9ce5343",
            new_state.upper(),
            actor_type="HUMANO",
        )
        return result
    except ValueError as e:
        return {"status": "INVALID_STATE", "detail": str(e)}
    except PermissionError as e:
        return {"status": "FORBIDDEN", "detail": str(e)}
    except LookupError as e:
        return {"status": "HIVE_NOT_FOUND", "detail": str(e)}

@app.get("/health")
def health():
    return {
        "service": "colmeia-central",
        "status": "online",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/worker/heartbeat")
def worker_heartbeat():
    checked_at = datetime.now(timezone.utc)

    try:
        response = httpx.get(WORKER_URL, headers={"X-Colmeia-Secret": WORKER_SECRET}, timeout=5.0)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return {
            "worker_id": WORKER_ID,
            "status": "OFFLINE",
            "error": str(exc),
            "checked_at": checked_at.isoformat()
        }

    if data.get("worker_id") != WORKER_ID:
        return {
            "worker_id": WORKER_ID,
            "status": "IDENTIDADE_INVALIDA",
            "checked_at": checked_at.isoformat()
        }

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agents SET status = %s, last_seen_at = %s, updated_at = %s WHERE metadata->>%s = %s",
                ("ONLINE", checked_at, checked_at, "worker_id", WORKER_ID)
            )

    return {
        "worker_id": WORKER_ID,
        "status": "ONLINE",
        "worker_status": data.get("status"),
        "checked_at": checked_at.isoformat()
    }


@app.post("/mission/authorize/{mission_id}")
def authorize_mission(mission_id: str):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        m.id,
                        m.hive_id,
                        m.agent_id,
                        m.title,
                        m.state::text
                    FROM missions m
                    WHERE m.id = %s
                    """,
                    (mission_id,)
                )
                mission = cur.fetchone()

                if not mission:
                    return {
                        "status": "MISSION_NOT_FOUND",
                        "mission_id": mission_id
                    }

                db_id, hive_id, agent_id, title, current_state = mission

                if current_state != "CRIADA":
                    return {
                        "status": "MISSION_NOT_IN_CREATED_STATE",
                        "mission_id": mission_id,
                        "state": current_state
                    }

                cur.execute(
                    """
                    SELECT state::text
                    FROM hive
                    WHERE id = %s
                    """,
                    (hive_id,)
                )
                hive = cur.fetchone()

                if not hive:
                    return {
                        "status": "HIVE_NOT_FOUND",
                        "mission_id": mission_id
                    }

                if hive[0] != "ATIVA":
                    return {
                        "status": "HIVE_NOT_ACTIVE",
                        "mission_id": mission_id,
                        "state": hive[0]
                    }

                cur.execute(
                    """
                    UPDATE missions
                    SET state = 'AUTORIZADA'::mission_state
                    WHERE id = %s
                    RETURNING id, state::text
                    """,
                    (mission_id,)
                )
                updated = cur.fetchone()

                cur.execute(
                    """
                    INSERT INTO audit_log (
                        hive_id,
                        actor_type,
                        action,
                        target_type,
                        target_id,
                        result,
                        details
                    )
                    VALUES (
                        %s,
                        'HUMANO',
                        'MISSION_AUTHORIZE',
                        'MISSION',
                        %s,
                        'ALLOWED',
                        %s::jsonb
                    )
                    """,
                    (
                        hive_id,
                        mission_id,
                        json.dumps(
                            {
                                "title": title,
                                "previous_state": current_state,
                                "new_state": "AUTORIZADA"
                            },
                            ensure_ascii=False
                        )
                    )
                )

                cur.execute(
                    """
                    INSERT INTO events (
                        hive_id,
                        agent_id,
                        mission_id,
                        event_type,
                        severity,
                        message,
                        data
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        hive_id,
                        agent_id,
                        mission_id,
                        "MISSION_AUTHORIZED",
                        "INFO",
                        "Missão autorizada pelo Mestre/Apicultor.",
                        json.dumps(
                            {
                                "previous_state": current_state,
                                "new_state": "AUTORIZADA"
                            },
                            ensure_ascii=False
                        )
                    )
                )

                conn.commit()

        return {
            "status": "MISSION_AUTHORIZED",
            "mission_id": str(updated[0]),
            "state": updated[1],
            "actor_type": "HUMANO"
        }

    except Exception as exc:
        return {
            "status": "MISSION_AUTHORIZE_FAILED",
            "mission_id": mission_id,
            "error": str(exc)
        }



@app.post("/mission/retry/{mission_id}")
def retry_mission(mission_id: str):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.id, m.hive_id, m.title, m.state::text
                    FROM missions m
                    WHERE m.id = %s
                    """,
                    (mission_id,)
                )
                row = cur.fetchone()

                if not row:
                    return {
                        "status": "MISSION_NOT_FOUND",
                        "mission_id": mission_id
                    }

                db_mission_id, hive_id, title, current_state = row

                if current_state != "FALHOU":
                    return {
                        "status": "MISSION_NOT_RETRYABLE",
                        "mission_id": mission_id,
                        "state": current_state
                    }

                cur.execute(
                    "SELECT state::text FROM hive WHERE id = %s",
                    (hive_id,)
                )
                hive_row = cur.fetchone()

                if not hive_row or hive_row[0] != "ATIVA":
                    return {
                        "status": "HIVE_NOT_ACTIVE",
                        "mission_id": mission_id,
                        "state": hive_row[0] if hive_row else None
                    }

                cur.execute(
                    """
                    UPDATE missions
                    SET state = 'AUTORIZADA',
                        started_at = NULL,
                        completed_at = NULL,
                        metadata = metadata || jsonb_build_object(
                            'retry_of', id::text,
                            'retry_authorized_at', now()::text
                        )
                    WHERE id = %s
                    """,
                    (mission_id,)
                )

                cur.execute(
                    """
                    INSERT INTO audit_log
                    (hive_id, actor_type, action, target_type, target_id, result, details)
                    VALUES (%s, 'HUMANO', 'MISSION_RETRY', 'MISSION', %s, 'ALLOWED', %s)
                    """,
                    (
                        hive_id,
                        db_mission_id,
                        json.dumps({
                            "reason": "REPROCESSAMENTO_AUTORIZADO_PELO_MESTRE"
                        }),
                    )
                )

                cur.execute(
                    """
                    INSERT INTO events
                    (hive_id, mission_id, event_type, severity, message, data)
                    VALUES (%s, %s, 'MISSION_RETRY_AUTHORIZED', 'INFO',
                            'Reprocessamento da missão autorizado pelo Mestre/Apicultor.',
                            %s)
                    """,
                    (
                        hive_id,
                        db_mission_id,
                        json.dumps({
                            "actor_type": "HUMANO",
                            "previous_state": "FALHOU",
                            "new_state": "AUTORIZADA"
                        }),
                    )
                )

            conn.commit()

        return {
            "status": "MISSION_RETRY_AUTHORIZED",
            "mission_id": mission_id,
            "state": "AUTORIZADA",
            "title": title
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/mission/dispatch/{mission_id}")
def dispatch_mission(mission_id: str):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        m.id,
                        m.title,
                        m.objective,
                        m.origin,
                        m.destination,
                        m.source,
                        m.state::text,
                        a.metadata->>'worker_id',
                        COALESCE(m.metadata->>'type', 'TESTE_INTERNO'),
                        m.metadata->>'query'
                    FROM missions m
                    LEFT JOIN agents a ON a.id = m.agent_id
                    WHERE m.id = %s
                    """,
                    (mission_id,)
                )
                row = cur.fetchone()

        if not row:
            return {
                "mission_id": mission_id,
                "status": "MISSION_NOT_FOUND"
            }

        (
            db_mission_id,
            title,
            objective,
            origin,
            destination,
            source,
            state,
            worker_id,
            mission_type,
            query
        ) = row

        if state != "AUTORIZADA":
            return {
                "mission_id": mission_id,
                "status": "MISSION_NOT_AUTHORIZED",
                "state": state
            }

        if worker_id != WORKER_ID:
            return {
                "mission_id": mission_id,
                "status": "WORKER_IDENTITY_INVALID",
                "worker_id": worker_id
            }

        payload = {
            "mission_id": str(db_mission_id),
            "title": title,
            "objective": objective,
            "origin": origin,
            "destination": destination,
            "source": source,
            "mission_type": mission_type,
            "query": query
        }

        response = httpx.post(
            WORKER_MISSION_URL,
            headers={"X-Colmeia-Secret": WORKER_SECRET},
            json=payload,
            timeout=5.0
        )
        response.raise_for_status()
        worker_result = response.json()

        return {
            "mission_id": mission_id,
            "status": "DISPATCHED",
            "worker_id": WORKER_ID,
            "worker_response": worker_result
        }

    except Exception as exc:
        return {
            "mission_id": mission_id,
            "status": "DISPATCH_FAILED",
            "error": str(exc)
        }

@app.post("/worker/callback")
def worker_callback(payload: dict, x_colmeia_secret: str | None = Header(default=None, alias="X-Colmeia-Secret")):
    if x_colmeia_secret != WORKER_SECRET:
        return {
            "status": "UNAUTHORIZED"
        }

    mission_id = payload.get("mission_id")
    state = payload.get("state")
    result = payload.get("result")
    error = payload.get("error")
    event_type = payload.get("event_type", state)
    message = payload.get("message")

    allowed_states = {
        "EM_EXECUCAO",
        "CONCLUIDA",
        "FALHOU",
        "PAUSADA",
        "RETORNANDO",
        "CANCELADA",
        "QUARENTENA",
    }

    if not mission_id:
        return {
            "status": "INVALID_PAYLOAD",
            "error": "mission_id ausente"
        }

    if state not in allowed_states:
        return {
            "status": "INVALID_STATE",
            "state": state
        }

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, hive_id, agent_id, state::text FROM missions WHERE id = %s",
                    (mission_id,)
                )
                mission = cur.fetchone()

                if not mission:
                    return {
                        "status": "MISSION_NOT_FOUND",
                        "mission_id": mission_id
                    }

                db_id, hive_id, agent_id, current_state = mission

                cur.execute(
                    """
                    UPDATE missions
                    SET state = %s,
                        started_at = CASE
                            WHEN %s = 'EM_EXECUCAO' AND started_at IS NULL
                            THEN now()
                            ELSE started_at
                        END,
                        completed_at = CASE
                            WHEN %s IN ('CONCLUIDA', 'FALHOU', 'CANCELADA')
                            THEN now()
                            ELSE completed_at
                        END
                    WHERE id = %s
                    """,
                    (state, state, state, mission_id)
                )

                cur.execute(
                    """
                    INSERT INTO events (
                        hive_id,
                        agent_id,
                        mission_id,
                        event_type,
                        severity,
                        message,
                        data
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        hive_id,
                        agent_id,
                        mission_id,
                        event_type or state,
                        "ERROR" if state == "FALHOU" else "INFO",
                        message or f"Worker informou estado {state}.",
                        json.dumps(
                            result if isinstance(result, dict) else {
                                "result": result,
                                "error": error
                            },
                            ensure_ascii=False
                        )
                    )
                )

            conn.commit()

        return {
            "status": "ACCEPTED",
            "mission_id": mission_id,
            "previous_state": current_state,
            "state": state
        }

    except Exception as exc:
        return {
            "status": "CALLBACK_FAILED",
            "mission_id": mission_id,
            "error": str(exc)
        }


@app.post("/mission/create")
def create_mission(payload: dict):
    title = str(payload.get("title") or "").strip()
    objective = str(payload.get("objective") or "").strip()

    if not title:
        return {
            "status": "INVALID_PAYLOAD",
            "error": "title é obrigatório"
        }

    if not objective:
        return {
            "status": "INVALID_PAYLOAD",
            "error": "objective é obrigatório"
        }

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, state::text
                    FROM hive
                    WHERE identifier = %s
                    LIMIT 1
                    """,
                    ("COLMEIA-001",)
                )
                hive = cur.fetchone()

                if not hive:
                    return {
                        "status": "HIVE_NOT_FOUND",
                        "identifier": "COLMEIA-001"
                    }

                hive_id, hive_state = hive

                if hive_state != "ATIVA":
                    return {
                        "status": "HIVE_NOT_ACTIVE",
                        "state": hive_state
                    }

                cur.execute(
                    """
                    SELECT id
                    FROM agents
                    WHERE hive_id = %s
                      AND metadata->>'worker_id' = %s
                      AND role::text = 'BATEDORA'
                      AND lifecycle::text = 'ATIVA'
                    LIMIT 1
                    """,
                    (hive_id, WORKER_ID)
                )
                agent = cur.fetchone()

                if not agent:
                    return {
                        "status": "AGENT_NOT_AVAILABLE",
                        "worker_id": WORKER_ID
                    }

                agent_id = agent[0]

                metadata = {
                    "type": "PESQUISA_HISTORICA"
                }

                if payload.get("query"):
                    metadata["query"] = str(payload["query"]).strip()

                cur.execute(
                    """
                    INSERT INTO missions (
                        hive_id,
                        agent_id,
                        title,
                        objective,
                        state,
                        metadata
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s,
                        'CRIADA'::mission_state,
                        %s::jsonb
                    )
                    RETURNING id, state::text, created_at
                    """,
                    (
                        hive_id,
                        agent_id,
                        title,
                        objective,
                        json.dumps(metadata, ensure_ascii=False)
                    )
                )

                mission_id, state, created_at = cur.fetchone()
                conn.commit()

        return {
            "status": "MISSION_CREATED",
            "mission_id": str(mission_id),
            "state": state,
            "title": title,
            "objective": objective,
            "created_at": created_at.isoformat() if created_at else None
        }

    except Exception as exc:
        return {
            "status": "MISSION_CREATE_FAILED",
            "error": str(exc)
        }


# ============================================================
# API REAL DO PAINEL — CONSULTAS
# ============================================================

@app.get("/panel/overview")
def panel_overview():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        name,
                        identifier,
                        state::text,
                        created_at,
                        updated_at
                    FROM hive
                    ORDER BY created_at
                    LIMIT 1
                    """
                )
                hive = cur.fetchone()

                cur.execute(
                    """
                    SELECT
                        id,
                        name,
                        role::text,
                        lifecycle::text,
                        status,
                        metadata,
                        last_seen_at
                    FROM agents
                    WHERE metadata->>'worker_id' = %s
                    LIMIT 1
                    """,
                    (WORKER_ID,)
                )
                worker = cur.fetchone()

                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE state = 'CONCLUIDA') AS concluidas,
                        COUNT(*) FILTER (WHERE state = 'EM_EXECUCAO') AS em_execucao,
                        COUNT(*) FILTER (WHERE state = 'AUTORIZADA') AS autorizadas,
                        COUNT(*) FILTER (WHERE state = 'FALHOU') AS falhou,
                        COUNT(*) AS total
                    FROM missions
                    """
                )
                mission_counts = cur.fetchone()

        return {
            "hive": {
                "id": str(hive[0]) if hive else None,
                "name": hive[1] if hive else None,
                "identifier": hive[2] if hive else None,
                "state": hive[3] if hive else None,
                "created_at": hive[4].isoformat() if hive and hive[4] else None,
                "updated_at": hive[5].isoformat() if hive and hive[5] else None,
            },
            "worker": {
                "id": str(worker[0]) if worker else None,
                "name": worker[1] if worker else None,
                "role": worker[2] if worker else None,
                "lifecycle": worker[3] if worker else None,
                "status": worker[4] if worker else None,
                "metadata": worker[5] if worker else None,
                "last_seen_at": worker[6].isoformat() if worker and worker[6] else None,
            },
            "missions": {
                "concluidas": mission_counts[0],
                "em_execucao": mission_counts[1],
                "autorizadas": mission_counts[2],
                "falhou": mission_counts[3],
                "total": mission_counts[4],
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        return {
            "status": "PANEL_OVERVIEW_FAILED",
            "error": str(exc)
        }



@app.get("/panel/agentes")
def panel_agentes():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        hive_id,
                        name,
                        role::text,
                        lifecycle::text,
                        status,
                        capabilities,
                        metadata,
                        last_seen_at,
                        created_at,
                        updated_at
                    FROM agents
                    WHERE hive_id = (
                        SELECT id
                        FROM hive
                        WHERE identifier = 'COLMEIA-001'
                        LIMIT 1
                    )
                    ORDER BY created_at
                    """
                )
                rows = cur.fetchall()

        return {
            "status": "OK",
            "count": len(rows),
            "agentes": [
                {
                    "id": str(row[0]),
                    "hive_id": str(row[1]),
                    "name": row[2],
                    "role": row[3],
                    "lifecycle": row[4],
                    "status": row[5],
                    "capabilities": row[6],
                    "metadata": row[7],
                    "last_seen_at": row[8].isoformat() if row[8] else None,
                    "created_at": row[9].isoformat() if row[9] else None,
                    "updated_at": row[10].isoformat() if row[10] else None,
                }
                for row in rows
            ],
        }

    except Exception as exc:
        return {
            "status": "PANEL_AGENTES_FAILED",
            "error": str(exc),
        }

@app.get("/panel/quarentena")
def panel_quarentena():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    q.id,
                    q.hive_id,
                    q.agent_id,
                    q.mission_id,
                    q.reason,
                    q.evidence,
                    q.released,
                    q.created_at,
                    q.released_at
                FROM quarantine q
                JOIN hive h ON h.id = q.hive_id
                WHERE h.identifier = %s
                ORDER BY q.created_at DESC
                LIMIT 200
            """, ("COLMEIA-001",))

            rows = cur.fetchall()

            return {
                "status": "OK",
                "count": len(rows),
                "quarentena": [
                    {
                        "id": str(r[0]),
                        "hive_id": str(r[1]),
                        "agent_id": str(r[2]) if r[2] else None,
                        "mission_id": str(r[3]) if r[3] else None,
                        "reason": r[4],
                        "evidence": r[5],
                        "released": r[6],
                        "created_at": r[7].isoformat() if r[7] else None,
                        "released_at": r[8].isoformat() if r[8] else None
                    }
                    for r in rows
                ]
            }

@app.get("/panel/eventos")
def panel_eventos():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        hive_id,
                        agent_id,
                        mission_id,
                        event_type,
                        severity,
                        message,
                        data,
                        created_at
                    FROM events
                    WHERE hive_id = (
                        SELECT id
                        FROM hive
                        WHERE identifier = 'COLMEIA-001'
                        LIMIT 1
                    )
                    ORDER BY created_at DESC
                    LIMIT 200
                    """
                )
                rows = cur.fetchall()

        return {
            "status": "OK",
            "count": len(rows),
            "eventos": [
                {
                    "id": row[0],
                    "hive_id": str(row[1]),
                    "agent_id": str(row[2]) if row[2] else None,
                    "mission_id": str(row[3]) if row[3] else None,
                    "event_type": row[4],
                    "severity": row[5],
                    "message": row[6],
                    "data": row[7],
                    "created_at": row[8].isoformat(),
                }
                for row in rows
            ],
        }

    except Exception as exc:
        return {
            "status": "PANEL_EVENTOS_FAILED",
            "error": str(exc),
        }

@app.get("/panel/arquivos")
def panel_arquivos():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        hive_id,
                        mission_id,
                        agent_id,
                        title,
                        description,
                        evidence::text,
                        rights::text,
                        source_url,
                        source_name,
                        document_date,
                        content_hash,
                        quarantined,
                        created_at,
                        license_name,
                        usage_terms,
                        license_url,
                        attribution_required,
                        author,
                        media_url
                    FROM discoveries
                    WHERE hive_id = (
                        SELECT id
                        FROM hive
                        WHERE identifier = 'COLMEIA-001'
                        LIMIT 1
                    )
                    ORDER BY created_at DESC
                    """
                )
                rows = cur.fetchall()

        return {
            "status": "OK",
            "count": len(rows),
            "arquivos": [
                {
                    "id": str(row[0]),
                    "hive_id": str(row[1]),
                    "mission_id": str(row[2]) if row[2] else None,
                    "agent_id": str(row[3]) if row[3] else None,
                    "title": row[4],
                    "description": row[5],
                    "evidence": row[6],
                    "rights": row[7],
                    "source_url": row[8],
                    "source_name": row[9],
                    "document_date": row[10].isoformat() if row[10] else None,
                    "content_hash": row[11],
                    "quarantined": row[12],
                    "created_at": row[13].isoformat() if row[13] else None,
                    "license_name": row[14],
                    "usage_terms": row[15],
                    "license_url": row[16],
                    "attribution_required": row[17],
                    "author": row[18],
                    "media_url": row[19],
                }
                for row in rows
            ],
        }

    except Exception as exc:
        return {
            "status": "PANEL_ARQUIVOS_FAILED",
            "error": str(exc),
        }

@app.get("/panel/batedores")
def panel_batedores():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        hive_id,
                        name,
                        role::text,
                        lifecycle::text,
                        status,
                        capabilities,
                        metadata,
                        last_seen_at,
                        created_at,
                        updated_at
                    FROM agents
                    WHERE hive_id = (
                        SELECT id
                        FROM hive
                        WHERE identifier = 'COLMEIA-001'
                        LIMIT 1
                    )
                    AND role::text = 'BATEDORA'
                    ORDER BY created_at
                    """
                )
                rows = cur.fetchall()

        return {
            "status": "OK",
            "count": len(rows),
            "batedores": [
                {
                    "id": str(row[0]),
                    "hive_id": str(row[1]),
                    "name": row[2],
                    "role": row[3],
                    "lifecycle": row[4],
                    "status": row[5],
                    "capabilities": row[6],
                    "metadata": row[7],
                    "last_seen_at": row[8].isoformat() if row[8] else None,
                    "created_at": row[9].isoformat() if row[9] else None,
                    "updated_at": row[10].isoformat() if row[10] else None,
                }
                for row in rows
            ],
        }

    except Exception as exc:
        return {
            "status": "PANEL_BATEDORES_FAILED",
            "error": str(exc),
        }


@app.get("/panel/auditoria")
def panel_auditoria():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    a.id,
                    a.hive_id,
                    a.actor_type,
                    a.actor_id,
                    a.action,
                    a.target_type,
                    a.target_id,
                    a.result,
                    a.details,
                    a.created_at
                FROM audit_log a
                JOIN hive h ON h.id = a.hive_id
                WHERE h.identifier = %s
                ORDER BY a.created_at DESC
                LIMIT 200
            """, ("COLMEIA-001",))
            rows = cur.fetchall()

            return {
                "status": "OK",
                "count": len(rows),
                "auditoria": [
                    {
                        "id": r[0],
                        "hive_id": str(r[1]) if r[1] else None,
                        "actor_type": r[2],
                        "actor_id": str(r[3]) if r[3] else None,
                        "action": r[4],
                        "target_type": r[5],
                        "target_id": str(r[6]) if r[6] else None,
                        "result": r[7],
                        "details": r[8],
                        "created_at": r[9].isoformat() if r[9] else None
                    }
                    for r in rows
                ]
            }


@app.get("/panel/auditoria-ui")
def panel_auditoria_ui():
    return FileResponse("/home/servidor/colmeia/panel/auditoria.html")

@app.get("/panel/quarentena-ui")
def panel_quarentena_ui():
    return FileResponse("/home/servidor/colmeia/panel/quarentena.html")

@app.get("/panel/eventos-ui")
def panel_eventos_ui():
    return FileResponse("/home/servidor/colmeia/panel/eventos.html")

@app.get("/panel/arquivos-ui")
def panel_arquivos_ui():
    return FileResponse("/home/servidor/colmeia/panel/arquivos.html")

@app.get("/panel/batedores-ui")
def panel_batedores_ui():
    return FileResponse("/home/servidor/colmeia/panel/batedores.html")

@app.get("/panel/agentes-ui")
def panel_agentes_ui():
    return FileResponse("/home/servidor/colmeia/panel/agentes.html")

@app.get("/panel/worker")
def panel_worker():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        name,
                        role::text,
                        lifecycle::text,
                        status,
                        capabilities,
                        metadata,
                        last_seen_at
                    FROM agents
                    WHERE metadata->>'worker_id' = %s
                    LIMIT 1
                    """,
                    (WORKER_ID,)
                )
                row = cur.fetchone()

        if not row:
            return {
                "status": "WORKER_NOT_FOUND",
                "worker_id": WORKER_ID
            }

        return {
            "id": str(row[0]),
            "name": row[1],
            "role": row[2],
            "lifecycle": row[3],
            "status": row[4],
            "capabilities": row[5],
            "metadata": row[6],
            "last_seen_at": row[7].isoformat() if row[7] else None,
        }

    except Exception as exc:
        return {
            "status": "PANEL_WORKER_FAILED",
            "error": str(exc)
        }


@app.get("/panel/missions")
def panel_missions():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        m.id,
                        m.title,
                        m.objective,
                        m.state::text,
                        m.origin,
                        m.destination,
                        m.source,
                        m.started_at,
                        m.completed_at,
                        m.created_at,
                        a.name,
                        a.metadata->>'worker_id'
                    FROM missions m
                    LEFT JOIN agents a ON a.id = m.agent_id
                    ORDER BY m.created_at DESC
                    """
                )
                rows = cur.fetchall()

        return {
            "missions": [
                {
                    "id": str(row[0]),
                    "title": row[1],
                    "objective": row[2],
                    "state": row[3],
                    "origin": row[4],
                    "destination": row[5],
                    "source": row[6],
                    "started_at": row[7].isoformat() if row[7] else None,
                    "completed_at": row[8].isoformat() if row[8] else None,
                    "created_at": row[9].isoformat() if row[9] else None,
                    "worker_name": row[10],
                    "worker_id": row[11],
                }
                for row in rows
            ],
            "count": len(rows),
        }

    except Exception as exc:
        return {
            "status": "PANEL_MISSIONS_FAILED",
            "error": str(exc)
        }


@app.get("/panel/missions/{mission_id}")
def panel_mission_detail(mission_id: str):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        m.id,
                        m.title,
                        m.objective,
                        m.state::text,
                        m.origin,
                        m.destination,
                        m.source,
                        m.started_at,
                        m.completed_at,
                        m.created_at,
                        m.metadata,
                        a.id,
                        a.name,
                        a.role::text,
                        a.metadata->>'worker_id'
                    FROM missions m
                    LEFT JOIN agents a ON a.id = m.agent_id
                    WHERE m.id = %s
                    """,
                    (mission_id,)
                )
                mission = cur.fetchone()

                if not mission:
                    return {
                        "status": "MISSION_NOT_FOUND",
                        "mission_id": mission_id
                    }

                cur.execute(
                    """
                    SELECT
                        id,
                        event_type,
                        severity,
                        message,
                        data,
                        created_at
                    FROM events
                    WHERE mission_id = %s
                    ORDER BY id
                    """,
                    (mission_id,)
                )
                events = cur.fetchall()

        return {
            "id": str(mission[0]),
            "title": mission[1],
            "objective": mission[2],
            "state": mission[3],
            "origin": mission[4],
            "destination": mission[5],
            "source": mission[6],
            "started_at": mission[7].isoformat() if mission[7] else None,
            "completed_at": mission[8].isoformat() if mission[8] else None,
            "created_at": mission[9].isoformat() if mission[9] else None,
            "metadata": mission[10],
            "worker": {
                "id": str(mission[11]) if mission[11] else None,
                "name": mission[12],
                "role": mission[13],
                "worker_id": mission[14],
            },
            "events": [
                {
                    "id": event[0],
                    "event_type": event[1],
                    "severity": event[2],
                    "message": event[3],
                    "data": event[4],
                    "created_at": event[5].isoformat() if event[5] else None,
                }
                for event in events
            ],
        }

    except Exception as exc:
        return {
            "status": "PANEL_MISSION_DETAIL_FAILED",
            "mission_id": mission_id,
            "error": str(exc)
        }


@app.get("/panel/events")
def panel_events():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        e.id,
                        e.hive_id,
                        e.agent_id,
                        e.mission_id,
                        e.event_type,
                        e.severity,
                        e.message,
                        e.data,
                        e.created_at
                    FROM events e
                    ORDER BY e.id DESC
                    LIMIT 100
                    """
                )
                rows = cur.fetchall()

        return {
            "events": [
                {
                    "id": row[0],
                    "hive_id": str(row[1]) if row[1] else None,
                    "agent_id": str(row[2]) if row[2] else None,
                    "mission_id": str(row[3]) if row[3] else None,
                    "event_type": row[4],
                    "severity": row[5],
                    "message": row[6],
                    "data": row[7],
                    "created_at": row[8].isoformat() if row[8] else None,
                }
                for row in rows
            ],
            "count": len(rows),
        }

    except Exception as exc:
        return {
            "status": "PANEL_EVENTS_FAILED",
            "error": str(exc)
        }

# ============================================================
# STATUS OPERACIONAL REAL DO WORKER
# ============================================================

@app.get("/panel/worker/live")
def panel_worker_live():
    checked_at = datetime.now(timezone.utc)

    try:
        response = httpx.get(
            WORKER_URL,
            headers={"X-Colmeia-Secret": WORKER_SECRET},
            timeout=5.0
        )
        response.raise_for_status()
        data = response.json()

        if data.get("worker_id") != WORKER_ID:
            return {
                "status": "IDENTIDADE_INVALIDA",
                "worker_id": WORKER_ID,
                "checked_at": checked_at.isoformat()
            }

        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE agents
                    SET status = %s,
                        last_seen_at = %s,
                        updated_at = %s
                    WHERE metadata->>'worker_id' = %s
                    """,
                    ("ONLINE", checked_at, checked_at, WORKER_ID)
                )

        return {
            "worker_id": WORKER_ID,
            "status": "ONLINE",
            "worker_status": data.get("status"),
            "name": data.get("name"),
            "hostname": data.get("hostname"),
            "ip": data.get("ip"),
            "role": data.get("role"),
            "checked_at": checked_at.isoformat()
        }

    except Exception as exc:
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE agents
                        SET status = %s,
                            updated_at = %s
                        WHERE metadata->>'worker_id' = %s
                        """,
                        ("OFFLINE", checked_at, WORKER_ID)
                    )
        except Exception:
            pass

        return {
            "worker_id": WORKER_ID,
            "status": "OFFLINE",
            "checked_at": checked_at.isoformat(),
            "error": str(exc)
        }




@app.get("/panel/globe")
def panel_globe():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    d.id,
                    d.hive_id,
                    d.mission_id,
                    d.agent_id,
                    d.title,
                    d.description,
                    d.evidence,
                    d.rights,
                    d.source_url,
                    d.source_name,
                    d.document_date,
                    d.license_name,
                    d.license_url,
                    d.attribution_required,
                    d.author,
                    d.media_url,
                    d.latitude,
                    d.longitude,
                    d.created_at
                FROM discoveries d
                WHERE d.latitude IS NOT NULL
                  AND d.longitude IS NOT NULL
                ORDER BY d.created_at DESC
                LIMIT 1000
                """
            )
            rows = cur.fetchall()

    items = []
    for row in rows:
        (
            discovery_id,
            hive_id,
            mission_id,
            agent_id,
            title,
            description,
            evidence,
            rights,
            source_url,
            source_name,
            document_date,
            license_name,
            license_url,
            attribution_required,
            author,
            media_url,
            latitude,
            longitude,
            created_at,
        ) = row

        items.append(
            {
                "id": str(discovery_id),
                "hive_id": str(hive_id),
                "mission_id": str(mission_id) if mission_id else None,
                "agent_id": str(agent_id) if agent_id else None,
                "title": title,
                "description": description,
                "evidence": evidence,
                "rights": rights,
                "source_url": source_url,
                "source_name": source_name,
                "document_date": document_date.isoformat() if document_date else None,
                "license_name": license_name,
                "license_url": license_url,
                "attribution_required": attribution_required,
                "author": author,
                "media_url": media_url,
                "latitude": latitude,
                "longitude": longitude,
                "created_at": created_at.isoformat() if created_at else None,
            }
        )

    return {
        "status": "OK",
        "total": len(items),
        "items": items,
    }


@app.post("/worker/discovery")
def worker_discovery(
    payload: dict,
    x_colmeia_secret: str | None = Header(default=None, alias="X-Colmeia-Secret")
):
    if x_colmeia_secret != WORKER_SECRET:
        return {
            "status": "UNAUTHORIZED"
        }

    mission_id = payload.get("mission_id")
    agent_id = payload.get("agent_id")
    title = payload.get("title")
    description = payload.get("description")
    evidence = payload.get("evidence", "DESCONHECIDO")
    rights = payload.get("rights", "DESCONHECIDO")
    source_url = payload.get("source_url")
    source_name = payload.get("source_name")
    document_date = payload.get("document_date")
    content_hash = payload.get("content_hash")
    quarantined = bool(payload.get("quarantined", False))
    license_name = payload.get("license_name")
    usage_terms = payload.get("usage_terms")
    license_url = payload.get("license_url")
    attribution_required = payload.get("attribution_required")
    author = payload.get("author")
    media_url = payload.get("media_url")
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")

    try:
        latitude = float(latitude) if latitude is not None else None
        longitude = float(longitude) if longitude is not None else None
    except (TypeError, ValueError):
        return {
            "status": "INVALID_COORDINATES",
            "error": "latitude e longitude devem ser numéricas"
        }

    if latitude is not None and not -90 <= latitude <= 90:
        return {
            "status": "INVALID_COORDINATES",
            "error": "latitude fora do intervalo válido"
        }

    if longitude is not None and not -180 <= longitude <= 180:
        return {
            "status": "INVALID_COORDINATES",
            "error": "longitude fora do intervalo válido"
        }

    if not mission_id or not title:
        return {
            "status": "INVALID_PAYLOAD",
            "error": "mission_id e title são obrigatórios"
        }

    allowed_evidence = {
        "CONFIRMADO",
        "BEM_SUSTENTADO",
        "POSSIVEL",
        "HIPOTESE",
        "CONTRADITO",
        "DESCONHECIDO",
    }

    allowed_rights = {
        "DOMINIO_PUBLICO",
        "AUTORIZADO",
        "LICENCA_CONDICIONAL",
        "DIREITO_AUTORAL",
        "RESTRITO",
        "DESCONHECIDO",
    }

    if evidence not in allowed_evidence:
        return {
            "status": "INVALID_EVIDENCE",
            "evidence": evidence
        }

    if rights not in allowed_rights:
        return {
            "status": "INVALID_RIGHTS",
            "rights": rights
        }

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, hive_id, agent_id
                    FROM missions
                    WHERE id = %s
                    """,
                    (mission_id,)
                )
                mission = cur.fetchone()

                if not mission:
                    return {
                        "status": "MISSION_NOT_FOUND",
                        "mission_id": mission_id
                    }

                db_mission_id, hive_id, mission_agent_id = mission

                if agent_id and str(mission_agent_id) != str(agent_id):
                    return {
                        "status": "AGENT_MISMATCH",
                        "mission_id": mission_id,
                        "agent_id": agent_id
                    }

                effective_agent_id = agent_id or mission_agent_id

                cur.execute(
                    """
                    INSERT INTO discoveries (
                        hive_id,
                        mission_id,
                        agent_id,
                        title,
                        description,
                        evidence,
                        rights,
                        source_url,
                        source_name,
                        document_date,
                        content_hash,
                        quarantined,
                        license_name,
                        usage_terms,
                        license_url,
                        attribution_required,
                        author,
                        media_url,
                        latitude,
                        longitude
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s::evidence_state,
                        %s::rights_state, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s
                    )
                    RETURNING id, created_at
                    """,
                    (
                        hive_id,
                        db_mission_id,
                        effective_agent_id,
                        title,
                        description,
                        evidence,
                        rights,
                        source_url,
                        source_name,
                        document_date,
                        content_hash,
                        quarantined,
                        license_name,
                        usage_terms,
                        license_url,
                        attribution_required,
                        author,
                        media_url,
                        latitude,
                        longitude,
                    )
                )

                discovery_id, created_at = cur.fetchone()

                cur.execute(
                    """
                    INSERT INTO events (
                        hive_id,
                        agent_id,
                        mission_id,
                        event_type,
                        severity,
                        message,
                        data
                    )
                    VALUES (
                        %s, %s, %s,
                        'DISCOVERY_RECEIVED',
                        'INFO',
                        %s,
                        %s::jsonb
                    )
                    """,
                    (
                        hive_id,
                        effective_agent_id,
                        db_mission_id,
                        "Nova descoberta recebida do Batedor.",
                        json.dumps(
                            {
                                "discovery_id": str(discovery_id),
                                "title": title,
                                "evidence": evidence,
                                "rights": rights,
                                "source_url": source_url,
                                "source_name": source_name,
                                "quarantined": quarantined,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )

            conn.commit()

        return {
            "status": "DISCOVERY_ACCEPTED",
            "discovery_id": str(discovery_id),
            "mission_id": str(db_mission_id),
            "agent_id": str(effective_agent_id) if effective_agent_id else None,
            "created_at": created_at.isoformat(),
        }

    except Exception as exc:
        return {
            "status": "DISCOVERY_FAILED",
            "mission_id": mission_id,
            "error": str(exc)
        }




# ============================================================
# BIBLIOTECÁRIA — FILA REAL DE DESCOBERTAS PENDENTES
# ============================================================
@app.get("/favo/pending")
def favo_pending_discoveries():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        d.id,
                        d.hive_id,
                        d.mission_id,
                        d.agent_id,
                        d.title,
                        d.description,
                        d.evidence::text,
                        d.rights::text,
                        d.source_url,
                        d.source_name,
                        d.document_date,
                        d.license_name,
                        d.license_url,
                        d.attribution_required,
                        d.author,
                        d.media_url,
                        d.latitude,
                        d.longitude,
                        d.quarantined,
                        d.created_at
                    FROM public.discoveries d
                    LEFT JOIN public.favo_items f
                        ON f.discovery_id = d.id
                    WHERE f.id IS NULL
                    ORDER BY d.created_at ASC
                    LIMIT 500
                    """
                )

                rows = cur.fetchall()

        items = []
        for row in rows:
            (
                discovery_id,
                hive_id,
                mission_id,
                agent_id,
                title,
                description,
                evidence,
                rights,
                source_url,
                source_name,
                document_date,
                license_name,
                license_url,
                attribution_required,
                author,
                media_url,
                latitude,
                longitude,
                quarantined,
                created_at,
            ) = row

            items.append(
                {
                    "discovery_id": str(discovery_id),
                    "hive_id": str(hive_id),
                    "mission_id": str(mission_id) if mission_id else None,
                    "agent_id": str(agent_id) if agent_id else None,
                    "title": title,
                    "description": description,
                    "evidence": evidence,
                    "rights": rights,
                    "source_url": source_url,
                    "source_name": source_name,
                    "document_date": document_date.isoformat() if document_date else None,
                    "license_name": license_name,
                    "license_url": license_url,
                    "attribution_required": bool(attribution_required),
                    "author": author,
                    "media_url": media_url,
                    "latitude": latitude,
                    "longitude": longitude,
                    "quarantined": bool(quarantined),
                    "created_at": created_at.isoformat() if created_at else None,
                }
            )

        return {
            "status": "OK",
            "total": len(items),
            "items": items,
        }

    except Exception as exc:
        return {
            "status": "FAVO_PENDING_FAILED",
            "total": 0,
            "items": [],
            "error": str(exc),
        }

# ============================================================
# BIBLIOTECÁRIA — CATALOGAÇÃO REAL NO FAVO
# ============================================================
@app.post("/favo/catalog/{discovery_id}")
def catalog_favo_item(discovery_id: str):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        d.id,
                        d.hive_id,
                        d.mission_id,
                        d.agent_id,
                        d.title,
                        d.evidence::text,
                        d.rights::text,
                        d.source_url,
                        d.source_name,
                        d.content_hash,
                        d.quarantined
                    FROM public.discoveries d
                    WHERE d.id = %s
                    FOR UPDATE
                    """,
                    (discovery_id,)
                )
                discovery = cur.fetchone()

                if not discovery:
                    return {
                        "status": "DISCOVERY_NOT_FOUND",
                        "discovery_id": discovery_id,
                    }

                (
                    db_discovery_id,
                    hive_id,
                    mission_id,
                    agent_id,
                    title,
                    evidence,
                    rights,
                    source_url,
                    source_name,
                    content_hash,
                    quarantined,
                ) = discovery

                cur.execute(
                    """
                    SELECT state::text
                    FROM public.hive
                    WHERE id = %s
                    """,
                    (hive_id,)
                )
                hive = cur.fetchone()

                if not hive:
                    return {
                        "status": "HIVE_NOT_FOUND",
                        "discovery_id": discovery_id,
                    }

                if hive[0] != "ATIVA":
                    return {
                        "status": "HIVE_NOT_ACTIVE",
                        "discovery_id": discovery_id,
                        "state": hive[0],
                    }

                if quarantined:
                    return {
                        "status": "DISCOVERY_QUARANTINED",
                        "discovery_id": discovery_id,
                    }

                cur.execute(
                    """
                    SELECT
                        f.id,
                        f.discovery_id,
                        f.duplicate_role
                    FROM public.favo_items f
                    WHERE f.discovery_id = %s
                    """,
                    (db_discovery_id,)
                )
                existing = cur.fetchone()

                if existing:
                    return {
                        "status": "ALREADY_CATALOGUED",
                        "discovery_id": str(db_discovery_id),
                        "favo_id": str(existing[0]),
                        "duplicate_role": existing[2],
                    }

                duplicate_favo = None

                if content_hash:
                    cur.execute(
                        """
                        SELECT
                            f.id,
                            f.discovery_id,
                            f.duplicate_role
                        FROM public.favo_items f
                        JOIN public.discoveries d2 ON d2.id = f.discovery_id
                        WHERE d2.content_hash = %s
                        ORDER BY f.created_at
                        LIMIT 1
                        """,
                        (content_hash,)
                    )
                    duplicate_favo = cur.fetchone()

                if not duplicate_favo and source_url:
                    cur.execute(
                        """
                        SELECT
                            f.id,
                            f.discovery_id,
                            f.duplicate_role
                        FROM public.favo_items f
                        JOIN public.discoveries d2 ON d2.id = f.discovery_id
                        WHERE d2.source_url = %s
                        ORDER BY f.created_at
                        LIMIT 1
                        """,
                        (source_url,)
                    )
                    duplicate_favo = cur.fetchone()

                if duplicate_favo:
                    duplicate_role = "DUPLICADO_CONFIRMADO"
                    duplicate_state = "DUPLICATA_CONFIRMADA"
                    duplicate_of = duplicate_favo[0]
                    classification = "NAO_CLASSIFICADO"
                    message = "Descoberta catalogada no Favo como duplicata confirmada."
                else:
                    duplicate_role = "UNICO"
                    duplicate_state = "NAO_DUPLICADO"
                    duplicate_of = None
                    classification = "NAO_CLASSIFICADO"
                    message = "Descoberta catalogada no Favo como item único."

                cur.execute(
                    """
                    INSERT INTO public.favo_items (
                        hive_id,
                        discovery_id,
                        collection_key,
                        classification,
                        organization_state,
                        duplicate_state,
                        provenance_state,
                        evidence_state,
                        notes,
                        duplicate_role,
                        duplicate_of
                    )
                    VALUES (
                        %s,
                        %s,
                        'GERAL',
                        %s,
                        'RECEBIDO',
                        %s,
                        'REGISTRADA',
                        'HERDADA_DA_DESCoberta',
                        %s,
                        %s,
                        %s
                    )
                    RETURNING id
                    """,
                    (
                        hive_id,
                        db_discovery_id,
                        classification,
                        duplicate_state,
                        (
                            "Catalogação pela Bibliotecária. "
                            f"Fonte: {source_name or 'NÃO INFORMADA'}."
                        ),
                        duplicate_role,
                        duplicate_of,
                    )
                )
                favo_id = cur.fetchone()[0]

                cur.execute(
                    """
                    INSERT INTO audit_log (
                        hive_id,
                        actor_type,
                        action,
                        target_type,
                        target_id,
                        result,
                        details
                    )
                    VALUES (
                        %s,
                        'HUMANO',
                        'FAVO_CATALOG',
                        'FAVO_ITEM',
                        %s,
                        'ALLOWED',
                        %s::jsonb
                    )
                    """,
                    (
                        hive_id,
                        favo_id,
                        json.dumps(
                            {
                                "discovery_id": str(db_discovery_id),
                                "title": title,
                                "duplicate_role": duplicate_role,
                                "duplicate_of": str(duplicate_of) if duplicate_of else None,
                                "content_hash_match": bool(content_hash and duplicate_favo),
                                "source_url_match": bool(
                                    source_url
                                    and duplicate_favo
                                    and not (content_hash and duplicate_favo)
                                ),
                                "rights": rights,
                                "evidence": evidence,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )

                cur.execute(
                    """
                    INSERT INTO events (
                        hive_id,
                        agent_id,
                        mission_id,
                        event_type,
                        severity,
                        message,
                        data
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        hive_id,
                        agent_id,
                        mission_id,
                        "FAVO_CATALOGADO",
                        "INFO",
                        message,
                        json.dumps(
                            {
                                "favo_id": str(favo_id),
                                "discovery_id": str(db_discovery_id),
                                "duplicate_role": duplicate_role,
                                "duplicate_of": str(duplicate_of) if duplicate_of else None,
                                "source_name": source_name,
                                "rights": rights,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )

                conn.commit()

        return {
            "status": "FAVO_CATALOGUED",
            "favo_id": str(favo_id),
            "discovery_id": str(db_discovery_id),
            "duplicate_role": duplicate_role,
            "duplicate_of": str(duplicate_of) if duplicate_of else None,
        }

    except Exception as exc:
        return {
            "status": "FAVO_CATALOG_FAILED",
            "discovery_id": discovery_id,
            "error": str(exc),
        }


# ============================================================
# INTERFACE VISUAL REAL DA CENTRAL DE COMANDO
# ============================================================
@app.get("/panel/favo")
def panel_favo():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    f.id,
                    f.collection_key,
                    f.classification,
                    f.organization_state,
                    f.duplicate_state,
                    f.duplicate_role,
                    f.duplicate_of,
                    f.provenance_state,
                    f.notes,
                    f.created_at,
                    f.updated_at,
                    d.id,
                    d.title,
                    d.description,
                    d.evidence,
                    d.rights,
                    d.source_url,
                    d.source_name,
                    d.document_date,
                    d.license_name,
                    d.license_url,
                    d.attribution_required,
                    d.author,
                    d.media_url,
                    d.latitude,
                    d.longitude
                FROM public.favo_items f
                JOIN public.discoveries d ON d.id = f.discovery_id
                ORDER BY f.created_at DESC
                LIMIT 1000
                """
            )
            rows = cur.fetchall()

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE f.duplicate_role = 'UNICO') AS unicos,
                    COUNT(*) FILTER (WHERE f.duplicate_role = 'PRINCIPAL') AS principais,
                    COUNT(*) FILTER (WHERE f.duplicate_role = 'DUPLICADO_CONFIRMADO') AS duplicados,
                    COUNT(*) FILTER (WHERE d.rights = 'DOMINIO_PUBLICO') AS dominio_publico,
                    COUNT(*) FILTER (WHERE d.rights = 'LICENCA_CONDICIONAL') AS licenca_condicional,
                    COUNT(*) FILTER (WHERE d.rights = 'DESCONHECIDO') AS desconhecido
                FROM public.favo_items f
                JOIN public.discoveries d ON d.id = f.discovery_id
                """
            )
            summary_row = cur.fetchone()

    (
        total,
        unicos,
        principais,
        duplicados,
        dominio_publico,
        licenca_condicional,
        desconhecido,
    ) = summary_row

    items = []
    for row in rows:
        (
            favo_id,
            collection_key,
            classification,
            organization_state,
            duplicate_state,
            duplicate_role,
            duplicate_of,
            provenance_state,
            notes,
            created_at,
            updated_at,
            discovery_id,
            title,
            description,
            evidence,
            rights,
            source_url,
            source_name,
            document_date,
            license_name,
            license_url,
            attribution_required,
            author,
            media_url,
            latitude,
            longitude,
        ) = row

        items.append(
            {
                "id": str(favo_id),
                "discovery_id": str(discovery_id),
                "collection_key": collection_key,
                "classification": classification,
                "organization_state": organization_state,
                "duplicate_state": duplicate_state,
                "duplicate_role": duplicate_role,
                "duplicate_of": str(duplicate_of) if duplicate_of else None,
                "provenance_state": provenance_state,
                "notes": notes,
                "created_at": created_at.isoformat() if created_at else None,
                "updated_at": updated_at.isoformat() if updated_at else None,
                "title": title,
                "description": description,
                "evidence": evidence,
                "rights": rights,
                "source_url": source_url,
                "source_name": source_name,
                "document_date": document_date.isoformat() if document_date else None,
                "license_name": license_name,
                "license_url": license_url,
                "attribution_required": attribution_required,
                "author": author,
                "media_url": media_url,
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    return {
        "status": "OK",
        "summary": {
            "total": total,
            "unicos": unicos,
            "principais": principais,
            "duplicados": duplicados,
            "dominio_publico": dominio_publico,
            "licenca_condicional": licenca_condicional,
            "desconhecido": desconhecido,
        },
        "items": items,
    }

@app.get("/panel/favo-ui", response_class=HTMLResponse)
def favo_ui():
    return FileResponse("/home/servidor/colmeia/panel/favo.html")


@app.get("/panel", response_class=HTMLResponse)
def panel():
    return Path("/home/servidor/colmeia/panel/index.html").read_text(
        encoding="utf-8"
    )

# ============================================================
# INTERFACE VISUAL REAL — CENTRAL DE MISSÕES
# ============================================================

@app.get("/panel/globe-ui", response_class=HTMLResponse)
def globe_ui():
    return Path("/home/servidor/colmeia/panel/globe.html").read_text(
        encoding="utf-8"
    )

@app.get("/panel/missions-ui", response_class=HTMLResponse)
def missions_ui():
    return Path("/home/servidor/colmeia/panel/missions.html").read_text(
        encoding="utf-8"
    )

# ============================================================
# INTERFACE VISUAL REAL — DETALHES DA MISSÃO
# ============================================================

@app.get("/panel/mission-detail-ui", response_class=HTMLResponse)
def mission_detail_ui():
    return Path("/home/servidor/colmeia/panel/mission-detail.html").read_text(
        encoding="utf-8"
    )
