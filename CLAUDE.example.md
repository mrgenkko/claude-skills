# Mi Skills Workspace — Contexto para Claude Code

## ¿Qué es este repositorio?

Directorio de trabajo central para skills, guías y servidores MCP usados con Claude Code.
Contiene las herramientas que se despliegan en mis proyectos.

## Estructura

```
Skills/
├── CLAUDE.md                    ← tu archivo real (gitignoreado)
├── CLAUDE.example.md            ← esta plantilla
├── README.md                    ← visión general
├── requirements.txt             ← dependencias Python de los MCPs
├── .gitignore
├── scripts/
│   ├── add-mcp-to-project.py    ← script para registrar MCPs en un proyecto
│   ├── secrets.json             ← credenciales reales (gitignoreado)
│   └── secrets.example.json     ← plantilla de credenciales
├── guides/
│   ├── cursor.md                ← MCP global en Cursor (~/.cursor/mcp.json)
│   ├── mcp-databases.md         ← cómo crear MCP de bases de datos
│   ├── mcp-gcloud.md            ← cómo crear MCP de Google Cloud
│   ├── mcp-lottie-creator.md    ← LottieFiles Creator (npm + browser)
│   ├── mcp-obsidian.md          ← vault Obsidian
│   ├── mcp-ssh.md               ← cómo crear MCP para servidores SSH
│   ├── mcp-gh.md                ← GitHub CLI: despliegues/Actions/PRs + escritura gated
│   └── mcp-webprobe.md          ← diagnóstico de landings (Playwright)
├── examples/
│   ├── mcp-database/server.py   ← MCP mínimo para PostgreSQL
│   ├── mcp-gcloud/server.py     ← MCP mínimo para gcloud
│   ├── mcp-ssh/server.py        ← MCP mínimo para SSH
│   └── mcp-obsidian/server.py   ← MCP mínimo para vault de Obsidian
└── deployed/
    ├── gcloud/server.py         ← servidor gcloud en producción
    ├── postgres/server.py       ← servidor postgres en producción
    ├── ssh/server.py            ← servidor SSH en producción
    ├── obsidian/server.py       ← servidor Obsidian en producción
    ├── gh/server.py             ← GitHub CLI (despliegues/Actions/PRs + escritura gated)
    └── webprobe/server.py       ← diagnóstico de landings (Playwright)
```

## MCPs activos

Completar con los MCPs propios. Ejemplo de estructura:

| Nombre              | Tipo          | Proyecto / BD              |
|---------------------|---------------|----------------------------|
| `gcloud-proyecto-a` | Python custom | mi-proyecto-gcp            |
| `gcloud-proyecto-b` | Python custom | mi-otro-proyecto-gcp       |
| `postgres-bd-1`     | Python custom | DB: mi_base_de_datos       |
| `postgres-bd-2`     | Python custom | DB: mi_otra_base           |
| `ssh-servidor-01`   | Python custom | 192.168.1.100              |
| `obsidian`              | Python custom | Vault: ~/ObsidianVault     |
| `lottiefiles-creator`   | npm oficial   | Creator + `~/.claude/mcp-servers/lottie/` |
| `gh-*`                  | Python custom | GitHub CLI multi-account (token por org): despliegues/Actions/PRs (lectura) + merge/release (escritura gated) |
| `webprobe`              | Python custom | Diagnóstico de landings (Playwright): FPS/jank/INP/latencia de botón + entrance-check |

El servidor gcloud está en: `~/.claude/mcp-servers/gcloud/server.py`  
El servidor postgres está en: `~/.claude/mcp-servers/postgres/server.py`  
El servidor SSH está en: `~/.claude/mcp-servers/ssh/server.py`  
El servidor Obsidian está en: `~/.claude/mcp-servers/obsidian/server.py`  
El servidor gh está en: `~/.claude/mcp-servers/gh/server.py` (instalar con `scripts/install-gh-mcp.sh`). Multi-account por **token por instancia** (`GH_TOKEN`): `gh` es stateless, sin deriva de cuenta. Una instancia por org; cada llamada recibe `repo` (`owner/repo`). Mutaciones (`pr merge`, `release create`, `workflow run`...) bloqueadas salvo `allow_write: true`. Ver `guides/mcp-gh.md`.  
Lottie: `~/.claude/mcp-servers/lottie/node_modules/@lottiefiles/creator-mcp/dist/index.mjs` (instalar con `scripts/install-lottie-mcp.sh`).  
El servidor webprobe está en: `~/.claude/mcp-servers/webprobe/server.py` (instalar con `scripts/install-webprobe-mcp.sh`; agrega `playwright` al venv + `playwright install chromium`). Una sola instancia genérica; el agente pasa la URL en `goto`. Ver `guides/mcp-webprobe.md`.  
Ambas instancias gcloud usan el mismo binario con distintos `--project` y `--account`.  
Ambas instancias postgres usan el mismo binario con distintos `--db`.  
En **Cursor**, Lottie y Obsidian suelen ir en `~/.cursor/mcp.json` (global). Ver `guides/mcp-lottie-creator.md` y `guides/cursor.md`.

## Quirk importante: VSCode Extension

La extensión VSCode de Claude Code **ignora** `~/.claude/mcp.json` y `~/.claude/settings.json`.
Los MCPs se deben registrar directamente en `~/.claude.json` bajo `projects["/ruta/proyecto"]["mcpServers"]`.

Usar `scripts/add-mcp-to-project.py` para registrar los MCPs en un proyecto nuevo.

## MCP Obsidian y vault

El MCP `obsidian` expone el vault de Obsidian con estas herramientas:

| Tool | Uso |
|---|---|
| `get_context` | Leer `wiki/CONTEXT.md` (convenciones globales); con `org="<org>"` añade el portal de la org — llamar siempre antes de crear o buscar notas |
| `read_note` | Leer una nota por path relativo al vault |
| `write_note` | Crear o reemplazar una nota completa |
| `append_note` | Agregar contenido al final de una nota existente |
| `search_notes` | Grep recursivo en `.md` del vault |
| `list_notes` | Listar archivos `.md` de una carpeta |
| `delete_note` | Eliminar nota o carpeta entera |
| `add_attachment` | Copiar imagen/PDF al vault y retornar sintaxis `![[filename]]` |

### Animaciones Lottie en el vault

Con `lottiefiles-creator` activo: exportar desde Creator → `add_attachment` con el `.json` / `.lottie` → referenciar en la nota con `![[archivo]]`. Detalle: `guides/mcp-lottie-creator.md` (sección «Uso con Obsidian»).

### Estructura del vault

```
~/ObsidianVault/         ← contenedor (no es repo Git)
├── wiki/               ← repo vault-wiki: conocimiento acumulativo
│   ├── CONTEXT.md      ← convenciones globales (leer con get_context)
│   ├── templates/      ← plantillas transversales (ADR, documentación)
│   ├── index.md        ← catálogo maestro por categoría
│   ├── log.md          ← registro append-only de ingestas y queries
│   ├── schema.md       ← convenciones, plantilla de página y protocolos
│   ├── conceptos/
│   ├── herramientas/
│   ├── personas/
│   ├── patrones/
│   └── attachments/    ← imágenes y PDFs (![[filename]])
├── claude-memory/      ← symlinks a memorias de proyecto (no editar)
└── <org>/              ← una carpeta (repo) por organización
    ├── CONTEXT.md      ← portal de la org (get_context org="<org>")
    └── ecosistema/     ← infra propia de la org
```

## Wiki de Conocimiento

La carpeta `wiki/` es un cerebro externo acumulativo — crece con cada fuente procesada y mejora con el tiempo.

### Cuándo hacer ingest

- Al recibir un artículo, paper, video, imagen o URL relevante
- Al descubrir un patrón de arquitectura o técnica no trivial
- Al resolver un problema que otros podrían volver a enfrentar

### Cuándo NO ingestar

- Info específica de un proyecto → `<org>/proyectos/<nombre>/` en el vault
- Decisiones de negocio → `ecosistema/` en el vault
- Estado efímero de la conversación → `claude-memory/` (automático)

### Protocolo ingest

1. Leer/ver la fuente completa antes de ingestar
2. Identificar páginas wiki afectadas (mínimo 3, máximo 15)
3. Actualizar cada página: agregar/refinar secciones + cross-refs bidireccionales
4. Si el concepto no existe → crear página con la plantilla de `wiki/schema.md`
5. Registrar en `wiki/log.md` antes de cerrar la sesión
6. Si hay categoría nueva → actualizar `wiki/index.md`

### Protocolo imágenes y attachments

1. Llamar `add_attachment(source_path, filename)` para copiar al vault
2. Leer la imagen con el tool `Read` para extraer su contenido
3. Referenciar en la nota con `![[filename.png]]`

### Protocolo lint (al detectar >30 días sin lint en `wiki/log.md`)

1. Páginas huérfanas (sin incoming links)
2. Contradicciones entre páginas relacionadas
3. Claims sin fuente en frontmatter `sources`
4. Reportar hallazgos al usuario — no corregir automáticamente

## Convenciones

- Los servidores MCP custom van en `~/.claude/mcp-servers/<nombre>/server.py`
- El venv para los MCP Python es `~/Mrgenkko Skills/.venv`
- Las service account keys van en `~/keys/<proyecto>/` (nunca en el repo)
- Las credenciales van en `scripts/secrets.json` (gitignoreado)
