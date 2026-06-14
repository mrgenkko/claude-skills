# MCP `focusyn`

Cliente HTTP del `focusyn`. **Reemplazo completo** del MCP `obsidian` raw:
lecturas y escrituras del vault pasan por el gateway (audit trail + GraphRAG).

## Por qué existe

El MCP `obsidian` (raw) leía/escribía directamente al filesystem, sin frontmatter
canónico, sin audit trail y sin commit+push a GitHub. `focusyn` envuelve el gateway
que garantiza todo eso y, además, enriquece las lecturas con el grafo de conocimiento:

- Las **escrituras** pasan por `propose` + `apply` → frontmatter canónico, governance
  (puede rechazar con `violations`), audit trail y commit+push automático.
- Las **lecturas** consultan el índice (Postgres) y el grafo (Neo4j): `read_note`
  devuelve el contenido **más** las entidades extraídas y los documentos relacionados;
  `search_notes` es una búsqueda **semántica** GraphRAG, no un grep.

A partir de la migración de junio 2026, `focusyn` es el único MCP de Obsidian
registrado en los proyectos activos. El binario `obsidian` raw se conserva en
`~/.claude/mcp-servers/obsidian/` solo como fallback local.

## Tools expuestas

| Tool | Endpoint gateway | Descripción |
|---|---|---|
| `get_context(vault)` | `GET /v1/read?path=CONTEXT.md` | CONTEXT.md del vault + (para `lait`/`melquiades`) concatena el global de `wiki`. Llamar **siempre** antes de crear o buscar notas. `vault`: wiki \| lait \| melquiades |
| `read_note(path, enrich=False)` | `GET /v1/read` | Contenido del doc + entidades GraphRAG + documentos relacionados del grafo. Con `enrich=true` añade `graph_summary` (síntesis LLM del neighborhood, +1-3s — solo cuando necesites entender el contexto del doc). `path` relativo al vault con prefijo (ej. `lait/proyectos/x/index.md`) |
| `list_notes(vault, path_prefix, kind, limit)` | `GET /v1/list` | Lista docs indexados (índice Postgres, no filesystem). Filtros opcionales. Latencia ~15s tras un write |
| `search_notes(query, vault, top_n)` | `POST /v1/graphrag/query` | Búsqueda **semántica** (vector + grafo + BM25 + reranker): una pregunta en lenguaje natural. `vault` (scope): wiki \| lait \| melquiades \| all (default `all`) |
| `get_contracts(doc, role="")` | `GET /v1/contracts/{doc_id}[/{role}]` | Bloques tipados (json/yaml/openapi/proto/mermaid) del doc, listos para consumo máquina. `doc` acepta doc_id canónico o path; `role` filtra (schema \| example \| config…) |
| `write_note(path, body)` | `propose` + `apply` | Crea/reemplaza un doc. `body` es **siempre** el documento completo (no hay edición por str_replace) |
| `append_note(path, content)` | `read` + `propose` + `apply` | Agrega contenido al final (lee el body actual y reescribe completo) |
| `delete_note(path, reason)` | `POST /v1/write/delete` | Borra un doc (requiere `id` en frontmatter + estar indexado en GraphRAG) |
| `link_notes(source, target, relation="related")` | `POST /v1/write/link` | Cross-link gobernado: añade el doc_id del target a la lista frontmatter `relation` del doc ORIGEN (commit+push+audit; el watcher lo materializa en el grafo). `source`/`target` aceptan doc_id o path. Idempotente |
| `push_vault(vault)` | `POST /v1/sync/push` | Empuja commits locales pendientes a GitHub (push fallido / commits manuales). Al día → `pushed: false`. Requiere scope `sync` |
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
   `target_doc_id`, que preserva doc_id y path; si no → create).
2. Para creates **infiere el `kind` de la ruta pedida** y lo pasa como hint — con
   `doc_kind` + `target_vault` + `project` completos el gateway clasifica de forma
   **determinista (sin LLM)** y el doc cae en la carpeta pedida:

   | Ruta pedida | kind inferido |
   |---|---|
   | `CONTEXT.md` / `index.md` (raíz) | portal / index |
   | `proyectos/<p>/{index,arquitectura,glosario}.md` | index / architecture / glossary |
   | `proyectos/<p>/decisiones/` | decision |
   | `proyectos/<p>/instructivos/{http,ws,grpc,cli,sdk}/` | instructivo_* |
   | `proyectos/<p>/{runbooks,propuestas,pendientes}/` | runbook / proposal / pending |
   | `<org>/ecosistema/` · `<org>/integraciones/` | ecosystem / integration |
   | wiki: `conceptos/ patrones/ herramientas/ tutoriales/ referencia/ personas/` | concept / pattern / tool / tutorial / reference / person |
   | wiki: `log.md` | log |

   Ruta ambigua (sin match) → clasificación LLM como antes.
3. El **nombre final del archivo** lo computa el gateway como slug del `intent`; el
   wrapper construye el intent desde el **nombre de archivo pedido** (o el título H1
   si el nombre es muy corto), así el slug queda fiel a lo pedido. En ADRs el prefijo
   numérico lo pone el gateway con la secuencia del doc_id.
4. Llama `POST /v1/write/propose` con el body completo y los hints.
   - `violations` → retorna `{"status": "rejected", violations, target_path}` sin aplicar.
   - `requires_approval` (confidence baja) → retorna `{"status": "requires_approval", ...}`
     sin aplicar; reintentar con un path más explícito.
5. Llama `POST /v1/write/apply` → commit+push a GitHub.
6. Retorna `{status, doc_id, kind, path, commit}`. El `doc_id` en creates se extrae del
   frontmatter del `rendered_preview`; el `path` viene **con prefijo de vault**
   (ej. `melquiades/proyectos/x/pendientes/mi-nota.md`) listo para `read_note`.

## Instalación

### 1. Crear la key del agente en el gateway

```bash
cd /home/melquiades/focusyn
uv run focusyn agent create \
  --name focusyn \
  --scopes "read,propose,apply,sync" \
  --rate-limit 120
```

> `sync` es necesario para `push_vault` (añadido 2026-06-12; al agente existente
> se le agregó vía UPDATE en `a2a_ops.agents` — la CLI no edita scopes).

La key se muestra **una sola vez**. Para rotarla:

```bash
uv run focusyn agent rotate-key focusyn
```

### 2. Arrancar el gateway

```bash
cd /home/melquiades/focusyn
make dev          # uvicorn a2a_gateway.main:app --reload --port 7680
```

Verificar:

```bash
curl -s http://localhost:7680/health        # status ok + dependencias
curl -s http://localhost:7680/v1/capabilities
```

### 3. Copiar el servidor MCP

```bash
mkdir -p ~/.claude/mcp-servers/focusyn
cp "~/Mrgenkko Skills/deployed/focusyn/server.py" \
   ~/.claude/mcp-servers/focusyn/server.py
```

### 4. Registrar en `scripts/secrets.json`

```json
{
  "name": "focusyn",
  "type": "focusyn",
  "gateway_url": "http://localhost:7680",
  "gateway_key": "a2a_<KEY>",
  "vault_path": "/home/melquiades/ObsidianVault"
}
```

`add-mcp-to-project.py` construye la entrada con estos campos como variables de entorno
(`FOCUSYN_GATEWAY_URL`, `FOCUSYN_GATEWAY_KEY`, `OBSIDIAN_VAULT`).

### 5. Registrar en un proyecto (reemplaza al raw)

```bash
/mcp-project add <proyecto> focusyn      # agrega focusyn
/mcp-project remove <proyecto> obsidian       # quita el raw
```

O vía script directamente:

```bash
python3 "scripts/add-mcp-to-project.py" /ruta/proyecto --only focusyn
```

Después: **reiniciar Claude Code en VSCode** para que cargue el MCP.

## Variables de entorno

| Variable | Valor dev | Descripción |
|---|---|---|
| `FOCUSYN_GATEWAY_URL` | `http://localhost:7680` | URL base del gateway |
| `FOCUSYN_GATEWAY_KEY` | `a2a_<...>` | API key del agente MCP (header `X-Agent-Key`) |
| `OBSIDIAN_VAULT` | `/home/melquiades/ObsidianVault` | Raíz del vault en el host (para leer el `id` del frontmatter y para `add_attachment`) |

## Latencia esperada

- `read_note` / `get_context` / `list_notes` / `get_contracts`: < 1s (índice Postgres + grafo).
- `read_note(enrich=true)`: +1–3s (síntesis LLM del neighborhood).
- `search_notes`: 7–8s (retrieval híbrido + reranker en GPU + LLM de síntesis).
- `write_note` nuevo doc con kind inferible de la ruta: 1–3s (sin LLM, solo git push).
- `write_note` nuevo doc con ruta ambigua: 2–5s (LLM de clasificación + git push).
- `write_note` update / `link_notes`: 1–3s (sin clasificación LLM).
- `delete_note` / `push_vault`: ~1s.

## Smoke test

Con el gateway corriendo y la key en el entorno:

```bash
FOCUSYN_GATEWAY_KEY="a2a_<KEY>" python3 - <<'PY'
import asyncio, importlib.util
spec = importlib.util.spec_from_file_location("srv", "/home/melquiades/.claude/mcp-servers/focusyn/server.py")
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
cd /home/melquiades/focusyn
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

- Binario instalado: `~/.claude/mcp-servers/focusyn/server.py`
- Fuente desplegada: `deployed/focusyn/server.py`
- Ejemplo base público: `examples/mcp-focusyn/server.py`
- Gateway: `/home/melquiades/focusyn/`
- MCP raw (legacy, fallback): `guides/mcp-obsidian.md`
