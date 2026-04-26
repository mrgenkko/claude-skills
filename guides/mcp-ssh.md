# Guía: MCP de SSH

## ¿Qué hace el servidor SSH?

Permite a Claude ejecutar comandos y manipular archivos en servidores Ubuntu privados (o cualquier Linux) vía SSH.
El mismo binario (`server.py`) sirve para múltiples servidores — se diferencia por argumentos.

## Tools disponibles

| Tool         | Descripción                                              |
|--------------|----------------------------------------------------------|
| `shell`      | Ejecuta un comando bash arbitrario en el servidor remoto |
| `read_file`  | Lee el contenido de un archivo remoto (SFTP)             |
| `write_file` | Escribe contenido a un archivo remoto (SFTP)             |
| `list_dir`   | Lista el contenido de un directorio remoto               |

## Argumentos del servidor

| Argumento     | Requerido | Descripción                                        |
|---------------|-----------|----------------------------------------------------|
| `--host`      | Sí        | IP o hostname del servidor (ej: `192.168.1.100`)   |
| `--port`      | No        | Puerto SSH (default: `22`)                         |
| `--user`      | Sí        | Usuario SSH (ej: `ubuntu`, `root`)                 |
| `--key-file`  | No*       | Ruta a la clave privada SSH (recomendado)          |
| `--password`  | No*       | Password SSH (alternativa a `--key-file`)          |
| `--name`      | No        | Nombre descriptivo del servidor para los tools     |

*Se debe proveer `--key-file` o `--password`.

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
  "password": null
}
```

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
