# Mrgenkko Skills

Repositorio de herramientas, guías y servidores MCP para Claude Code, orientado a mejorar la experiencia de trabajo en la extensión VSCode.

Incluye servidores MCP para conectar Claude a bases de datos PostgreSQL, proyectos de Google Cloud, servidores SSH, vaults de Obsidian, LottieFiles Creator y Blender, junto con scripts y guías para registrarlos en cada proyecto de trabajo.

**Cuándo ejecutar `add-mcp-to-project.py`:**  
Una vez por proyecto de VSCode que quieras que tenga acceso a los MCPs. No hace falta repetirlo a menos que cambies credenciales (usa `--update`) o agregues un proyecto nuevo. Los proyectos que no lo tengan registrado simplemente no verán los MCPs.

## Estructura

```
Mrgenkko Skills/
├── CLAUDE.example.md            ← plantilla de contexto para Claude (tu CLAUDE.md va en .gitignore)
├── README.md
├── requirements.txt             ← dependencias Python de los MCPs
├── .gitignore
├── scripts/
│   ├── add-mcp-to-project.py   ← registra MCPs en un proyecto nuevo
│   ├── install-lottie-mcp.sh   ← instala @lottiefiles/creator-mcp en ~/.claude/mcp-servers/lottie
│   ├── run-lottie-mcp.sh       ← arranque Lottie para Claude Code (stdio)
│   ├── secrets.json            ← credenciales y configuración (gitignoreado)
│   └── secrets.example.json    ← plantilla de secrets
├── guides/
│   ├── cursor.md               ← MCP en Cursor IDE (~/.cursor/mcp.json, global)
│   ├── mcp-databases.md        ← cómo crear MCPs de bases de datos
│   ├── mcp-blender.md          ← Blender Lab oficial (add-on + servidor MCP)
│   ├── mcp-gcloud.md           ← cómo crear MCPs para Google Cloud
│   ├── mcp-lottie-creator.md   ← LottieFiles Creator (npm + browser bridge)
│   ├── mcp-obsidian.md         ← MCP vault Obsidian raw (legacy/fallback)
│   ├── focusyn.md     ← MCP vault Obsidian vía gateway a2a (reads + writes)
│   └── mcp-ssh.md              ← cómo crear MCPs para servidores SSH
├── examples/
│   ├── mcp-database/server.py      ← MCP mínimo para PostgreSQL
│   ├── mcp-gcloud/server.py        ← MCP mínimo para gcloud CLI
│   ├── mcp-ssh/server.py           ← MCP mínimo para SSH
│   ├── mcp-obsidian/server.py      ← MCP mínimo para vault de Obsidian (raw)
│   ├── focusyn/server.py  ← MCP cliente del gateway a2a (reads + writes)
│   └── mcp-webprobe/server.py      ← MCP mínimo de diagnóstico de landings (interacción + web vitals)
└── deployed/
    ├── gcloud/server.py        ← servidor gcloud (multi-proyecto)
    ├── postgres/server.py      ← servidor postgres (read + write)
    ├── ssh/server.py           ← servidor SSH (shell + SFTP)
    ├── obsidian/server.py      ← servidor Obsidian raw (legacy/fallback)
    ├── focusyn/server.py  ← servidor Obsidian vía gateway a2a (reads + writes auditados)
    └── webprobe/server.py      ← diagnóstico de landings (Playwright): feel/perf + interacción
```

---

## Configuración inicial (una sola vez)

**1. Crear el entorno Python:**

```bash
cd ~/Mrgenkko\ Skills
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**2. Configurar credenciales:**

```bash
cp scripts/secrets.example.json scripts/secrets.json
# Editar secrets.json con tus proyectos, BDs y credenciales
```

**3. Copiar contexto de Claude:**

```bash
cp CLAUDE.example.md CLAUDE.md
# Editar CLAUDE.md con tus MCPs activos
```

---

## Agregar MCPs a un proyecto nuevo

**Por qué hace falta este paso:**  
La extensión VSCode de Claude Code no lee `~/.claude/settings.json` ni `~/.claude/mcp.json` — esos archivos los usa el CLI de línea de comandos, no la extensión. La extensión guarda su configuración en `~/.claude.json` (archivo en tu home directory, fuera de cualquier proyecto), donde cada proyecto tiene su propia entrada con los MCPs que puede usar. Sin registrar el proyecto ahí, Claude no ve ningún MCP aunque estén instalados.

`add-mcp-to-project.py` automatiza ese registro: lee `secrets.json`, construye la configuración de cada servidor y la escribe en `~/.claude.json` para el proyecto que le indiques.

```bash
# Ver todos los proyectos registrados y qué MCPs tienen
python3 ~/Mrgenkko\ Skills/scripts/add-mcp-to-project.py

# Registrar todos los MCPs en un proyecto nuevo
python3 ~/Mrgenkko\ Skills/scripts/add-mcp-to-project.py /ruta/absoluta/al/proyecto

# Actualizar entradas ya existentes (cuando cambian credenciales o argumentos)
python3 ~/Mrgenkko\ Skills/scripts/add-mcp-to-project.py /ruta/absoluta/al/proyecto --update
```

Por defecto no sobreescribe entradas existentes; usa `--update` para forzarlo.  
Después de ejecutarlo, **reiniciar Claude Code en VSCode** para que carguen los nuevos MCPs.

---

## Servidores MCP incluidos

### gcloud (`deployed/gcloud/server.py`)

Servidor Python para controlar proyectos GCP via `gcloud` CLI.  
Una instancia por proyecto GCP, mismo binario con distintos argumentos.

| Tool               | Descripción                              |
|--------------------|------------------------------------------|
| `gcloud`           | Ejecuta cualquier comando gcloud CLI     |
| `cloud_run_status` | Estado de servicios Cloud Run            |
| `gcloud_logs`      | Logs de un servicio Cloud Run            |
| `secret_list`      | Lista secrets de Secret Manager          |
| `secret_get`       | Obtiene el valor de un secret            |
| `shell`            | Ejecuta un comando shell en el workdir   |

### postgres (`deployed/postgres/server.py`)

Servidor Python custom para PostgreSQL. Soporta lectura y escritura.  
Una instancia por base de datos, mismo binario con distintos `--db`.

| Tool       | Descripción                                       |
|------------|---------------------------------------------------|
| `query`    | Ejecuta cualquier SQL (SELECT/INSERT/UPDATE/DELETE/DDL) |
| `tables`   | Lista tablas del schema público con tamaño        |
| `describe` | Describe columnas de una tabla                    |

### ssh (`deployed/ssh/server.py`)

Servidor Python para controlar servidores Ubuntu/Linux privados vía SSH.  
Una instancia por servidor, mismo binario con distintos `--host` y `--user`.  
Autenticación por clave privada (`--key-file`) o contraseña (`--password`).

| Tool         | Descripción                                      |
|--------------|--------------------------------------------------|
| `shell`      | Ejecuta un comando bash en el servidor remoto    |
| `read_file`  | Lee el contenido de un archivo remoto (SFTP)     |
| `write_file` | Escribe contenido a un archivo remoto (SFTP)     |
| `list_dir`   | Lista el contenido de un directorio remoto       |

### focusyn (`deployed/focusyn/server.py`)

Cliente HTTP del `focusyn`. **Reemplazo completo** del MCP `obsidian` raw:
reads y writes del vault pasan por el gateway (audit trail + GraphRAG). Configurado por
variables de entorno (`FOCUSYN_GATEWAY_URL`, `FOCUSYN_GATEWAY_KEY`, `OBSIDIAN_VAULT`).
Ver `guides/mcp-focusyn.md`.

| Tool             | Descripción                                                      |
|------------------|------------------------------------------------------------------|
| `get_context`    | CONTEXT.md del vault + global de `wiki` — llamar siempre primero  |
| `read_note`      | Lee un doc + entidades GraphRAG + documentos relacionados         |
| `list_notes`     | Lista docs indexados de un vault (índice Postgres)               |
| `search_notes`   | Búsqueda semántica GraphRAG (pregunta en lenguaje natural)        |
| `write_note`     | Crea o reemplaza un doc (propose+apply, audit, commit+push)       |
| `append_note`    | Agrega contenido al final (lee + reescribe completo)             |
| `delete_note`    | Elimina un doc (requiere `id` en frontmatter + indexado)          |
| `add_attachment` | Copia un binario al vault y retorna el wikilink `![[...]]`        |

### obsidian (`deployed/obsidian/server.py`) — legacy/fallback

Servidor Python raw que lee y escribe notas directo al filesystem de un vault local
(`--vault-path`). Reemplazado por `focusyn` en los proyectos activos; se conserva
solo como fallback local. Tools: `read_note`, `write_note`, `append_note`, `edit_note`,
`search_notes` (grep), `list_notes`, `delete_note`, `add_attachment`.

### lottiefiles-creator (npm oficial)

Paquete `@lottiefiles/creator-mcp` instalado en `~/.claude/mcp-servers/lottie/`. Bridge con [creator.lottiefiles.com](https://creator.lottiefiles.com) (WebSocket + stdio). No va en `secrets.json` ni en `add-mcp-to-project.py`.

| Contexto | Configuración |
|----------|----------------|
| **Cursor** (todos los workspaces) | `~/.cursor/mcp.json` → ver `guides/cursor.md` y `guides/mcp-lottie-creator.md` |
| **Claude Code VSCode** | `~/.claude.json` por proyecto → `scripts/run-lottie-mcp.sh` |

Instalación: `bash scripts/install-lottie-mcp.sh`

### blender (Blender Lab oficial)

MCP oficial de Blender Lab para conectar un cliente MCP con una sesión local de Blender.
Usa un add-on dentro de Blender y un servidor MCP local que se comunica por `stdio` hacia el cliente y por TCP hacia Blender.

No va en `secrets.json` ni en `add-mcp-to-project.py`; se registra manualmente porque depende del artefacto oficial instalado (`.mcpb` o ejecutable local `blender-mcp`).

| Contexto | Configuración |
|----------|----------------|
| **Cursor** (todos los workspaces o por proyecto) | `~/.cursor/mcp.json` o importación del bundle oficial `.mcpb` |
| **Claude Code VSCode** | `~/.claude.json` por proyecto |
| **Blender** | add-on / extensión oficial de Blender Lab con host y puerto configurados |

Guía completa: `guides/mcp-blender.md`

---

## Archivos MCP en el sistema

Los servidores MCP viven en `~/.claude/mcp-servers/` (fuera de este repositorio).  
La configuración por proyecto se guarda en `~/.claude.json` (también fuera del repo).

```
~/                              ← home directory
├── .cursor/mcp.json            ← MCPs globales de Cursor IDE (gcloud, postgres, obsidian, lottie, blender, …)
├── .claude.json                ← config global de Claude Code (VSCode): MCPs por proyecto
└── .claude/
    └── mcp-servers/
        ├── gcloud/server.py    ← servidor gcloud activo
        ├── postgres/server.py  ← servidor postgres activo
        ├── ssh/server.py        ← servidor SSH activo
        ├── focusyn/server.py ← servidor Obsidian activo (vía gateway a2a, reads + writes)
        ├── obsidian/server.py   ← servidor Obsidian raw (legacy/fallback)
        ├── lottie/             ← npm: @lottiefiles/creator-mcp (node_modules/…/dist/index.mjs)
        └── blender/            ← instalación local del servidor MCP oficial (si usas stdio en vez de `.mcpb`)
```
