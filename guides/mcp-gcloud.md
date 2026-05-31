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
| `bq_query`        | Ejecuta SQL en BigQuery con timeout 120s (evita el cuelgue de `bq` CLI en `shell`) |

## Argumentos del servidor

| Argumento    | Requerido | Descripción                                      |
|--------------|-----------|--------------------------------------------------|
| `--project`  | Sí        | ID del proyecto GCP (ej: `gz-procurement`)       |
| `--region`   | Sí        | Región por defecto (ej: `us-east4`)              |
| `--workdir`  | Sí        | Directorio de trabajo para comandos `shell`      |
| `--account`  | No        | Service account email para autenticarse          |
| `--key-file` | No        | Ruta al JSON de la key de la service account     |

Si se pasan `--account` y `--key-file`, el servidor activa la cuenta automáticamente al arrancar.

### Aislamiento multi-cuenta con `CLOUDSDK_CONFIG`

Todos los MCPs de gcloud comparten por defecto `~/.config/gcloud` (mismas credenciales,
misma "cuenta activa"). Cuando hay **dos instancias del mismo proyecto/producto** —típico
dev vs prod— la cuenta activa puede derivar entre invocaciones y `bq_query` / `shell` (que
no aceptan `--account`) terminan ejecutándose con la SA equivocada. Detalle completo del
problema en la wiki del vault: `wiki/herramientas/mcps/mcp-gcloud.md`.

Solución: dar a cada MCP su propio `~/.config/gcloud` vía la variable de entorno
`CLOUDSDK_CONFIG`. En `secrets.json` se declara con el campo `config_dir`; el script
`add-mcp-to-project.py` lo inyecta como `env.CLOUDSDK_CONFIG` en `~/.claude.json`.

```json
{
  "name": "gcloud-mi-proyecto-prod",
  "type": "gcloud",
  "project": "mi-proyecto-prod",
  "region": "us-east4",
  "workdir": "/home/melquiades/mi-proyecto",
  "account": "sa-prod@mi-proyecto-prod.iam.gserviceaccount.com",
  "key_file": "/home/melquiades/keys/mi-proyecto-prod/sa-prod.json",
  "config_dir": "/home/melquiades/.config/gcloud-mi-proyecto-prod"
}
```

Cada `config_dir` debe inicializarse **una vez** activando su SA:

```bash
mkdir -p ~/.config/gcloud-mi-proyecto-prod
CLOUDSDK_CONFIG=~/.config/gcloud-mi-proyecto-prod \
  gcloud auth activate-service-account sa-prod@mi-proyecto-prod.iam.gserviceaccount.com \
  --key-file=~/keys/mi-proyecto-prod/sa-prod.json
```

### Patrón dev/prod separado: ejemplo gz

| MCP             | Proyecto GCP      | Service Account                                  | config_dir                      |
|-----------------|-------------------|--------------------------------------------------|---------------------------------|
| `gcloud-gz-dev` | `gz-procurement`  | `gz-procurement-sa@gz-procurement.iam...`        | `~/.config/gcloud-gz-dev`       |
| `gcloud-gz-prod`| `ai-ptt-497912`   | `gz-prod-mcp@ai-ptt-497912.iam...`               | `~/.config/gcloud-gz-prod`      |

Son cuentas/organizaciones distintas: dev es de Lait (`juandelgado@lait.com.co` como humano),
prod es de Grupo Zambrano. El MCP **nunca usa la cuenta humana** — solo la key de su SA. La
SA de prod se crea desde la consola web de GCP (logueado como Grupo Zambrano) y se descarga su
JSON; no hace falta loguear esa cuenta humana en el `gcloud` CLI local.

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
- `roles/secretmanager.secretAccessor` — leer el valor de un secret por nombre (`secret_get`)
- `roles/secretmanager.viewer` — **listar** secrets (`secret_list`); `secretAccessor` solo no alcanza para listar
- `roles/bigquery.jobUser` — ejecutar `bq_query`

Además, el proyecto necesita las APIs habilitadas (proyecto nuevo no las trae):

```bash
gcloud services enable \
  run.googleapis.com secretmanager.googleapis.com logging.googleapis.com \
  bigquery.googleapis.com cloudresourcemanager.googleapis.com \
  --project=<proyecto>
```

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

## Timeouts: comandos largos vs cuelgues

Los tools `gcloud` y `shell` aceptan un parámetro opcional `timeout` (segundos, default
`30`, máx `300`). Subirlo solo para **lecturas** largas (queries, exports, listados
grandes). Ejemplo: `shell(command="gsutil -m ls -r gs://bucket/**", timeout=180)`.

Dos límites actúan en serie — el efectivo es el menor:

1. **Server (`server.py`)**: el `timeout` del tool, clamp `1–300s`. Al vencer devuelve
   `[timeout] El comando tardó más de Ns…`.
2. **Cliente (extensión VSCode)**: campo `timeout` por servidor en `~/.claude.json`
   (en **ms**), o variable de entorno `MCP_TOOL_TIMEOUT` (ms). Es un corte hard.

Por eso el campo cliente se fija en `320000` (320s), **por encima** del tope del server
(300s): así ante un comando largo gana el mensaje limpio del server y no el corte seco del
cliente. En `secrets.json` se declara con `timeout_ms`; `add-mcp-to-project.py` lo emite
como el campo `timeout` de la entrada del MCP.

> **Mutaciones (IAM bindings, deploys en lote): NO usar loops largos en un solo `shell`.**
> Cada `add-iam-policy-binding` es un read-modify-write completo de la IAM policy; varios
> seguidos agotan el tiempo y, si se cortan a la mitad, dejan **estado parcial** difícil de
> diagnosticar. Preferir comandos **individuales e idempotentes** — el timeout largo es para
> lecturas, no para encadenar mutaciones.

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
