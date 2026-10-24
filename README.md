# sync_pcloud

Copia **espejo** de una o varias carpetas de un PC con Windows a **pCloud**, con un script de Python y un `.bat`.

**Lo local manda**:
- sube lo que falta en la nube o es distinto;
- **borra de la nube** lo que ya no existe en local (ficheros y carpetas);
- crea en la nube las carpetas locales, también las vacías.

Para saber si dos ficheros son iguales **no descarga nada**: compara nombre y tamaño y, opcionalmente, la huella **SHA-1** del fichero local con la que calcula el propio servidor de pCloud. Cada fichero que sube se verifica por SHA-1 en la nube y, si no coincide, se borra y se vuelve a subir.

> ⚠️ Es un **espejo con borrado**: si borras algo en local, se borrará en pCloud. Prueba primero con `--simular` y con una carpeta de prueba.

---

## Características

- Varias parejas **carpeta local → carpeta en pCloud** en un mismo `config.ini`, con un **resumen final** por carpeta.
- Comparación por **tamaño** (rápida) o por **SHA-1** (a fondo), con caché para no recalcular, guardada cada minuto (si se corta, sigue donde lo dejó).
- **Verificación SHA-1 de cada subida** y reintentos configurables.
- **Pasadas de comprobación** al final: vuelve a comparar todo hasta que nube y local coinciden (`VERIFICADO: nube y local son idénticos`).
- **Subidas en paralelo** configurables (de 1 en 1, de 3 en 3...).
- Inicio de sesión por **OAuth** (sirve con 2FA), por **token** copiado del navegador o por **email y contraseña** (cuentas sin 2FA).
- La sesión se guarda **cifrada con DPAPI** de Windows: solo tu usuario en ese PC puede descifrarla. La contraseña no se guarda nunca.
- **Una sola ejecución a la vez** (bloqueo con renovación cada 5 min); tras un corte de luz espera 10 min y luego se recupera solo.
- Seguridades: aborta si la carpeta local no existe (disco desconectado), límite `max_borrados`, no permite carpetas de la nube solapadas, nunca la raíz `/`.
- Progreso legible para subidas de días: barras, % con 2 decimales, velocidad, tiempo restante y **hora estimada de fin**.
- Log con **rotación** automática (`log_max_mb`, `log_copias`).

## Requisitos

- **Windows** (el cifrado de la sesión usa DPAPI y el bloqueo usa `msvcrt`).
- **Python 3.8 o superior**. Si no lo tienes, `sync_pcloud.bat` te ofrece instalarlo con `winget`; o descárgalo de [python.org](https://www.python.org/downloads/) marcando *Add python.exe to PATH*.
- La librería [`requests`](https://pypi.org/project/requests/): el `.bat` la instala sola la primera vez (`requirements.txt`).
- Una cuenta de [pCloud](https://www.pcloud.com/).

## Ficheros

| Fichero | Qué es |
|---|---|
| `sync_pcloud.bat` | El que se ejecuta (doble clic o Programador de tareas). Busca Python y lo lanza. |
| `sync_pcloud.py` | El programa. |
| `config.ini` | La configuración, con valores de ejemplo y todas las opciones explicadas. |
| `key.ini` | *(Opcional, no viene en el repo)* lo crea el usuario si quiere: email y contraseña. |

El programa crea solo, junto a ellos: `sync_pcloud.log` (y `.log.1`, `.log.2`...), `cache_sha1.json` y `config.lock`.

---

## Puesta en marcha

### 1. Configura las carpetas

Abre `config.ini` con el Bloc de notas y, al final, pon una sección `[sync NOMBRE]` por cada carpeta:

```ini
[sync Documentos]
local = D:\Documentos
remota = /Backup/Documentos

[sync Fotos]
local = E:\Fotos
remota = /Backup/Fotos
```

- `remota` se crea sola si no existe. Nunca `/` a secas.
- Cada `NOMBRE` distinto y cada `remota` en su propia carpeta: **ni repetida ni una dentro de otra** (mal: `/Backup` y `/Backup/Fotos`, porque la de fuera borraría la de dentro). Si no se cumple, el programa lo dice y no hace nada.
- Se procesan una detrás de otra, en el orden del fichero.

Repasa también `[opciones]`: cada una está explicada dentro del fichero.

### 2. Inicia sesión en pCloud (elige una forma)

**A) OAuth — recomendada, funciona con 2FA**
1. Entra en [docs.pcloud.com](https://docs.pcloud.com), inicia sesión y ve a **My Apps**.
2. Solicita una app: *Personal use*, *All folders*, *Write access = Yes*. pCloud tiene que aprobarla.
3. En `config.ini`, `[cuenta]`, pon su `oauth_client_id` y `oauth_client_secret`, y deja `sesion =` vacío.
4. Al ejecutar a mano se abre el navegador: inicias sesión (con tu 2FA), autorizas y pegas en la consola el código que te muestra pCloud.

El Client ID y el Client Secret identifican a la app, no a la cuenta: la misma app sirve para varias cuentas, cada una con su propio login.

**B) Token del navegador — mientras no tengas la app OAuth**
1. Entra en [my.pcloud.com](https://my.pcloud.com) con Chrome o Edge e inicia sesión.
2. Pulsa **F12** → pestaña **Network** → **F5** → escribe `eapi` en el filtro.
3. Pincha en una petición (`userinfo`, `listfolder`...). En la URL verás `auth=XXXX...`: copia la cadena **sin** `auth=`.
4. En `config.ini`, `[cuenta]`: `token_manual = XXXX...` y `sesion =` vacío.

No pulses *Cerrar sesión* en la web de pCloud: invalidaría el token.

**C) Email y contraseña — solo cuentas sin 2FA**
El programa te los pide en la consola. Para no escribirlos, crea tú un `key.ini` junto a `config.ini` con dos líneas: el email en la primera y la contraseña en la segunda. El programa solo lo lee y nunca lo modifica. Con 2FA activado esta forma **no funciona**: pCloud no acepta el código por esta vía.

En las tres, tras el primer login correcto la sesión se guarda **cifrada** en `config.ini` (`sesion = dpapi:...`) y `token_manual` se vacía solo. Si usaste `key.ini`, ya puedes borrarlo.

### 3. Primera ejecución

Haz doble clic en `sync_pcloud.bat`. Inicia sesión, muestra lo que hay en local y en la nube y el **PLAN** (qué borrará, creará y subirá).

> ⚠️ No es una simulación: justo después del PLAN empieza a borrar y subir. Para ver solo el plan: `sync_pcloud.bat --simular`.

Si quieres comprobar el bloqueo, corta la ejecución **cerrando la ventana con la X** (no con Ctrl+C, que la termina limpiamente) y vuelve a lanzarla: saldrá sin hacer nada (código 4) hasta que pasen 10 minutos.

### 4. Sincroniza

```
sync_pcloud.bat
```

Al final repite la comparación y, si todo coincide, escribe `VERIFICADO: nube y local son idénticos`, seguido del resumen de todas las carpetas.

---

## Uso

| Comando | Qué hace |
|---|---|
| `sync_pcloud.bat` | Sincroniza y espera una tecla al final. |
| `sync_pcloud.bat --simular` | Solo muestra lo que haría, sin tocar la nube. |
| `sync_pcloud.bat --auto` | Sin preguntas ni pausa (para automatizar). |
| `sync_pcloud.bat --comparar tamano` / `sha1` | Ignora `comparar` del `config.ini` solo en esa ejecución. |

Se pueden combinar: `sync_pcloud.bat --auto --comparar sha1`.

### Programador de tareas de Windows (por ejemplo, cada hora)

- Acción: *Iniciar un programa* → `sync_pcloud.bat`, argumentos `--auto`, *Iniciar en*: la carpeta del programa.
- Ejecútala con **tu mismo usuario de Windows** (si no, no podrá descifrar la sesión).
- Si una ejecución anterior sigue en marcha, la nueva sale sin hacer nada (código 4) y la ventana se cierra sola a los 10 segundos.

### Modo `sha1`

La primera vez es lenta: lee enteros los ficheros locales que coinciden en tamaño con la nube (lo limita la velocidad del disco) y pide a pCloud su SHA-1. Después solo recalcula lo que cambia. Una opción práctica: `comparar = tamano` en la tarea de cada hora y otra tarea semanal con `--auto --comparar sha1`. Aun en modo `tamano`, cada fichero que se **sube** se verifica por SHA-1.

---

## Cómo se ve

Cada pasada tiene 5 fases (`[1/5] LOCAL`, `[2/5] NUBE`, `[3/5] COMPARAR`, `[4/5] PLAN`, `[5/5] EJECUTAR`) y cada línea dice dónde ocurre: **LOCAL** (tu PC), **NUBE** (pCloud) o **LOCAL → NUBE** (subida). En la consola, cada tanda de mensajes lleva una cabecera con la fecha y la hora:

```
─── 2026-10-23 02:09:27 ───
[1/2] [█████████░░░]  77.04%   241.60 GB de 313.6 GB     1.4 MB/s   quedan 14 h 07 min    · Copia A 2026-10-06 (Completo).7z
[2/2] [██████░░░░░░]  51.29%   241.44 GB de 470.7 GB     1.4 MB/s   quedan 44 h 58 min    · Copia B 2026-10-08 (Completo).7z
TOTAL [███████░░░░░]  61.59%   483.04 GB de 784.3 GB     2.9 MB/s   quedan entre 29 h 32 min y 44 h 58 min – fin estimado entre mañana 07:42 y 23:07  · 0 de 2 terminados
```

Con varias subidas a la vez, el fin estimado es un margen: la primera hora supone que, al acabar un fichero, los demás aprovechan su velocidad; la segunda, que cada uno sigue a su ritmo. `esperando a pCloud` significa que el fichero ya se envió entero y pCloud lo está guardando.

Al terminar, con varias carpetas:

```
RESUMEN DE 3 CARPETAS
Documentos  →  pCloud:/Backup/Documentos   12 min       OK: nube = local
Fotos       →  pCloud:/Backup/Fotos        3 h 05 min   OK: nube = local
Obra        →  pCloud:/Backup/Obra         2 min        terminado CON ERRORES en algún fichero
```

En `sync_pcloud.log` cada línea lleva su fecha. Las líneas de progreso se guardan solo cada 5 minutos, y al arrancar, si el log supera `log_max_mb` (10 MB), se rota a `.log.1`, `.log.2`, `.log.3` (unos 40 MB como máximo).

---

## Seguridades

- **Una ejecución a la vez**: mientras corre, renueva cada 5 min un bloqueo de 10 min en `config.lock`. Tras un corte de luz o un cierre brusco no arranca otra hasta que vencen esos 10 minutos; nunca queda bloqueado para siempre.
- **Carpeta local inexistente** (por ejemplo, un disco desconectado): aborta, para no vaciar la nube.
- **`max_borrados`**: si una pasada fuese a borrar más ficheros que ese número, aborta sin tocar nada. Se aplica a cada carpeta por separado.
- **Carpetas de la nube solapadas o repetidas** entre secciones `[sync ...]`: no hace nada y lo explica.
- Si una subcarpeta local no se puede leer, **no se borra** su copia en la nube.
- Al sobrescribir un fichero, pCloud guarda la versión anterior como **revisión**.

## Códigos de salida

| Código | Significado |
|---|---|
| 0 | Todo correcto. |
| 1 | Terminó, pero algún fichero dio error (ver `sync_pcloud.log`). |
| 2 | Error de configuración, de red, o falta Python. |
| 3 | Abortado por seguridad (carpeta local inexistente o `max_borrados`). |
| 4 | Ya había otra ejecución en marcha, o bloqueo de 10 min tras un corte. |
| 5 | Hay que iniciar sesión: ejecuta el `.bat` **a mano**, sin `--auto`. |

## Problemas frecuentes

- **"No hay una sesión válida de pCloud" (código 5)**: la sesión caducó o se cerró en la web. Repite el paso 2 y ejecuta a mano.
- **"No se puede descifrar la sesión guardada"**: se está ejecutando con otro usuario de Windows o en otro PC. Deja `sesion =` vacío y vuelve a iniciar sesión con el usuario correcto.
- **"Ya hay otra ejecución en marcha" / "se cortó sin terminar" (código 4)**: es normal. Espera a que termine la otra, o a que pasen los 10 minutos tras un corte.

## Limitaciones

- **Una subida cortada empieza desde cero** en la siguiente ejecución: la API pública de pCloud no documenta subidas reanudables. No se corrompe nada; el fichero incompleto se detecta por tamaño o SHA-1 y se repite.
- **El login con contraseña no admite 2FA** (pCloud responde siempre *Please provide 'code'*). Con 2FA usa OAuth o el token del navegador.
- Solo **Windows**. En otros sistemas el programa funciona, pero guarda la sesión **sin cifrar**.
- Sincronización **en un solo sentido** (local → nube). No descarga nada.

## Licencia

[MIT](LICENSE).

API de pCloud: <https://docs.pcloud.com>
