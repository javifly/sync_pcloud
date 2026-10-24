#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_pcloud.py - Espejo unidireccional: carpeta local  ->  carpeta en pCloud

LO LOCAL MANDA:
  * Sube lo que no existe en la nube o es distinto.
  * Borra de la nube lo que ya no existe en local (ficheros y carpetas),
    incluso si la carpeta local está vacía.
  * Crea en la nube las carpetas locales (también las vacías).

COMPARACIÓN (sin descargar nada):
  * comparar = tamano -> misma ruta/nombre + mismo tamaño exacto en bytes.
  * comparar = sha1   -> además, si el tamaño coincide, compara el SHA-1 local
                         con el que calcula pCloud en su servidor (checksumfile).

VERIFICACIÓN: tras cada subida se compara el SHA-1 de lo enviado con el que
calcula pCloud; si no coincide se borra en la nube y se vuelve a subir.
Al terminar se repite la comparación completa (hasta "pasadas_maximas" veces)
hasta que nube y local coinciden.

SESIÓN: se guarda el token de sesión de pCloud en config.ini, cifrado con
DPAPI de Windows (solo tu usuario en este PC puede descifrarlo). Para volver
a iniciar sesión, deja vacío "sesion =" en el ini.
Opcional: key.ini junto a config.ini (1ª línea email, 2ª contraseña), creado
por ti, para no tener que escribirlos en la consola. El script solo lo lee,
nunca lo modifica; tras el primer login correcto puedes borrarlo.

BLOQUEO: solo puede haber una ejecución a la vez (ver clase Bloqueo).

USO:
  python sync_pcloud.py [--config config.ini] [--simular] [--auto]
    --simular  Solo muestra lo que haría, sin tocar nada en la nube.
    --auto     Modo desatendido (Programador de tareas): nunca pregunta nada.

CÓDIGOS DE SALIDA:
  0 = todo bien             1 = terminó con errores en algún fichero
  2 = error de config/red   3 = abortado por seguridad
  4 = ya había otra ejecución en marcha (no se hizo nada)
  5 = hace falta iniciar sesión (ejecuta el .bat a mano, sin --auto)

API: https://docs.pcloud.com
"""

import argparse
import base64
import configparser
import fnmatch
import getpass
import hashlib
import json
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
except ImportError:
    print("Falta la librería 'requests'. Instálala con:  python -m pip install requests")
    sys.exit(2)

VERSION = "2.1"
HOSTS = {"eu": "https://eapi.pcloud.com", "us": "https://api.pcloud.com"}
# Duración máxima de un token de sesión según docs.pcloud.com (global parameters):
AUTH_EXPIRE = 63072000           # 2 años
AUTH_INACTIVE_EXPIRE = 5356800   # 62 días sin usarse
AVISO_PROGRESO_SEG = 15          # cada cuánto se muestra el progreso en consola
PROGRESO_LOG_SEG = 300           # cada cuánto se guarda el progreso en el .log
GUARDAR_CACHE_SEG = 60           # guardado periódico de la caché SHA-1
log = logging.getLogger("sync_pcloud")


class Salida(Exception):
    """Termina el programa con un código concreto."""
    def __init__(self, codigo, mensaje):
        super().__init__(mensaje)
        self.codigo = codigo


# =========================================================================== #
#  Bloqueo de instancia única
# =========================================================================== #
class Bloqueo:
    """
    Dos capas (basta una para impedir arrancar):
      1) "Arrendamiento" escrito en el fichero .lock: cada 5 min se escribe
         bloqueado_hasta = ahora + 10 min. Mientras esa hora no haya pasado,
         NO arranca otra instancia, aunque la anterior haya muerto (corte de
         luz, cierre...). Tras un corte, como mucho 10 min después ya se
         puede ejecutar de nuevo: nunca se queda bloqueado para siempre.
      2) Bloqueo del sistema operativo sobre el mismo fichero mientras el
         proceso vive (por si una ejecución larga no pudiera renovar a tiempo).
         Windows lo suelta solo si el proceso muere.
    """
    RENOVAR = 300       # cada 5 minutos
    DURACION = 600      # bloqueo de 10 minutos por delante
    OFFSET = 1 << 20    # byte bloqueado (lejos del texto, para poder leerlo)
    TAM = 512

    def __init__(self, ruta):
        self.ruta = ruta
        self.fd = None
        self.os_ok = False
        self._parar = threading.Event()
        self._hilo = None
        self.inicio = time.time()
        self.cortada = False

    # --- bloqueo del SO ---------------------------------------------------
    def _lock(self):
        if os.name == "nt":
            import msvcrt
            os.lseek(self.fd, self.OFFSET, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.lockf(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, self.OFFSET, os.SEEK_SET)

    def _unlock(self):
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self.fd, self.OFFSET, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.lockf(self.fd, fcntl.LOCK_UN, 1, self.OFFSET, os.SEEK_SET)
        except OSError:
            pass

    # --- arrendamiento ------------------------------------------------------
    def leer(self):
        try:
            if self.fd is not None:
                # Leer por el mismo descriptor: en Linux/macOS cerrar OTRO
                # descriptor del mismo fichero soltaría el bloqueo.
                os.lseek(self.fd, 0, os.SEEK_SET)
                txt = os.read(self.fd, self.TAM).decode("utf-8", "replace")
            else:
                with open(self.ruta, "rb") as f:
                    txt = f.read(self.TAM).decode("utf-8", "replace")
        except OSError:
            return {}
        d = {}
        for linea in txt.splitlines():
            if "=" in linea:
                k, v = linea.split("=", 1)
                d[k.strip()] = v.strip()
        return d

    def _escribir(self, estado):
        ahora = time.time()
        hasta = ahora + self.DURACION if estado == "en_ejecucion" else ahora
        txt = (f"estado = {estado}\n"
               f"pid = {os.getpid()}\n"
               f"equipo = {socket.gethostname()}\n"
               f"inicio = {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.inicio))}\n"
               f"bloqueado_hasta = {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(hasta))}\n"
               f"bloqueado_hasta_ts = {int(hasta)}\n")
        data = txt.encode("utf-8").ljust(self.TAM, b" ")[:self.TAM]
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, data)
        try:
            os.fsync(self.fd)
        except OSError:
            pass

    def _latido(self):
        while not self._parar.wait(self.RENOVAR):
            try:
                self._escribir("en_ejecucion")
            except OSError as e:
                log.warning("No se pudo renovar el bloqueo: %s", e)

    # --- API pública -------------------------------------------------------
    def adquirir(self):
        """Devuelve True si se obtiene el bloqueo; False si hay otra instancia viva."""
        self.fd = os.open(self.ruta, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self._lock()
            self.os_ok = True
        except OSError as e:
            import errno
            ocupado = {errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", -1),
                       getattr(errno, "EDEADLOCK", -1)}
            if e.errno in ocupado:
                os.close(self.fd)
                self.fd = None
                return False
            log.warning("El sistema no admite bloqueo de ficheros aquí (%s); "
                        "se usa solo el bloqueo por tiempo.", e)

        previo = self.leer()
        if previo.get("estado") == "en_ejecucion":
            try:
                hasta = int(previo.get("bloqueado_hasta_ts", "0"))
            except ValueError:
                hasta = 0
            if hasta > time.time():
                # La anterior no terminó limpiamente y su bloqueo de 10 min
                # aún no ha vencido: se respeta SIEMPRE, aunque el proceso
                # ya no exista (p. ej. tras un corte de luz).
                self.cortada = self.os_ok   # con bloqueo del SO libre = proceso muerto
                if self.os_ok:
                    self._unlock()
                os.close(self.fd)
                self.fd = None
                return False
            log.warning("La ejecución anterior (pid %s, iniciada %s) no terminó limpiamente "
                        "(¿corte de luz o cierre?) y su bloqueo ya venció. Se continúa.",
                        previo.get("pid", "?"), previo.get("inicio", "?"))

        self._escribir("en_ejecucion")
        self._hilo = threading.Thread(target=self._latido, daemon=True)
        self._hilo.start()
        return True

    def liberar(self):
        if self.fd is None:
            return
        self._parar.set()
        try:
            self._escribir("terminado")
        except OSError:
            pass
        if self.os_ok:
            self._unlock()
        os.close(self.fd)
        self.fd = None


# =========================================================================== #
#  Cifrado del token (DPAPI de Windows)
# =========================================================================== #
_ENTROPIA = b"sync_pcloud/v2"


def _dpapi(datos, cifrar):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    fn = crypt32.CryptProtectData if cifrar else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.POINTER(BLOB),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(BLOB)]
    fn.restype = wintypes.BOOL

    entrada, _b1 = blob(datos)
    entropia, _b2 = blob(_ENTROPIA)
    salida = BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not fn(ctypes.byref(entrada), None, ctypes.byref(entropia), None, None,
              CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(salida)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(salida.pbData, salida.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(salida.pbData, ctypes.c_void_p))


def cifrar_sesion(d):
    raw = json.dumps(d).encode("utf-8")
    if os.name == "nt":
        return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
    log.warning("Fuera de Windows no hay DPAPI: la sesión se guarda SIN cifrar.")
    return "plano:" + base64.b64encode(raw).decode("ascii")


def descifrar_sesion(valor):
    valor = (valor or "").strip()
    if not valor:
        return None
    tipo, _, b64 = valor.partition(":")
    raw = base64.b64decode(b64)
    if tipo == "dpapi":
        if os.name != "nt":
            raise ValueError("sesión cifrada con DPAPI: solo se puede leer en Windows")
        raw = _dpapi(raw, False)
    elif tipo != "plano":
        raise ValueError("formato de sesión desconocido")
    return json.loads(raw.decode("utf-8"))


def guardar_en_ini(ruta, seccion, clave, valor):
    """Cambia/añade 'clave = valor' en [seccion] conservando comentarios y orden."""
    with open(ruta, encoding="utf-8-sig") as f:
        lineas = f.read().splitlines()
    dentro, ini_sec, hecho = False, None, False
    for i, l in enumerate(lineas):
        s = l.strip()
        if s.startswith("[") and s.endswith("]"):
            dentro = s[1:-1].strip().lower() == seccion.lower()
            if dentro:
                ini_sec = i
            continue
        if dentro and not s.startswith((";", "#")) and "=" in s \
                and s.split("=", 1)[0].strip().lower() == clave.lower():
            lineas[i] = f"{clave} = {valor}"
            hecho = True
            break
    if not hecho:
        if ini_sec is None:
            lineas += ["", f"[{seccion}]", f"{clave} = {valor}"]
        else:
            lineas.insert(ini_sec + 1, f"{clave} = {valor}")
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n") as f:
        f.write("\n".join(lineas) + "\n")
    os.replace(tmp, ruta)


# =========================================================================== #
#  Cliente mínimo de la API HTTP/JSON de pCloud
# =========================================================================== #
class PCloudError(Exception):
    def __init__(self, metodo, codigo, mensaje):
        super().__init__(f"{metodo} -> error {codigo}: {mensaje}")
        self.codigo = codigo


class _LectorSHA1:
    """Envuelve el fichero: calcula el SHA-1 de lo que se va enviando y nunca
    envía más bytes que el tamaño anunciado (por si el fichero crece)."""
    def __init__(self, f, tam, progreso=None):
        self.f, self.tam, self.n = f, tam, 0
        self.h = hashlib.sha1()
        self.progreso = progreso

    def read(self, size=-1):
        resto = self.tam - self.n
        if resto <= 0:
            return b""
        size = resto if size is None or size < 0 else min(size, resto)
        b = self.f.read(size)
        self.h.update(b)
        self.n += len(b)
        if self.progreso and b:
            self.progreso(self.n, self.tam)
        return b

    def __len__(self):
        return self.tam

    def __iter__(self):
        while True:
            b = self.read(1 << 20)
            if not b:
                return
            yield b


class PCloud:
    # 5000/5001 = error interno, 2041 = conexión rota  -> se reintenta
    REINTENTABLES = {5000, 5001, 2041}

    def __init__(self, base, auth=None, oauth=None):
        self.base = base.rstrip("/")
        self.auth = auth          # token de sesión (login con email/contraseña)
        self.oauth = oauth        # access_token OAuth2
        self._tls = threading.local()

    @property
    def s(self):
        if not hasattr(self._tls, "s"):   # una sesión HTTP por hilo
            s = requests.Session()
            s.headers["User-Agent"] = f"sync_pcloud/{VERSION}"
            self._tls.s = s
        return self._tls.s

    def _params(self, p):
        p = {k: v for k, v in p.items() if v is not None}
        if self.auth:
            p["auth"] = self.auth
        if self.oauth:
            p["access_token"] = self.oauth
        return p

    @staticmethod
    def _check(metodo, r):
        r.raise_for_status()
        data = r.json()
        codigo = data.get("result", 0)
        if codigo != 0:
            raise PCloudError(metodo, codigo, data.get("error", "?"))
        return data

    def _reintentar(self, metodo, funcion, intentos):
        for i in range(1, intentos + 1):
            try:
                return funcion()
            except PCloudError as e:
                if e.codigo not in self.REINTENTABLES or i == intentos:
                    raise
                err = e
            except (requests.RequestException, ValueError) as e:
                if i == intentos:
                    raise
                err = e
            log.warning("    reintento %d/%d de %s (%s)", i, intentos - 1, metodo, err)
            time.sleep(3 * i)

    def raw(self, metodo, timeout=60, **params):
        """Llamada sin interpretar el código de error (para el login)."""
        r = self.s.get(f"{self.base}/{metodo}", params=self._params(params), timeout=timeout)
        r.raise_for_status()
        return r.json()

    def call(self, metodo, intentos=3, timeout=60, **params):
        url = f"{self.base}/{metodo}"
        return self._reintentar(
            metodo,
            lambda: self._check(metodo, self.s.get(url, params=self._params(params), timeout=timeout)),
            intentos,
        )

    def subir(self, folderid, nombre, ruta, mtime, intentos=3, progreso=None):
        """PUT con el fichero en crudo (streaming, sin cargarlo en memoria).
        Devuelve (metadata, sha1_de_lo_enviado, bytes_enviados)."""
        url = f"{self.base}/uploadfile"
        params = self._params({"folderid": folderid, "filename": nombre,
                               "nopartial": 1, "mtime": int(mtime)})

        def _una_vez():
            with open(ruta, "rb") as f:
                tam = os.fstat(f.fileno()).st_size
                if tam > 0:
                    lector = _LectorSHA1(f, tam, progreso)
                    r = self.s.put(url, params=params, data=lector, timeout=(30, 3600))
                    sha, n = lector.h.hexdigest(), lector.n
                else:
                    # pCloud no acepta un PUT de 0 bytes -> multipart POST
                    r = self.s.post(url, params=params, files={"file": (nombre, b"")},
                                    timeout=(30, 600))
                    sha, n = hashlib.sha1(b"").hexdigest(), 0
            return self._check("uploadfile", r)["metadata"][0], sha, n

        return self._reintentar("uploadfile", _una_vez, intentos)


# =========================================================================== #
#  Inicio de sesión
# =========================================================================== #
def _hosts(c):
    servidor = c.get("servidor_api", "").strip()
    if servidor:
        return [servidor]
    region = c.get("region", "auto").strip().lower()
    return [HOSTS[region]] if region in HOSTS else [HOSTS["eu"], HOSTS["us"]]


def _pide_2fa(r):
    """¿La respuesta del login indica que falta el código 2FA?
    Observado en eapi.pcloud.com: result 1022, "Please provide 'code'."."""
    return bool(r.get("token")) or r.get("result") in (1022, 2297) \
        or "'code'" in str(r.get("error", ""))


def _diag(r):
    """Respuesta de pCloud para el log, ocultando tokens."""
    return ", ".join(f"{k}=<oculto>" if k in ("auth", "token", "access_token") else f"{k}={v!r}"
                     for k, v in sorted(r.items()))


def leer_key(ruta):
    """key.ini (lo creas tú, el script solo lo LEE y nunca lo modifica):
    1ª línea = email, 2ª línea = contraseña. Devuelve (email, password)."""
    if not os.path.exists(ruta):
        return None, None
    with open(ruta, encoding="utf-8-sig") as f:
        lineas = f.read().splitlines()
    email = lineas[0].strip() if lineas and lineas[0].strip() else None
    pw = lineas[1].rstrip("\r\n") if len(lineas) > 1 and lineas[1].strip() else None
    return email, pw


def _login_password(c, hosts, ruta_key, interactivo):
    """Email + contraseña (+ código 2FA si la cuenta lo tiene).
    Email y contraseña salen de key.ini si existe; si no, se preguntan."""
    k_email, k_pw = leer_key(ruta_key)
    defecto = k_email or c.get("usuario", "").strip()
    if k_email:
        email = k_email
    elif interactivo:
        email = input(f"Email de pCloud{f' [{defecto}]' if defecto else ''}: ").strip() or defecto
    else:
        email = defecto
    if k_pw:
        password = k_pw
        log.info("Usando email y contraseña de key.ini (%s).", email)
    elif interactivo:
        password = getpass.getpass("Contraseña de pCloud (no se ve al escribir): ")
    else:
        raise Salida(5, "No hay sesión válida ni contraseña en key.ini. Ejecuta el .bat a mano.")

    def exito(h, r):
        # key.ini NUNCA se modifica: solo se lee. Lo que se guarda es el token.
        if k_pw:
            log.info("Login correcto con los datos de key.ini. La sesión queda guardada "
                     "en config.ini: ya puedes borrar key.ini si quieres.")
        return "auth", h, r["auth"], r.get("email", email)

    exp = dict(authexpire=AUTH_EXPIRE, authinactiveexpire=AUTH_INACTIVE_EXPIRE)
    errores = []
    for h in hosts:
        api = PCloud(h)
        try:
            r = api.raw("userinfo", getauth=1, username=email, password=password, **exp)
        except requests.RequestException as e:
            errores.append(f"{h}: {e}")
            continue
        if r.get("result") == 0 and r.get("auth"):
            return exito(h, r)

        if _pide_2fa(r):
            # Contraseña correcta en este servidor; falta el código 2FA.
            log.info("%s pide el código 2FA. Respuesta: %s", h, _diag(r))
            if not interactivo:
                raise Salida(5, "pCloud pide el código 2FA y en modo --auto no se puede "
                                "preguntar. Ejecuta el .bat a mano.")
            for intento in range(3):
                codigo = input("Código 2FA (6 dígitos de tu app o SMS): ").strip().replace(" ", "")
                try:
                    if r.get("token"):
                        # Variante A: pCloud dio un token temporal -> tfa_login
                        r2 = api.raw("tfa_login", token=r["token"], code=codigo, trustdevice=1, **exp)
                    else:
                        # Variante B: se repite el login añadiendo el código
                        r2 = api.raw("userinfo", getauth=1, username=email, password=password,
                                     code=codigo, trustdevice=1, **exp)
                        if r2.get("result") != 0 and r2.get("token"):
                            r = r2   # pidió el paso intermedio: probar tfa_login con este código
                            r2 = api.raw("tfa_login", token=r["token"], code=codigo,
                                         trustdevice=1, **exp)
                except requests.RequestException as e:
                    raise Salida(2, f"Error de red en el 2FA: {e}")
                if r2.get("result") == 0 and r2.get("auth"):
                    return exito(h, r2)
                log.warning("2FA no aceptado (intento %d/3). Respuesta: %s", intento + 1, _diag(r2))
            raise Salida(5, "No se pudo completar el 2FA. Pásale a Claude las líneas "
                            "'2FA no aceptado' del log, o usa el método OAuth "
                            "(oauth_client_id / oauth_client_secret en config.ini).")
        errores.append(f"{h}: error {r.get('result')} - {r.get('error')}")
    if k_pw and any("2000" in e for e in errores):
        errores.append("(la contraseña viene de key.ini: revisa que sea correcta)")
    raise Salida(5, "No se pudo iniciar sesión en pCloud:\n  " + "\n  ".join(errores))


def _login_oauth(c, hosts):
    """OAuth2 documentado: el login (con 2FA) se hace en el navegador."""
    cid = c.get("oauth_client_id", "").strip()
    secret = c.get("oauth_client_secret", "").strip()
    url = f"https://my.pcloud.com/oauth2/authorize?client_id={cid}&response_type=code"
    print("\nAbre esta dirección, inicia sesión en pCloud (con tu 2FA) y autoriza la app:")
    print("  " + url)
    try:
        webbrowser.open(url)
    except Exception:  # noqa
        pass
    code = input("\nPega aquí el código que te muestra pCloud: ").strip()
    errores = []
    for h in hosts:
        try:
            r = PCloud(h).raw("oauth2_token", client_id=cid, client_secret=secret, code=code)
        except requests.RequestException as e:
            errores.append(f"{h}: {e}")
            continue
        if r.get("result") == 0 and r.get("access_token"):
            return "oauth", h, r["access_token"], None
        errores.append(f"{h}: error {r.get('result')} - {r.get('error')}")
    raise Salida(5, "No se pudo obtener el token OAuth:\n  " + "\n  ".join(errores))


def conectar(cfg, ruta_config, interactivo):
    c = cfg["cuenta"]
    hosts = _hosts(c)

    # 0) Token pegado a mano (p. ej. copiado del navegador): se valida, se
    #    guarda cifrado en "sesion" y se borra de "token_manual".
    manual = c.get("token_manual", "").strip()
    if manual:
        errores = []
        for h in hosts:
            api = PCloud(h, auth=manual)
            try:
                info = api.raw("userinfo")
            except requests.RequestException as e:
                errores.append(f"{h}: {e}")
                continue
            if info.get("result") == 0:
                valor = cifrar_sesion({"tipo": "auth", "host": h, "token": manual})
                guardar_en_ini(ruta_config, "cuenta", "sesion", valor)
                guardar_en_ini(ruta_config, "cuenta", "token_manual", "")
                log.info("token_manual válido en %s (%s). Guardado en 'sesion'%s y borrado de "
                         "'token_manual'.", h, info.get("email", "?"),
                         " cifrado con DPAPI" if valor.startswith("dpapi:") else "")
                return api
            errores.append(f"{h}: error {info.get('result')} - {info.get('error')}")
        raise Salida(5, "El token_manual de config.ini no es válido:\n  " + "\n  ".join(errores))

    # 1) Sesión guardada
    valor = c.get("sesion", "").strip()
    if valor:
        try:
            ses = descifrar_sesion(valor)
        except Exception as e:  # noqa
            log.warning("No se puede descifrar la sesión guardada (%s). "
                        "¿Otro usuario de Windows u otro PC? Hay que iniciar sesión de nuevo.", e)
            ses = None
        if ses:
            api = PCloud(ses["host"], **{ses["tipo"]: ses["token"]})
            try:
                info = api.call("userinfo")
                log.info("Conectado a %s como %s (sesión guardada)", ses["host"], info.get("email", "?"))
                return api
            except PCloudError as e:
                log.warning("La sesión guardada ya no es válida (%s).", e)
            except requests.RequestException as e:
                raise Salida(2, f"Sin conexión con pCloud: {e}")

    # 2) Hay que iniciar sesión
    ruta_key = os.path.join(os.path.dirname(ruta_config), "key.ini")
    if c.get("oauth_client_id", "").strip():
        if not interactivo:
            raise Salida(5, "No hay una sesión válida de pCloud. Ejecuta sync_pcloud.bat a mano "
                            "(sin --auto) para iniciar sesión; después ya funcionará en automático.")
        tipo, host, token, email = _login_oauth(c, hosts)
    else:
        if not interactivo and not os.path.exists(ruta_key):
            raise Salida(5, "No hay una sesión válida de pCloud. Ejecuta sync_pcloud.bat a mano "
                            "(sin --auto) para iniciar sesión; después ya funcionará en automático.")
        tipo, host, token, email = _login_password(c, hosts, ruta_key, interactivo)

    valor = cifrar_sesion({"tipo": tipo, "host": host, "token": token})
    guardar_en_ini(ruta_config, "cuenta", "sesion", valor)
    log.info("Sesión iniciada en %s%s. Token guardado en config.ini%s.",
             host, f" como {email}" if email else "",
             " (cifrado con DPAPI)" if valor.startswith("dpapi:") else "")
    return PCloud(host, **{tipo: token})


# =========================================================================== #
#  Utilidades
# =========================================================================== #
def ruta_larga_windows(p):
    """Prefijo \\\\?\\ para soportar rutas de más de 260 caracteres en Windows."""
    p = os.path.abspath(p)
    if os.name != "nt" or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def unir(a, b):
    return f"{a}/{b}" if a else b


def ancestros(rel):
    partes = rel.split("/")[:-1]
    return ["/".join(partes[:i + 1]) for i in range(len(partes))]


def bajo_alguno(rel, conjunto):
    return any(a in conjunto for a in ancestros(rel))


def preciso(n):
    """Tamaño con 2 decimales para el progreso (412.37 GB): así cambia a
    menudo aunque el porcentaje avance despacio."""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024


_DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


def hora_fin(seg_restantes):
    """'hoy 18:40', 'mañana 03:15' o 'jue 23 10:05'."""
    t = time.localtime(time.time() + max(0, seg_restantes))
    hoy = time.localtime()
    dias = (time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)) -
            time.mktime((hoy.tm_year, hoy.tm_mon, hoy.tm_mday, 0, 0, 0, 0, 0, -1))) / 86400
    hm = time.strftime("%H:%M", t)
    if round(dias) == 0:
        return f"hoy {hm}"
    if round(dias) == 1:
        return f"mañana {hm}"
    return f"{_DIAS[t.tm_wday]} {t.tm_mday} {hm}"


def linea_progreso(etiqueta, ancho_et, hecho, total, vel, resto_txt, cola=""):
    """Una línea de progreso con columnas fijas, empezando siempre por la barra:
    [1/2] [█████████░░░]  77.04%   241.60 GB de 313.6 GB    1.4 MB/s   quedan 14 h 07 min  · nombre
    """
    frac = hecho / max(total, 1)
    vel_txt = f"{legible(vel)}/s" if vel is not None else ""
    txt = (f"{etiqueta:<{ancho_et}} {barra(frac, 12)} {frac * 100:6.2f}%  "
           f"{preciso(hecho):>10} de {legible(total):<9} {vel_txt:>11}   {resto_txt}")
    return txt + (f"  · {cola}" if cola else "")


def rango_fin(seg_a, seg_b):
    """'entre mañana 07:42 y 23:07' (mismo día) o 'entre mañana 07:42 y sáb 25 10:00'."""
    a, b = hora_fin(seg_a), hora_fin(seg_b)
    dia_a, _, hm_a = a.rpartition(" ")
    dia_b, _, hm_b = b.rpartition(" ")
    return f"entre {a} y {hm_b}" if dia_a == dia_b else f"entre {a} y {b}"


def legible(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


class Exclusiones:
    def __init__(self, texto):
        self.patrones = [p.strip().lower() for p in texto.replace("\n", ";").split(";") if p.strip()]

    def __call__(self, rel):
        rel_l = rel.lower()
        nombre = rel_l.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatchcase(rel_l if "/" in p else nombre, p) for p in self.patrones)


def sha1_fichero(ruta, bloque=4 * 1024 * 1024, progreso=None):
    """SHA-1 de un fichero local. progreso(n) se llama con cada bloque leído."""
    h = hashlib.sha1()
    with open(ruta, "rb") as f:
        while True:
            b = f.read(bloque)
            if not b:
                break
            h.update(b)
            if progreso:
                progreso(len(b))
    return h.hexdigest()


def duracion(seg):
    seg = int(seg)
    if seg < 90:
        return f"{seg} s"
    if seg < 5400:
        return f"{round(seg / 60)} min"
    return f"{seg // 3600} h {seg % 3600 // 60:02d} min"


def timeout_checksum(tam):
    """pCloud calcula el SHA-1 en su servidor: con ficheros enormes puede
    tardar. Margen de 60 s + 1 s por cada 50 MB (p. ej. 100 GB -> ~35 min)."""
    return 60 + int(tam / (50 * 1024 * 1024))


# =========================================================================== #
#  Escaneo local y remoto
# =========================================================================== #
def escanear_local(raiz, excluido):
    items, ilegibles = {}, set()

    def on_error(e):
        rel = os.path.relpath(e.filename, raiz).replace(os.sep, "/") if e.filename else "?"
        ilegibles.add("" if rel == "." else rel)
        log.error("  No se puede leer la carpeta local %s (%s)", e.filename, e.strerror)

    for dirpath, dirnames, filenames in os.walk(raiz, onerror=on_error):
        rel_dir = os.path.relpath(dirpath, raiz).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        seguir = []
        for d in dirnames:
            rel = unir(rel_dir, d)
            full = os.path.join(dirpath, d)
            if excluido(rel):
                continue
            if os.path.islink(full) or (hasattr(os.path, "isjunction") and os.path.isjunction(full)):
                log.info("  (se ignora enlace/junction %s)", rel)
                continue
            items[rel] = {"dir": True}
            seguir.append(d)
        dirnames[:] = seguir
        for f in filenames:
            rel = unir(rel_dir, f)
            if excluido(rel):
                continue
            full = os.path.join(dirpath, f)
            try:
                st = os.stat(full)
            except OSError as e:
                ilegibles.add(rel)
                log.error("  No se puede leer %s (%s)", rel, e.strerror)
                continue
            items[rel] = {"dir": False, "size": st.st_size, "mtime": st.st_mtime,
                          "mtime_ns": st.st_mtime_ns, "full": full}
    return items, ilegibles


def aplanar_remoto(meta, prefijo, out):
    for it in meta.get("contents", []):
        rel = unir(prefijo, it["name"])
        if it.get("isfolder"):
            out[rel] = {"dir": True, "id": it["folderid"]}
            aplanar_remoto(it, rel, out)
        else:
            out[rel] = {"dir": False, "id": it["fileid"], "size": int(it.get("size", 0)),
                        "hash": str(it.get("hash", ""))}


def asegurar_carpeta_remota(api, ruta, simular):
    partes = [p for p in ruta.split("/") if p]
    actual, fid = "", 0
    for p in partes:
        actual += "/" + p
        if simular:
            try:
                fid = api.call("listfolder", path=actual, nofiles=1)["metadata"]["folderid"]
            except PCloudError as e:
                if e.codigo in (2005, 2002):
                    return None
                raise
        else:
            fid = api.call("createfolderifnotexists", path=actual)["metadata"]["folderid"]
    return fid


# =========================================================================== #
#  Una pasada de sincronización local -> remota
# =========================================================================== #
def barra(frac, ancho=20):
    frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
    k = int(round(frac * ancho))
    return "[" + "█" * k + "░" * (ancho - k) + "]"


def puntos(texto, ancho=34):
    return texto + " " + "." * max(2, ancho - len(texto))


_ULTIMO_PROGRESO_LOG = [0.0]


def progreso_extra():
    """extra= para log.info de líneas de progreso: devuelve si esta tanda
    debe ir también al fichero de log (una vez cada PROGRESO_LOG_SEG)."""
    ahora = time.time()
    if ahora - _ULTIMO_PROGRESO_LOG[0] >= PROGRESO_LOG_SEG:
        _ULTIMO_PROGRESO_LOG[0] = ahora
        return {"al_log": True}
    return {"al_log": False}


class _FormatoConsola(logging.Formatter):
    """Consola: las líneas que salen juntas se agrupan bajo una cabecera
        <línea en blanco>
        <línea en blanco>
        ─── 2026-10-23 02:09:15 ───
        línea
        línea
    Empieza una tanda nueva si han pasado CABECERA_PAUSA_SEG sin escribir
    nada, o CABECERA_MAX_SEG desde la última cabecera. (El fichero .log
    mantiene la fecha y hora en cada línea.)"""
    CABECERA_PAUSA_SEG = 2
    CABECERA_MAX_SEG = 15

    def __init__(self):
        super().__init__("%(message)s")
        self._ultima = 0.0
        self._cabecera = 0.0

    def format(self, record):
        txt = super().format(record)
        t = record.created
        if (t - self._ultima >= self.CABECERA_PAUSA_SEG or
                t - self._cabecera >= self.CABECERA_MAX_SEG):
            self._cabecera = t
            cab = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
            txt = f"\n\n─── {cab} ───\n{txt}"
        self._ultima = t
        return txt


def _norm_remota(r):
    return "/" + r.strip().strip("/").lower()


def validar_trabajos(trabajos):
    """Comprueba las [sync ...] antes de tocar nada. Lo peligroso es que una
    carpeta de la nube esté DENTRO de otra (o sea la misma): la de fuera vería
    la de dentro como 'solo en la nube' y la borraría entera."""
    ok = True
    vistos = []
    for nombre, sec in trabajos:
        local, remota = sec.get("local", "").strip(), sec.get("remota", "").strip()
        if not local or not remota:
            log.error("config.ini: a [sync %s] le falta local= o remota=", nombre)
            ok = False
            continue
        r = _norm_remota(remota)
        if r == "/":
            log.error("config.ini: [sync %s] tiene remota = / (la raíz de pCloud). No se permite.", nombre)
            ok = False
            continue
        for otro, r2, remota2 in vistos:
            if r == r2:
                log.error("config.ini: [sync %s] y [sync %s] usan la MISMA carpeta en la nube (%s). "
                          "Cada una borraría lo de la otra.", otro, nombre, remota)
                ok = False
            elif r.startswith(r2 + "/") or r2.startswith(r + "/"):
                fuera, dentro = (otro, nombre) if r.startswith(r2 + "/") else (nombre, otro)
                log.error("config.ini: la carpeta de la nube de [sync %s] está DENTRO de la de "
                          "[sync %s] (%s / %s). [sync %s] la borraría entera. Usa carpetas "
                          "separadas, p. ej. /Backup/Fotos y /Backup/Documentos.",
                          dentro, fuera, remota2, remota, fuera)
                ok = False
        vistos.append((nombre, r, remota))
    if not ok:
        log.error("No se ha hecho nada. Corrige config.ini y vuelve a ejecutar.")
    return ok


def rotar_log(ruta, max_bytes, copias):
    """Si el log supera max_bytes: log.(copias) se borra, log.2 -> log.3,
    log.1 -> log.2, log -> log.1. Con copias = 0 simplemente se vacía.
    max_bytes = 0 -> nunca rota. Devuelve True si ha rotado."""
    try:
        if max_bytes <= 0 or not os.path.exists(ruta) or os.path.getsize(ruta) < max_bytes:
            return False
        if copias == 0:
            open(ruta, "w").close()
            return True
        for i in range(copias, 0, -1):
            origen = ruta if i == 1 else f"{ruta}.{i - 1}"
            destino = f"{ruta}.{i}"
            if os.path.exists(origen):
                os.replace(origen, destino)
        return True
    except OSError as e:
        print(f"Aviso: no se pudo rotar el log ({e}); se sigue escribiendo en el mismo.")
        return False


class _FiltroLog(logging.Filter):
    def filter(self, record):
        return getattr(record, "al_log", True)


def fase(n, lado, texto):
    log.info("")
    log.info("[%d/5] %-13s %s", n, lado, texto)


SANGRIA = ""   # todo pegado a la izquierda (cada tanda ya lleva su cabecera de fecha)


def sincronizar(api, nombre, local_raiz, remota_raiz, opc, excluido, cache, simular):
    """Devuelve (codigo, acciones_realizadas_o_previstas)."""
    t0 = time.time()

    if not os.path.isdir(local_raiz):
        log.error("La carpeta local NO existe o no está accesible: %s", local_raiz)
        log.error("Abortado (si fuera un disco desconectado, se borraría toda la nube).")
        return 3, 0
    remota_raiz = "/" + remota_raiz.strip().strip("/")
    if remota_raiz == "/":
        log.error("La ruta remota no puede ser la raíz '/' de pCloud. Abortado.")
        return 2, 0

    # ===================== [1/5] LOCAL ======================================
    fase(1, "LOCAL", "Leyendo la carpeta de tu PC")
    loc, ilegibles = escanear_local(ruta_larga_windows(local_raiz), excluido)
    if "" in ilegibles:
        log.error("No se puede leer la carpeta raíz local. Abortado.")
        return 3, 0
    n_loc = sum(1 for v in loc.values() if not v["dir"])
    log.info("%s→ %d ficheros, %d carpetas, %s", SANGRIA, n_loc, len(loc) - n_loc,
             legible(sum(v.get("size", 0) for v in loc.values())))

    # ===================== [2/5] NUBE =======================================
    fase(2, "NUBE", "Leyendo la carpeta en pCloud")
    raiz_id = asegurar_carpeta_remota(api, remota_raiz, simular)
    rem = {}
    if raiz_id is not None:
        meta = api.call("listfolder", folderid=raiz_id, recursive=1, timeout=600)["metadata"]
        aplanar_remoto(meta, "", rem)
    n_rem = sum(1 for v in rem.values() if not v["dir"])
    log.info("%s→ %d ficheros, %d carpetas, %s", SANGRIA, n_rem, len(rem) - n_rem,
             legible(sum(v.get("size", 0) for v in rem.values())))

    # ===================== [3/5] COMPARAR ===================================
    fase(3, "COMPARAR", "nombre + tamaño" + (" + SHA-1" if opc["comparar"] == "sha1" else ""))

    # borrados (de menos a más profundidad; una carpeta borrada arrastra su contenido)
    borrar, borrar_dirs = [], set()
    for rel in sorted(rem, key=lambda r: (r.count("/"), r)):
        if bajo_alguno(rel, borrar_dirs):
            continue
        if excluido(rel):
            continue                      # excluido: no se toca en la nube
        if rel in ilegibles or bajo_alguno(rel, ilegibles):
            continue                      # no se pudo leer en local: no se borra por si acaso
        r, l = rem[rel], loc.get(rel)
        if l is None or l["dir"] != r["dir"]:
            borrar.append(rel)
            if r["dir"]:
                borrar_dirs.add(rel)
    n_borrados = sum(1 if not rem[b]["dir"] else
                     sum(1 for k, v in rem.items() if not v["dir"] and k.startswith(b + "/"))
                     for b in borrar)

    borrar_set = set(borrar)
    crear = sorted((rel for rel, v in loc.items() if v["dir"]
                    and not (rel in rem and rem[rel]["dir"] and rel not in borrar_set)),
                   key=lambda r: (r.count("/"), r))

    subir, iguales_tam = [], []
    n_nuevos = n_tam = 0
    for rel, l in loc.items():
        if l["dir"]:
            continue
        r = rem.get(rel)
        if r is None or r["dir"] or rel in borrar_set:
            subir.append((rel, "nuevo"))
            n_nuevos += 1
        elif r["size"] != l["size"]:
            subir.append((rel, f"distinto tamaño: nube {legible(r['size'])}, local {legible(l['size'])}"))
            n_tam += 1
        else:
            iguales_tam.append(rel)

    b_iguales = sum(loc[r]["size"] for r in iguales_tam)
    sha = opc["comparar"] == "sha1"
    log.info("%s%s %4d  (%s)%s", SANGRIA, puntos("· Mismo nombre y tamaño"), len(iguales_tam),
             legible(b_iguales), "  → se comprueban por SHA-1" if sha and iguales_tam else
             "  → iguales, no se tocan")
    log.info("%s%s %4d%s", SANGRIA, puntos("· Distinto tamaño"), n_tam, "  → se subirán" if n_tam else "")
    log.info("%s%s %4d%s", SANGRIA, puntos("· Solo en local (nuevos)"), n_nuevos,
             "  → se subirán" if n_nuevos else "")
    log.info("%s%s %4d%s", SANGRIA, puntos("· Solo en la nube"), len(borrar),
             "  → se borrarán de la nube" if borrar else "")

    clave_l = f"{local_raiz}|{remota_raiz}"
    c_loc = cache["local"].setdefault(clave_l, {})
    c_rem = cache["remoto"]

    # --- comparación SHA-1 (opcional) -----------------------------------------
    if sha and iguales_tam:
        def en_cache_loc(rel):
            l, ent = loc[rel], c_loc.get(rel)
            return bool(ent) and ent[0] == l["size"] and ent[1] == l["mtime_ns"]

        def en_cache_rem(rel):
            r = rem[rel]
            ent = c_rem.get(str(r["id"]))
            return bool(ent) and ent[0] == r["hash"]

        a_leer = [rel for rel in iguales_tam if not en_cache_loc(rel)]
        a_pedir = [rel for rel in iguales_tam if not en_cache_rem(rel)]
        bytes_leer = sum(loc[rel]["size"] for rel in a_leer)
        log.info("")
        log.info("%sComprobación SHA-1 de los %d ficheros con mismo nombre y tamaño:",
                 SANGRIA, len(iguales_tam))
        log.info("%sNUBE  > pCloud calcula el SHA-1 en su servidor: %d fichero(s)%s",
                 SANGRIA, len(a_pedir), " (el resto ya en caché)" if len(a_pedir) < len(iguales_tam) else "")
        log.info("%sLOCAL > hay que leer tu disco: %d fichero(s), %s%s", SANGRIA, len(a_leer),
                 legible(bytes_leer), " (el resto ya en caché)" if len(a_leer) < len(iguales_tam) else "")

        def sha_remoto(rel):
            # Solo hace la llamada; la caché la actualiza el hilo principal.
            r = rem[rel]
            return api.call("checksumfile", fileid=r["id"],
                            timeout=timeout_checksum(r["size"]))["sha1"].lower()

        est = {"leidos": 0, "i": 0, "fich": "", "f_leido": 0, "f_tam": 0,
               "t_aviso": time.time(), "nube_aviso": -1}
        t_ini = t_guardado = time.time()

        def aviso(n=0):
            # Se llama con cada bloque leído: hay progreso aunque los ficheros
            # sean enormes (cientos de GB).
            est["leidos"] += n
            est["f_leido"] += n
            ahora = time.time()
            if ahora - est["t_aviso"] < AVISO_PROGRESO_SEG:
                return
            est["t_aviso"] = ahora
            vel = est["leidos"] / max(ahora - t_ini, 0.001)
            resto = (bytes_leer - est["leidos"]) / vel if vel > 0 else 0
            ex = progreso_extra()
            log.info("%s", linea_progreso(
                "LOCAL", 5, est["leidos"], bytes_leer, vel,
                f"quedan {duracion(resto)} – fin estimado {hora_fin(resto)}",
                f"leyendo [{est['i']}/{len(iguales_tam)}] {est['fich']} "
                f"({est['f_leido'] * 100 / max(est['f_tam'], 1):.2f}% de {legible(est['f_tam'])})"),
                extra=ex)
            hechos = sum(1 for f in futuros.values() if f.done())
            log.info("NUBE  > %d de %d SHA-1 recibidos de pCloud", hechos, len(futuros), extra=ex)

        with ThreadPoolExecutor(max_workers=opc["hilos"]) as pool:
            futuros = {rel: pool.submit(sha_remoto, rel) for rel in a_pedir}
            for i, rel in enumerate(iguales_tam, 1):
                l = loc[rel]
                est.update(i=i, fich=rel, f_leido=0, f_tam=l["size"])
                calculado = rel in futuros or not en_cache_loc(rel)
                if en_cache_loc(rel):
                    sl = c_loc[rel][2]
                else:
                    try:
                        sl = sha1_fichero(l["full"], progreso=aviso)
                    except OSError as e:
                        log.error("%sLOCAL > no se puede leer %s (%s)", SANGRIA, rel, e)
                        continue
                    c_loc[rel] = [l["size"], l["mtime_ns"], sl]
                if rel in futuros:
                    if not futuros[rel].done():
                        log.info("%sNUBE  > esperando a que pCloud calcule el SHA-1 de %s (%s)...",
                                 SANGRIA, rel, legible(rem[rel]["size"]))
                    try:
                        sr = futuros[rel].result()
                    except Exception as e:  # noqa
                        log.error("%sNUBE  > falló el SHA-1 de %s: %s", SANGRIA, rel, e)
                        continue
                    r = rem[rel]
                    c_rem[str(r["id"])] = [r["hash"], sr]
                else:
                    sr = c_rem[str(rem[rel]["id"])][1]
                if sl != sr:
                    subir.append((rel, "mismo tamaño pero SHA-1 distinto"))
                    log.info("%s≠ DISTINTO  %s  → se subirá", SANGRIA, rel)
                elif calculado:
                    log.info("%s= IDÉNTICO  %s", SANGRIA, rel)
                if time.time() - t_guardado >= GUARDAR_CACHE_SEG and opc.get("guardar_cache"):
                    t_guardado = time.time()
                    opc["guardar_cache"]()   # si se corta, no se pierde lo ya calculado
        n_dist = sum(1 for _, m in subir if m.startswith("mismo tamaño"))
        log.info("%sResultado SHA-1: %d idénticos, %d distintos", SANGRIA,
                 len(iguales_tam) - n_dist, n_dist)
    for k in list(c_loc):
        if k not in loc:
            del c_loc[k]

    subir.sort()
    total_subir = sum(loc[r]["size"] for r, _ in subir)
    acciones = len(borrar) + len(crear) + len(subir)

    # ===================== [4/5] PLAN =======================================
    fase(4, "PLAN", "Qué hay que hacer")
    if not acciones:
        log.info("%sNada: la nube ya es una copia exacta de la carpeta local.", SANGRIA)
        return 0, 0
    log.info("%s%s %d elemento(s), %d fichero(s)", SANGRIA, puntos("NUBE         > borrar", 30),
             len(borrar), n_borrados)
    log.info("%s%s %d", SANGRIA, puntos("NUBE         > crear carpetas", 30), len(crear))
    log.info("%s%s %d fichero(s), %s", SANGRIA, puntos("LOCAL → NUBE > subir", 30),
             len(subir), legible(total_subir))
    for b in borrar[:30]:
        log.info("%s- borrar  %s%s", SANGRIA, b, "/  (carpeta entera)" if rem[b]["dir"] else "")
    for c in crear[:30]:
        log.info("%s+ carpeta %s/", SANGRIA, c)
    for rel, motivo in subir[:30]:
        log.info("%s↑ subir   %s  (%s, %s)", SANGRIA, rel, legible(loc[rel]["size"]), motivo)
    if max(len(borrar), len(crear), len(subir)) > 30:
        log.info("%s... (lista recortada a 30 por apartado)", SANGRIA)

    if opc["max_borrados"] > 0 and n_borrados > opc["max_borrados"]:
        log.error("Se borrarían %d ficheros en la nube y el límite es max_borrados = %d.",
                  n_borrados, opc["max_borrados"])
        log.error("Abortado sin tocar nada. Revisa con --simular y sube el límite si es correcto.")
        return 3, 0

    if simular:
        log.info("")
        log.info("%s(SIMULACIÓN: no se ha tocado nada en la nube)", SANGRIA)
        return 0, acciones

    # ===================== [5/5] EJECUTAR ===================================
    fase(5, "EJECUTAR", "Aplicando el plan")
    errores = 0

    if borrar:
        log.info("%sNUBE > borrando %d elemento(s)...", SANGRIA, len(borrar))
    for b in borrar:
        r = rem[b]
        try:
            if r["dir"]:
                api.call("deletefolderrecursive", folderid=r["id"])
                log.info("%sborrada carpeta  %s/", SANGRIA, b)
            else:
                api.call("deletefile", fileid=r["id"])
                log.info("%sborrado          %s", SANGRIA, b)
        except Exception as e:  # noqa
            errores += 1
            log.error("%sERROR borrando %s: %s", SANGRIA, b, e)

    ids = {"": raiz_id}
    for rel, v in rem.items():
        if v["dir"] and rel not in borrar_set and not bajo_alguno(rel, borrar_dirs):
            ids[rel] = v["id"]
    if crear:
        log.info("%sNUBE > creando %d carpeta(s)...", SANGRIA, len(crear))
    for c in crear:
        try:
            meta = api.call("createfolderifnotexists", path=f"{remota_raiz}/{c}")["metadata"]
            ids[c] = meta["folderid"]
            log.info("%screada carpeta   %s/", SANGRIA, c)
        except Exception as e:  # noqa
            errores += 1
            log.error("%sERROR creando carpeta %s: %s", SANGRIA, c, e)

    # --- subidas, con un panel de progreso común ----------------------------
    N = len(subir)
    if N:
        log.info("%sLOCAL → NUBE > subiendo %d fichero(s), %s, %s", SANGRIA, N, legible(total_subir),
                 "de uno en uno" if opc["paralelas"] == 1 else f"de {opc['paralelas']} en {opc['paralelas']}")
    panel = {"activos": {}, "hechos_bytes": 0, "hechos": 0, "t_ini": time.time(),
             "t_aviso": time.time(), "lock": threading.Lock()}

    def pintar_panel(forzar=False):
        """Panel de progreso: una línea por fichero y una de TOTAL, todas con
        el mismo formato (etiqueta, barra, %, tamaño, velocidad, tiempo · detalle):
        [1/2] [█████████░░░]  77.04%   241.60 GB de 313.6 GB    1.4 MB/s   quedan 14 h 07 min  · nombre
        TOTAL [███████░░░░░]  61.59%   483.04 GB de 784.3 GB    2.9 MB/s   quedan ... – fin estimado ...  · 0 de 2 terminados
        """
        ahora = time.time()
        if not forzar and ahora - panel["t_aviso"] < AVISO_PROGRESO_SEG:
            return
        panel["t_aviso"] = ahora
        ex = progreso_extra()
        ancho = max(len(f"[{N}/{N}]"), len("TOTAL"))
        peor_fichero = 0.0
        for n in sorted(panel["activos"]):
            a = panel["activos"][n]
            vel = a["enviado"] / max(ahora - a["t"], 0.001)
            resto = (a["total"] - a["enviado"]) / vel if vel > 0 else 0
            if a["enviado"] >= a["total"]:
                resto_txt, vel = "esperando a pCloud", None   # enviado entero; pCloud lo guarda
            else:
                resto_txt = f"quedan {duracion(resto)}"
                peor_fichero = max(peor_fichero, resto)
            log.info("%s", linea_progreso(f"[{n}/{N}]", ancho, a["enviado"], a["total"], vel,
                                          f"{resto_txt:<20}", a["rel"]), extra=ex)
        hecho = panel["hechos_bytes"] + sum(a["enviado"] for a in panel["activos"].values())
        vel = hecho / max(ahora - panel["t_ini"], 0.001)
        # Optimista: la velocidad total se mantiene (al acabar un fichero, los
        # demás aprovechan su ancho de banda). Pesimista: cada fichero sigue a
        # su propio ritmo. El total no puede acabar antes que su fichero más lento.
        optimista = (total_subir - hecho) / vel if vel > 0 else 0
        pesimista = max(optimista, peor_fichero)
        if pesimista > optimista * 1.1 and pesimista - optimista > 600:
            resto_txt = (f"quedan entre {duracion(optimista)} y {duracion(pesimista)}"
                         f" – fin estimado {rango_fin(optimista, pesimista)}")
        else:
            resto_txt = f"quedan {duracion(pesimista)} – fin estimado {hora_fin(pesimista)}"
        log.info("%s", linea_progreso("TOTAL", ancho, hecho, total_subir, vel, resto_txt,
                                      f"{panel['hechos']} de {N} terminados"), extra=ex)

    def subir_uno(n, rel, motivo):
        l = loc[rel]
        padre = rel.rsplit("/", 1)[0] if "/" in rel else ""
        nombre_f = rel.rsplit("/", 1)[-1]
        etiqueta = f"[{n}/{N}]"
        if padre not in ids:
            log.error("%s ERROR: no existe la carpeta remota para %s", etiqueta, rel)
            return False
        max_int = opc["intentos_verificacion"]
        try:
            for intento in range(1, max_int + 1):
                log.info("%s empieza  %s  (%s)%s", etiqueta, rel, legible(l["size"]),
                         f"  intento {intento}/{max_int}" if intento > 1 else "")
                t = time.time()
                with panel["lock"]:
                    panel["activos"][n] = {"rel": rel, "enviado": 0, "total": l["size"], "t": t}

                def progreso(enviado, total):
                    with panel["lock"]:
                        panel["activos"][n]["enviado"] = enviado
                        pintar_panel()

                try:
                    meta, sha_env, enviados = api.subir(ids[padre], nombre_f, l["full"],
                                                        l["mtime"], progreso=progreso)
                except Exception as e:  # noqa
                    log.error("%s ERROR subiendo %s: %s", etiqueta, rel, e)
                    return False

                fallo = None
                if int(meta.get("size", -1)) != enviados:
                    fallo = f"tamaño en la nube {meta.get('size')} != enviado {enviados}"
                elif opc["verificar"]:
                    if enviados >= 1024 ** 3:
                        log.info("%s NUBE > verificando SHA-1 de %s...", etiqueta, rel)
                    try:
                        sha_nube = api.call("checksumfile", fileid=meta["fileid"],
                                            timeout=timeout_checksum(enviados))["sha1"].lower()
                    except Exception as e:  # noqa
                        log.error("%s ERROR verificando %s: %s", etiqueta, rel, e)
                        return False
                    if sha_nube != sha_env:
                        fallo = "el SHA-1 en la nube no coincide con lo enviado"

                if fallo:
                    log.warning("%s VERIFICACIÓN FALLIDA %s: %s. Se borra y se repite.",
                                etiqueta, rel, fallo)
                    try:
                        api.call("deletefile", fileid=meta["fileid"])
                    except Exception as e:  # noqa
                        log.error("%s no se pudo borrar la copia defectuosa: %s", etiqueta, e)
                    continue

                try:
                    st = os.stat(l["full"])
                    intacto = st.st_size == enviados and st.st_mtime_ns == l["mtime_ns"]
                except OSError:
                    intacto = False
                if not intacto:
                    log.warning("%s %s cambió mientras se subía; se revisará en la siguiente pasada",
                                etiqueta, rel)
                else:
                    c_loc[rel] = [l["size"], l["mtime_ns"], sha_env]
                    c_rem[str(meta["fileid"])] = [str(meta.get("hash", "")), sha_env]
                dt = max(time.time() - t, 0.001)
                with panel["lock"]:
                    panel["activos"].pop(n, None)
                    panel["hechos_bytes"] += l["size"]
                    panel["hechos"] += 1
                log.info("%s OK  %s  (%s en %s, %s/s)%s", etiqueta, rel,
                         legible(l["size"]), duracion(dt), legible(l["size"] / dt),
                         "  · verificado: SHA-1 igual en local y en nube" if opc["verificar"] else "")
                return True
            log.error("%s ERROR: %s no se pudo subir correctamente tras %d intentos",
                      etiqueta, rel, max_int)
            return False
        finally:
            with panel["lock"]:
                panel["activos"].pop(n, None)

    with ThreadPoolExecutor(max_workers=opc["paralelas"]) as pool:
        resultados = list(pool.map(lambda a: subir_uno(*a),
                                   [(n, rel, m) for n, (rel, m) in enumerate(subir, 1)]))
    errores += resultados.count(False)

    log.info("")
    log.info("%sPasada terminada en %s. Subido %s. Errores: %d", SANGRIA,
             duracion(time.time() - t0), legible(panel["hechos_bytes"]), errores)
    return (1 if errores else 0), acciones


# =========================================================================== #
#  main
# =========================================================================== #
def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    base = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Espejo carpeta local -> pCloud (lo local manda)")
    ap.add_argument("--config", default=os.path.join(base, "config.ini"))
    ap.add_argument("--simular", action="store_true", help="no cambia nada, solo muestra el plan")
    ap.add_argument("--auto", action="store_true", help="modo desatendido: nunca pregunta nada")
    ap.add_argument("--comparar", choices=("tamano", "sha1"),
                    help="ignora 'comparar' del config.ini en esta ejecución")
    args = ap.parse_args()
    ruta_config = os.path.abspath(args.config)

    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=None)
    cfg.optionxform = str
    try:
        leido = cfg.read(ruta_config, encoding="utf-8-sig")
    except configparser.DuplicateSectionError as e:
        print(f"config.ini: la sección [{e.section}] está repetida (línea {e.lineno}). "
              f"Cada [sync NOMBRE] debe tener un nombre distinto.")
        return 2
    except configparser.Error as e:
        print(f"config.ini tiene un error de formato: {e}")
        return 2
    if not leido:
        print(f"No encuentro el fichero de configuración: {ruta_config}")
        return 2
    if not cfg.has_section("cuenta"):
        cfg.add_section("cuenta")

    o = cfg["opciones"] if cfg.has_section("opciones") else {}
    si = lambda v: str(v).strip().lower() in ("si", "sí", "yes", "true", "1")  # noqa
    try:
        opc = {
            "comparar": o.get("comparar", "tamano").strip().lower(),
            "max_borrados": int(o.get("max_borrados", "200")),
            "hilos": max(1, int(o.get("hilos_checksum", "8"))),
            "paralelas": max(1, int(o.get("subidas_paralelas", "1"))),
            "verificar": si(o.get("verificar_subidas", "si")),
            "intentos_verificacion": max(1, int(o.get("intentos_por_fichero", "3"))),
            "pasadas": max(1, int(o.get("pasadas_maximas", "3"))),
        }
    except ValueError as e:
        print(f"config.ini: valor numérico no válido ({e})")
        return 2
    simular = args.simular or si(o.get("simular", "no"))
    if args.comparar:
        opc["comparar"] = args.comparar
    if opc["comparar"] not in ("tamano", "sha1"):
        print("config.ini: comparar debe ser 'tamano' o 'sha1'")
        return 2

    # --- logging ---------------------------------------------------------
    log.setLevel(logging.INFO)
    h1 = logging.StreamHandler(sys.stdout)
    h1.setFormatter(_FormatoConsola())
    log.addHandler(h1)
    fichero_log = o.get("log", "sync_pcloud.log").strip()
    if fichero_log and not os.path.isabs(fichero_log):
        fichero_log = os.path.join(base, fichero_log)
    try:
        log_max = max(0.0, float(o.get("log_max_mb", "10"))) * 1024 * 1024
        log_copias = max(0, int(o.get("log_copias", "3")))
    except ValueError as e:
        print(f"config.ini: log_max_mb / log_copias no válidos ({e})")
        return 2

    def abrir_log():
        h2 = logging.FileHandler(fichero_log, encoding="utf-8")
        h2.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        h2.addFilter(_FiltroLog())
        log.addHandler(h2)

    # --- bloqueo de instancia única --------------------------------------
    bloqueo = Bloqueo(os.path.splitext(ruta_config)[0] + ".lock")
    if not bloqueo.adquirir():
        if fichero_log:
            abrir_log()      # solo añade su línea; no rota (el fichero lo usa la otra)
        otro = bloqueo.leer()
        if bloqueo.cortada:
            log.info("La ejecución anterior (pid %s, desde %s) se cortó sin terminar. Su bloqueo "
                     "sigue vigente hasta %s; hasta entonces no se ejecuta. No se hace nada.",
                     otro.get("pid", "?"), otro.get("inicio", "?"), otro.get("bloqueado_hasta", "?"))
        else:
            log.info("Ya hay otra ejecución en marcha (pid %s en %s, desde %s, bloqueo renovado hasta %s). "
                     "No se hace nada.", otro.get("pid", "?"), otro.get("equipo", "?"),
                     otro.get("inicio", "?"), otro.get("bloqueado_hasta", "?"))
        return 4

    if fichero_log:
        # Solo quien tiene el bloqueo rota el log, y antes de abrirlo: así
        # nunca se renombra un fichero que otra ejecución tenga abierto.
        rotado = rotar_log(fichero_log, log_max, log_copias)
        abrir_log()
        if rotado:
            log.info("(%s superaba %s: se ha guardado como %s.1 y se empieza uno nuevo)",
                     os.path.basename(fichero_log), legible(log_max), os.path.basename(fichero_log))

    try:
        return _ejecutar(args, cfg, ruta_config, base, opc, simular, o)
    finally:
        bloqueo.liberar()


def _ejecutar(args, cfg, ruta_config, base, opc, simular, o):
    excluido = Exclusiones(o.get("excluir", ""))
    trabajos = [(s.split(" ", 1)[1].strip() if " " in s else s, cfg[s])
                for s in cfg.sections() if s.lower().startswith("sync")]
    if not trabajos:
        log.error("config.ini: no hay ninguna sección [sync NOMBRE] con local= y remota=")
        return 2
    if not validar_trabajos(trabajos):
        return 2

    cache_path = os.path.join(base, "cache_sha1.json")
    cache = {"local": {}, "remoto": {}}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache.update(json.load(f))
        except Exception:  # noqa
            log.warning("Caché de SHA-1 dañada; se regenera.")

    def guardar_cache():
        # Se llama al final y cada minuto durante la comparación SHA-1, para
        # no perder lo calculado si la ejecución se corta. (La caché solo
        # describe ficheros, así que también se guarda al simular.)
        try:
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, cache_path)
        except Exception as e:  # noqa
            log.warning("No se pudo guardar la caché: %s", e)
    opc["guardar_cache"] = guardar_cache

    log.info("sync_pcloud %s - %s%s", VERSION, time.strftime("%Y-%m-%d %H:%M:%S"),
             "   (SIMULACIÓN)" if simular else "")
    interactivo = not args.auto and sys.stdin is not None and sys.stdin.isatty()
    try:
        api = conectar(cfg, ruta_config, interactivo)
    except Salida as e:
        log.error(str(e))
        return e.codigo
    except (KeyboardInterrupt, EOFError):
        log.error("Inicio de sesión cancelado.")
        return 5

    peor = 0
    max_pasadas = 1 if simular else opc["pasadas"]
    resumen = []
    try:
        for nombre, sec in trabajos:
            local, remota = sec.get("local", "").strip(), sec.get("remota", "").strip()
            rc, acciones, t_job = 0, 0, time.time()
            for pasada in range(1, max_pasadas + 1):
                log.info("")
                log.info("=" * 74)
                log.info("[%s]  PASADA %d de %d  ·  %s", nombre, pasada, max_pasadas,
                         "sincronizar" if pasada == 1 else "comprobación: ¿nube = local?")
                log.info("LOCAL: %s", local)
                log.info("NUBE:  pCloud:%s", remota)
                log.info("=" * 74)
                try:
                    rc, acciones = sincronizar(api, nombre, local, remota, opc, excluido, cache, simular)
                except Exception as e:  # noqa
                    log.exception("[%s] Error inesperado: %s", nombre, e)
                    rc, acciones = 2, -1
                if rc in (2, 3) or simular:
                    break
                if acciones == 0:
                    if pasada > 1:
                        log.info("[%s] VERIFICADO: nube y local son idénticos.", nombre)
                    break
                if pasada == max_pasadas:
                    log.warning("[%s] Tras %d pasadas aún hubo cambios; se revisará en la próxima ejecución.",
                                nombre, max_pasadas)
            peor = max(peor, rc)
            if rc == 2:
                estado = "ERROR (configuración o conexión)"
            elif rc == 3:
                estado = "ABORTADO por seguridad, no se tocó nada"
            elif rc == 1:
                estado = "terminado CON ERRORES en algún fichero"
            elif simular:
                estado = f"simulación: {acciones} cambio(s) previsto(s)" if acciones else "simulación: ya está igual"
            elif acciones:
                estado = "quedan cambios: se revisará en la próxima ejecución"
            else:
                estado = "OK: nube = local"
            resumen.append((nombre, remota, estado, time.time() - t_job))
    finally:
        guardar_cache()

    if len(resumen) > 1:
        ancho = max(len(n) for n, _, _, _ in resumen)
        anchor = max(len(r) for _, r, _, _ in resumen)
        log.info("")
        log.info("=" * 74)
        log.info("RESUMEN DE %d CARPETAS", len(resumen))
        log.info("=" * 74)
        for nombre, remota, estado, dur in resumen:
            log.info("%-*s  →  pCloud:%-*s   %-8s  %s", ancho, nombre, anchor, remota, duracion(dur), estado)

    log.info("")
    log.info({0: "FIN: todo correcto.", 1: "FIN con errores (revisa el log).",
              2: "FIN: error de configuración o conexión.",
              3: "FIN: abortado por seguridad."}.get(peor, f"FIN (código {peor})"))
    return peor


if __name__ == "__main__":
    sys.exit(main())
