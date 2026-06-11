# MCP `obsidian-a2a`

Wrapper HTTP del `a2a-obsidian-gateway` para writes auditados al vault de Obsidian.

## Por qué existe

El MCP `obsidian` (raw) escribe directamente al filesystem, sin frontmatter canónico,
sin audit trail y sin commit+push a GitHub. `obsidian-a2a` envuelve el gateway que
garantiza todo eso. Ambos MCPs coexisten durante la migración (Fase B del plan de cutover).

## Estrategia de migración

- **obsidian** (raw) — sigue activo para lectura y para pruebas comparativas.
- **obsidian-a2a** — todo write nuevo debe ir aquí.
- Cuando la tasa de writes vía gateway sea 100% durante 7 días → desregistrar obsidian-raw de los proyectos activos (Fase D). El binario se mantiene en `~/.claude/mcp-servers/obsidian/` como fallback.

## Tools expuestas

| Tool | Descripción |
|---|---|
| `write_note` | Crea o reemplaza un doc (propose + apply, con frontmatter canónico) |
| `append_note` | Agrega contenido al final del doc actual |
| `delete_note` | Borra un doc del vault (requiere `id` en frontmatter) |

Las operaciones de lectura (`read_note`, `search_notes`, `list_notes`, `get_context`,
`add_attachment`) siguen en el MCP `obsidian`.

## Instalación

### 1. Crear el agente en el gateway (ya hecho)

```bash
cd /home/melquiades/a2a-obsidian-gateway
uv run a2a-gateway agent create \
  --name mcp-obsidian-a2a \
  --scopes "read,propose,apply" \
  --rate-limit 120
```

Para rotar la key:

```bash
uv run a2a-gateway agent rotate-key mcp-obsidian-a2a
```

### 2. Agregar a `secrets.json`

```json
{
  "name": "obsidian-a2a",
  "type": "obsidian-a2a",
  "gateway_url": "http://localhost:7680",
  "gateway_key": "a2a_<KEY>",
  "vault_path": "/home/melquiades/ObsidianVault"
}
```

### 3. Registrar en un proyecto

```bash
/mcp-project add <proyecto> obsidian-a2a
```

O vía script directamente:

```bash
python3 "scripts/add-mcp-to-project.py" /ruta/proyecto --only obsidian-a2a
```

## Variables de entorno

| Variable | Valor dev | Descripción |
|---|---|---|
| `A2A_GATEWAY_URL` | `http://localhost:7680` | URL base del gateway |
| `A2A_GATEWAY_KEY` | `a2a_<...>` | API key del agente MCP |
| `OBSIDIAN_VAULT` | `/home/melquiades/ObsidianVault` | Raíz del vault |

## Comportamiento de write_note

1. Lee el frontmatter del doc existente para extraer el `id` (si existe → update, si no → create).
2. Llama `POST /v1/write/propose` con el cuerpo completo y hints de vault/proyecto.
3. Si hay `violations` → retorna error sin hacer apply.
4. Llama `POST /v1/write/apply` → commit+push a GitHub.
5. Retorna `{status, doc_id, path, commit}`.

## Latencia esperada

- `write_note` nuevo doc: 2–5s (LLM de clasificación + git push).
- `write_note` update: 1–3s (sin clasificación LLM, reutiliza doc_id).
- `delete_note`: ~1s.

## Monitoreo (Fase C)

```bash
cd /home/melquiades/a2a-obsidian-gateway
uv run python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text
async def main():
    from a2a_gateway.config.settings import Settings
    s = Settings()
    engine = create_async_engine(s.PG_OPS_DSN)
    Session = async_sessionmaker(engine, class_=AsyncSession)
    async with Session() as sess:
        r = await sess.execute(text(
            \"SELECT event_type, count(*) FROM audit_log \"
            \"WHERE created_at > now() - interval '7 days' \"
            \"GROUP BY event_type ORDER BY count DESC\"
        ))
        for row in r: print(row)
asyncio.run(main())
"
```

## Archivos

- Binario instalado: `~/.claude/mcp-servers/obsidian-a2a/server.py`
- Fuente: `deployed/obsidian-a2a/server.py`
- Gateway: `/home/melquiades/a2a-obsidian-gateway/`
- Doc de migración: `/home/melquiades/a2a-obsidian-gateway/docs/09-migracion-mcp-obsidian.md`
