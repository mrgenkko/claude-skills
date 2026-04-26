# Guía: MCP de Google Cloud

## ¿Qué hace el servidor gcloud?

Permite a Claude ejecutar comandos `gcloud` CLI y `shell` directamente sobre un proyecto GCP.
El mismo binario (`server.py`) sirve para múltiples proyectos — se diferencia por argumentos.

## Tools disponibles

| Tool              | Descripción                                              |
|-------------------|----------------------------------------------------------|
| `gcloud`          | Ejecuta cualquier comando `gcloud` arbitrario            |
| `cloud_run_status`| Lista servicios Cloud Run con estado y URL               |
| `gcloud_logs`     | Logs de un servicio Cloud Run por nombre                 |
| `secret_list`     | Lista secrets de Secret Manager                          |
| `secret_get`      | Obtiene el valor de un secret (versión específica)       |
| `shell`           | Ejecuta un comando de shell en el `workdir` del proyecto |

## Argumentos del servidor

| Argumento    | Requerido | Descripción                                      |
|--------------|-----------|--------------------------------------------------|
| `--project`  | Sí        | ID del proyecto GCP (ej: `gz-procurement`)       |
| `--region`   | Sí        | Región por defecto (ej: `us-east4`)              |
| `--workdir`  | Sí        | Directorio de trabajo para comandos `shell`      |
| `--account`  | No        | Service account email para autenticarse          |
| `--key-file` | No        | Ruta al JSON de la key de la service account     |

Si se pasan `--account` y `--key-file`, el servidor activa la cuenta automáticamente al arrancar.

## Instalar en un proyecto nuevo

### 1. Copiar el servidor

```bash
mkdir -p ~/.claude/mcp-servers/gcloud
cp /home/melquiades/.claude/mcp-servers/gcloud/server.py ~/.claude/mcp-servers/gcloud/
```

### 2. Instalar dependencias Python

```bash
# Usar el venv del proyecto o crear uno nuevo
pip install mcp
```

El venv de MCPs: `~/Mrgenkko Skills/.venv`

### 3. Registrar en `~/.claude.json`

```json
{
  "projects": {
    "/ruta/al/proyecto": {
      "mcpServers": {
        "gcloud-mi-proyecto": {
          "type": "stdio",
          "command": "/home/melquiades/Mrgenkko Skills/.venv/bin/python",
          "args": [
            "/home/melquiades/.claude/mcp-servers/gcloud/server.py",
            "--project=mi-proyecto-id",
            "--region=us-east4",
            "--workdir=/home/melquiades/mi-proyecto",
            "--account=mi-sa@mi-proyecto-id.iam.gserviceaccount.com",
            "--key-file=/home/melquiades/keys/mi-proyecto/mi-sa.json"
          ],
          "env": {}
        }
      }
    }
  }
}
```

### 4. Agregar con el script automático

Si el proyecto usa los MCPs estándar:

```bash
python3 "~/Mrgenkko Skills/scripts/add-mcp-to-project.py" /ruta/al/proyecto
```

---

## Crear una instancia para un proyecto GCP nuevo

### Requisitos previos

1. Tener `gcloud` CLI instalado y configurado
2. Tener una service account con los permisos necesarios
3. Descargar el JSON de la key a `~/keys/<proyecto>/`

### Permisos mínimos recomendados para la SA

- `roles/run.viewer` — ver servicios Cloud Run
- `roles/logging.viewer` — leer logs
- `roles/secretmanager.secretAccessor` — leer secrets

### Añadir el servidor al script `scripts/add-mcp-to-project.py`

Agregar una entrada en `MCP_SERVERS`:

```python
"gcloud-nuevo-proyecto": {
    "type": "stdio",
    "command": "/home/melquiades/Mrgenkko Skills/.venv/bin/python",
    "args": [
        "/home/melquiades/.claude/mcp-servers/gcloud/server.py",
        "--project=nuevo-proyecto-id",
        "--region=us-east4",
        "--workdir=/home/melquiades/nuevo-proyecto",
        "--account=sa@nuevo-proyecto-id.iam.gserviceaccount.com",
        "--key-file=/home/melquiades/keys/nuevo-proyecto/sa.json"
    ],
    "env": {}
}
```

---

## Extender el servidor con nuevas tools

Editar `deployed/gcloud/server.py` (o el original en `~/.claude/mcp-servers/gcloud/server.py`).

### Agregar un tool nuevo

**1. Declararlo en `list_tools()`:**

```python
types.Tool(
    name="cloud_sql_list",
    description="Lista instancias Cloud SQL del proyecto.",
    inputSchema={"type": "object", "properties": {}},
),
```

**2. Implementarlo en `call_tool()`:**

```python
elif name == "cloud_sql_list":
    output = _run([
        "gcloud", "sql", "instances", "list",
        f"--project={PROJECT}",
        "--format=table(name,databaseVersion,state,ipAddresses[0].ipAddress)",
    ] + account_flags)
```

---

## Troubleshooting

**`gcloud: command not found`**  
→ Verificar que `gcloud` está en el PATH del sistema, no solo en el shell interactivo.  
→ Usar ruta absoluta en el servidor si es necesario: `/usr/bin/gcloud`.

**Error de autenticación al arrancar**  
→ Verificar que el path del `--key-file` existe y tiene permisos de lectura.  
→ Probar manualmente: `gcloud auth activate-service-account --key-file=ruta.json`

**El MCP no aparece en VSCode**  
→ Los MCPs deben estar en `~/.claude.json`, no en `settings.json`.  
→ Reiniciar la extensión de Claude Code.
