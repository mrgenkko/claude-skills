# Guía: MCP de Redis

MCP custom en Python, mismo patrón que `postgres` y `ssh`: un solo binario
(`~/.claude/mcp-servers/redis/server.py`) reutilizado en varias instancias con
distintos `--host`/`--db`/`--name`. Se registra vía `secrets.json` +
`add-mcp-to-project.py` (o la skill `/mcp-project`).

## Tools que expone

| Tool | Uso |
|---|---|
| `command` | Ejecuta cualquier comando Redis crudo: `GET`, `SET`, `HGETALL`, `EXPIRE`, `DEL`, `INCR`, `LPUSH`… Equivalente al `query` de postgres. |
| `keys` | Lista keys por patrón glob (vía `SCAN`, no bloquea) con su tipo y TTL. Equivalente a `tables`. |
| `info` | Devuelve `INFO` del server (memoria, clientes, persistencia, stats). Acepta `section` opcional. |

## Seguridad: FLUSHALL / FLUSHDB

A diferencia de Postgres, `FLUSHALL`/`FLUSHDB` son **irreversibles** y borran toda
la base. Por eso el `command` los **bloquea por defecto**. Para habilitarlos en una
instancia concreta, poner `"allow_flush": true` en su entrada de `secrets.json` y
re-registrar el proyecto con `--update`.

El bloqueo solo cubre `FLUSHALL`/`FLUSHDB`. Comandos destructivos puntuales como
`DEL`, `EXPIRE` o `SET` sobre una key existente pasan libres (igual que un `DELETE`
en el MCP de postgres).

## Configuración en `secrets.json`

```json
{
  "name": "redis-mi-cache",
  "type": "redis",
  "host": "TU_HOST_REDIS",
  "port": 6379,
  "db": 0,
  "password": null,
  "allow_flush": false
}
```

Multi-instancia: igual que postgres con varias DB, registrar `redis-cache`,
`redis-sessions`, etc. con el mismo binario y distinto `--db` o `--host`. El
`--name` se deriva del nombre quitando el prefijo `redis-`.

## Registrar en un proyecto

```bash
# vía skill
/mcp-project add mi-proyecto redis-mi-cache

# o directo
python3 "~/Mrgenkko Skills/scripts/add-mcp-to-project.py" /ruta/al/proyecto --only redis-mi-cache
```

Reiniciar la extensión VSCode para que cargue.

## Config resultante en `~/.claude.json`

```json
"redis-mi-cache": {
  "type": "stdio",
  "command": "/home/melquiades/Mrgenkko Skills/.venv/bin/python",
  "args": [
    "/home/melquiades/.claude/mcp-servers/redis/server.py",
    "--host=TU_HOST_REDIS",
    "--port=6379",
    "--db=0",
    "--name=mi-cache"
  ],
  "env": {}
}
```

## ¿Por qué custom y no el oficial `redis/mcp-redis`?

El oficial (Python, mantenido por Redis Inc.) es completo y soporta vector search,
pero trae su propio esquema de config por env vars que no encaja con el patrón
multi-instancia de `secrets.json` + `add-mcp-to-project.py`. El custom mantiene la
consistencia con gcloud/postgres/ssh/obsidian y da control total sobre qué tools se
exponen. Considerar el oficial si en el futuro se necesita RAG/vector search sobre
Redis sin escribir código.

## Troubleshooting

**`Error Redis: Authentication required`**
→ Falta `password` en `secrets.json`.

**`Error Redis: Connection refused`**
→ Verificar host/puerto y que Redis acepte conexiones remotas (`bind` y `protected-mode`).
→ Probar directo: `redis-cli -h HOST -p 6379 -a PASS PING`

**`Bloqueado: 'FLUSHALL' está deshabilitado`**
→ Comportamiento esperado. Poner `"allow_flush": true` en la entrada y re-registrar con `--update`.

**El MCP no aparece en Claude Code (VSCode)**
→ Verificar que está en `~/.claude.json` (no en `settings.json`) y reiniciar la extensión.
