# MCP `focusyn`

MCP remoto **Streamable HTTP** del gateway `focusyn` (endpoint `/mcp`, in-process).
**Reemplazo completo** del MCP `obsidian` raw: lecturas y escrituras del vault pasan
por el gateway (audit trail + GraphRAG). Reemplaza también al wrapper **stdio legacy**
(`~/.claude/mcp-servers/focusyn/server.py`, retirado jun-2026).

## Por qué existe

El MCP `obsidian` (raw) leía/escribía directamente al filesystem, sin frontmatter
canónico, sin audit trail y sin commit+push a GitHub. `focusyn` envuelve el gateway
que garantiza todo eso y, además, enriquece las lecturas con el grafo de conocimiento:

- Las **escrituras** pasan por `propose` + `apply` → frontmatter canónico, governance
  (puede rechazar con `violations`), audit trail y commit+push automático.
- Las **lecturas** consultan el índice (Postgres) y el grafo (Neo4j): `read_note`
  devuelve el contenido **más** las entidades extraídas y los documentos relacionados;
  `find_notes` LOCALIZA notas rápido (lista rankeada, retrieval-only, sin LLM) y
  `search_notes` PREGUNTA (respuesta semántica sintetizada por el LLM) — ninguno es un grep.

A partir de la migración de junio 2026, `focusyn` es el único MCP de Obsidian
registrado en los proyectos activos. El binario `obsidian` raw se conserva en
`~/.claude/mcp-servers/obsidian/` solo como fallback local.

## Tools expuestas

| Tool | Endpoint gateway | Descripción |
|---|---|---|
| `get_context(vault)` | `GET /v1/read?path=CONTEXT.md` | CONTEXT.md del vault + (para `lait`/`melquiades`) concatena el global de `wiki`. Llamar **siempre** antes de crear o buscar notas. `vault`: wiki \| lait \| melquiades |
| `read_note(path, enrich=False)` | `GET /v1/read` | Contenido del doc + entidades GraphRAG + documentos relacionados del grafo. Con `enrich=true` añade `graph_summary` (síntesis LLM del neighborhood, +1-3s — solo cuando necesites entender el contexto del doc). `path` relativo al vault con prefijo (ej. `lait/proyectos/x/index.md`) |
| `list_notes(vault, path_prefix, kind, limit)` | `GET /v1/list` | Lista docs indexados (índice Postgres, no filesystem). Filtros opcionales. Latencia ~15s tras un write |
| `map_vault(vault="", path="", depth=1)` | `GET /v1/map` | Navega el árbol del vault un nivel a la vez (progressive disclosure). Sin args = los 3 vaults; desciende con `vault` + `path`; `depth` 1-4 anida `children`. `index.md` plegado como cabecera de carpeta; `CONTEXT.md` visible como doc. Complementa `list_notes` (plano/filtrado) |
| `find_notes(query, vault, kind, top_k, rerank)` | `POST /v1/search` | **ENCONTRAR** docs rápido: lista rankeada (vector + grafo + reranker), **sin respuesta sintetizada** (retrieval-only, p95<2s). Para localizar/listar notas por relevancia. `vault`: wiki \| lait \| melquiades \| `all` (= todos; se mapea a NULL en el contrato). `kind` filtra; `top_k` 1-25 (default 10); `rerank` default true. Devuelve `{results, took_ms, mode}` |
| `search_notes(query, vault, top_n)` | `POST /v1/graphrag/query` | **PREGUNTAR**: búsqueda **semántica** (vector + grafo + BM25 + reranker) **con respuesta sintetizada por LLM** (más lento que `find_notes`). Una pregunta en lenguaje natural. `vault` (scope): wiki \| lait \| melquiades \| all (default `all`) |
| `get_contracts(doc, role="")` | `GET /v1/contracts/{doc_id}[/{role}]` | Bloques tipados (json/yaml/openapi/proto/mermaid) del doc, listos para consumo máquina. `doc` acepta doc_id canónico o path; `role` filtra (schema \| example \| config…) |
| `write_note(path, body)` | `propose` + `apply` | Crea/reemplaza un doc completo. `body` es **siempre** el documento entero. Para cambios puntuales usa `edit_note`/`append_note`/`patch_frontmatter` |
| `edit_note(path, old_string, new_string, replace_all=False)` | `POST /v1/write/patch` | **Edición quirúrgica del body** (como el Edit tool): reemplaza `old_string`→`new_string` sin reinsertar el doc. El gateway lee SU checkout (no disco local). No revalida el frontmatter → funciona con deuda. old_string debe ser exacto; ambiguo → usa `replace_all` |
| `append_note(path, content)` | `POST /v1/write/append` | Concatena `content` al final del body (server-side, no reescribe el doc ni revalida el frontmatter) |
| `patch_frontmatter(path, set={}, unset=[])` | `POST /v1/write/patch-frontmatter` | **Salda deuda de frontmatter**: fija/borra campos sin tocar el body. Para arreglar docs viejos que fallan por `MISSING_FRONTMATTER`. No toca claves de identidad. Devuelve `remaining_debt` |
| `delete_note(path, reason)` | `POST /v1/write/delete` | Borra un doc (requiere `id` en frontmatter + estar indexado en GraphRAG) |
| `link_notes(source, target, relation="related")` | `POST /v1/write/link` | Cross-link gobernado: añade el doc_id del target a la lista frontmatter `relation` del doc ORIGEN (commit+push+audit; el watcher lo materializa en el grafo). `source`/`target` aceptan doc_id o path. Idempotente |
| `supersede_note(new, old, reason="")` | `POST /v1/write/supersede` | **Superseción bidireccional ATÓMICA**: el ADR `new` reemplaza al `old`. Escribe `supersedes:[old]` en el nuevo y `superseded_by:[new]` + `status:deprecated` en el viejo, en una sola operación (cada lado con commit+audit). Úsala SIEMPRE para superseder — no marques el back-pointer ni el status a mano. Ambos kind decision/proposal. Idempotente |
| `lint_vault(vault="", kind="")` | `GET /v1/lint` | Lista docs indexados que violan el schema vigente (deuda de frontmatter). Sin filtros audita los 3 vaults. Sáldalos con `patch_frontmatter` |
| `peek_id(vault, kind)` | `GET /v1/ids/peek` | Último/próximo doc_id para `(vault, kind)` SIN consumir uno (no cuentes a mano). `in_sync=false` señala drift contador↔índice |
| `push_vault(vault)` | `POST /v1/sync/push` | Empuja commits locales pendientes a GitHub (push fallido / commits manuales). Al día → `pushed: false`. Requiere scope `sync` |
| `add_attachment(source_path, vault, doc_id="", alt="", filename="")` | `POST /v1/write/attachment` | Sube un binario (imagen/PDF/audio) al **NAS vía gateway** (fuera de Git). Devuelve `markdown_ref` (`![alt](/v1/attachment/{file_id})`) + `file_id` + `status`. Con `doc_id` + imagen → indexado multimodal (recuperable cross-modal por `search_notes`). Idempotente por `(vault, source_path)`. Scope `apply` |
| `delete_attachment(file_id)` | `DELETE /v1/attachment/{file_id}` | Borra una imagen/binario **suelto** del NAS por `file_id` (el UUID de `![alt](/v1/attachment/{file_id})`): binario + fila `attachments` + chunk multimodal. Devuelve `{file_id, status}` (`deleted` \| `already_deleted`). Idempotente. Para borrar TODOS los adjuntos de un doc usa `delete_note` (cascade). Scope `apply` |

> **Edición quirúrgica gobernada (Fase 8):** `edit_note` (str_replace del body),
> `append_note` y `patch_frontmatter` editan en puntos específicos vía
> `apply_transform` del gateway — **no reinsertas el doc entero**. El gateway lee SU
> checkout (no el disco local del agente → robusto ante la migración a server) y
> **no revalida el frontmatter**, así que funcionan aunque el doc arrastre deuda. Si
> una escritura falla con `MISSING_FRONTMATTER`, sáldala con `patch_frontmatter` y
> reintenta; `lint_vault` revela qué docs tienen deuda.

> **`search_notes` — degradación explícita:** la respuesta incluye `answer_status`
> (`ok` | `no_evidence` | `synthesis_failed`). Si es `synthesis_failed`, NO uses
> `answer` (viene vacío): la recuperación sí funcionó, lee `evidence_docs`.

> **Invariantes relacionales gobernados por el gateway (no por el agente):**
> - **Superseder**: usar `supersede_note(new, old)` — el back-pointer y el `status:deprecated`
>   los pone el gateway de forma atómica. `patch_frontmatter` ahora **rechaza** un `status`
>   fuera del enum (antes lo dejaba pasar).
> - **Cross-refs en el grafo**: `related_ids`/`link_notes`/`supersede_note` se materializan
>   como aristas `LINKS_TO` en el ingest (antes solo contaban los doc_id en el cuerpo).
> - **`apply` devuelve `next_actions`**: si un ADR recién creado aún no está referenciado
>   desde el `index.md` del proyecto, lo recuerda en la respuesta — cerrar el cross-link con
>   `edit_note`/`link_notes`.

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

El MCP vive **dentro del gateway** (`src/focusyn/mcp_app.py`, endpoint Streamable HTTP
`/mcp`, montado en `main.py`). No hay binario que copiar ni proceso stdio que lanzar:
basta con que el gateway esté corriendo y registrar el endpoint con una key de agente.

### 1. Crear la key del agente en el gateway

```bash
cd /home/melquiades/focusyn
uv run focusyn agent create \
  --name focusyn-mcp-local \
  --scopes "read,propose,apply,sync" \
  --rate-limit 120
```

> `sync` es necesario para `push_vault`. La key se muestra **una sola vez**; rótala con
> `uv run focusyn agent rotate-key focusyn-mcp-local` (y re-registra con la nueva).

### 2. Arrancar el gateway

```bash
cd /home/melquiades/focusyn
docker compose up -d        # contenedor focusyn-gateway-1, :7415
# o en dev: make dev        # uvicorn --reload --port 7415
```

Verificar el endpoint MCP (sin auth = 401; con la key, el handshake responde):

```bash
curl -s -o /dev/null -w "health: %{http_code}\n" http://localhost:7415/health   # 200
curl -s -o /dev/null -w "/mcp/: %{http_code}\n"   http://localhost:7415/mcp/     # 401 (sin auth)
```

### 3. Registrar en `scripts/secrets.json`

```json
{
  "name": "focusyn",
  "type": "focusyn",
  "url": "http://localhost:7415/mcp/",
  "agent_key": "a2a_<KEY>"
}
```

`add-mcp-to-project.py` construye con estos campos una entrada **http**:
`{"type":"http","url":...,"headers":{"X-Agent-Key": <agent_key>}}` (ya no es stdio).

### 4. Registrar como MCP GLOBAL (user-scope)

focusyn se registra **una sola vez** en el `mcpServers` top-level de `~/.claude.json`
(user-scope) y queda disponible en **todos** los proyectos sin repetirlo:

```bash
claude mcp add --scope user --transport http focusyn \
  http://localhost:7415/mcp/ --header "X-Agent-Key: a2a_<KEY>"
```

> Precedencia de scopes: **Local > Project > User**. No dejes un `focusyn` per-project
> (en `projects[path].mcpServers`) o ganará sobre el global. Para un override puntual en
> un proyecto sí puedes registrarlo local con
> `python3 "scripts/add-mcp-to-project.py" /ruta/proyecto --only focusyn`.

Después: **reiniciar Claude Code en VSCode** para que cargue el MCP.

## Configuración del registro

| Campo | Valor | Descripción |
|---|---|---|
| `url` | `http://localhost:7415/mcp/` | Endpoint Streamable HTTP del gateway (in-process `/mcp`) |
| header `X-Agent-Key` | `a2a_<...>` | Key del agente de máquina (`focusyn-mcp-local`); el gateway resuelve scopes y aplica governance |

El MCP ya **no lee el disco del cliente**: el `id` del frontmatter (para
`write_note`/`delete_note`/`link`/`supersede`) lo resuelve el gateway vía
`GET /v1/ids/resolve` (in-process). Por eso no hay `OBSIDIAN_VAULT` ni nada que montar.

## Latencia esperada

- `read_note` / `get_context` / `list_notes` / `get_contracts`: < 1s (índice Postgres + grafo).
- `read_note(enrich=true)`: +1–3s (síntesis LLM del neighborhood).
- `find_notes`: p95 < 2s (retrieval-only: híbrido + reranker, **sin** LLM de síntesis).
- `search_notes`: 7–8s (retrieval híbrido + reranker + LLM de síntesis).
- `write_note` nuevo doc con kind inferible de la ruta: 1–3s (sin LLM, solo git push).
- `write_note` nuevo doc con ruta ambigua: 2–5s (LLM de clasificación + git push).
- `write_note` update / `link_notes`: 1–3s (sin clasificación LLM).
- `delete_note` / `push_vault`: ~1s.
- `add_attachment`: 1–4s (subida al NAS; +1–2s si indexa multimodal una imagen con `doc_id`).
- `map_vault`: < 1s (índice Postgres).

## Smoke test

Con el gateway corriendo, un handshake MCP `initialize` debe devolver
`serverInfo.name = "focusyn"`:

```bash
curl -s -X POST http://localhost:7415/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Agent-Key: a2a_<KEY>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```

`tools/list` (mismo endpoint, `"method":"tools/list"`) enumera las 26 tools. Sin el
header `X-Agent-Key` el endpoint responde **401**.

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

- MCP (in-process): `/home/melquiades/focusyn/src/focusyn/mcp_app.py` (montado en `main.py`)
- Gateway: `/home/melquiades/focusyn/` (contenedor `focusyn-gateway-1`, `:7415`)
- Registro global: `mcpServers.focusyn` (top-level) en `~/.claude.json` (user-scope)
- Tooling de registro: `scripts/secrets.json` (entry `focusyn`) + `scripts/add-mcp-to-project.py`
- Wrapper stdio legacy: **retirado** jun-2026 (antes en `~/.claude/mcp-servers/focusyn/` + `deployed/focusyn/`)
- MCP raw (legacy, fallback): `guides/mcp-obsidian.md`
