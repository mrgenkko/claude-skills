#!/usr/bin/env python3
"""MCP focusyn: reemplazo completo de obsidian-raw vía el focusyn.

Lecturas y escrituras pasan por el gateway HTTP (puerto 7415 en dev):

- Lecturas (`read_note` [con enrich opcional], `get_context`, `list_notes`,
  `find_notes`, `search_notes`, `get_contracts`, `lint_vault`, `peek_id`, `map_vault`)
  consultan el gateway →
  contenido + entidades GraphRAG + documentos relacionados + bloques tipados +
  deuda de frontmatter + contador autoritativo de doc_id.
- Escrituras gobernadas (frontmatter canónico, audit, commit+push):
  - `write_note` (crear/reemplazar completo), `delete_note`, `link_notes`.
  - Edición quirúrgica server-side (lee el checkout del gateway, NO disco local;
    no revalida el frontmatter → funciona con deuda): `edit_note` (str_replace del
    body), `append_note` (concatena al body), `patch_frontmatter` (saldar deuda).
- `push_vault` empuja commits locales pendientes (push fallido / commits manuales).
- `add_attachment` sube binarios al NAS vía el gateway (fuera de Git, referencia por
  proxy estable); con doc_id + imagen se indexa multimodal.
- `delete_attachment` borra un binario suelto del NAS por file_id (idempotente; el
  cascade al borrar un doc lo hace `delete_note`).
"""

import asyncio
import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "focusyn",
    instructions="""\
Gateway del vault del ecosistema — memoria larga, no un destino opcional. Úsalo, no lo saltes.

ANTES de crear o buscar: get_context(vault) (convenciones + portal de la org). El vault y el schema del proyecto los nombra su CLAUDE.md.

LECTURA lean por defecto: read_note(path)=raw_content+frontmatter. graph=true solo para "entender X y su entorno"; enrich=true síntesis. find_notes = LOCALIZAR notas rápido (lista rankeada, sin LLM, p95<2s). search_notes = PREGUNTA semántica con respuesta sintetizada (más lento). Ninguno es grep. map_vault() explora la ESTRUCTURA del vault un nivel a la vez (sin args = los 3 vaults).

ESCRITURA — no reescribas el doc para cambios puntuales:
- write_note(path,body): crear (SIN id) / reemplazar (CON id). Devuelve next_actions (recordatorios).
- edit_note / append_note: edición quirúrgica del body / al final
- patch_frontmatter(set/unset): salda deuda; rechaza valores fuera de enum
- link_notes(src,tgt,relation): cross-link (materializa arista)
- supersede_note(nuevo,viejo): superseder ADR (back-pointer+status:deprecated atómico). NO a mano.
- delete_note(path,reason)

IDs los asigna el gateway: crear→OMITE id; actualizar→INCLUYE el id; planear→peek_id(vault,kind). NUNCA inventes contadores ni grepees el disco.

REGLAS DURAS:
- Tras crear un ADR: referencialo desde el index del proyecto (el next_actions lo recuerda).
- Wikilinks [[...]] solo dentro del mismo vault; cross-vault solo por doc_id en related_ids (materializa arista).
- El grafo es índice (mapa), NO verdad: verifica leyendo el archivo real.""",
)

GATEWAY_URL = os.environ.get("FOCUSYN_GATEWAY_URL", "http://localhost:7415")
GATEWAY_KEY = os.environ["FOCUSYN_GATEWAY_KEY"]
VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/melquiades/ObsidianVault"))

_HEADERS = {"X-Agent-Key": GATEWAY_KEY, "Content-Type": "application/json"}
_TIMEOUT = 60.0  # propose y graphrag pueden tardar por el LLM
_KNOWN_VAULTS = {"wiki", "lait", "melquiades"}


# ── helpers ───────────────────────────────────────────────────────────────────


def _vault_and_relpath(obs_path: str) -> tuple[str, str]:
    """obs_path "lait/proyectos/mi-proj/index.md" → ("lait", "proyectos/mi-proj/index.md")."""
    parts = obs_path.strip("/").split("/", 1)
    vault = parts[0]
    if vault not in _KNOWN_VAULTS:
        raise ValueError(
            f"Vault desconocido: '{vault}'. Válidos: {', '.join(sorted(_KNOWN_VAULTS))}"
        )
    return vault, parts[1] if len(parts) > 1 else ""


def _project(rel_path: str) -> str | None:
    segs = rel_path.split("/")
    if segs and segs[0] == "proyectos" and len(segs) >= 2:
        return segs[1]
    return None


# Inferencia de kind desde la ruta pedida (espejo de vault/paths.py del gateway).
# Con doc_kind + target_vault + project en hints la clasificación es determinista
# (el gateway salta el LLM) y el doc cae en la carpeta que el agente pidió.

_PROJECT_FILE_KINDS = {
    "index.md": "index",
    "arquitectura.md": "architecture",
    "glosario.md": "glossary",
}
_PROJECT_FOLDER_KINDS = {
    "decisiones": "decision",
    "runbooks": "runbook",
    "propuestas": "proposal",
    "pendientes": "pending",
}
_INSTRUCTIVO_KINDS = {p: f"instructivo_{p}" for p in ("http", "ws", "grpc", "cli", "sdk")}
_WIKI_FOLDER_KINDS = {
    "conceptos": "concept",
    "patrones": "pattern",
    "herramientas": "tool",
    "tutoriales": "tutorial",
    "referencia": "reference",
    "personas": "person",
}


def _infer_kind(vault: str, rel: str) -> str | None:
    """Kind canónico inferido de la ruta pedida; None si es genuinamente ambiguo."""
    if rel == "CONTEXT.md":
        return "portal"
    if rel == "index.md":
        return "index"
    segs = rel.split("/")
    if vault == "wiki":
        if rel == "log.md":
            return "log"
        return _WIKI_FOLDER_KINDS.get(segs[0])
    if segs[0] == "ecosistema":
        return "ecosystem"
    if segs[0] == "integraciones":
        return "integration"
    if segs[0] == "proyectos" and len(segs) >= 3:
        inner = segs[2:]
        if len(inner) == 1:
            return _PROJECT_FILE_KINDS.get(inner[0])
        if inner[0] == "instructivos" and len(inner) >= 2:
            return _INSTRUCTIVO_KINDS.get(inner[1])
        return _PROJECT_FOLDER_KINDS.get(inner[0])
    return None


def _build_intent(rel: str, body: str) -> str:
    """Intent para propose. El gateway deriva el slug del archivo desde el intent
    (title_to_slug), así que se construye desde el nombre pedido — o el título H1
    si el nombre es muy corto (el gateway exige intent ≥ 10 chars)."""
    stem = rel.rsplit("/", 1)[-1].removesuffix(".md")
    words = re.sub(r"[-_]+", " ", stem).strip()
    # ADRs llevan prefijo numérico en el nombre ("042-auth-jwt"); el gateway re-añade
    # la secuencia del doc_id, así que se quita aquí para no duplicarla en el slug.
    words = re.sub(r"^\d+\s*", "", words).strip()
    if len(words) >= 10:
        return words[:500]
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    title = m.group(1).strip() if m else ""
    candidate = title or words
    if len(candidate) < 10:
        candidate = f"Documento {rel}"
    return candidate[:500]


def _extract_fm_id(content: str) -> str | None:
    """Campo 'id' del frontmatter YAML de un documento (None si no hay)."""
    m = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return None
    for line in m.group(1).splitlines():
        if line.startswith("id:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _idempotency_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:36]


def _read_doc_id(obs_path: str) -> str | None:
    """Lee el campo 'id' del frontmatter YAML de un doc del vault (desde disco)."""
    try:
        full = VAULT_ROOT / obs_path.lstrip("/")
        if not full.suffix:
            full = full.with_suffix(".md")
        return _extract_fm_id(full.read_text(encoding="utf-8", errors="replace")[:2048])
    except (OSError, UnicodeDecodeError):
        return None


# Nota (migración a server): las escrituras quirúrgicas (edit_note/append_note/
# patch_frontmatter) NO leen disco local — el gateway lee SU checkout. Solo queda
# _read_doc_id (usado por write_note/delete_note) acoplado al disco del agente;
# se moverá al gateway (resolución por path) en la migración.


# ── LECTURAS (vía gateway) ──────────────────────────────────────────────────────


@mcp.tool()
async def read_note(path: str, graph: bool = False, enrich: bool = False) -> dict:
    """Lee un documento del vault (raw_content + frontmatter).

    Lectura lean por defecto. `graph=true` añade entities + related_docs (contexto
    GraphRAG en una sola llamada); `enrich=true` añade además `graph_summary`
    (síntesis LLM, +1-3s; implica graph). Usa los flags solo cuando necesites
    razonar sobre el entorno del doc, no para lecturas rutinarias.

    path: ruta relativa al ObsidianVault (ej. "lait/proyectos/mi-proj/index.md").
    """
    vault, rel = _vault_and_relpath(path)
    params: dict = {"vault": vault, "path": rel}
    if graph:
        params["graph"] = "true"
    if enrich:
        params["enrich"] = "true"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GATEWAY_URL}/v1/read", headers=_HEADERS, params=params)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_context(vault: str) -> dict:
    """Convenciones del vault: su CONTEXT.md (+ el global de `wiki` si es org).

    Llamar SIEMPRE antes de crear o buscar notas. vault: wiki | lait | melquiades.
    """
    if vault not in _KNOWN_VAULTS:
        raise ValueError(
            f"Vault desconocido: '{vault}'. Válidos: {', '.join(sorted(_KNOWN_VAULTS))}"
        )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        portal = await client.get(
            f"{GATEWAY_URL}/v1/read",
            headers=_HEADERS,
            params={"vault": vault, "path": "CONTEXT.md"},
        )
        portal.raise_for_status()
        result = {"portal": portal.json()}
        if vault != "wiki":
            global_ctx = await client.get(
                f"{GATEWAY_URL}/v1/read",
                headers=_HEADERS,
                params={"vault": "wiki", "path": "CONTEXT.md"},
            )
            if global_ctx.status_code == 200:
                result["global"] = global_ctx.json()
        return result


@mcp.tool()
async def list_notes(
    vault: str,
    path_prefix: str = "",
    kind: str = "",
    limit: int = 100,
) -> dict:
    """Lista documentos indexados de un vault (índice Postgres, no el disco).

    vault: wiki|lait|melquiades. Filtros opcionales: path_prefix (ej. "proyectos/gz-"),
    kind (index|decision|runbook|concept…), limit (1-500).
    """
    params: dict = {"vault": vault, "limit": limit}
    if path_prefix:
        params["path_prefix"] = path_prefix
    if kind:
        params["kind"] = kind
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GATEWAY_URL}/v1/list", headers=_HEADERS, params=params)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def search_notes(
    query: str, vault: str = "all", top_n: int = 8, verbose: bool = False
) -> dict:
    """Búsqueda semántica híbrida GraphRAG: una PREGUNTA en lenguaje natural, no un grep.

    Devuelve una respuesta sintetizada con evidencia (evidence_docs). Úsala para
    "cómo funciona X" / "qué se decidió sobre Y". vault: wiki|lait|melquiades|all.
    top_n: docs a rerankear (1-20). verbose=true añade la traza de razonamiento.
    """
    # El gateway acepta scope ∈ {wiki, lait, melquiades, all}. Pasamos el vault
    # directo para acotar; "all" es el default seguro ante valores desconocidos.
    scope = vault if vault in {"wiki", "lait", "melquiades", "all"} else "all"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/graphrag/query",
            headers=_HEADERS,
            json={
                "request_id": str(uuid4()),
                "question": query,
                "scope": scope,
                "top_n_rerank": top_n,
                "verbose": verbose,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # answer_status distingue una respuesta real de una degradación. Si la
        # síntesis falló, NO leas 'answer' como respuesta (viene vacío): la
        # recuperación sí funcionó, usa evidence_docs.
        if data.get("answer_status") == "synthesis_failed":
            data["message"] = (
                "La recuperación funcionó (ver evidence_docs) pero la síntesis del "
                f"LLM falló ({data.get('synthesis_error')}). No uses 'answer' como "
                "respuesta; léela de evidence_docs."
            )
        return data


@mcp.tool()
async def find_notes(
    query: str, vault: str = "all", kind: str = "", top_k: int = 10, rerank: bool = True
) -> dict:
    """ENCONTRAR documentos rápido: lista rankeada por relevancia, SIN respuesta sintetizada (p95<2s).

    Retrieval-only (vector + grafo + reranker, sin LLM de síntesis). Úsalo para
    LOCALIZAR/LISTAR las notas más relevantes a una consulta y decidir cuál leer.
    Diferencia con search_notes: find_notes = ENCONTRAR (rápido, devuelve una lista
    rankeada de hits con snippet); search_notes = PREGUNTAR (respuesta sintetizada con
    el LLM sobre la evidencia, más lento). Si querés "qué docs hablan de X" → find_notes;
    si querés "explicame X / qué se decidió sobre X" → search_notes.

    Devuelve {results, took_ms, mode}. Cada hit: doc_id, vault, kind, path, title,
    score, snippet (~300 chars), heading_path.

    query: texto de la búsqueda (lenguaje natural o términos).
    vault: acota el scope — wiki|lait|melquiades. "all" (o vacío) = todos los vaults.
    kind: filtro opcional por kind canónico (decision|runbook|concept…); vacío = todos.
    top_k: nº de hits a devolver (1-25, default 10).
    rerank: aplica el reranker para reordenar por relevancia (default true).
    """
    # /v1/search usa vault=NULL para "todos" (NO acepta "all"). Mapeamos "all"/""/
    # valor desconocido → None (omitido = todos, el default seguro); wiki|lait|
    # melquiades se pasan tal cual.
    body: dict = {
        "query": query,
        "top_k": max(1, min(top_k, 25)),
        "rerank": rerank,
    }
    if vault in {"wiki", "lait", "melquiades"}:
        body["vault"] = vault
    if kind:
        body["kind"] = kind
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/search", headers=_HEADERS, json=body
        )
        if resp.status_code >= 400:
            return _gateway_error(resp, "search", vault=vault, kind=kind)
        return resp.json()


@mcp.tool()
async def lint_vault(vault: str = "", kind: str = "") -> dict:
    """Documentos indexados que violan el schema vigente (deuda de frontmatter).

    Revela docs que se leen bien pero fallan al escribir por campos requeridos sin
    migrar; sáldalos con patch_frontmatter. Sin filtros audita los 3 vaults.
    Filtros opcionales: vault (wiki|lait|melquiades), kind (architecture, tool…).
    """
    params: dict = {}
    if vault:
        params["vault"] = vault
    if kind:
        params["kind"] = kind
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GATEWAY_URL}/v1/lint", headers=_HEADERS, params=params)
        if resp.status_code >= 400:
            return _gateway_error(resp, "lint", vault=vault)
        return resp.json()


@mcp.tool()
async def peek_id(vault: str, kind: str) -> dict:
    """Último/próximo doc_id para (vault, kind) SIN consumir uno (fuente autoritativa).

    Para planear paths/cross-links sin contar a mano. `in_sync=false` señala drift.
    vault: wiki|lait|melquiades. kind: canónico (decision|architecture|tool|runbook…).
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{GATEWAY_URL}/v1/ids/peek",
            headers=_HEADERS,
            params={"vault": vault, "kind": kind},
        )
        if resp.status_code >= 400:
            return _gateway_error(resp, "peek_id", vault=vault, kind=kind)
        return resp.json()


@mcp.tool()
async def map_vault(vault: str = "", path: str = "", depth: int = 1) -> dict:
    """Navega el árbol del vault UN NIVEL A LA VEZ (mapa jerárquico, progressive disclosure).

    Empieza SIN argumentos para ver los 3 vaults; luego desciende pasando `vault`
    (raíz del vault) y, opcionalmente, `path` (prefijo de carpeta dentro del vault).
    Cada nodo trae title/description (del frontmatter) para decidir si abrirlo, y las
    carpetas su doc_count recursivo. El index.md se pliega como cabecera de cada
    carpeta; CONTEXT.md aparece como documento.

    Complementa list_notes: list_notes da una lista PLANA filtrable; map_vault da la
    ESTRUCTURA de carpetas para explorar sin cargar el vault entero al contexto.

    vault: wiki|lait|melquiades. Omitido = raíz cross-vault (un nodo por vault).
    path: prefijo de carpeta relativo al vault (ej. "proyectos/focusyn"). Requiere vault.
    depth: niveles a anidar (1-4, default 1). >1 anida 'children'.
    """
    params: dict = {"depth": depth}
    if vault:
        params["vault"] = vault
    if path:
        params["path"] = path
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GATEWAY_URL}/v1/map", headers=_HEADERS, params=params)
        if resp.status_code >= 400:
            return _gateway_error(resp, "map", vault=vault, path=path)
        return resp.json()


@mcp.tool()
async def suggest_entity_aliases(vault: str = "", min_cosine: float = 0.0, limit: int = 50) -> dict:
    """Sugiere pares de entidades casi-sinónimas del grafo para alias SUPERVISADO.

    Dedup de entidad CON confirmación: el gateway propone "X se parece a Y" (vecino
    del mismo tipo por embedding de nombre, sobre el umbral, tras los guards que
    descartan padre/hijo y hermanos enumerados). SOLO sugiere — no fusiona. Revisá
    cada par (mirá el grado: alto = importa más) y, si es el mismo concepto,
    confirmá con confirm_entity_alias; si no, ignorá. "Mapa, no verdad": verificá las
    entidades reales antes de fusionar.

    vault: acota a entidades de ese vault (wiki|lait|melquiades). Omitido = todas.
    min_cosine: override del umbral (0 = usar el default del gateway, ~0.93).
    limit: nº máx de pares (1-200, default 50), ordenados por coseno desc.
    """
    params: dict = {"limit": limit}
    if vault:
        params["vault"] = vault
    if min_cosine > 0:
        params["min_cosine"] = min_cosine
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{GATEWAY_URL}/v1/entities/alias-candidates", headers=_HEADERS, params=params
        )
        if resp.status_code >= 400:
            return _gateway_error(resp, "alias_candidates", vault=vault)
        return resp.json()


@mcp.tool()
async def confirm_entity_alias(keep_entity_id: str, alias_entity_id: str) -> dict:
    """Fusiona DOS entidades confirmadas como la misma (alias supervisado).

    `alias_entity_id` se funde dentro de `keep_entity_id` (survivor): el survivor
    conserva su identidad y absorbe las relaciones del otro; el nombre del alias
    queda en sus `aliases`. Úsalo SOLO tras revisar un par de suggest_entity_aliases
    y confirmar que son el mismo concepto — la fusión MUTA el grafo y se revierte por
    snapshot, no en caliente. Idempotente (re-confirmar → already_merged).

    keep_entity_id: la entidad que SOBREVIVE (la canónica/de mayor grado).
    alias_entity_id: la que se absorbe.
    """
    payload = {"keep_entity_id": keep_entity_id, "alias_entity_id": alias_entity_id}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/entities/alias-confirm", headers=_HEADERS, json=payload
        )
        if resp.status_code >= 400:
            return _gateway_error(resp, "alias_confirm",
                                  keep=keep_entity_id, alias=alias_entity_id)
        return resp.json()


# ── ESCRITURAS (vía gateway: propose + apply) ───────────────────────────────────


def _gateway_error(resp: httpx.Response, stage: str, **extra: object) -> dict:
    """Expone el cuerpo de error del gateway al agente (code, message, request_id)
    en vez de tragárselo con raise_for_status — sin esto un 404/409/410 llega como
    status HTTP pelado y es indiagnosticable desde la sesión."""
    try:
        detail = resp.json()
    except Exception:
        detail = {"raw": resp.text[:500]}
    return {
        "status": "error",
        "stage": stage,
        "http_status": resp.status_code,
        "gateway_error": detail,
        **extra,
    }


@mcp.tool()
async def write_note(path: str, body: str) -> dict:
    """Crea o reemplaza un documento en el vault vía el gateway a2a.

    Garantiza frontmatter canónico, audit trail y commit+push a GitHub. Si el doc
    ya existe (tiene 'id' en frontmatter), hace un update gobernado reutilizando el
    doc_id y su path. No hay edición por str_replace: el `body` es siempre el
    documento completo.

    En creates el path FINAL lo gobierna el gateway: la carpeta sale del kind
    (inferido de la ruta pedida: decisiones/ → decision, runbooks/ → runbook,
    instructivos/<proto>/, pendientes/, propuestas/, etc.) y el nombre del archivo
    del nombre pedido (o del título H1 si el nombre es muy corto). La respuesta
    incluye el `path` definitivo — úsalo para lecturas posteriores.

    Si el body se parece mucho a un doc existente, la respuesta trae
    `possible_duplicates` + `duplicate_hint` (no bloquea): confirmá superseder/
    enlazar o ignorá.

    Args:
        path: ruta relativa al ObsidianVault (ej. "lait/proyectos/mi-proj/index.md")
        body: contenido completo del documento en Markdown
    """
    vault, rel = _vault_and_relpath(path)
    request_id = str(uuid4())
    target_doc_id = _read_doc_id(path)

    # org sólo aplica a vaults de organización (no a la wiki transversal).
    hints: dict = {"target_vault": vault, "project": _project(rel)}
    if vault in {"lait", "melquiades"}:
        hints["org"] = vault
    kind = _infer_kind(vault, rel)
    if kind:
        hints["doc_kind"] = kind

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        propose_resp = await client.post(
            f"{GATEWAY_URL}/v1/write/propose",
            headers=_HEADERS,
            json={
                "request_id": request_id,
                "intent": _build_intent(rel, body),
                "content": body,
                "source_agent": "focusyn",
                "hints": hints,
                "target_doc_id": target_doc_id,
            },
        )
        if propose_resp.status_code >= 400:
            return _gateway_error(propose_resp, "propose", path=path)
        proposal = propose_resp.json()

        if proposal.get("violations"):
            viols = proposal["violations"]
            missing = [
                v.get("field")
                for v in viols
                if v.get("code") == "MISSING_FRONTMATTER" and v.get("field")
            ]
            # En un update con campos requeridos faltantes, el contenido del agente
            # es válido — el bloqueo es deuda de frontmatter del doc. Reencuadra el
            # error como accionable (no como "tu contenido es inválido").
            if target_doc_id and missing:
                return {
                    "status": "frontmatter_debt",
                    "doc_id": target_doc_id,
                    "missing_fields": missing,
                    "violations": viols,
                    "target_path": proposal.get("target_path"),
                    "message": (
                        f"El doc {target_doc_id} tiene deuda de frontmatter: faltan "
                        f"{', '.join(missing)} (campos que el schema vigente exige). Tu "
                        "contenido es válido; sáldalos con patch_frontmatter(path, "
                        "set={...}) y reintenta. No reescribas el doc entero."
                    ),
                }
            return {
                "status": "rejected",
                "violations": viols,
                "target_path": proposal.get("target_path"),
            }
        if proposal.get("requires_approval"):
            return {
                "status": "requires_approval",
                "proposal_id": proposal["proposal_id"],
                "target_path": proposal.get("target_path"),
                "message": (
                    "La propuesta requiere aprobación manual (confidence baja del "
                    "clasificador); no se aplicó. Reintenta con un path más explícito."
                ),
            }

        ik = _idempotency_key(path, body, request_id)
        apply_payload = {"proposal_id": proposal["proposal_id"], "idempotency_key": ik}
        apply_resp = await client.post(
            f"{GATEWAY_URL}/v1/write/apply", headers=_HEADERS, json=apply_payload
        )
        # El gateway responde el propose ANTES de que el commit del proposal sea
        # visible en Postgres; con bodies grandes (~70 KB) la ventana supera el
        # turnaround del apply y devuelve PROPOSAL_NOT_FOUND transitorio. Retry
        # corto hasta que el commit aterrice (idempotente por idempotency_key).
        for _ in range(4):
            if apply_resp.status_code != 404:
                break
            await asyncio.sleep(0.3)
            apply_resp = await client.post(
                f"{GATEWAY_URL}/v1/write/apply", headers=_HEADERS, json=apply_payload
            )
        if apply_resp.status_code >= 400:
            return _gateway_error(
                apply_resp, "apply", proposal_id=proposal["proposal_id"], path=path
            )
        result = apply_resp.json()
        # En updates el doc_id es el target; en creates viene en el frontmatter
        # del preview renderizado (propose no lo devuelve como campo propio).
        doc_id = target_doc_id or _extract_fm_id(proposal.get("rendered_preview", ""))
        out = {
            "status": result["status"],
            "doc_id": doc_id,
            "kind": proposal.get("classification", {}).get("kind"),
            "path": f"{vault}/{result['final_path']}",
            "commit": result["commit_sha"],
        }
        # Deuda de frontmatter preexistente (ratchet): la escritura pasó, pero el
        # doc arrastra campos faltantes. Se informa para que se saneé con
        # patch_frontmatter cuando convenga (no bloquea).
        debt = proposal.get("frontmatter_debt")
        if debt:
            out["frontmatter_debt"] = debt
        # Recordatorios accionables del gateway (ej. ADR recién creado aún no
        # referenciado desde el index del proyecto). No bloquean.
        next_actions = result.get("next_actions")
        if next_actions:
            out["next_actions"] = next_actions
        # Dedup con confirmación (9.5): el gateway detectó docs existentes muy
        # parecidos al body. NO bloquea (la escritura ya pasó); surface para que
        # confirmes una relación o ignores. "Mapa, no verdad": verificá leyendo el
        # doc real antes de superseder/enlazar.
        dups = proposal.get("possible_duplicates")
        if dups:
            out["possible_duplicates"] = dups
            top = dups[0]
            out["duplicate_hint"] = (
                f"El body se parece a {top['doc_id']} (similitud {top['similarity']}). "
                f"Si lo reemplaza: supersede_note; si lo respalda: "
                f"link_notes(relation='{top['suggested_relation']}'); si no, ignorá."
            )
        return out


@mcp.tool()
async def append_note(path: str, content: str) -> dict:
    """Agrega contenido al final de un documento existente vía el gateway.

    Append gobernado server-side: el gateway lee el cuerpo de SU checkout y
    concatena (no reescribe el doc entero ni revalida el frontmatter), así que
    funciona aunque el doc tenga deuda de frontmatter. Preserva el frontmatter.

    Args:
        path: ruta relativa al ObsidianVault (ej. "lait/proyectos/mi-proj/index.md")
        content: contenido a agregar al final
    """
    vault, rel = _vault_and_relpath(path)
    request_id = str(uuid4())
    ik = _idempotency_key(path, content, request_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/write/append",
            headers=_HEADERS,
            json={
                "request_id": request_id,
                "vault": vault,
                "path": rel,
                "content": content,
                "idempotency_key": ik,
            },
        )
        if resp.status_code >= 400:
            return _gateway_error(resp, "append", path=path)
        result = resp.json()
        return {
            "status": result["status"],
            "doc_id": result.get("doc_id"),
            "path": f"{vault}/{result['final_path']}",
            "commit": result["commit_sha"],
        }


@mcp.tool()
async def edit_note(
    path: str, old_string: str, new_string: str, replace_all: bool = False
) -> dict:
    """Edita un documento reemplazando ``old_string`` por ``new_string`` (como el Edit tool).

    Edición quirúrgica del CUERPO sin reinsertar el doc entero: el gateway lee su
    checkout, aplica el reemplazo y commitea+pushea. No revalida el frontmatter
    (funciona sobre docs con deuda). Para cambiar campos del frontmatter usa
    ``patch_frontmatter``, no esto.

    - old_string debe aparecer EXACTO (espacios/indentación incluidos).
    - Si aparece 0 veces → error STRING no encontrado.
    - Si aparece >1 vez y replace_all=False → error de match ambiguo (amplía el
      contexto del old_string o pasa replace_all=True).

    Args:
        path: ruta relativa al ObsidianVault
        old_string: texto exacto a reemplazar (en el body)
        new_string: texto nuevo
        replace_all: si True, reemplaza todas las ocurrencias
    """
    vault, rel = _vault_and_relpath(path)
    request_id = str(uuid4())
    ik = _idempotency_key(path, old_string, new_string, request_id)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/write/patch",
            headers=_HEADERS,
            json={
                "request_id": request_id,
                "vault": vault,
                "path": rel,
                "edits": [
                    {
                        "old_string": old_string,
                        "new_string": new_string,
                        "replace_all": replace_all,
                    }
                ],
                "idempotency_key": ik,
            },
        )
        if resp.status_code >= 400:
            return _gateway_error(resp, "patch", path=path)
        result = resp.json()
        return {
            "status": result["status"],
            "doc_id": result.get("doc_id"),
            "path": f"{vault}/{result['final_path']}",
            "commit": result["commit_sha"],
        }


@mcp.tool()
async def patch_frontmatter(
    path: str, set: dict | None = None, unset: list | None = None
) -> dict:
    """Arregla SOLO campos del frontmatter de un doc (saldar deuda), sin tocar el cuerpo.

    Úsalo cuando una escritura falla con MISSING_FRONTMATTER porque el doc es viejo
    y el schema endureció los campos requeridos de su kind (ej. architecture exige
    depends_on/exposes; tool exige vendor/license/version_seen). Tras corregir, el
    doc vuelve a aceptar write_note/edit_note/append_note normalmente.

    No puede tocar claves de identidad (id/vault/org/project/kind/...). Devuelve
    ``remaining_debt`` con lo que el schema aún exige.

    Args:
        path: ruta relativa al ObsidianVault
        set: dict de campos a fijar/añadir (ej. {"depends_on": [], "exposes": []})
        unset: lista de campos a borrar
    """
    vault, rel = _vault_and_relpath(path)
    request_id = str(uuid4())
    set_fields = set or {}
    unset_fields = unset or []
    ik = _idempotency_key(
        path, str(sorted(set_fields)), str(sorted(unset_fields)), request_id
    )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/write/patch-frontmatter",
            headers=_HEADERS,
            json={
                "request_id": request_id,
                "vault": vault,
                "path": rel,
                "set": set_fields,
                "unset": unset_fields,
                "idempotency_key": ik,
            },
        )
        if resp.status_code >= 400:
            return _gateway_error(resp, "patch_frontmatter", path=path)
        return resp.json()


@mcp.tool()
async def delete_note(path: str, reason: str = "Borrado vía MCP") -> dict:
    """Borra un documento del vault con audit trail vía el gateway a2a.

    Requiere que el doc tenga 'id' en el frontmatter (lo tienen todos los docs
    creados por el gateway) y que esté indexado en GraphRAG.

    Args:
        path: ruta relativa al ObsidianVault
        reason: motivo del borrado (queda en el audit trail)
    """
    doc_id = _read_doc_id(path)
    if not doc_id:
        return {
            "status": "error",
            "message": (
                "No se encontró 'id' en el frontmatter. "
                "El doc puede no estar indexado en el gateway."
            ),
        }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/write/delete",
            headers=_HEADERS,
            json={
                "request_id": str(uuid4()),
                "doc_id": doc_id,
                "reason": reason,
                "idempotency_key": _idempotency_key(path, doc_id, "delete"),
            },
        )
        resp.raise_for_status()
        return resp.json()


_DOC_ID_RE = re.compile(r"^[A-Z]+-[A-Z]+-\d+$")


def _resolve_doc_id(path_or_id: str) -> str | None:
    """Acepta un doc_id canónico directo (MEL-DEC-001) o un path del vault."""
    if _DOC_ID_RE.match(path_or_id):
        return path_or_id
    return _read_doc_id(path_or_id)


@mcp.tool()
async def link_notes(source: str, target: str, relation: str = "related") -> dict:
    """Crea un cross-link gobernado entre dos documentos (frontmatter + grafo).

    Añade el doc_id del target a la lista frontmatter `relation` del documento
    ORIGEN, con commit+push y audit trail. El watcher re-ingesta el doc y el
    link se materializa en el grafo Neo4j. Idempotente: si el link ya existe
    no duplica ni genera commits de ruido.

    Args:
        source: doc origen — doc_id canónico (ej. "MEL-ARCH-022") o ruta
            relativa al ObsidianVault (ej. "melquiades/proyectos/x/index.md")
        target: doc destino — doc_id canónico o ruta, igual que source
        relation: clave de frontmatter donde anotar el link (default "related";
            también p.ej. "depends_on", "exposes" — minúsculas/dígitos/guion bajo)
    """
    source_id = _resolve_doc_id(source)
    target_id = _resolve_doc_id(target)
    missing = [p for p, d in ((source, source_id), (target, target_id)) if not d]
    if missing:
        return {
            "status": "error",
            "message": f"No se pudo resolver doc_id de: {', '.join(missing)} "
            "(¿sin 'id' en frontmatter o aún no indexado?)",
        }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/write/link",
            headers=_HEADERS,
            json={
                "request_id": str(uuid4()),
                "source_doc_id": source_id,
                "target_doc_id": target_id,
                "relation": relation,
                "idempotency_key": _idempotency_key(source_id, target_id, relation),
            },
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def supersede_note(new: str, old: str, reason: str = "") -> dict:
    """Superseción bidireccional ATÓMICA: el ADR ``new`` reemplaza al ``old``.

    En UNA operación el gateway escribe ``supersedes:[old]`` en el nuevo y
    ``superseded_by:[new]`` + ``status:deprecated`` en el viejo (cada lado con
    commit+push+audit). Úsala SIEMPRE para superseder — NO marques el back-pointer
    ni el status a mano: el gateway garantiza la coherencia y rechaza un status
    inválido. Ambos deben ser kind decision/proposal. Idempotente: reintentar no
    duplica.

    Args:
        new: ADR/propuesta NUEVO (el que supersede) — doc_id canónico (ej.
            "MEL-DEC-046") o ruta relativa al ObsidianVault.
        old: ADR/propuesta VIEJO (el superseído) — doc_id canónico o ruta.
        reason: motivo (queda en el audit trail).
    """
    new_id = _resolve_doc_id(new)
    old_id = _resolve_doc_id(old)
    missing = [p for p, d in ((new, new_id), (old, old_id)) if not d]
    if missing:
        return {
            "status": "error",
            "message": f"No se pudo resolver doc_id de: {', '.join(missing)} "
            "(¿sin 'id' en frontmatter o aún no indexado?)",
        }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/write/supersede",
            headers=_HEADERS,
            json={
                "request_id": str(uuid4()),
                "new_doc_id": new_id,
                "old_doc_id": old_id,
                "reason": reason,
                "idempotency_key": _idempotency_key(new_id, old_id, "supersede"),
            },
        )
        if resp.status_code >= 400:
            return _gateway_error(resp, "supersede", new=new_id, old=old_id)
        return resp.json()


@mcp.tool()
async def push_vault(vault: str) -> dict:
    """Empuja a GitHub los commits locales pendientes de un vault.

    En el flujo normal cada escritura ya pushea sola; esto cubre el caso de
    commits que quedaron sin pushear (push fallido por red/credenciales,
    commits manuales en el checkout). Si el remoto está al día devuelve
    `pushed: false` sin tocar nada.

    Args:
        vault: wiki | lait | melquiades
    """
    if vault not in _KNOWN_VAULTS:
        raise ValueError(
            f"Vault desconocido: '{vault}'. Válidos: {', '.join(sorted(_KNOWN_VAULTS))}"
        )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/sync/push", headers=_HEADERS, json={"vault": vault}
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_contracts(doc: str, role: str = "") -> dict:
    """Bloques tipados (json/yaml/openapi/proto/mermaid) que el ingest extrajo del doc.

    Para consumo programático sin parsear el MD. doc: doc_id canónico (ej.
    "MEL-HTTP-050") o ruta. role: filtro opcional (schema|example|config|…).
    """
    doc_id = _resolve_doc_id(doc)
    if not doc_id:
        return {"status": "error", "message": f"No se pudo resolver doc_id de: {doc}"}
    url = f"{GATEWAY_URL}/v1/contracts/{doc_id}"
    if role:
        url += f"/{role}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()
        return resp.json()


# ── ATTACHMENTS (vía gateway → NAS, fuera de Git, multimodal) ───────────────────


@mcp.tool()
async def add_attachment(
    source_path: str,
    vault: str,
    doc_id: str = "",
    alt: str = "",
    filename: str = "",
) -> dict:
    """Sube un binario (imagen, PDF, audio…) al NAS vía el gateway y retorna su referencia markdown.

    NO copia al disco ni usa wikilinks Obsidian: el binario vive fuera de Git en el
    NAS y se referencia por un proxy estable del gateway. Si pasas `doc_id` y el
    archivo es una imagen, se indexa multimodal (recuperable cross-modal por
    search_notes). Idempotente: re-subir el mismo (source_path, vault) devuelve el
    attachment existente (status="already_uploaded") sin duplicar.

    Pega `markdown_ref` en la nota (con edit_note/append_note/write_note); el alt
    text debe describir el CONTENIDO, no el nombre del archivo.

    source_path: ruta absoluta al binario en el host.
    vault: vault destino (wiki|lait|melquiades) — aísla el storage.
    doc_id: doc_id del doc que lo referencia (ej. "MEL-CONCEPT-012"); requerido para
        la indexación multimodal de imágenes.
    alt: texto alternativo que describe el contenido (va en el markdown_ref).
    filename: nombre a registrar; default = nombre del source_path.
    """
    if vault not in _KNOWN_VAULTS:
        raise ValueError(
            f"Vault desconocido: '{vault}'. Válidos: {', '.join(sorted(_KNOWN_VAULTS))}"
        )
    src = Path(source_path).expanduser().resolve()
    if not src.is_file():
        return {"status": "error", "message": f"No es un archivo: {source_path}"}
    name = filename or src.name
    try:
        content = src.read_bytes()
    except OSError as exc:
        return {"status": "error", "message": f"No se pudo leer {source_path}: {exc}"}

    # idempotency_key determinista por (path absoluto, vault): re-subir el mismo
    # binario desde el mismo origen devuelve el existente, no uno nuevo.
    ik = _idempotency_key(str(src), vault, "attachment")

    # GOTCHA multipart: NO mandar el Content-Type: application/json del _HEADERS
    # global — httpx fija el boundary multipart él solo cuando usas files=. Solo
    # va X-Agent-Key (auth); el Content-Type lo pone httpx.
    files = {"file": (name, content)}
    data: dict[str, str] = {"vault": vault, "idempotency_key": ik}
    if doc_id:
        data["doc_id"] = doc_id
    if alt:
        data["alt"] = alt

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/write/attachment",
            headers={"X-Agent-Key": GATEWAY_KEY},
            files=files,
            data=data,
        )
        if resp.status_code >= 400:
            return _gateway_error(resp, "attachment", source_path=source_path, vault=vault)
        result = resp.json()
        return {
            "status": result["status"],
            "file_id": result["file_id"],
            "markdown_ref": result["markdown_ref"],
            "content_type": result.get("content_type"),
            "size_bytes": result.get("size_bytes"),
        }


@mcp.tool()
async def delete_attachment(file_id: str) -> dict:
    """Borra una imagen/binario suelto del NAS por file_id (DELETE /v1/attachment/{id}).

    El file_id es el UUID que aparece en la ref markdown ![alt](/v1/attachment/{file_id}).
    Idempotente (already_deleted si ya no está). Para borrar TODOS los adjuntos de un doc,
    usa delete_note (cascade automático).
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.delete(
            f"{GATEWAY_URL}/v1/attachment/{file_id}", headers=_HEADERS
        )
        if resp.status_code >= 400:
            return _gateway_error(resp, "delete_attachment", file_id=file_id)
        return resp.json()


if __name__ == "__main__":
    mcp.run()
