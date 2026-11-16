# telegram-stream

Paquete Python en desarrollo para trabajar con streaming en las historias de Telegram.

> **Estado:** configuración inicial. Todavía no se han implementado la conexión con
> Telegram, la autenticación ni la transmisión de vídeo. La API pública y el mecanismo
> de streaming están pendientes de definir.

## Requisitos

- Python 3.14 o superior; el entorno de desarrollo utiliza 3.14.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.12.23 o superior.
- `make` es opcional: todos los comandos se pueden ejecutar directamente con `uv`.

## Desarrollo local

Desde la raíz del repositorio:

```sh
uv sync --locked
uv run python -c "import telegram_stream"
```

`uv sync` crea `.venv`, instala el paquete en modo editable y añade las herramientas
de desarrollo. `uv.lock` se versiona para reproducir el entorno; las herramientas de
desarrollo no son dependencias del paquete distribuido.

## Comandos

| Tarea | Con Make | Sin Make |
| --- | --- | --- |
| Preparar el entorno | `make setup` | `uv sync --locked` |
| Aplicar formato | `make format` | `uv run --locked ruff format .` |
| Verificar formato | `make format-check` | `uv run --locked ruff format --check .` |
| Revisar código | `make lint` | `uv run --locked ruff check .` |
| Comprobar tipos | `make typecheck` | `uv run --locked ty check` |
| Ejecutar pruebas | `make test` | `uv run --locked pytest` |
| Generar distribuciones | `make build` | `uv build --no-sources` |

`make check` verifica formato, lint y tipos sin modificar archivos.

Pytest está preparado para descubrir archivos `test_*.py`, preferiblemente bajo
`tests/`, con imports en modo `importlib`. Todavía no hay pruebas funcionales;
hasta que se añadan, `make test` devolverá el código 5 de pytest (sin pruebas).
La CI comprueba formato, lint, tipos y empaquetado.

## Estructura

```text
src/telegram_stream/  Código importable de la librería
pyproject.toml       Metadatos, dependencias y configuración de herramientas
uv.lock              Dependencias de desarrollo resueltas
.python-version      Versión de Python para desarrollo
.github/workflows/   Integración continua
```

El paquete utiliza la estructura `src/`, el backend `uv_build` y el marcador
`py.typed` para distribuir anotaciones de tipos. No incluye una CLI por ahora.

## Empaquetado

```sh
make build
```

Genera un wheel y una distribución de código fuente en `dist/`. El paquete todavía
no está publicado en PyPI. Antes de distribuirlo públicamente, hay que elegir una
licencia y definir su API; no se ha configurado publicación automática.

## Credenciales

No guardes claves, tokens ni sesiones de Telegram en el código o en Git. Los
archivos `.env` y las sesiones `*.session` y sus archivos auxiliares están excluidos.
El paquete aún no lee variables de entorno ni crea sesiones.
