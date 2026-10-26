"""Chori QR: servidor sin dependencias externas (solo biblioteca estándar).

    python server.py                     inicia el servidor
    python server.py importar lista.csv  carga personas (nombre,dni,cantidad)

Variables de entorno:
    STAFF_PIN      PIN del personal de la parrilla (obligatorio)
    ADMIN_PIN      PIN del panel /admin (obligatorio, distinto de STAFF_PIN)
    DB_PATH        ruta de la base SQLite (por defecto /data/chori.db)
    PORT           puerto (por defecto 8080)
    COOKIE_SECURE  "0" para probar sin HTTPS (por defecto "1")
    PRECIO_CHORI   precio por chori que se muestra (por defecto 6000)
    RESERVAS_CERRADAS  "1" para dejar de recibir comprobantes (por defecto "0")
    HOST_USERS, HOST_STAFF, HOST_ADMIN
                   dominio de cada página; en esos dominios "/" sirve la página
                   correspondiente y solo se expone su API. En cualquier otro
                   host (localhost) funcionan /, /staff y /admin.
"""

import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "/data/chori.db")
PORT = int(os.environ.get("PORT", "8080"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"
STATIC = Path(__file__).parent / "static"
SESION_SEGUNDOS = 3 * 24 * 3600
TRANSFER_SEGUNDOS = 600
PRECIO_CHORI = int(os.environ.get("PRECIO_CHORI", "6000"))
RESERVAS_CERRADAS = os.environ.get("RESERVAS_CERRADAS", "0") == "1"  # "1": no se aceptan más comprobantes
COMPROBANTES = Path(DB_PATH).parent / "comprobantes"
MAX_COMPROBANTE = 8 * 1024 * 1024
MAX_REGISTROS_IP, VENTANA_REGISTROS = 5, 3600
registros_ip: dict[str, list[float]] = {}
ALFABETO_CODIGO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # sin 0/O, 1/I/L
MAX_FALLOS, BLOQUEO_SEGUNDOS = 3, 900  # 3 fallos seguidos -> 15 minutos bloqueado
ROLES_POR_HOST = {
    os.environ[var].lower(): rol
    for var, rol in (("HOST_USERS", "users"), ("HOST_STAFF", "staff"), ("HOST_ADMIN", "admin"))
    if os.environ.get(var)
}
PAGINAS = {"users": "index.html", "staff": "staff.html", "admin": "admin.html"}
API_POR_ROL = {
    "users": ("/api/login", "/api/cambiar-clave", "/api/logout", "/api/yo", "/api/intercambio/",
              "/api/registro", "/api/comprobantes/"),
    "staff": ("/api/staff/", "/api/logout"),
    "admin": ("/api/admin/", "/api/logout"),
}

ESQUEMA = """
create table if not exists personas (
  id         text primary key,
  nombre     text not null,
  dni        text unique,
  total      int  not null check (total >= 0),
  restantes  int  not null check (restantes >= 0 and restantes <= total),
  clave_hash text
);
create table if not exists entregas (
  id          integer primary key,
  persona_id  text not null references personas(id),
  tipo        text not null check (tipo in ('entrega', 'deshacer')),
  dispositivo text,
  creado_en   text not null default (datetime('now'))
);
create table if not exists sesiones (
  token      text primary key,
  persona_id text references personas(id),
  staff      int  not null default 0,  -- 0 asistente, 1 parrilla, 2 admin
  expira     real not null
);
-- Intercambio entre asistentes. Una oferta abierta "tiene" el chori: al crearla
-- se descuenta del total y restantes del donante; al tomarla pasa al receptor.
create table if not exists pedidos (
  id          integer primary key,
  persona_id  text not null references personas(id),
  estado      text not null default 'abierto' check (estado in ('abierto', 'cumplido', 'cancelado')),
  creado_en   text not null default (datetime('now'))
);
create table if not exists ofertas (
  id          integer primary key,
  persona_id  text not null references personas(id),
  estado      text not null default 'abierta' check (estado in ('abierta', 'tomada', 'cancelada')),
  creado_en   text not null default (datetime('now'))
);
create table if not exists regalos (
  id          integer primary key,
  de_id       text not null references personas(id),
  a_id        text not null references personas(id),
  origen      text not null check (origen in ('pedido', 'oferta', 'codigo')),
  cantidad    int  not null default 1 check (cantidad >= 1),
  creado_en   text not null default (datetime('now'))
);
-- Transferencia directa: los choris salen del donante al generar el código y
-- vuelven si vence (TRANSFER_SEGUNDOS) o se cancela.
create table if not exists transferencias (
  id          integer primary key,
  codigo      text not null unique,
  de_id       text not null references personas(id),
  a_id        text references personas(id),
  cantidad    int  not null check (cantidad >= 1),
  estado      text not null default 'pendiente' check (estado in ('pendiente', 'canjeada', 'cancelada', 'vencida')),
  expira      real not null
);
-- Comprobantes de pago subidos por los asistentes; el admin los aprueba o rechaza.
create table if not exists comprobantes (
  id                integer primary key,
  persona_id        text not null references personas(id),
  archivo           text not null,
  tipo              text not null,
  cantidad_pedida   int  not null check (cantidad_pedida >= 1),
  cantidad_aprobada int,
  estado            text not null default 'pendiente' check (estado in ('pendiente', 'aprobado', 'rechazado')),
  motivo            text,
  creado            real not null,
  resuelto          real
);
-- Registro de accesos (ver Handler.auditar). Datos personales: borrar después del evento.
create table if not exists auditoria (
  id          integer primary key,
  momento     real not null,
  evento      text not null,
  dni         text,
  resultado   text,
  detalle     text,
  ip          text
);
create index if not exists auditoria_dni on auditoria (dni);
"""

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript(ESQUEMA)


def migrar_regalos():
    """Bases creadas antes de las transferencias con código: amplía el check de origen y agrega cantidad."""
    sql = db.execute("select sql from sqlite_master where name = 'regalos'").fetchone()[0]
    if "'codigo'" in sql:
        return
    db.executescript("""
        begin;
        alter table regalos rename to regalos_viejo;
        create table regalos (
          id          integer primary key,
          de_id       text not null references personas(id),
          a_id        text not null references personas(id),
          origen      text not null check (origen in ('pedido', 'oferta', 'codigo')),
          cantidad    int  not null default 1 check (cantidad >= 1),
          creado_en   text not null default (datetime('now'))
        );
        insert into regalos (id, de_id, a_id, origen, creado_en)
          select id, de_id, a_id, origen, creado_en from regalos_viejo;
        drop table regalos_viejo;
        commit;
    """)


migrar_regalos()


def migrar_ofertas():
    """Reserva de una donación por una cuenta nueva, a la espera de que el donante apruebe."""
    columnas = {c["name"] for c in db.execute("pragma table_info(ofertas)")}
    if "solicitante_id" not in columnas:
        db.execute("alter table ofertas add column solicitante_id text references personas(id)")
    if "reserva_expira" not in columnas:
        db.execute("alter table ofertas add column reserva_expira real")


migrar_ofertas()


def migrar_comprobantes():
    """Huella SHA-256 de cada comprobante, para detectar el mismo archivo subido dos veces."""
    columnas = {c["name"] for c in db.execute("pragma table_info(comprobantes)")}
    if "huella" not in columnas:
        db.execute("alter table comprobantes add column huella text")
    for c in db.execute("select id, archivo from comprobantes where huella is null").fetchall():
        ruta = COMPROBANTES / c["archivo"]
        if ruta.exists():
            db.execute("update comprobantes set huella = ? where id = ?",
                       (hashlib.sha256(ruta.read_bytes()).hexdigest(), c["id"]))


migrar_comprobantes()
db_lock = threading.Lock()
fallos: dict[str, tuple[int, float]] = {}  # clave -> (cantidad, momento del último fallo)
bloqueos: dict[str, float] = {}            # clave -> momento en que se desbloquea


def solo_digitos(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def hash_clave(clave: str, sal: bytes | None = None) -> str:
    sal = sal or secrets.token_bytes(16)
    h = hashlib.scrypt(clave.encode(), salt=sal, n=2**14, r=8, p=1)
    return sal.hex() + "$" + h.hex()


def verificar_clave(clave: str, guardado: str) -> bool:
    sal, _ = guardado.split("$")
    return hmac.compare_digest(hash_clave(clave, bytes.fromhex(sal)), guardado)


def verificar_bloqueo(clave: str):
    hasta = bloqueos.get(clave, 0)
    if hasta > time.time():
        minutos = int((hasta - time.time()) // 60) + 1
        raise ErrorApi(429, f"Demasiados intentos fallidos. Espere {minutos} minuto{'s' if minutos != 1 else ''}.")
    bloqueos.pop(clave, None)


def registrar_fallo(clave: str, mensaje: str, estado: int = 401):
    """Suma un fallo; al llegar a MAX_FALLOS bloquea la clave por BLOQUEO_SEGUNDOS."""
    ahora = time.time()
    cantidad, ultimo = fallos.get(clave, (0, 0))
    if ahora - ultimo > BLOQUEO_SEGUNDOS:
        cantidad = 0  # los fallos viejos no cuentan
    cantidad += 1
    if cantidad >= MAX_FALLOS:
        fallos.pop(clave, None)
        bloqueos[clave] = ahora + BLOQUEO_SEGUNDOS
        raise ErrorApi(429, f"{mensaje} Se bloqueó el acceso por {BLOQUEO_SEGUNDOS // 60} minutos.")
    fallos[clave] = (cantidad, ahora)
    restantes = MAX_FALLOS - cantidad
    raise ErrorApi(estado, f"{mensaje} Le queda{'n' if restantes != 1 else ''} {restantes} intento{'s' if restantes != 1 else ''}.")


def limpiar_fallos(clave: str):
    fallos.pop(clave, None)
    bloqueos.pop(clave, None)


class ErrorApi(Exception):
    def __init__(self, estado: int, mensaje: str):
        self.estado, self.mensaje = estado, mensaje


def validar_dni(valor) -> str | None:
    dni = solo_digitos(str(valor or ""))
    if not dni:
        return None
    if not 7 <= len(dni) <= 8:
        raise ErrorApi(400, "El DNI debe tener 7 u 8 dígitos.")
    return dni.zfill(8)


def validar_cantidad(valor) -> int:
    try:
        cantidad = int(valor)
    except (TypeError, ValueError):
        raise ErrorApi(400, "Cantidad inválida.")
    if not 0 <= cantidad <= 100:
        raise ErrorApi(400, "La cantidad debe estar entre 0 y 100.")
    return cantidad


@contextmanager
def transaccion():
    db.execute("begin immediate")
    try:
        yield
    except BaseException:
        db.execute("rollback")
        raise
    db.execute("commit")


RESERVA_SEGUNDOS = 600


def liberar_reservas():
    """Una donación pedida por una cuenta nueva vuelve a estar libre si el donante no respondió a tiempo."""
    db.execute("update ofertas set solicitante_id = null, reserva_expira = null "
               "where estado = 'abierta' and reserva_expira is not null and reserva_expira < ?", (time.time(),))


def vencer_transferencias():
    """Devuelve al donante los choris de los códigos vencidos sin canjear."""
    vencidas = db.execute(
        "select id, de_id, cantidad from transferencias where estado = 'pendiente' and expira < ?", (time.time(),)
    ).fetchall()
    if not vencidas:
        return
    with transaccion():
        for t in vencidas:
            db.execute("update transferencias set estado = 'vencida' where id = ?", (t["id"],))
            db.execute("update personas set total = total + ?, restantes = restantes + ? where id = ?",
                       (t["cantidad"], t["cantidad"], t["de_id"]))


def nuevo_codigo() -> str:
    while True:
        codigo = "".join(secrets.choice(ALFABETO_CODIGO) for _ in range(6))
        if not db.execute("select 1 from transferencias where codigo = ?", (codigo,)).fetchone():
            return codigo


def tipo_archivo(datos: bytes) -> tuple[str, str] | None:
    """Tipo real según los primeros bytes (no se confía en el Content-Type del cliente)."""
    if datos[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if datos[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return "image/webp", "webp"
    if datos[:5] == b"%PDF-":
        return "application/pdf", "pdf"
    if datos[4:8] == b"ftyp" and datos[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return "image/heic", "heic"
    return None


def nombre_corto(nombre: str) -> str:
    """'Juana María De La Torre' -> 'Juana M.' (para mostrar a otros asistentes)."""
    partes = nombre.split()
    if len(partes) < 2:
        return nombre
    apellido = next((x for x in partes[1:] if x[0].isupper() and x.lower() not in ("de", "la", "del")), partes[-1])
    return f"{partes[0]} {apellido[0].upper()}."


def publico(p: sqlite3.Row) -> dict:
    return {"id": p["id"], "nombre": p["nombre"], "restantes": p["restantes"], "total": p["total"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "chori"

    # ---------- utilidades ----------
    def enviar(self, estado: int, cuerpo: bytes, tipo: str, extra: dict | None = None):
        self.send_response(estado)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(cuerpo)

    def json(self, estado: int, datos, extra: dict | None = None):
        self.enviar(estado, json.dumps(datos, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def leer_json(self) -> dict:
        largo = int(self.headers.get("Content-Length") or 0)
        if largo > 10_000:
            raise ErrorApi(413, "Pedido demasiado grande")
        try:
            return json.loads(self.rfile.read(largo) or b"{}")
        except json.JSONDecodeError:
            raise ErrorApi(400, "JSON inválido")

    def cookie_sesion(self, token: str, max_age: int) -> dict:
        c = f"sesion={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        return {"Set-Cookie": c + ("; Secure" if COOKIE_SECURE else "")}

    def sesion(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        if "sesion" not in c:
            return None
        return db.execute(
            "select * from sesiones where token = ? and expira > ?", (c["sesion"].value, time.time())
        ).fetchone()

    def requiere_persona(self) -> sqlite3.Row:
        s = self.sesion()
        if not s or not s["persona_id"]:
            raise ErrorApi(401, "Sesión no iniciada")
        return db.execute("select * from personas where id = ?", (s["persona_id"],)).fetchone()

    def requiere_participante(self) -> sqlite3.Row:
        p = self.requiere_persona()
        if p["clave_hash"] is None:
            raise ErrorApi(403, "Primero cree su contraseña.")
        return p

    def requiere_staff(self, nivel: int = 1):
        s = self.sesion()
        if not s or s["staff"] < nivel:
            raise ErrorApi(401, "PIN requerido")

    def nueva_sesion(self, persona_id: str | None, staff: int) -> str:
        token = secrets.token_urlsafe(32)
        db.execute(
            "insert into sesiones (token, persona_id, staff, expira) values (?, ?, ?, ?)",
            (token, persona_id, staff, time.time() + SESION_SEGUNDOS),
        )
        return token

    def auditar(self, evento: str, **campos):
        """Registra un evento en la tabla auditoria y en el log. Nunca incluye contraseñas ni PIN.
        No llamar dentro de transaccion(): un rollback borraría el registro."""
        ip = self.ip_cliente()
        texto = " ".join(f"{k}={v}" for k, v in campos.items())
        sys.stderr.write(f"{self.log_date_time_string()} AUDIT {evento} {texto} ip={ip}\n")
        dni = campos.pop("dni", None)
        resultado = campos.pop("resultado", None)
        detalle = " ".join(f"{k}={v}" for k, v in campos.items()) or None
        db.execute(
            "insert into auditoria (momento, evento, dni, resultado, detalle, ip) values (?, ?, ?, ?, ?, ?)",
            (time.time(), evento, None if dni in (None, "-") else str(dni), resultado, detalle, ip[:64]))

    def log_message(self, formato, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), formato % args))

    # ---------- rutas ----------
    def ip_cliente(self) -> str:
        # Detrás de Cloudflare + Traefik la IP real viene en Cf-Connecting-Ip.
        return (self.headers.get("Cf-Connecting-Ip")
                or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                or self.client_address[0])

    def rol(self) -> str | None:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return ROLES_POR_HOST.get(host)

    def permitido(self, ruta: str) -> bool:
        rol = self.rol()
        return rol is None or ruta.startswith(API_POR_ROL[rol])

    def do_GET(self):
        ruta = self.path.split("?")[0]
        rol = self.rol()
        if rol:
            archivos = {"/": PAGINAS[rol]}
        else:
            archivos = {"/": "index.html", "/staff": "staff.html", "/staff/": "staff.html",
                        "/admin": "admin.html", "/admin/": "admin.html"}
        if ruta == "/api/yo" and not self.permitido(ruta):
            self.enviar(404, b"No encontrado", "text/plain; charset=utf-8")
        elif ruta in archivos:
            self.enviar(200, (STATIC / archivos[ruta]).read_bytes(), "text/html; charset=utf-8")
        elif ruta == "/api/yo":
            self.despachar(self.api_yo)
        elif ruta.startswith("/api/admin/comprobante/") and self.permitido(ruta):
            self.despachar(lambda: self.api_admin_ver_comprobante(ruta.rsplit("/", 1)[1]))
        elif ruta == "/salud":
            self.enviar(200, b"ok", "text/plain")
        else:
            self.enviar(404, b"No encontrado", "text/plain; charset=utf-8")

    def do_POST(self):
        rutas = {
            "/api/login": self.api_login,
            "/api/cambiar-clave": self.api_cambiar_clave,
            "/api/logout": self.api_logout,
            "/api/staff/login": self.api_staff_login,
            "/api/staff/consultar": self.api_consultar,
            "/api/staff/entregar": lambda: self.api_mover(-1),
            "/api/staff/deshacer": lambda: self.api_mover(+1),
            "/api/staff/buscar": self.api_buscar,
            "/api/staff/resumen": self.api_resumen,
            "/api/admin/login": self.api_admin_login,
            "/api/admin/personas": self.api_admin_personas,
            "/api/admin/guardar": self.api_admin_guardar,
            "/api/admin/reset-clave": self.api_admin_reset_clave,
            "/api/admin/borrar": self.api_admin_borrar,
            "/api/admin/bloqueos": self.api_admin_bloqueos,
            "/api/admin/desbloquear": self.api_admin_desbloquear,
            "/api/admin/auditoria": self.api_admin_auditoria,
            "/api/intercambio/estado": self.api_intercambio_estado,
            "/api/intercambio/pedir": self.api_pedir,
            "/api/intercambio/cancelar-pedido": self.api_cancelar_pedido,
            "/api/intercambio/dar": self.api_dar,
            "/api/intercambio/donar": self.api_donar,
            "/api/intercambio/cancelar-oferta": self.api_cancelar_oferta,
            "/api/intercambio/tomar": self.api_tomar,
            "/api/intercambio/transferir": self.api_transferir,
            "/api/intercambio/cancelar-transferencia": self.api_cancelar_transferencia,
            "/api/intercambio/canjear": self.api_canjear,
            "/api/intercambio/responder-solicitud": self.api_responder_solicitud,
            "/api/intercambio/cancelar-solicitud": self.api_cancelar_solicitud,
            "/api/registro": self.api_registro,
            "/api/comprobantes/subir": self.api_subir_comprobante,
            "/api/comprobantes/mios": self.api_mis_comprobantes,
            "/api/admin/comprobantes": self.api_admin_comprobantes,
            "/api/admin/resolver-comprobante": self.api_admin_resolver_comprobante,
        }
        ruta = self.path.split("?")[0]
        fn = rutas.get(ruta)
        if not fn or not self.permitido(ruta):
            return self.enviar(404, b"No encontrado", "text/plain; charset=utf-8")
        if ruta == "/api/comprobantes/subir" and RESERVAS_CERRADAS:
            self.close_connection = True  # el cuerpo no se lee
            return self.json(403, {"error": "La reserva de choris ya cerró: no se reciben más comprobantes."})
        if ruta == "/api/comprobantes/subir":
            # El archivo se lee fuera del lock: una subida lenta no traba al resto (el asador escaneando).
            largo = int(self.headers.get("Content-Length") or 0)
            if largo > MAX_COMPROBANTE:
                self.close_connection = True
                return self.json(413, {"error": "El archivo supera los 8 MB."})
            self.archivo = self.rfile.read(largo)
        self.despachar(fn)

    def despachar(self, fn):
        try:
            with db_lock:
                fn()
        except ErrorApi as e:
            self.json(e.estado, {"error": e.mensaje})
        except Exception as e:  # noqa: BLE001
            self.log_error("error: %r", e)
            self.json(500, {"error": "Error interno"})

    # ---------- asistentes ----------
    def api_login(self):
        datos = self.leer_json()
        usuario = solo_digitos(datos.get("usuario"))
        clave = str(datos.get("clave") or "")
        dni_log = usuario.zfill(8) if 7 <= len(usuario) <= 8 else (usuario[:12] or "-")
        clave_bloqueo = "dni:" + usuario.zfill(8)
        try:
            verificar_bloqueo(clave_bloqueo)
        except ErrorApi:
            self.auditar("LOGIN", dni=dni_log, resultado="bloqueado")
            raise

        p = None
        if 7 <= len(usuario) <= 8:
            p = db.execute("select * from personas where dni = ?", (usuario.zfill(8),)).fetchone()

        if p is None:
            # Se avisa que la cuenta no existe (pedido del organizador); no suma intentos fallidos.
            self.auditar("LOGIN", dni=dni_log, resultado="dni_inexistente")
            if not 7 <= len(usuario) <= 8:
                raise ErrorApi(400, "Ingrese su DNI sin puntos (7 u 8 números).")
            raise ErrorApi(404, "Cuenta inexistente: no hay ninguna cuenta con ese DNI. Puede crearla con «Crear cuenta».")

        if p["clave_hash"]:
            valido = verificar_clave(clave, p["clave_hash"])
        else:
            # Primer ingreso: la clave inicial es el DNI.
            valido = solo_digitos(clave).zfill(8) == p["dni"]
        if not valido:
            motivo = "clave_incorrecta" if p["clave_hash"] else "clave_inicial_incorrecta"
            try:
                registrar_fallo(clave_bloqueo, "Contraseña incorrecta.")
            except ErrorApi as e:
                self.auditar("LOGIN", dni=dni_log, resultado=motivo + ("+bloqueo" if e.estado == 429 else ""))
                raise

        limpiar_fallos(clave_bloqueo)
        self.auditar("LOGIN", dni=dni_log, resultado="ok" if p["clave_hash"] else "ok_primer_ingreso")
        token = self.nueva_sesion(p["id"], staff=0)
        self.json(200, {"debe_cambiar": p["clave_hash"] is None}, self.cookie_sesion(token, SESION_SEGUNDOS))

    def api_cambiar_clave(self):
        p = self.requiere_persona()
        nueva = str(self.leer_json().get("clave_nueva") or "")
        if len(nueva) < 6:
            raise ErrorApi(400, "La contraseña debe tener al menos 6 caracteres.")
        if solo_digitos(nueva) == nueva and nueva.zfill(8) == p["dni"]:
            raise ErrorApi(400, "La contraseña no puede ser su DNI.")
        db.execute("update personas set clave_hash = ? where id = ?", (hash_clave(nueva), p["id"]))
        self.auditar("CAMBIO_CLAVE", dni=p["dni"], primera_vez="si" if p["clave_hash"] is None else "no")
        # Cierra cualquier otra sesión abierta con la clave inicial.
        token_actual = self.sesion()["token"]
        db.execute("delete from sesiones where persona_id = ? and token <> ?", (p["id"], token_actual))
        self.json(200, {"ok": True})

    def api_yo(self):
        p = self.requiere_persona()
        if p["clave_hash"] is None:
            return self.json(200, {"debe_cambiar": True, "nombre": p["nombre"]})
        entregados = db.execute(
            "select coalesce(sum(case tipo when 'entrega' then 1 else -1 end), 0) from entregas where persona_id = ?",
            (p["id"],)).fetchone()[0]
        self.json(200, {"debe_cambiar": False, "reservas_cerradas": RESERVAS_CERRADAS, "entregados": entregados, **publico(p)})

    def api_logout(self):
        s = self.sesion()
        if s:
            db.execute("delete from sesiones where token = ?", (s["token"],))
        self.json(200, {"ok": True}, self.cookie_sesion("", 0))

    # ---------- registro y comprobantes ----------
    def api_registro(self):
        datos = self.leer_json()
        ip = self.ip_cliente()
        ahora = time.time()
        recientes = [t for t in registros_ip.get(ip, []) if ahora - t < VENTANA_REGISTROS]
        if len(recientes) >= MAX_REGISTROS_IP:
            self.auditar("REGISTRO", resultado="limite_ip")
            raise ErrorApi(429, "Demasiadas cuentas creadas desde esta conexión. Intente más tarde.")
        nombre = " ".join(str(datos.get("nombre") or "").split())[:80]
        dni = validar_dni(datos.get("dni"))
        clave = str(datos.get("clave") or "")
        if len(nombre) < 3 or len(nombre.split()) < 2:
            raise ErrorApi(400, "Escriba nombre y apellido.")
        if not dni:
            raise ErrorApi(400, "Falta el DNI.")
        if len(clave) < 6:
            raise ErrorApi(400, "La contraseña debe tener al menos 6 caracteres.")
        if solo_digitos(clave) == clave and clave.zfill(8) == dni:
            raise ErrorApi(400, "La contraseña no puede ser su DNI.")
        if db.execute("select 1 from personas where dni = ?", (dni,)).fetchone():
            self.auditar("REGISTRO", dni=dni, resultado="dni_existente")
            raise ErrorApi(409, "Ese DNI ya tiene cuenta. Use \"Ingresar\": si ya había comprado, "
                                "la primera vez la contraseña es su DNI.")
        pid = str(uuid.uuid4())
        db.execute("insert into personas (id, nombre, dni, total, restantes, clave_hash) values (?, ?, ?, 0, 0, ?)",
                   (pid, nombre, dni, hash_clave(clave)))
        registros_ip[ip] = recientes + [ahora]
        self.auditar("REGISTRO", dni=dni, resultado="ok")
        token = self.nueva_sesion(pid, staff=0)
        self.json(200, {"ok": True}, self.cookie_sesion(token, SESION_SEGUNDOS))

    def api_subir_comprobante(self):
        p = self.requiere_participante()
        try:
            cantidad = int((self.path.split("cantidad=", 1) + ["0"])[1].split("&")[0])
        except ValueError:
            cantidad = 0
        if not 1 <= cantidad <= 50:
            raise ErrorApi(400, "Indique cuántos choris pagó (1 a 50).")
        datos = getattr(self, "archivo", b"")
        if not datos:
            raise ErrorApi(400, "Falta el archivo.")
        tipo = tipo_archivo(datos)
        if not tipo:
            raise ErrorApi(400, "Formato no admitido. Suba una foto (JPG, PNG, WEBP, HEIC) o un PDF.")
        if db.execute("select count(*) from comprobantes where persona_id = ? and estado = 'pendiente'",
                      (p["id"],)).fetchone()[0] >= 3:
            raise ErrorApi(400, "Ya tiene 3 comprobantes esperando revisión.")
        huella = hashlib.sha256(datos).hexdigest()
        previo = db.execute(
            "select c.persona_id from comprobantes c where c.huella = ? and c.estado <> 'rechazado'", (huella,)
        ).fetchone()
        if previo:
            propio = previo["persona_id"] == p["id"]
            self.auditar("COMPROBANTE_SUBIDO", dni=p["dni"], resultado="duplicado_propio" if propio else "duplicado_ajeno")
            raise ErrorApi(409, "Ese comprobante ya lo envió y está en revisión o aprobado." if propio else
                           "Ese comprobante ya fue enviado por otra cuenta. Si pagó por varias personas, "
                           "hable con un organizador.")
        COMPROBANTES.mkdir(mode=0o700, exist_ok=True)
        archivo = f"{uuid.uuid4().hex}.{tipo[1]}"
        ruta = COMPROBANTES / archivo
        ruta.write_bytes(datos)
        ruta.chmod(0o600)
        db.execute("insert into comprobantes (persona_id, archivo, tipo, cantidad_pedida, creado, huella) "
                   "values (?, ?, ?, ?, ?, ?)", (p["id"], archivo, tipo[0], cantidad, time.time(), huella))
        self.auditar("COMPROBANTE_SUBIDO", dni=p["dni"], resultado="pendiente", cantidad=cantidad)
        self.json(200, {"ok": True})

    def api_mis_comprobantes(self):
        p = self.requiere_participante()
        filas = db.execute("select * from comprobantes where persona_id = ? order by id desc", (p["id"],)).fetchall()
        self.json(200, {"precio": PRECIO_CHORI, "cerrado": RESERVAS_CERRADAS, "comprobantes": [
            {"id": c["id"], "estado": c["estado"], "pedida": c["cantidad_pedida"], "aprobada": c["cantidad_aprobada"],
             "motivo": c["motivo"], "fecha": time.strftime("%d/%m %H:%M", time.localtime(c["creado"]))}
            for c in filas]})

    def api_admin_comprobantes(self):
        self.requiere_staff(2)
        filas = db.execute(
            "select c.*, p.nombre, p.dni, p.total, "
            "  (select min(o.id) from comprobantes o where o.huella = c.huella and o.id <> c.id"
            "   and o.estado <> 'rechazado' and (o.estado = 'aprobado' or o.id < c.id)) as duplicado_de "
            "from comprobantes c join personas p on p.id = c.persona_id "
            "where c.estado = 'pendiente' order by c.id").fetchall()
        self.json(200, {"precio": PRECIO_CHORI, "cerrado": RESERVAS_CERRADAS, "comprobantes": [
            {"id": c["id"], "nombre": c["nombre"], "dni": c["dni"], "pedida": c["cantidad_pedida"], "tipo": c["tipo"],
             "total_actual": c["total"], "fecha": time.strftime("%d/%m %H:%M", time.localtime(c["creado"])),
             "duplicado_de": c["duplicado_de"]}
            for c in filas]})

    def api_admin_ver_comprobante(self, cid: str):
        self.requiere_staff(2)
        c = db.execute("select * from comprobantes where id = ?", (cid,)).fetchone()
        if not c:
            raise ErrorApi(404, "Comprobante no encontrado.")
        datos = (COMPROBANTES / c["archivo"]).read_bytes()
        self.enviar(200, datos, c["tipo"], {
            "Content-Disposition": f'inline; filename="comprobante-{c["id"]}.{c["archivo"].rsplit(".", 1)[1]}"',
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; plugin-types application/pdf",
        })

    def api_admin_resolver_comprobante(self):
        self.requiere_staff(2)
        datos = self.leer_json()
        aprobar = bool(datos.get("aprobar"))
        motivo = " ".join(str(datos.get("motivo") or "").split())[:200] or None
        with transaccion():
            c = db.execute("select * from comprobantes where id = ? and estado = 'pendiente'",
                           (datos.get("id"),)).fetchone()
            if not c:
                raise ErrorApi(409, "Ese comprobante ya fue resuelto.")
            if aprobar:
                cantidad = validar_cantidad(datos.get("cantidad"))
                if cantidad < 1:
                    raise ErrorApi(400, "Para aprobar, la cantidad debe ser al menos 1.")
                db.execute("update personas set total = total + ?, restantes = restantes + ? where id = ?",
                           (cantidad, cantidad, c["persona_id"]))
                db.execute("update comprobantes set estado = 'aprobado', cantidad_aprobada = ?, motivo = ?, resuelto = ? "
                           "where id = ?", (cantidad, motivo, time.time(), c["id"]))
            else:
                if not motivo:
                    raise ErrorApi(400, "Indique el motivo del rechazo.")
                db.execute("update comprobantes set estado = 'rechazado', motivo = ?, resuelto = ? where id = ?",
                           (motivo, time.time(), c["id"]))
        dni = db.execute("select dni from personas where id = ?", (c["persona_id"],)).fetchone()["dni"]
        self.auditar("COMPROBANTE_" + ("APROBADO" if aprobar else "RECHAZADO"), dni=dni,
                     resultado=f"{cantidad} choris" if aprobar else "rechazado")
        self.json(200, {"ok": True})

    # ---------- intercambio entre asistentes ----------
    def api_intercambio_estado(self):
        vencer_transferencias()
        liberar_reservas()
        p = self.requiere_participante()
        pedidos = db.execute(
            "select d.id, d.persona_id, x.nombre from pedidos d join personas x on x.id = d.persona_id "
            "where d.estado = 'abierto' order by d.id"
        ).fetchall()
        ofertas = db.execute(
            "select o.id, o.persona_id, o.solicitante_id, x.nombre, s.nombre as solicitante "
            "from ofertas o join personas x on x.id = o.persona_id left join personas s on s.id = o.solicitante_id "
            "where o.estado = 'abierta' order by o.id"
        ).fetchall()
        recibidos = db.execute(
            "select r.id, r.origen, r.cantidad, x.nombre from regalos r join personas x on x.id = r.de_id "
            "where r.a_id = ? order by r.id desc limit 5", (p["id"],)
        ).fetchall()
        self.json(200, {
            "restantes": p["restantes"],
            "pedidos": [{"id": d["id"], "nombre": nombre_corto(d["nombre"]), "mio": d["persona_id"] == p["id"]}
                        for d in pedidos],
            "total": p["total"],
            "ofertas": [{"id": o["id"], "nombre": nombre_corto(o["nombre"]), "mio": o["persona_id"] == p["id"],
                         "reservada": o["solicitante_id"] is not None,
                         "pedida_por_mi": o["solicitante_id"] == p["id"],
                         "solicitante": nombre_corto(o["solicitante"]) if o["solicitante"] and o["persona_id"] == p["id"] else None}
                        for o in ofertas],
            "recibidos": [{"id": r["id"], "de": nombre_corto(r["nombre"]), "origen": r["origen"],
                           "cantidad": r["cantidad"]} for r in recibidos],
            "transferencias": [
                {"id": t["id"], "codigo": f"{t['codigo'][:3]}-{t['codigo'][3:]}", "cantidad": t["cantidad"],
                 "minutos": max(1, int((t["expira"] - time.time()) // 60) + 1)}
                for t in db.execute(
                    "select * from transferencias where de_id = ? and estado = 'pendiente' order by id", (p["id"],))
            ],
        })

    def api_transferir(self):
        """Reserva N choris propios y genera un código de un solo uso."""
        vencer_transferencias()
        p = self.requiere_participante()
        cantidad = validar_cantidad(self.leer_json().get("cantidad"))
        if cantidad < 1:
            raise ErrorApi(400, "Elija al menos 1 chori.")
        with transaccion():
            cur = db.execute(
                "update personas set total = total - ?, restantes = restantes - ? where id = ? and restantes >= ?",
                (cantidad, cantidad, p["id"], cantidad))
            if cur.rowcount != 1:
                raise ErrorApi(400, f"No tiene {cantidad} chori{'s' if cantidad != 1 else ''} para transferir.")
            codigo = nuevo_codigo()
            db.execute("insert into transferencias (codigo, de_id, cantidad, expira) values (?, ?, ?, ?)",
                       (codigo, p["id"], cantidad, time.time() + TRANSFER_SEGUNDOS))
        self.json(200, {"codigo": f"{codigo[:3]}-{codigo[3:]}", "cantidad": cantidad})

    def api_cancelar_transferencia(self):
        p = self.requiere_participante()
        tid = self.leer_json().get("id")
        with transaccion():
            t = db.execute("select * from transferencias where id = ? and de_id = ? and estado = 'pendiente'",
                           (tid, p["id"])).fetchone()
            if not t:
                raise ErrorApi(409, "Ese código ya no está pendiente.")
            db.execute("update transferencias set estado = 'cancelada' where id = ?", (t["id"],))
            db.execute("update personas set total = total + ?, restantes = restantes + ? where id = ?",
                       (t["cantidad"], t["cantidad"], p["id"]))
        self.json(200, {"ok": True})

    def api_canjear(self):
        vencer_transferencias()
        p = self.requiere_participante()
        clave_bloqueo = "codigo:" + p["id"]
        verificar_bloqueo(clave_bloqueo)
        codigo = re.sub(r"[^A-Za-z0-9]", "", str(self.leer_json().get("codigo") or "")).upper()
        t = db.execute("select * from transferencias where codigo = ? and estado = 'pendiente'", (codigo,)).fetchone()
        if not t:
            registrar_fallo(clave_bloqueo, "Código inválido, vencido o ya usado.", estado=400)
        if t["de_id"] == p["id"]:
            raise ErrorApi(400, "Es su propio código; puede cancelarlo.")
        with transaccion():
            db.execute("update transferencias set estado = 'canjeada', a_id = ? where id = ?", (p["id"], t["id"]))
            db.execute("update personas set total = total + ?, restantes = restantes + ? where id = ?",
                       (t["cantidad"], t["cantidad"], p["id"]))
            db.execute("insert into regalos (de_id, a_id, origen, cantidad) values (?, ?, 'codigo', ?)",
                       (t["de_id"], p["id"], t["cantidad"]))
        limpiar_fallos(clave_bloqueo)
        de = db.execute("select nombre from personas where id = ?", (t["de_id"],)).fetchone()["nombre"]
        self.json(200, {"cantidad": t["cantidad"], "de": nombre_corto(de)})

    def api_pedir(self):
        p = self.requiere_participante()
        if p["restantes"] > 0:
            raise ErrorApi(400, "Todavía tiene choris para retirar.")
        if db.execute("select 1 from pedidos where persona_id = ? and estado = 'abierto'", (p["id"],)).fetchone():
            raise ErrorApi(400, "Ya tiene un pedido abierto.")
        db.execute("insert into pedidos (persona_id) values (?)", (p["id"],))
        self.json(200, {"ok": True})

    def api_cancelar_pedido(self):
        p = self.requiere_participante()
        db.execute("update pedidos set estado = 'cancelado' where persona_id = ? and estado = 'abierto'", (p["id"],))
        self.json(200, {"ok": True})

    def api_dar(self):
        """Da 1 chori propio a un pedido abierto."""
        p = self.requiere_participante()
        pedido_id = self.leer_json().get("id")
        with transaccion():
            d = db.execute("select * from pedidos where id = ? and estado = 'abierto'", (pedido_id,)).fetchone()
            if not d:
                raise ErrorApi(409, "Ese pedido ya no está disponible.")
            if d["persona_id"] == p["id"]:
                raise ErrorApi(400, "No puede darse un chori a usted mismo.")
            cur = db.execute(
                "update personas set total = total - 1, restantes = restantes - 1 where id = ? and restantes > 0",
                (p["id"],))
            if cur.rowcount != 1:
                raise ErrorApi(400, "No le quedan choris para dar.")
            db.execute("update personas set total = total + 1, restantes = restantes + 1 where id = ?",
                       (d["persona_id"],))
            db.execute("update pedidos set estado = 'cumplido' where id = ?", (d["id"],))
            db.execute("insert into regalos (de_id, a_id, origen) values (?, ?, 'pedido')", (p["id"], d["persona_id"]))
        self.json(200, {"ok": True})

    def api_donar(self):
        """Publica 1 chori propio; queda reservado en la oferta hasta que alguien lo tome."""
        p = self.requiere_participante()
        with transaccion():
            cur = db.execute(
                "update personas set total = total - 1, restantes = restantes - 1 where id = ? and restantes > 0",
                (p["id"],))
            if cur.rowcount != 1:
                raise ErrorApi(400, "No le quedan choris para donar.")
            db.execute("insert into ofertas (persona_id) values (?)", (p["id"],))
        self.json(200, {"ok": True})

    def api_cancelar_oferta(self):
        p = self.requiere_participante()
        oferta_id = self.leer_json().get("id")
        with transaccion():
            cur = db.execute(
                "update ofertas set estado = 'cancelada' where id = ? and persona_id = ? and estado = 'abierta'",
                (oferta_id, p["id"]))
            if cur.rowcount != 1:
                raise ErrorApi(409, "Esa donación ya no está disponible.")
            db.execute("update personas set total = total + 1, restantes = restantes + 1 where id = ?", (p["id"],))
        self.json(200, {"ok": True})

    def entregar_oferta(self, o: sqlite3.Row, receptor_id: str):
        """Pasa el chori de una oferta abierta al receptor. Llamar dentro de transaccion()."""
        db.execute("update ofertas set estado = 'tomada', solicitante_id = ?, reserva_expira = null where id = ?",
                   (receptor_id, o["id"]))
        db.execute("update personas set total = total + 1, restantes = restantes + 1 where id = ?", (receptor_id,))
        db.execute("update pedidos set estado = 'cumplido' where persona_id = ? and estado = 'abierto'", (receptor_id,))
        db.execute("insert into regalos (de_id, a_id, origen) values (?, ?, 'oferta')", (o["persona_id"], receptor_id))

    def api_tomar(self):
        """Quien ya tuvo choris lo toma al instante; una cuenta nueva lo reserva y el donante decide."""
        liberar_reservas()
        p = self.requiere_participante()
        oferta_id = self.leer_json().get("id")
        if p["restantes"] > 0:
            raise ErrorApi(400, "Las donaciones son para quienes ya no tienen choris.")
        with transaccion():
            o = db.execute("select * from ofertas where id = ? and estado = 'abierta'", (oferta_id,)).fetchone()
            if not o:
                raise ErrorApi(409, "Otra persona lo tomó primero.")
            if o["persona_id"] == p["id"]:
                raise ErrorApi(400, "Es su propia donación; puede cancelarla.")
            if o["solicitante_id"] and o["solicitante_id"] != p["id"]:
                raise ErrorApi(409, "Otra persona ya lo pidió y espera la aprobación del donante.")
            if p["total"] > 0:
                self.entregar_oferta(o, p["id"])
                pendiente = False
            else:
                if db.execute("select 1 from ofertas where solicitante_id = ? and estado = 'abierta' and id <> ?",
                              (p["id"], o["id"])).fetchone():
                    raise ErrorApi(400, "Ya tiene una donación pedida esperando aprobación.")
                db.execute("update ofertas set solicitante_id = ?, reserva_expira = ? where id = ?",
                           (p["id"], time.time() + RESERVA_SEGUNDOS, o["id"]))
                pendiente = True
        self.json(200, {"ok": True, "pendiente": pendiente})

    def api_responder_solicitud(self):
        """El donante aprueba o rechaza el pedido de una cuenta nueva sobre su donación."""
        liberar_reservas()
        p = self.requiere_participante()
        datos = self.leer_json()
        with transaccion():
            o = db.execute("select * from ofertas where id = ? and persona_id = ? and estado = 'abierta' "
                           "and solicitante_id is not null", (datos.get("id"), p["id"])).fetchone()
            if not o:
                raise ErrorApi(409, "Ese pedido ya no está pendiente.")
            if datos.get("aprobar"):
                self.entregar_oferta(o, o["solicitante_id"])
            else:
                db.execute("update ofertas set solicitante_id = null, reserva_expira = null where id = ?", (o["id"],))
        self.json(200, {"ok": True})

    def api_cancelar_solicitud(self):
        p = self.requiere_participante()
        db.execute("update ofertas set solicitante_id = null, reserva_expira = null "
                   "where id = ? and solicitante_id = ? and estado = 'abierta'", (self.leer_json().get("id"), p["id"]))
        self.json(200, {"ok": True})

    # ---------- personal ----------
    def login_pin(self, variable: str, nivel: int):
        pin = str(self.leer_json().get("pin") or "")
        # Por IP: si fuera global, un tercero podría dejar afuera a todos los asadores.
        clave_bloqueo = f"{variable}:{self.ip_cliente()}"
        evento = "LOGIN_ASADOR" if variable == "STAFF_PIN" else "LOGIN_ADMIN"
        try:
            verificar_bloqueo(clave_bloqueo)
        except ErrorApi:
            self.auditar(evento, resultado="bloqueado")
            raise
        if not hmac.compare_digest(pin.encode(), os.environ[variable].encode()):
            try:
                registrar_fallo(clave_bloqueo, "PIN incorrecto.")
            except ErrorApi as e:
                self.auditar(evento, resultado="pin_incorrecto" + ("+bloqueo" if e.estado == 429 else ""))
                raise
        limpiar_fallos(clave_bloqueo)
        self.auditar(evento, resultado="ok")
        token = self.nueva_sesion(None, staff=nivel)
        self.json(200, {"ok": True}, self.cookie_sesion(token, SESION_SEGUNDOS))

    def api_staff_login(self):
        self.login_pin("STAFF_PIN", 1)

    def api_consultar(self):
        self.requiere_staff()
        pid = str(self.leer_json().get("id") or "")
        p = db.execute("select * from personas where id = ?", (pid,)).fetchone()
        self.json(200, {"persona": publico(p) if p else None})

    def api_mover(self, delta: int):
        self.requiere_staff()
        datos = self.leer_json()
        pid = str(datos.get("id") or "")
        if delta < 0:
            cur = db.execute("update personas set restantes = restantes - 1 where id = ? and restantes > 0", (pid,))
        else:
            cur = db.execute("update personas set restantes = restantes + 1 where id = ? and restantes < total", (pid,))
        ok = cur.rowcount == 1
        if ok:
            db.execute(
                "insert into entregas (persona_id, tipo, dispositivo) values (?, ?, ?)",
                (pid, "entrega" if delta < 0 else "deshacer", str(datos.get("dispositivo") or "")[:40]),
            )
        p = db.execute("select * from personas where id = ?", (pid,)).fetchone()
        self.json(200, {"ok": ok, "persona": publico(p) if p else None})

    def api_buscar(self):
        self.requiere_staff()
        texto = str(self.leer_json().get("texto") or "").strip()
        filas = db.execute(
            "select * from personas where nombre like ? order by nombre limit 10", (f"%{texto}%",)
        ).fetchall()
        self.json(200, {"personas": [publico(p) for p in filas]})

    def api_resumen(self):
        self.requiere_staff()
        filas = db.execute("select * from personas order by nombre").fetchall()
        self.json(200, {"personas": [publico(p) for p in filas]})


    # ---------- administración ----------
    def api_admin_login(self):
        self.login_pin("ADMIN_PIN", 2)

    def api_admin_personas(self):
        self.requiere_staff(2)
        filas = db.execute("select * from personas order by nombre collate nocase").fetchall()
        vencer_transferencias()
        en_oferta = db.execute(
            "select (select count(*) from ofertas where estado = 'abierta')"
            " + (select coalesce(sum(cantidad), 0) from transferencias where estado = 'pendiente')"
        ).fetchone()[0]
        self.json(200, {"en_oferta": en_oferta, "personas": [
            {**publico(p), "dni": p["dni"], "tiene_clave": p["clave_hash"] is not None} for p in filas
        ]})

    def api_admin_guardar(self):
        """Crea una persona (sin id) o edita nombre, DNI y total (con id)."""
        self.requiere_staff(2)
        datos = self.leer_json()
        nombre = " ".join(str(datos.get("nombre") or "").split())
        if not nombre:
            raise ErrorApi(400, "Falta el nombre.")
        dni = validar_dni(datos.get("dni"))
        total = validar_cantidad(datos.get("total"))
        if dni and db.execute(
            "select 1 from personas where dni = ? and id <> ?", (dni, str(datos.get("id") or ""))
        ).fetchone():
            raise ErrorApi(409, "Ese DNI ya está cargado en otra persona.")

        pid = datos.get("id")
        if not pid:
            pid = str(uuid.uuid4())
            db.execute(
                "insert into personas (id, nombre, dni, total, restantes) values (?, ?, ?, ?, ?)",
                (pid, nombre, dni, total, total),
            )
        else:
            p = db.execute("select * from personas where id = ?", (str(pid),)).fetchone()
            if not p:
                raise ErrorApi(404, "Persona no encontrada.")
            entregados = p["total"] - p["restantes"]
            if total < entregados:
                raise ErrorApi(400, f"Ya retiró {entregados}; el total no puede ser menor.")
            db.execute(
                "update personas set nombre = ?, dni = ?, total = ?, restantes = ? where id = ?",
                (nombre, dni, total, total - entregados, pid),
            )
        p = db.execute("select * from personas where id = ?", (pid,)).fetchone()
        self.json(200, {"persona": {**publico(p), "dni": p["dni"], "tiene_clave": p["clave_hash"] is not None}})

    def api_admin_reset_clave(self):
        self.requiere_staff(2)
        pid = str(self.leer_json().get("id") or "")
        db.execute("update personas set clave_hash = null where id = ?", (pid,))
        p = db.execute("select dni from personas where id = ?", (pid,)).fetchone()
        if p and p["dni"]:
            limpiar_fallos("dni:" + p["dni"])
        self.auditar("RESET_CLAVE_ADMIN", dni=(p["dni"] if p and p["dni"] else "-"))
        db.execute("delete from sesiones where persona_id = ?", (pid,))
        self.json(200, {"ok": True})

    def api_admin_borrar(self):
        self.requiere_staff(2)
        pid = str(self.leer_json().get("id") or "")
        if db.execute("select 1 from entregas where persona_id = ?", (pid,)).fetchone():
            raise ErrorApi(409, "Tiene entregas registradas; no se puede borrar. Ponga el total en lo ya entregado.")
        if db.execute(
            "select 1 from regalos where de_id = ?1 or a_id = ?1 union all "
            "select 1 from ofertas where persona_id = ?1 and estado = 'abierta' union all "
            "select 1 from transferencias where de_id = ?1 or a_id = ?1 union all "
            "select 1 from comprobantes where persona_id = ?1 and estado = 'aprobado'", (pid,)
        ).fetchone():
            raise ErrorApi(409, "Participó de intercambios de choris; no se puede borrar.")
        db.execute("delete from pedidos where persona_id = ?", (pid,))
        for c in db.execute("select archivo from comprobantes where persona_id = ?", (pid,)).fetchall():
            (COMPROBANTES / c["archivo"]).unlink(missing_ok=True)
        db.execute("delete from comprobantes where persona_id = ?", (pid,))
        db.execute("delete from sesiones where persona_id = ?", (pid,))
        db.execute("delete from personas where id = ?", (pid,))
        self.json(200, {"ok": True})


    def api_admin_bloqueos(self):
        self.requiere_staff(2)
        ahora = time.time()
        lista = []
        for clave, hasta in sorted(bloqueos.items(), key=lambda kv: kv[1]):
            if hasta <= ahora:
                continue
            tipo, _, valor = clave.partition(":")
            if tipo == "dni":
                p = db.execute("select nombre from personas where dni = ?", (valor,)).fetchone()
                titulo = p["nombre"] if p else "DNI no registrado"
                detalle = f"DNI {int(valor):,}".replace(",", ".") if valor.isdigit() else f"DNI {valor}"
            else:
                titulo = "PIN asador" if tipo == "STAFF_PIN" else "PIN admin"
                detalle = f"IP {valor}"
            lista.append({"clave": clave, "titulo": titulo, "detalle": detalle,
                          "minutos": int((hasta - ahora) // 60) + 1})
        self.json(200, {"bloqueos": lista})

    def api_admin_auditoria(self):
        """Últimos eventos, más nuevos primero. Filtros: dni (parcial), tipo (todos|fallos|claves|admin)."""
        self.requiere_staff(2)
        datos = self.leer_json()
        dni = solo_digitos(str(datos.get("dni") or ""))
        tipo = str(datos.get("tipo") or "todos")
        condiciones, args = [], []
        if dni:
            condiciones.append("a.dni like ?")
            args.append(f"%{dni}%")
        if tipo == "fallos":
            condiciones.append("(a.resultado like '%incorrect%' or a.resultado like '%inexistente%'"
                               " or a.resultado like '%bloque%')")
        elif tipo == "claves":
            condiciones.append("a.evento in ('CAMBIO_CLAVE', 'RESET_CLAVE_ADMIN')")
        elif tipo == "admin":
            condiciones.append("a.evento like '%ADMIN%' or a.evento = 'LOGIN_ASADOR'")
        donde = ("where " + " and ".join(f"({c})" for c in condiciones)) if condiciones else ""
        filas = db.execute(
            f"select a.*, p.nombre from auditoria a left join personas p on p.dni = a.dni {donde} "
            "order by a.id desc limit 200", args).fetchall()
        total = db.execute("select count(*) from auditoria").fetchone()[0]
        self.json(200, {"total": total, "eventos": [
            {"hora": time.strftime("%d/%m %H:%M:%S", time.localtime(f["momento"])), "evento": f["evento"],
             "dni": f["dni"], "nombre": f["nombre"], "resultado": f["resultado"], "detalle": f["detalle"],
             "ip": f["ip"]} for f in filas]})

    def api_admin_desbloquear(self):
        self.requiere_staff(2)
        clave = str(self.leer_json().get("clave") or "")
        limpiar_fallos(clave)
        self.auditar("DESBLOQUEO_ADMIN", bloqueo=clave.replace(" ", "")[:40])
        self.json(200, {"ok": True})


def importar(ruta: str):
    with open(ruta, encoding="utf-8-sig") as f:
        filas = list(csv.DictReader(f))
    nuevas = 0
    for fila in filas:
        dni = solo_digitos(fila.get("dni", "")).zfill(8) if fila.get("dni", "").strip() else None
        cantidad = int(fila["cantidad"])
        existe = db.execute(
            "select 1 from personas where (dni is not null and dni = ?) or nombre = ?",
            (dni, fila["nombre"].strip()),
        ).fetchone()
        if existe:
            print(f"  ya existe, se omite: {fila['nombre']}")
            continue
        db.execute(
            "insert into personas (id, nombre, dni, total, restantes) values (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), fila["nombre"].strip(), dni, cantidad, cantidad),
        )
        nuevas += 1
    print(f"{nuevas} personas importadas.")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "importar":
        return importar(sys.argv[2])
    for variable in ("STAFF_PIN", "ADMIN_PIN"):
        if not os.environ.get(variable):
            sys.exit(f"Falta la variable {variable}")
    if os.environ["STAFF_PIN"] == os.environ["ADMIN_PIN"]:
        sys.exit("ADMIN_PIN debe ser distinto de STAFF_PIN")
    db.execute("delete from sesiones where expira < ?", (time.time(),))
    print(f"Escuchando en :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
