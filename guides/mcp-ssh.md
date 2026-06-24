# Guía: MCP de SSH

## ¿Qué hace el servidor SSH?

Permite a Claude ejecutar comandos y manipular archivos en servidores Ubuntu privados (o cualquier Linux) vía SSH.
El mismo binario (`server.py`) sirve para múltiples servidores — se diferencia por argumentos.

## Tools disponibles

| Tool                | Descripción                                                        |
|---------------------|-------------------------------------------------------------------|
| `shell`             | Ejecuta un comando bash en el servidor remoto. **One-shot** (sin estado) por defecto; con `session` corre en una **sesión persistente** (ver abajo) |
| `read_file`         | Lee un archivo de **texto** remoto (SFTP). Binarios/grandes → redirige a `download_file` |
| `write_file`        | Escribe **texto** a un archivo remoto (SFTP)                      |
| `download_file`     | Descarga un archivo remoto a disco local (SFTP, disco-a-disco)    |
| `upload_file`       | Sube un archivo local al servidor remoto (SFTP, disco-a-disco)    |
| `list_dir`          | Lista el contenido de un directorio remoto                        |
| `sessions`          | Lista las sesiones persistentes activas (idle/corriendo, inactividad, último comando) |
| `end_session`       | Termina una sesión persistente y limpia sus temporales            |
| `interrupt_session` | Envía Ctrl-C al comando de una sesión **sin** terminarla (cortar un runaway) |

## Sesiones persistentes

Por defecto cada `shell` abre una conexión SSH nueva, corre el comando y la cierra: **el estado
no persiste** (`cd`, `export`, activar un venv se pierden) y un proceso largo se corta al `timeout`.

Pasando `session="nombre"` el comando corre dentro de una **sesión persistente server-side**
(implementada con tmux, oculto tras este vocabulario). La sesión se **crea sola** la primera vez:

```
shell(command="cd /opt/app && source .venv/bin/activate && export ENV=prod", session="deploy")
shell(command="pip install -r requirements.txt", session="deploy")   # mismo cwd, mismo venv, misma env
shell(command="./build.sh", session="deploy", timeout=10)            # build largo
# → si excede el timeout devuelve la salida parcial + nota "sigue corriendo" SIN matar la sesión
shell(command="cat build.log | tail", session="deploy")              # seguís consultando
sessions()                                                            # ver qué sesiones hay vivas
interrupt_session(session="deploy")                                   # Ctrl-C si quedó colgado
end_session(session="deploy")                                         # cerrar todo
```

- **Estado persistente**: `cd`/`export`/venv viven entre llamadas dentro de la misma sesión.
- **Procesos largos**: sobreviven al `timeout` (el estado vive en el server, no en la conexión SSH).
  Al expirar el `timeout`, `shell` devuelve lo que haya en stdout/stderr hasta el momento y avisa
  que el comando sigue corriendo; volvés a consultar la sesión cuando quieras.
- **Salida + exit code confiables**: la salida se captura a archivo y el exit code vía centinela
  (no se parsea la pantalla). El guard de 256 KB aplica igual que en `read_file`.
- **Nombre de sesión**: `[A-Za-z0-9_.-]`, 1-64 chars.
- **sudo en sesión**: el auto `sudo -S` por stdin aplica solo al modo one-shot. Dentro de una
  sesión usá `echo "$PASS" | sudo -S …` o configurá NOPASSWD en el servidor.
- **Requiere tmux** en el servidor remoto. Si falta, `shell(session=…)` devuelve un hint
  (`sudo apt install tmux`); el modo one-shot no lo necesita.

### Watcher: apaga sesiones idle

Un watcher en background apaga automáticamente las sesiones que llevan **N segundos inactivas**
(default `1800` = 30 min). "Inactiva" = el pane está en el prompt de la shell (sin proceso
corriendo) **y** sin actividad por más de N segundos. Un build largo produce output → su
actividad se mantiene fresca → **nunca se mata**. Solo toca sesiones creadas por este MCP (las
que tienen su dir bajo `/tmp/.mcp-ssh/`), nunca sesiones tmux que hayas creado a mano; también
limpia dirs huérfanos. Se configura con `--session-idle-timeout` (`0` lo desactiva).

## Transferencia de archivos (binarios y archivos grandes)

`read_file`/`write_file` son **solo para texto**: el contenido pasa por el contexto del modelo,
así que no sirven para binarios (se corrompen al decodificar como UTF-8) ni para archivos de varios MB.

Para **cualquier** archivo —especialmente binarios de decenas de MB— usar siempre `download_file` /
`upload_file`. Estas tools transfieren vía SFTP **disco-a-disco en la máquina local** (igual que `scp`,
pero dentro del MCP auditable): los bytes nunca tocan el chat, solo se devuelve la ruta, el tamaño y el
sha256. Esto evita tener que salirse a `scp` nativo.

### `download_file`

| Parámetro     | Requerido | Descripción                                                                 |
|---------------|-----------|-----------------------------------------------------------------------------|
| `remote_path` | Sí        | Ruta absoluta del archivo en el servidor remoto                             |
| `local_path`  | No        | Ruta local destino. Si se omite → `<download-dir>/<nombre>` (default `/tmp`) |
| `verify`      | No        | Si `true`, compara el sha256 local contra el `sha256sum` remoto             |

```
# Bajar un binario de ~20 MB y verificar integridad contra el remoto:
download_file(remote_path="/tmp/trader-bro-data.tgz", verify=true)
# → Descargado: /tmp/trader-bro-data.tgz → /tmp/trader-bro-data.tgz
#   bytes: 20658176
#   sha256: <hash>
#   verified: true (remoto <hash>)
```

### `upload_file`

| Parámetro     | Requerido | Descripción                                                       |
|---------------|-----------|-------------------------------------------------------------------|
| `local_path`  | Sí        | Ruta del archivo local a subir                                    |
| `remote_path` | Sí        | Ruta absoluta destino en el remoto (se crea el directorio padre)  |
| `verify`      | No        | Si `true`, compara el sha256 local contra el `sha256sum` remoto   |

```
upload_file(local_path="~/build/app.bin", remote_path="/opt/app/app.bin", verify=true)
```

Guard de `read_file`: si el archivo supera 256 KB o contiene bytes nulos, devuelve un mensaje que
redirige a `download_file` en vez de texto corrupto.

## Argumentos del servidor

| Argumento        | Requerido | Descripción                                        |
|------------------|-----------|----------------------------------------------------|
| `--host`         | Sí        | IP o hostname del servidor (ej: `192.168.1.100`)   |
| `--port`         | No        | Puerto SSH (default: `22`)                         |
| `--user`         | Sí        | Usuario SSH (ej: `ubuntu`, `root`)                 |
| `--key-file`     | No*       | Ruta a la clave privada SSH (recomendado)          |
| `--password`     | No*       | Password SSH (alternativa a `--key-file`)          |
| `--sudo-password`| No        | Password de sudo en el servidor remoto             |
| `--download-dir` | No        | Destino por defecto de `download_file` (default `/tmp`) |
| `--name`         | No        | Nombre descriptivo del servidor para los tools     |
| `--forbid-sessions` | No     | Deshabilita las sesiones persistentes (one-shot sigue activo). ON por defecto |
| `--session-idle-timeout` | No | Segundos de inactividad tras los que el watcher apaga una sesión idle (default `1800`; `0` = watcher off) |

*Se debe proveer `--key-file` o `--password`.

### Sudo

Si se pasa `--sudo-password`, el tool `shell` detecta automáticamente comandos que comienzan con `sudo`, inserta `-S` y alimenta la contraseña por stdin. Ejemplo:

```bash
# Claude puede invocar esto sin configuración extra:
sudo -u postgres psql -c "CREATE DATABASE mi_bd;"
sudo systemctl restart nginx
```

Alternativa más segura: configurar `NOPASSWD` en sudoers del servidor para comandos específicos y omitir `--sudo-password`:

```
# /etc/sudoers.d/mcp-user
tu-usuario ALL=(postgres) NOPASSWD: /usr/bin/psql
```

## Autenticación recomendada: clave SSH

La forma más segura y conveniente es usar una clave SSH.

### Opción A: usar una clave existente (ej: `~/.ssh/id_ed25519`)

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub usuario@ip-servidor
```

### Opción B: generar una clave nueva dedicada

```bash
ssh-keygen -t ed25519 -C "claude-mcp" -f ~/.ssh/claude_mcp
ssh-copy-id -i ~/.ssh/claude_mcp.pub usuario@ip-servidor
```

### Probar la conexión

```bash
ssh -i ~/.ssh/id_ed25519 usuario@ip-servidor
```

## Registrar en un proyecto

### 1. Agregar entrada en `scripts/secrets.json`

```json
{
  "name": "ssh-mi-servidor",
  "type": "ssh",
  "host": "192.168.1.100",
  "port": 22,
  "user": "ubuntu",
  "key_file": "/home/melquiades/.ssh/claude_mcp",
  "password": null,
  "sudo_password": null
}
```

Reemplaza `null` por la contraseña si el usuario SSH requiere sudo con password.

Campos opcionales de sesiones (defaults sensatos, no hace falta agregarlos):

| Campo                  | Default | Efecto                                                              |
|------------------------|---------|--------------------------------------------------------------------|
| `allow_sessions`       | `true`  | `false` agrega `--forbid-sessions` (deshabilita sesiones persistentes) |
| `session_idle_timeout` | `1800`  | Segundos de inactividad del watcher; agrega `--session-idle-timeout=N` (`0` = off) |

### 2. Registrar con el script automático

```bash
python3 "~/Mrgenkko Skills/scripts/add-mcp-to-project.py" /ruta/al/proyecto
```

### 3. Registro manual en `~/.claude.json`

```json
{
  "projects": {
    "/ruta/al/proyecto": {
      "mcpServers": {
        "ssh-mi-servidor": {
          "type": "stdio",
          "command": "/home/melquiades/Mrgenkko Skills/.venv/bin/python",
          "args": [
            "/home/melquiades/.claude/mcp-servers/ssh/server.py",
            "--host=192.168.1.100",
            "--port=22",
            "--user=ubuntu",
            "--key-file=/home/melquiades/.ssh/claude_mcp",
            "--sudo-password=tu-contraseña-sudo",
            "--name=mi-servidor"
          ],
          "env": {}
        }
      }
    }
  }
}
```

## Troubleshooting

**`Error SSH: Authentication failed`**
→ Verificar que la clave pública está en `~/.ssh/authorized_keys` del servidor.
→ Probar manualmente: `ssh -i ~/.ssh/claude_mcp usuario@host`

**`Error SSH: Connection timed out`**
→ Verificar IP/hostname y que el firewall permite el puerto SSH.
→ Confirmar que el servidor está encendido.

**`Error SSH: [Errno 111] Connection refused`**
→ El servicio SSH no está corriendo: `sudo systemctl start ssh` en el servidor.

**El MCP no aparece en VSCode**
→ Los MCPs deben estar en `~/.claude.json`, no en `settings.json`.
→ Reiniciar la extensión de Claude Code.

## Agregar más tools al servidor

Editar `deployed/ssh/server.py` (y sincronizar con `~/.claude/mcp-servers/ssh/server.py`).

### Ejemplo: tool para ver uso de disco

**En `list_tools()`:**

```python
types.Tool(
    name="disk_usage",
    description=f"Uso de disco en {SERVER_LABEL}.",
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "/", "description": "Directorio a analizar"},
        },
    },
),
```

**En `call_tool()`:**

```python
elif name == "disk_usage":
    path = arguments.get("path", "/")
    _, stdout, stderr = client.exec_command(f"df -h {path}")
    out = stdout.read().decode().strip()
    output = out or stderr.read().decode().strip()
```
