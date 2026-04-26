# Mrgenkko Skills

Repositorio de herramientas, guías y servidores MCP para Claude Code.

## Estructura

```
Skills/
├── CLAUDE.example.md            ← plantilla de contexto para Claude (tu CLAUDE.md va en .gitignore)
├── README.md
├── requirements.txt             ← dependencias Python de los MCPs
├── .gitignore
├── scripts/
│   ├── add-mcp-to-project.py   ← registra MCPs en un proyecto nuevo
│   ├── secrets.json            ← credenciales y configuración (gitignoreado)
│   └── secrets.example.json    ← plantilla de secrets
├── guides/
│   ├── mcp-databases.md        ← cómo crear MCPs de bases de datos
│   └── mcp-gcloud.md           ← cómo crear MCPs para Google Cloud
├── examples/
│   ├── mcp-database/server.py  ← MCP mínimo para PostgreSQL
│   └── mcp-gcloud/server.py    ← MCP mínimo para gcloud CLI
└── deployed/
    ├── gcloud/server.py        ← servidor gcloud (multi-proyecto)
    └── postgres/server.py      ← servidor postgres (read + write)
```

---

## Configuración inicial (una sola vez)

**1. Crear el entorno Python:**

```bash
cd ~/Skills
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

```bash
python3 ~/Skills/scripts/add-mcp-to-project.py /ruta/absoluta/al/proyecto
```

Lee `scripts/secrets.json`, registra todos los MCPs definidos en `~/.claude.json` para ese proyecto y reiniciar Claude Code (VSCode).

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

---

## Archivos MCP en el sistema

```
~/.claude/
├── mcp-servers/
│   ├── gcloud/server.py    ← servidor gcloud activo
│   └── postgres/server.py  ← servidor postgres activo
└── .claude.json            ← registro de MCPs por proyecto (VSCode)
```

> **Quirk VSCode:** la extensión ignora `~/.claude/settings.json`.
> Los MCPs deben registrarse en `~/.claude.json` por proyecto.
> Usar `scripts/add-mcp-to-project.py` para automatizarlo.
