# MCP `obsidian-a2a`

Cliente HTTP del `a2a-obsidian-gateway`. **Reemplazo completo** del MCP `obsidian` raw:
lecturas y escrituras del vault pasan por el gateway (audit trail + GraphRAG).

## Por qué existe

El MCP `obsidian` (raw) leía/escribía directamente al filesystem, sin frontmatter
canónico, sin audit trail y sin commit+push a GitHub. `obsidian-a2a` envuelve el gateway
que garantiza todo eso y, además, enriquece las lecturas con el grafo de conocimiento:

- Las **escrituras** pasan por `propose` + `apply` → frontmatter canónico, governance
  (puede rechazar con `violations`), audit trail y commit+push automático.
- Las **lecturas** consultan el índice (Postgres) y el grafo (Neo4j): `read_note`
  devuelve el contenido **más** las entidades extraídas y los documentos relacionados;
  `search_notes` es una búsqueda **semántica** GraphRAG, no un grep.

A partir de la migración de junio 2026, `obsidian-a2a` es el único MCP de Obsidian
registrado en los proyectos activos. El binario `obsidian` raw se conserva en
`~/.claude/mcp-servers/obsidian/` solo como fallback local.

## Tools expuestas

| Tool | Endpoint gateway | Descripción |
|---|---|---|
| `get_context(vault)` | `GET /v1/read?path=CONTEXT.md` | CONTEXT.md del vault + (para `lait`/`melquiades`) concatena el global de `wiki`. Llamar **siempre** antes de crear o buscar notas. `vault`: wiki \| lait \| melquiades |
| `read_note(path)` | `GET /v1/read` | Contenido del doc + entidades GraphRAG + documentos relacionados del grafo. `path` relativo al vault con prefijo (ej. `lait/proyectos/x/index.md`) |
| `list_notes(vault, path_prefix, kind, limit)` | `GET /v1/list` | Lista docs indexados (índice Postgres, no filesystem). Filtros opcionales. Latencia ~15s tras un write |
| `search_notes(query, vault, top_n)` | `POST /v1/graphrag/query` | Búsqueda **semántica** (vector + grafo + BM25 + reranker): una pregunta en lenguaje natural. `vault` (scope): wiki \| lait \| melquiades \| all (default `all`) |
| `write_note(path, body)` | `propose` + `apply` | Crea/reemplaza un doc. `body` es **siempre** el documento completo (no hay edición por str_replace) |
| `append_note(path, content)` | `read` + `propose` + `apply` | Agrega contenido al final (lee el body actual y reescribe completo) |
| `delete_note(path, reason)` | `POST /v1/write/delete` | Borra un doc (requiere `id` en frontmatter + estar indexado en GraphRAG) |
| `add_attachment(source_path, filename, folder)` | — (filesystem directo) | Copia un binario (imagen/PDF) al vault y retorna el wikilink `![[...]]`. Los attachments no son docs gobernados |

> **Cambio respecto al MCP raw:** `edit_note` (str_replace) **ya no existe** — el gateway
> no soporta ediciones parciales. Para un cambio puntual: `read_note` (traer el body
> actual) y luego `write_note` con el body completo modificado.

## Traducción de paths

El `path` que reciben las tools es relativo al `ObsidianVault` e incluye el vault como
primer segmento. El server lo descompone en `(vault, path_relativo)`:

```
"lait/proyectos/mi-proj/index.md"  →  vault="lait",  path="proyectos/mi-proj/index.md"
"melquiades/CONTEXT.md"            →  vault="melquiades", path="CONTEXT.md"
```

Los vaults conocidos están en `_KNOWN_VAULTS = {"wiki", "lait", "melquiades"}`.

## Comportamiento de los writes

1. `write_note` lee el `id` del frontmatter del doc existente (si lo hay → update con
   `target_doc_id`; si no → create con clasificación LLM).
2. Llama `POST /v1/write/propose` con el body completo y `hints` (`target_vault`,
   `project`, y `org` para vaults de organización).
3. Si la respuesta trae `violations` → retorna `{"status": "rejected", ...}` sin aplicar.
4. Llama `POST /v1/write/apply` → commit+push a GitHub.
5. Retorna `{status, doc_id, path, commit}`.

## Instalación

### 1. Crear la key del agente en el gateway

```bash
cd /home/melquiades/a2a-obsidian-gateway
uv run a2a-gateway agent create \
  --name mcp-obsidian-a2a \
  --scopes "read,propose,apply" \
  --rate-limit 120
```

La key se muestra **una sola vez**. Para rotarla:

```bash
uv run a2a-gateway agent rotate-key mcp-obsidian-a2a
```

### 2. Arrancar el gateway

```bash
cd /home/melquiades/a2a-obsidian-gateway
make dev          # uvicorn a2a_gateway.main:app --reload --port 7680
```

Verificar:

```bash
curl -s http://localhost:7680/health        # status ok + dependencias
curl -s http://localhost:7680/v1/capabilities
```

### 3. Copiar el servidor MCP

```bash
mkdir -p ~/.claude/mcp-servers/obsidian-a2a
cp "~/Mrgenkko Skills/deployed/obsidian-a2a/server.py" \
   ~/.claude/mcp-servers/obsidian-a2a/server.py
```

### 4. Registrar en `scripts/secrets.json`

```json
{
  "name": "obsidian-a2a",
  "type": "obsidian-a2a",
  "gateway_url": "http://localhost:7680",
  "gateway_key": "a2a_<KEY>",
  "vault_path": "/home/melquiades/ObsidianVault"
}
```

`add-mcp-to-project.py` construye la entrada con estos campos como variables de entorno
(`A2A_GATEWAY_URL`, `A2A_GATEWAY_KEY`, `OBSIDIAN_VAULT`).

### 5. Registrar en un proyecto (reemplaza al raw)

```bash
/mcp-project add <proyecto> obsidian-a2a      # agrega obsidian-a2a
/mcp-project remove <proyecto> obsidian       # quita el raw
```

O vía script directamente:

```bash
python3 "scripts/add-mcp-to-project.py" /ruta/proyecto --only obsidian-a2a
```

Después: **reiniciar Claude Code en VSCode** para que cargue el MCP.

## Variables de entorno

| Variable | Valor dev | Descripción |
|---|---|---|
| `A2A_GATEWAY_URL` | `http://localhost:7680` | URL base del gateway |
| `A2A_GATEWAY_KEY` | `a2a_<...>` | API key del agente MCP (header `X-Agent-Key`) |
| `OBSIDIAN_VAULT` | `/home/melquiades/ObsidianVault` | Raíz del vault en el host (para leer el `id` del frontmatter y para `add_attachment`) |

## Latencia esperada

- `read_note` / `get_context` / `list_notes`: < 1s (índice Postgres + grafo).
- `search_notes`: 2–6s (retrieval híbrido + reranker + LLM de síntesis).
- `write_note` nuevo doc: 2–5s (LLM de clasificación + git push).
- `write_note` update: 1–3s (reutiliza `doc_id`, sin clasificación LLM).
- `delete_note`: ~1s.

## Smoke test

Con el gateway corriendo y la key en el entorno:

```bash
A2A_GATEWAY_KEY="a2a_<KEY>" python3 - <<'PY'
import asyncio, importlib.util
spec = importlib.util.spec_from_file_location("srv", "/home/melquiades/.claude/mcp-servers/obsidian-a2a/server.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fn = lambda t: (t.fn if hasattr(t, "fn") else t)
async def main():
    gc = await fn(m.get_context)(vault="melquiades")
    print("get_context ->", list(gc.keys()))
    ln = await fn(m.list_notes)(vault="melquiades", limit=2)
    print("list_notes total ->", ln.get("total"))
    sn = await fn(m.search_notes)(query="arquitectura de autenticacion", vault="melquiades", top_n=2)
    print("search_notes evidence_docs ->", len(sn.get("evidence_docs", [])))
asyncio.run(main())
PY
```

## Monitoreo del audit trail

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
- Fuente desplegada: `deployed/obsidian-a2a/server.py`
- Ejemplo base público: `examples/mcp-obsidian-a2a/server.py`
- Gateway: `/home/melquiades/a2a-obsidian-gateway/`
- MCP raw (legacy, fallback): `guides/mcp-obsidian.md`
