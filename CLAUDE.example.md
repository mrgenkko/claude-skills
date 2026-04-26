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
│   ├── mcp-databases.md         ← cómo crear MCP de bases de datos
│   ├── mcp-gcloud.md            ← cómo crear MCP de Google Cloud
│   └── mcp-ssh.md               ← cómo crear MCP para servidores SSH
├── examples/
│   ├── mcp-database/server.py   ← MCP mínimo para PostgreSQL
│   ├── mcp-gcloud/server.py     ← MCP mínimo para gcloud
│   ├── mcp-ssh/server.py        ← MCP mínimo para SSH
│   └── mcp-obsidian/server.py   ← MCP mínimo para vault de Obsidian
└── deployed/
    ├── gcloud/server.py         ← servidor gcloud en producción
    ├── postgres/server.py       ← servidor postgres en producción
    ├── ssh/server.py            ← servidor SSH en producción
    └── obsidian/server.py       ← servidor Obsidian en producción
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
| `obsidian`          | Python custom | Vault: ~/ObsidianVault     |

El servidor gcloud está en: `~/.claude/mcp-servers/gcloud/server.py`  
El servidor postgres está en: `~/.claude/mcp-servers/postgres/server.py`  
El servidor SSH está en: `~/.claude/mcp-servers/ssh/server.py`  
El servidor Obsidian está en: `~/.claude/mcp-servers/obsidian/server.py`  
Ambas instancias gcloud usan el mismo binario con distintos `--project` y `--account`.  
Ambas instancias postgres usan el mismo binario con distintos `--db`.

## Quirk importante: VSCode Extension

La extensión VSCode de Claude Code **ignora** `~/.claude/mcp.json` y `~/.claude/settings.json`.
Los MCPs se deben registrar directamente en `~/.claude.json` bajo `projects["/ruta/proyecto"]["mcpServers"]`.

Usar `scripts/add-mcp-to-project.py` para registrar los MCPs en un proyecto nuevo.

## Convenciones

- Los servidores MCP custom van en `~/.claude/mcp-servers/<nombre>/server.py`
- El venv para los MCP Python es `~/Mrgenkko Skills/.venv`
- Las service account keys van en `~/keys/<proyecto>/` (nunca en el repo)
- Las credenciales van en `scripts/secrets.json` (gitignoreado)
