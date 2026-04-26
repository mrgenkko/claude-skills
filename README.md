# Mrgenkko Skills

Repositorio de herramientas, guías y servidores MCP para Claude Code, orientado a mejorar la experiencia de trabajo en la extensión VSCode.

Incluye servidores MCP para conectar Claude a bases de datos PostgreSQL, proyectos de Google Cloud y servidores SSH, junto con scripts para registrarlos en cada proyecto de trabajo.

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

---

## Archivos MCP en el sistema

Los servidores MCP viven en `~/.claude/mcp-servers/` (fuera de este repositorio).  
La configuración por proyecto se guarda en `~/.claude.json` (también fuera del repo).

```
~/                              ← home directory
├── .claude.json                ← config global de Claude Code (VSCode): MCPs por proyecto
└── .claude/
    └── mcp-servers/
        ├── gcloud/server.py    ← servidor gcloud activo
        ├── postgres/server.py  ← servidor postgres activo
        └── ssh/server.py       ← servidor SSH activo
```
