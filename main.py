import sys
# En Windows, cuando la salida estándar no es una consola UTF-8 (ej. corriendo
# como servicio, o con el log redirigido a un archivo), un print() con emoji
# (✅❌📸 etc.) lanza UnicodeEncodeError y hace crashear el request completo —
# incluso si la operación (ej. subir a Drive) ya había tenido éxito. Forzamos
# UTF-8 con reemplazo silencioso como red de seguridad.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import os
import re
import shutil
import secrets
import calendar
import xmlrpc.client
import json
import bcrypt
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from config.db_manager import obtener_conexion
from fastapi import Request

# Credenciales y config sensible SIEMPRE desde variables de entorno (archivo
# .env local, nunca versionado en git — ver .env.example para la plantilla).
# Antes estaban escritas directo en este archivo: si este código llega a
# subirse a un repositorio (aunque sea privado) o a otra PC, esas credenciales
# reales de Odoo quedarían expuestas dentro del código fuente.
load_dotenv()

def _env_requerida(nombre):
    valor = os.getenv(nombre)
    if not valor:
        raise RuntimeError(
            f"Falta la variable de entorno {nombre}. Copiá .env.example a .env "
            f"y completá los valores reales antes de correr el servidor."
        )
    return valor

# Ancla las rutas a "webapp_static/" al directorio real de este archivo en
# vez de al working directory del proceso (que en el runtime serverless de
# Vercel no es necesariamente la carpeta del proyecto).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Cultura Tejida Dashboard API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # El frontend se sirve desde este mismo servidor (mismo origen), así que
    # nunca necesita CORS cross-origin con credenciales. allow_origins="*" +
    # allow_credentials=True dejaría que CUALQUIER sitio externo hiciera
    # peticiones autenticadas usando la cookie de sesión de un usuario que
    # tenga la pestaña de Cultura Tejida abierta. Con credentials en False, un
    # navegador nunca envía la cookie en una petición cross-origin.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ODOO_URL = _env_requerida("ODOO_URL")
ODOO_DB = _env_requerida("ODOO_DB")
ODOO_USER = _env_requerida("ODOO_USER")
ODOO_PASSWORD = _env_requerida("ODOO_PASSWORD")
TARGET_COMPANY_ID = int(os.getenv("ODOO_COMPANY_ID", "2"))

# Mismas 8 tiendas que aparecen en los <select> del frontend. Se usa para que
# el módulo de Metas Mensuales siempre muestre las 8, aunque alguna todavía no
# tenga ninguna orden/usuario que la haya creado en la tabla `tiendas`.
TIENDAS_CONOCIDAS = [
    "Lemaler Dorado", "Lemaler Village", "Lemaler Ceibos", "Lemaler Entrerios",
    "Lemaler Quito", "Mariola Village", "Mariola Ceibos", "Lemaler Scala",
]

@app.get("/")
def index():
    ruta_html = os.path.join(BASE_DIR, "webapp_static", "index.html")
    if os.path.exists(ruta_html):
        return FileResponse(ruta_html)
    return {"mensaje": "Servidor corriendo, pero falta el archivo webapp_static/index.html"}

# ─── PWA: manifest + service worker + íconos. El service worker se sirve
# desde la raíz (no desde /icons/...) a propósito — así su "scope" por
# default cubre TODA la app y no solo una subcarpeta.
@app.get("/manifest.json")
def manifest_pwa():
    return FileResponse(os.path.join(BASE_DIR, "webapp_static", "manifest.json"), media_type="application/manifest+json")

@app.get("/sw.js")
def service_worker():
    return FileResponse(os.path.join(BASE_DIR, "webapp_static", "sw.js"), media_type="application/javascript")

app.mount("/icons", StaticFiles(directory=os.path.join(BASE_DIR, "webapp_static", "icons")), name="icons")


def _normalizar_tienda(nombre):
    """Mismo criterio de comparación que usa el frontend (minúsculas, sin espacios),
    para que 'Lemaler Village' y 'lemaler village' se traten como la misma tienda."""
    return re.sub(r"\s+", "", (nombre or "").lower())

def _extension_archivo(upload):
    """Extensión real del archivo subido (imagen o PDF) a partir de su nombre,
    con fallback al content-type. Antes se guardaba siempre como .jpg sin
    importar el tipo real, lo cual corrompía los PDF subidos como comprobante."""
    nombre = (upload.filename or "") if upload else ""
    if "." in nombre:
        ext = nombre.rsplit(".", 1)[-1].lower().strip()
        if ext and ext.isalnum() and len(ext) <= 5:
            return ext
    if upload and upload.content_type == "application/pdf":
        return "pdf"
    return "jpg"

# Categorías fijas del Reporte de Cuadre, en este orden exacto. El nombre real
# del método de "efectivo" en Odoo varía por tienda (ej. "Caja Lemaler Quito"),
# así que se normaliza a "Caja" — todo lo demás se compara tal cual. "Giftcard"
# no existe como método de pago configurado en Odoo (no se puede cobrar así
# desde el POS), así que SIEMPRE se agrega a mano al listado — ver los usos de
# GIFTCARD_SIEMPRE_PRESENTE más abajo.
ORDEN_METODOS_CUADRE = ["Caja", "Cheque", "Transferencia", "Tarjeta", "Cuenta de cliente", "Giftcard"]
GIFTCARD_SIEMPRE_PRESENTE = "Giftcard"

def _normalizar_metodo_pago(nombre):
    """Reduce el nombre real de Odoo a una de las categorías fijas del
    Reporte de Cuadre. Si no coincide con ninguna, se deja el nombre tal cual
    (no se pierde el dato, solo no tiene una posición fija en el orden)."""
    n = (nombre or "").strip().lower()
    if n.startswith("caja"):
        return "Caja"
    if "cheque" in n:
        return "Cheque"
    if "transferencia" in n:
        return "Transferencia"
    if "gift" in n or "regalo" in n:
        return "Giftcard"
    if "tarjeta" in n:
        return "Tarjeta"
    if "cuenta" in n and "cliente" in n:
        return "Cuenta de cliente"
    return nombre or "No especificado"

def _ordenar_metodos_cuadre(nombres):
    """Ordena una lista de nombres de método según ORDEN_METODOS_CUADRE (sin
    duplicados); lo que no está en la lista fija va al final."""
    vistos = list(dict.fromkeys(nombres))
    fijos = [n for n in ORDEN_METODOS_CUADRE if n in vistos]
    resto = [n for n in vistos if n not in ORDEN_METODOS_CUADRE]
    return fijos + resto


# ══════════════════════════════════════════════════════════════════════════
# MÓDULO 0: AUTENTICACIÓN Y ROLES
# Cada usuario pertenece a una tienda (o ninguna si es admin). Un admin ve y
# gestiona todo; un usuario normal solo ve/opera su propia tienda, y no tiene
# acceso a Historial de Cierres (esa auditoría es exclusiva del admin).
# ══════════════════════════════════════════════════════════════════════════

SESSION_COOKIE_NAME = "sociaboss_session"
SESSION_DURATION_HORAS = 12


def _hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verificar_password(password, password_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def _usuario_publico(u):
    """Forma que ve el frontend: nunca incluye password_hash."""
    return {
        "id": u["id"],
        "nombre": u["nombre"],
        "email": u["email"],
        "rol": u["rol"],
        "tienda_id": u.get("tienda_id"),
        "tienda": u.get("tienda_nombre"),
        "activo": u.get("activo", True),
    }


def obtener_usuario_actual(request: Request):
    """Dependency que valida la cookie de sesión contra Postgres.
    El vencimiento se compara en Python (no en SQL) para no depender de
    cómo esté configurada la zona horaria de la sesión de Postgres."""
    from config.db_manager import RealDictCursor

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="No has iniciado sesión.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT u.id, u.nombre, u.email, u.rol, u.tienda_id, u.activo,
                   t.nombre AS tienda_nombre, s.expira_en
            FROM sesiones s
            JOIN usuarios u ON u.id = s.usuario_id
            LEFT JOIN tiendas t ON t.id = u.tienda_id
            WHERE s.token = %s
        """, (token,))
        fila = cursor.fetchone()
        if not fila or not fila["activo"] or fila["expira_en"] < datetime.utcnow():
            raise HTTPException(status_code=401, detail="Sesión inválida o expirada.")
        return fila
    finally:
        cursor.close()
        conexion.close()


def requerir_admin(usuario=Depends(obtener_usuario_actual)):
    # El superadmin es un superconjunto del admin: todo lo que puede hacer un
    # admin, también lo puede hacer el superadmin.
    if usuario["rol"] not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Solo un administrador puede hacer esto.")
    return usuario


def requerir_superadmin(usuario=Depends(obtener_usuario_actual)):
    # Para los módulos reservados exclusivamente al superadmin (KPIs,
    # Comisiones, Auditoría, Reporte Ejecutivo, Roles) — un admin normal NO
    # tiene acceso, a diferencia de requerir_admin.
    if usuario["rol"] != "superadmin":
        raise HTTPException(status_code=403, detail="Este módulo está reservado al superadministrador.")
    return usuario


MAX_INTENTOS_FALLIDOS = 5
MINUTOS_BLOQUEO_LOGIN = 15

def _registrar_auditoria(cursor, usuario, accion, detalle=None):
    """Deja constancia de una acción sensible. Recibe el cursor YA ABIERTO de
    la transacción en curso (no abre conexión propia) para que quede
    confirmada junto con la acción que registra, o revertida junto con ella
    si algo más en esa misma transacción falla."""
    cursor.execute(
        "INSERT INTO auditoria (usuario_id, usuario_nombre, accion, detalle) VALUES (%s, %s, %s, %s)",
        (usuario.get("id") if usuario else None, usuario.get("nombre") if usuario else "Sistema", accion, detalle)
    )

@app.post("/api/auth/login")
async def login(response: Response, request: Request):
    from config.db_manager import RealDictCursor

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Solicitud inválida.")

    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email y contraseña son obligatorios.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT u.id, u.nombre, u.email, u.password_hash, u.rol, u.activo,
                   u.tienda_id, t.nombre AS tienda_nombre, u.intentos_fallidos, u.bloqueado_hasta
            FROM usuarios u
            LEFT JOIN tiendas t ON t.id = u.tienda_id
            WHERE u.email = %s
        """, (email,))
        usuario = cursor.fetchone()

        # Bloqueo temporal por fuerza bruta: si ya está bloqueado, ni siquiera
        # se verifica la contraseña (así no se "gasta" el intento).
        if usuario and usuario.get("bloqueado_hasta") and usuario["bloqueado_hasta"] > datetime.utcnow():
            minutos_restantes = max(1, int((usuario["bloqueado_hasta"] - datetime.utcnow()).total_seconds() / 60))
            raise HTTPException(status_code=429, detail=f"Demasiados intentos fallidos. Probá de nuevo en {minutos_restantes} minuto(s).")

        if not usuario or not usuario["activo"] or not _verificar_password(password, usuario["password_hash"]):
            if usuario:
                nuevos_intentos = (usuario.get("intentos_fallidos") or 0) + 1
                if nuevos_intentos >= MAX_INTENTOS_FALLIDOS:
                    bloqueado_hasta = datetime.utcnow() + timedelta(minutes=MINUTOS_BLOQUEO_LOGIN)
                    cursor.execute("UPDATE usuarios SET intentos_fallidos = 0, bloqueado_hasta = %s WHERE id = %s", (bloqueado_hasta, usuario["id"]))
                    _registrar_auditoria(cursor, usuario, "login_bloqueado", f"{MAX_INTENTOS_FALLIDOS} intentos fallidos seguidos")
                else:
                    cursor.execute("UPDATE usuarios SET intentos_fallidos = %s WHERE id = %s", (nuevos_intentos, usuario["id"]))
                conexion.commit()
            raise HTTPException(status_code=401, detail="Email o contraseña incorrectos.")

        if usuario.get("intentos_fallidos"):
            cursor.execute("UPDATE usuarios SET intentos_fallidos = 0, bloqueado_hasta = NULL WHERE id = %s", (usuario["id"],))

        token = secrets.token_urlsafe(32)
        expira_en = datetime.utcnow() + timedelta(hours=SESSION_DURATION_HORAS)
        cursor.execute(
            "INSERT INTO sesiones (token, usuario_id, expira_en) VALUES (%s, %s, %s)",
            (token, usuario["id"], expira_en)
        )
        conexion.commit()

        response.set_cookie(
            key=SESSION_COOKIE_NAME, value=token, httponly=True, secure=True,
            samesite="lax", max_age=SESSION_DURATION_HORAS * 3600
        )
        return _usuario_publico(usuario)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


def _enviar_email(destinatario, asunto, cuerpo_html):
    """Manda un email por SMTP usando las variables de entorno SMTP_*. Si no
    están configuradas, no rompe nada — simplemente no se puede enviar (se
    devuelve False para que el endpoint que llama decida qué avisar)."""
    host = os.getenv("SMTP_HOST")
    if not host:
        return False
    puerto = int(os.getenv("SMTP_PORT", "587"))
    usuario_smtp = os.getenv("SMTP_USER")
    password_smtp = os.getenv("SMTP_PASSWORD")
    remitente = os.getenv("SMTP_FROM", usuario_smtp)

    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(cuerpo_html, "html", "utf-8")
    msg["Subject"] = asunto
    msg["From"] = remitente
    msg["To"] = destinatario
    try:
        with smtplib.SMTP(host, puerto, timeout=10) as servidor:
            servidor.starttls()
            if usuario_smtp and password_smtp:
                servidor.login(usuario_smtp, password_smtp)
            servidor.sendmail(remitente, [destinatario], msg.as_string())
        return True
    except Exception as e:
        print(f"⚠️ No se pudo enviar el email a {destinatario}: {e}")
        return False


MINUTOS_VALIDEZ_TOKEN_RECUPERACION = 60

@app.post("/api/auth/olvide-password")
async def olvide_password(request: Request):
    """Por seguridad, siempre responde igual exista o no el email — así
    nadie puede usar este endpoint para adivinar qué emails están
    registrados en el sistema."""
    from config.db_manager import RealDictCursor
    try:
        body = await request.json()
        email = (body.get("email") or "").strip().lower()
    except Exception:
        raise HTTPException(status_code=400, detail="Solicitud inválida.")

    mensaje_generico = {"status": "success", "mensaje": "Si ese email está registrado, te llegará un correo con instrucciones."}
    if not email:
        return mensaje_generico

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id, nombre, activo FROM usuarios WHERE email = %s", (email,))
        usuario = cursor.fetchone()
        if not usuario or not usuario["activo"]:
            return mensaje_generico

        token = secrets.token_urlsafe(32)
        expira_en = datetime.utcnow() + timedelta(minutes=MINUTOS_VALIDEZ_TOKEN_RECUPERACION)
        cursor.execute(
            "INSERT INTO tokens_recuperacion (token, usuario_id, expira_en) VALUES (%s, %s, %s)",
            (token, usuario["id"], expira_en)
        )
        conexion.commit()

        base_url = str(request.base_url).rstrip("/")
        link = f"{base_url}/?reset={token}"
        enviado = _enviar_email(
            email, "Recuperar contraseña — Cultura Tejida",
            f"""<p>Hola {usuario['nombre']},</p>
            <p>Pediste restablecer tu contraseña de Cultura Tejida. Este enlace vale por {MINUTOS_VALIDEZ_TOKEN_RECUPERACION} minutos:</p>
            <p><a href="{link}">{link}</a></p>
            <p>Si no fuiste vos, ignorá este correo — tu contraseña actual sigue funcionando igual.</p>"""
        )
        if not enviado:
            print(f"ℹ️ SMTP no configurado — link de recuperación para {email}: {link}")
        return mensaje_generico
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/auth/resetear-password")
async def resetear_password(request: Request):
    from config.db_manager import RealDictCursor
    try:
        body = await request.json()
        token = (body.get("token") or "").strip()
        password_nueva = body.get("password") or ""
    except Exception:
        raise HTTPException(status_code=400, detail="Solicitud inválida.")

    if not token or len(password_nueva) < 6:
        raise HTTPException(status_code=400, detail="Token y una contraseña de al menos 6 caracteres son obligatorios.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT usuario_id, expira_en, usado FROM tokens_recuperacion WHERE token = %s", (token,))
        fila = cursor.fetchone()
        if not fila or fila["usado"] or fila["expira_en"] < datetime.utcnow():
            raise HTTPException(status_code=400, detail="Este enlace ya no es válido — pedí uno nuevo desde 'Olvidé mi contraseña'.")

        cursor.execute("UPDATE usuarios SET password_hash = %s, intentos_fallidos = 0, bloqueado_hasta = NULL WHERE id = %s",
                        (_hash_password(password_nueva), fila["usuario_id"]))
        cursor.execute("UPDATE tokens_recuperacion SET usado = TRUE WHERE token = %s", (token,))
        # Cierra cualquier sesión activa — si alguien más tenía la cuenta abierta, queda afuera.
        cursor.execute("DELETE FROM sesiones WHERE usuario_id = %s", (fila["usuario_id"],))
        conexion.commit()
        return {"status": "success", "mensaje": "Contraseña actualizada. Ya podés iniciar sesión."}
    except HTTPException:
        raise
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        conexion = obtener_conexion()
        cursor = conexion.cursor()
        try:
            cursor.execute("DELETE FROM sesiones WHERE token = %s", (token,))
            conexion.commit()
        finally:
            cursor.close()
            conexion.close()
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"status": "success"}


@app.get("/api/auth/me")
def quien_soy(usuario=Depends(obtener_usuario_actual)):
    return _usuario_publico(usuario)


# ─── GESTIÓN DE USUARIOS (admin y superadmin) — crear, editar datos básicos,
# activar/desactivar. El rol en sí (crear_usuario lo fija solo a 'admin' o
# 'usuario'; para 'superadmin', o para cambiar el rol de alguien ya
# existente, ver el módulo ROLES más abajo, exclusivo de superadmin) ───
@app.get("/api/usuarios")
def listar_usuarios(admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT u.id, u.nombre, u.email, u.rol, u.activo, u.tienda_id, t.nombre AS tienda_nombre
            FROM usuarios u
            LEFT JOIN tiendas t ON t.id = u.tienda_id
            ORDER BY u.rol DESC, u.nombre
        """)
        return [_usuario_publico(u) for u in cursor.fetchall()]
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/usuarios")
async def crear_usuario(request: Request, admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor

    body = await request.json()
    nombre = (body.get("nombre") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    rol = body.get("rol") or "usuario"
    tienda_nombre = (body.get("tienda") or "").strip()

    if rol not in ("admin", "usuario"):
        raise HTTPException(status_code=400, detail="Rol inválido.")
    if not nombre or not email or len(password) < 6:
        raise HTTPException(status_code=400, detail="Nombre, email y una contraseña de al menos 6 caracteres son obligatorios.")
    if rol == "usuario" and not tienda_nombre:
        raise HTTPException(status_code=400, detail="Un usuario (no-admin) debe tener una tienda asignada.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id FROM usuarios WHERE email = %s", (email,))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="Ya existe un usuario con ese email.")

        tienda_id = None
        if tienda_nombre:
            cursor.execute("INSERT INTO tiendas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (tienda_nombre,))
            cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_nombre,))
            tienda_id = cursor.fetchone()["id"]

        cursor.execute("""
            INSERT INTO usuarios (nombre, email, password_hash, rol, tienda_id)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (nombre, email, _hash_password(password), rol, tienda_id))
        nuevo_id = cursor.fetchone()["id"]
        _registrar_auditoria(cursor, admin, "crear_usuario", f"{nombre} <{email}> — rol: {rol}" + (f", tienda: {tienda_nombre}" if tienda_nombre else ""))
        conexion.commit()
        return {"status": "success", "id": nuevo_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


@app.put("/api/usuarios/{usuario_id}")
async def editar_usuario(usuario_id: int, request: Request, admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor

    body = await request.json()
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id, rol FROM usuarios WHERE id = %s", (usuario_id,))
        existente = cursor.fetchone()
        if not existente:
            raise HTTPException(status_code=404, detail="Usuario no encontrado.")

        # Un admin normal no puede tocar (desactivar, resetear contraseña,
        # renombrar) la cuenta de un superadmin — solo otro superadmin puede.
        if existente["rol"] == "superadmin" and admin["rol"] != "superadmin":
            raise HTTPException(status_code=403, detail="Solo un superadministrador puede editar esta cuenta.")

        campos, valores = [], []

        if "nombre" in body:
            campos.append("nombre = %s")
            valores.append((body.get("nombre") or "").strip())

        nuevo_rol = body.get("rol", existente["rol"])
        if "rol" in body:
            if nuevo_rol not in ("admin", "usuario"):
                raise HTTPException(status_code=400, detail="Rol inválido.")
            campos.append("rol = %s")
            valores.append(nuevo_rol)

        if "tienda" in body:
            tienda_nombre = (body.get("tienda") or "").strip()
            if nuevo_rol == "usuario" and not tienda_nombre:
                raise HTTPException(status_code=400, detail="Un usuario (no-admin) debe tener una tienda asignada.")
            tienda_id = None
            if tienda_nombre:
                cursor.execute("INSERT INTO tiendas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (tienda_nombre,))
                cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_nombre,))
                tienda_id = cursor.fetchone()["id"]
            campos.append("tienda_id = %s")
            valores.append(tienda_id)

        if "activo" in body:
            if usuario_id == admin["id"] and not body.get("activo"):
                raise HTTPException(status_code=400, detail="No puedes desactivar tu propio usuario.")
            campos.append("activo = %s")
            valores.append(bool(body.get("activo")))

        if "password" in body and body.get("password"):
            if len(body["password"]) < 6:
                raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres.")
            campos.append("password_hash = %s")
            valores.append(_hash_password(body["password"]))

        if not campos:
            return {"status": "success", "mensaje": "Nada que actualizar."}

        valores.append(usuario_id)
        cursor.execute(f"UPDATE usuarios SET {', '.join(campos)} WHERE id = %s", valores)

        # Si se desactivó o cambió la contraseña, invalidamos sus sesiones activas
        if "activo" in body and not body.get("activo"):
            cursor.execute("DELETE FROM sesiones WHERE usuario_id = %s", (usuario_id,))
        if "password" in body and body.get("password"):
            cursor.execute("DELETE FROM sesiones WHERE usuario_id = %s", (usuario_id,))

        _registrar_auditoria(cursor, admin, "editar_usuario", f"usuario_id={usuario_id} — campos: {', '.join(c.split(' =')[0] for c in campos)}")
        conexion.commit()
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ─── MÓDULO ROLES (solo superadmin) — otorgar/quitar el rol "admin" o
# "superadmin" es más sensible que la edición general de un usuario (nombre,
# tienda, contraseña, activo/inactivo, que sigue en /api/usuarios y la maneja
# cualquier admin), así que queda en un endpoint y una pantalla aparte,
# reservados exclusivamente al superadmin. ───
@app.put("/api/usuarios/{usuario_id}/rol")
async def cambiar_rol_usuario(usuario_id: int, request: Request, superadmin=Depends(requerir_superadmin)):
    from config.db_manager import RealDictCursor

    body = await request.json()
    nuevo_rol = (body.get("rol") or "").strip()
    if nuevo_rol not in ("usuario", "admin", "superadmin"):
        raise HTTPException(status_code=400, detail="Rol inválido.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id, nombre, rol, tienda_id, activo FROM usuarios WHERE id = %s", (usuario_id,))
        existente = cursor.fetchone()
        if not existente:
            raise HTTPException(status_code=404, detail="Usuario no encontrado.")

        if nuevo_rol == existente["rol"]:
            return {"status": "success", "mensaje": "Ese usuario ya tiene ese rol."}

        if nuevo_rol == "usuario" and not existente["tienda_id"]:
            raise HTTPException(
                status_code=400,
                detail="Este usuario no tiene tienda asignada — asignale una desde Usuarios antes de bajarlo a rol 'usuario'."
            )

        # No dejamos que el único superadmin activo se quite ese rol a sí
        # mismo — se quedaría sin nadie con acceso al módulo de Roles.
        if usuario_id == superadmin["id"] and existente["rol"] == "superadmin":
            cursor.execute("SELECT COUNT(*) AS total FROM usuarios WHERE rol = 'superadmin' AND activo = TRUE")
            if cursor.fetchone()["total"] <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="No podés quitarte el rol de superadministrador siendo el único activo — asignaselo a otra persona primero."
                )

        campos = ["rol = %s"]
        valores = [nuevo_rol]
        # admin y superadmin operan a nivel de toda la empresa, no de una
        # tienda puntual — si se promueve a alguien, se le suelta la tienda.
        if nuevo_rol in ("admin", "superadmin"):
            campos.append("tienda_id = NULL")
        valores.append(usuario_id)
        cursor.execute(f"UPDATE usuarios SET {', '.join(campos)} WHERE id = %s", valores)

        # Fuerza a re-loguearse: así el cambio de permisos se refleja de
        # inmediato en vez de quedar con el rol viejo hasta que la sesión expire.
        cursor.execute("DELETE FROM sesiones WHERE usuario_id = %s", (usuario_id,))

        _registrar_auditoria(cursor, superadmin, "cambiar_rol", f"{existente['nombre']}: {existente['rol']} → {nuevo_rol}")
        conexion.commit()
        return {"status": "success", "mensaje": "Rol actualizado."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ─── MÓDULO 1: CONSULTA DE ÓRDENES EN TIEMPO REAL CON DETALLES (POS) ───

_ODOO_TZ_CACHE = {}

def _obtener_tz_odoo(models, uid):
    """Lee la zona horaria configurada para el usuario en Odoo (res.users.tz)
    y la cachea en memoria (no cambia entre requests). Se usa para que el
    rango de fechas consultado coincida exactamente con lo que Odoo muestra
    como "de hoy" en su propia interfaz, ya que date_order se guarda en UTC."""
    if uid in _ODOO_TZ_CACHE:
        return _ODOO_TZ_CACHE[uid]
    tz_name = "UTC"
    try:
        usuario = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'res.users', 'read',
            [[uid]], {'fields': ['tz']}
        )
        if usuario and usuario[0].get('tz'):
            tz_name = usuario[0]['tz']
    except Exception:
        pass
    _ODOO_TZ_CACHE[uid] = tz_name
    return tz_name


@app.get("/api/ventas")
def obtener_ventas(fecha: str = None, usuario=Depends(obtener_usuario_actual)):
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise HTTPException(status_code=401, detail="Fallo de autenticación con Odoo")

        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        # ─── Rango de fecha calculado en la ZONA HORARIA de Odoo, convertido a UTC ───
        try:
            tz_odoo = ZoneInfo(_obtener_tz_odoo(models, uid))
        except Exception:
            tz_odoo = timezone.utc

        if not fecha:
            fecha = datetime.now(tz_odoo).strftime("%Y-%m-%d")

        inicio_local = datetime.strptime(fecha, "%Y-%m-%d").replace(tzinfo=tz_odoo)
        fin_local = inicio_local + timedelta(days=1)
        inicio_utc = inicio_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        fin_utc = fin_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        filtros = [
            ['company_id', '=', TARGET_COMPANY_ID],
            ['date_order', '>=', inicio_utc],
            ['date_order', '<', fin_utc],
            # Solo órdenes validadas (paid/done/invoiced): 'draft' aún no está
            # cerrada/pagada y 'cancel' no es una venta real, así el total
            # coincide con lo que Odoo reporta como venta efectiva.
            ['state', 'not in', ['draft', 'cancel']],
        ]

        sales = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read',
            [filtros], {
                # 'cashier' es el nombre del cajero real (campo de texto directo).
                # OJO: 'user_id' en pos.order NO es el cajero — en esta instancia
                # devuelve el punto de venta responsable (ej. "Lemaler Entrerios"),
                # así que usarlo como "quién hizo la venta" estaba mal.
                # 'l10n_ec_invoice_number' es el número de factura (localización EC).
                'fields': ['name', 'amount_total', 'config_id', 'company_id', 'lines', 'payment_ids', 'cashier', 'l10n_ec_invoice_number'],
                'order': 'date_order desc'
            }
        )

        # ─── Batch: UNA sola llamada XML-RPC para TODAS las líneas y TODOS los pagos ───
        # (antes se hacían 2 llamadas extra POR CADA orden → N+1 contra Odoo)
        todos_line_ids = [lid for sale in sales for lid in sale.get('lines', [])]
        todos_payment_ids = [pid for sale in sales for pid in sale.get('payment_ids', [])]

        lineas_por_orden = {}
        if todos_line_ids:
            lines_data = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.order.line', 'read',
                [todos_line_ids], {'fields': ['order_id', 'product_id', 'qty', 'price_subtotal_incl']}
            )
            for line in lines_data:
                orden_id = line['order_id'][0] if line.get('order_id') else None
                lineas_por_orden.setdefault(orden_id, []).append({
                    "nombre": line['product_id'][1] if line['product_id'] else "Producto",
                    "cantidad": line.get('qty', 1),
                    "subtotal": line.get('price_subtotal_incl', 0.0)
                })

        pagos_por_orden = {}
        if todos_payment_ids:
            payments_data = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.payment', 'read',
                [todos_payment_ids], {'fields': ['pos_order_id', 'payment_method_id', 'amount']}
            )
            for pago in payments_data:
                orden_id = pago['pos_order_id'][0] if pago.get('pos_order_id') else None
                pagos_por_orden.setdefault(orden_id, []).append({
                    "metodo": pago['payment_method_id'][1] if pago['payment_method_id'] else "No especificado",
                    "monto": pago.get('amount', 0.0)
                })

        lista_ventas = []
        for sale in sales:
            tienda_nombre = sale['config_id'][1] if sale['config_id'] else "Cultura Tejida Local"
            pagos = pagos_por_orden.get(sale['id'], [])
            # Pago mixto (ej. efectivo + tarjeta): mostramos TODOS los métodos usados
            # en vez de solo el primero, para que coincida con el desglose real de Odoo.
            tipo_pago = " + ".join(dict.fromkeys(p["metodo"] for p in pagos)) or "No especificado"

            lista_ventas.append({
                "orden_id": sale.get('name'),
                "tienda": tienda_nombre,
                "usuario": sale.get('cashier') or "Cajero",
                "numero_factura": sale.get('l10n_ec_invoice_number') or None,
                "total_venta": sale.get('amount_total', 0.0),
                "tipo_pago": tipo_pago,
                "pagos": pagos,
                "productos": lineas_por_orden.get(sale['id'], [])
            })

        # ─── 🔗 CRUCE DE DATOS CON POSTGRESQL ───
        from config.db_manager import RealDictCursor
        conexion = obtener_conexion()
        cursor = conexion.cursor(cursor_factory=RealDictCursor)
        
        try:
            cursor.execute("""
                SELECT orden_id, id_google_drive 
                FROM evidencias_ordenes
            """)
            comprobantes = cursor.fetchall()
            
            mapa_comprobantes = {c['orden_id'].strip(): c['id_google_drive'] for c in comprobantes}
            
            for venta in lista_ventas:
                id_orden = venta["orden_id"].strip() if venta["orden_id"] else ""
                if id_orden in mapa_comprobantes:
                    venta["comprobante_subido"] = True
                    venta["drive_id"] = mapa_comprobantes[id_orden]
                    # URL directa para que el frontend pueda mostrar/enlazar la imagen
                    # sin tener que reconstruirla a mano (antes solo se mandaba el ID).
                    venta["comprobante_url"] = f"https://drive.google.com/file/d/{venta['drive_id']}/view"
                    venta["estado_comprobante"] = "Enviado"
                else:
                    venta["comprobante_subido"] = False
                    venta["drive_id"] = None
                    venta["comprobante_url"] = None
                    venta["estado_comprobante"] = "Pendiente"

            # ─── Alcance por rol: un usuario normal solo ve SU tienda ───
            if usuario["rol"] not in ("admin", "superadmin"):
                if not usuario["tienda_id"]:
                    lista_ventas = []
                else:
                    tienda_usuario_norm = _normalizar_tienda(usuario["tienda_nombre"])
                    lista_ventas = [
                        v for v in lista_ventas
                        if _normalizar_tienda(v["tienda"]) == tienda_usuario_norm
                    ]
                    # Si esta tienda ya "envió" el reporte diario de esta fecha
                    # (botón "Generar e Iniciar Reporte Diario"), esas órdenes YA
                    # NO se editan más — pero siguen apareciendo, como una foto
                    # congelada tomada en ese momento (desde ventas_registradas,
                    # no en vivo desde Odoo). El admin sigue viendo todo en vivo
                    # siempre, sin este congelamiento.
                    cursor.execute("""
                        SELECT 1 FROM consolidado_ventas_diarias
                        WHERE fecha = %s AND tienda_id = %s
                    """, (fecha, usuario["tienda_id"]))
                    if cursor.fetchone():
                        cursor.execute("""
                            SELECT id, orden_id, total_venta, tipo_pago, facturado_por, numero_factura, comprobante_url
                            FROM ventas_registradas WHERE fecha = %s AND tienda_id = %s ORDER BY orden_id
                        """, (fecha, usuario["tienda_id"]))
                        filas_congeladas = cursor.fetchall()

                        lista_ventas = []
                        for f in filas_congeladas:
                            cursor.execute(
                                "SELECT nombre_producto, cantidad, subtotal FROM venta_productos WHERE venta_id = %s",
                                (f['id'],)
                            )
                            productos = cursor.fetchall()
                            lista_ventas.append({
                                "orden_id": f['orden_id'],
                                "tienda": usuario["tienda_nombre"],
                                "usuario": f['facturado_por'],
                                "numero_factura": f['numero_factura'],
                                "total_venta": f['total_venta'],
                                "tipo_pago": f['tipo_pago'],
                                "pagos": [],
                                "productos": [
                                    {"nombre": p['nombre_producto'], "cantidad": p['cantidad'], "subtotal": p['subtotal']}
                                    for p in productos
                                ],
                                "comprobante_subido": bool(f['comprobante_url']),
                                "drive_id": None,
                                "comprobante_url": f['comprobante_url'],
                                "estado_comprobante": "Enviado" if f['comprobante_url'] else "Pendiente",
                                "solo_lectura": True
                            })

        except Exception as db_e:
            print(f"Advertencia: No se pudo cruzar información de evidencias: {str(db_e)}")
        finally:
            cursor.close()
            conexion.close()

        return lista_ventas
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Odoo POS: {str(e)}")

# ─── MÓDULO 1.1: GUARDAR COMPROBANTE INDIVIDUAL POR VENTA ───
@app.post("/api/ventas/comprobante")
async def subir_comprobante_venta(
    orden_id: str = Form(...),
    tienda: str = Form(...),
    comprobante: UploadFile = File(...),
    usuario=Depends(obtener_usuario_actual)
):
    if usuario["rol"] not in ("admin", "superadmin") and _normalizar_tienda(tienda) != _normalizar_tienda(usuario["tienda_nombre"]):
        raise HTTPException(status_code=403, detail="No puedes subir comprobantes de otra tienda.")

    # Si esta orden ya quedó congelada (su tienda+fecha ya "envió" el reporte
    # diario), un usuario normal ya no puede seguir editándola. El admin sí.
    if usuario["rol"] not in ("admin", "superadmin"):
        conexion_chk = obtener_conexion()
        cursor_chk = conexion_chk.cursor()
        try:
            cursor_chk.execute("SELECT 1 FROM ventas_registradas WHERE orden_id = %s", (orden_id,))
            ya_congelada = cursor_chk.fetchone() is not None
        finally:
            cursor_chk.close()
            conexion_chk.close()
        if ya_congelada:
            raise HTTPException(status_code=403, detail="Esta orden ya fue enviada en el reporte diario y no se puede editar.")

    try:
        from config.drive_manager import subir_archivo_a_drive
        from config.db_manager import RealDictCursor

        orden_limpia = orden_id.replace("/", "-").replace(" ", "_")
        ruta_t = f"temp_comp_{comprobante.filename}"

        with open(ruta_t, "wb") as b:
            shutil.copyfileobj(comprobante.file, b)

        drive_id = subir_archivo_a_drive(ruta_t, f"COMPROBANTE_{orden_limpia}.{_extension_archivo(comprobante)}", comprobante.content_type)

        if os.path.exists(ruta_t):
            os.remove(ruta_t)

        # La tienda viene del propio registro de Odoo (config_id) que ya trae el
        # frontend en memoria — YA NO se adivina parseando el orden_id. Antes, un
        # reembolso como "REEMBOLSO DE Lemaler Village/2943" partía mal el nombre
        # y creaba una tienda fantasma ("REEMBOLSO DE Lemaler Village") en Postgres.
        tienda_detectada = tienda.strip() or "Cultura Tejida Local"

        conexion = obtener_conexion()
        cursor = conexion.cursor(cursor_factory=RealDictCursor)
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        
        # 🚨 CLAVE: Aseguramos que la tienda exista en Postgres antes de meter la evidencia
        cursor.execute("""
            INSERT INTO tiendas (nombre) VALUES (%s) 
            ON CONFLICT (nombre) DO NOTHING
        """, (tienda_detectada,))
        
        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_detectada,))
        tienda_id = cursor.fetchone()['id']

        cursor.execute("""
            INSERT INTO evidencias_ordenes (fecha, orden_id, tienda_id, id_google_drive)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (orden_id) DO UPDATE SET id_google_drive = EXCLUDED.id_google_drive
        """, (fecha_hoy, orden_id, tienda_id, drive_id))
        
        conexion.commit()
        cursor.close()
        conexion.close()
            
        comprobante_url = f"https://drive.google.com/file/d/{drive_id}/view" if drive_id else None
        return {
            "status": "success",
            "mensaje": f"Comprobante guardado.",
            "drive_id": drive_id,
            "comprobante_url": comprobante_url
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al subir comprobante: {str(e)}")

# ─── MÓDULO 1.15: RESUMEN DE CUADRE EN VIVO (para la pantalla de confirmación
# que se muestra al hacer clic en "Generar e Iniciar Reporte Diario") ───
# A diferencia de /api/reporte-cuadre (que lee de Postgres YA congelado),
# este consulta Odoo en vivo, en el momento exacto del clic, para poder
# comparar contra lo que el navegador tiene cargado en pantalla y detectar
# si algo cambió en Odoo desde que se cargó la página (venta nueva, orden
# anulada, etc.) antes de congelar el día.
@app.get("/api/ventas/resumen-cuadre")
def obtener_resumen_cuadre_vivo(fecha: str, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    tienda_objetivo = usuario["tienda_nombre"] if usuario["rol"] not in ("admin", "superadmin") else tienda
    if not tienda_objetivo:
        raise HTTPException(
            status_code=400,
            detail="Tu usuario no tiene una tienda asignada." if usuario["rol"] not in ("admin", "superadmin") else "Debes indicar una tienda."
        )

    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise HTTPException(status_code=401, detail="Fallo de autenticación con Odoo")

        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        try:
            tz_odoo = ZoneInfo(_obtener_tz_odoo(models, uid))
        except Exception:
            tz_odoo = timezone.utc

        inicio_local = datetime.strptime(fecha, "%Y-%m-%d").replace(tzinfo=tz_odoo)
        fin_local = inicio_local + timedelta(days=1)
        inicio_utc = inicio_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        fin_utc = fin_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        config_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'pos.config', 'search', [[['name', '=', tienda_objetivo]]]
        )

        # Métodos habilitados para esta tienda: siempre aparecen en la lista,
        # aunque no se haya usado ninguno ese día (mismo criterio que /api/reporte-cuadre).
        nombres_metodos_odoo = []
        if config_ids:
            configs = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.config', 'read', [config_ids], {'fields': ['payment_method_ids']}
            )
            ids_metodo = configs[0]['payment_method_ids'] if configs else []
            if ids_metodo:
                metodos = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD, 'pos.payment.method', 'read', [ids_metodo], {'fields': ['name']}
                )
                nombres_metodos_odoo = [_normalizar_metodo_pago(m['name']) for m in metodos]

        ordenes = []
        if config_ids:
            filtros = [
                ['config_id', 'in', config_ids],
                ['company_id', '=', TARGET_COMPANY_ID],
                ['date_order', '>=', inicio_utc],
                ['date_order', '<', fin_utc],
                ['state', 'not in', ['draft', 'cancel']],
            ]
            ordenes = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read',
                [filtros], {'fields': ['amount_total', 'payment_ids', 'lines']}
            )

        todos_payment_ids = [pid for o in ordenes for pid in o.get('payment_ids', [])]
        monto_por_metodo = {}
        cantidad_por_metodo = {}
        if todos_payment_ids:
            pagos = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.payment', 'read',
                [todos_payment_ids], {'fields': ['payment_method_id', 'amount']}
            )
            for p in pagos:
                nombre = _normalizar_metodo_pago(p['payment_method_id'][1] if p.get('payment_method_id') else "No especificado")
                monto_por_metodo[nombre] = monto_por_metodo.get(nombre, 0.0) + (p.get('amount', 0.0) or 0.0)
                cantidad_por_metodo[nombre] = cantidad_por_metodo.get(nombre, 0) + 1

        # Giftcard NO es un método de pago en Odoo (no hay pos.payment con ese
        # nombre) — se aplica como una línea de PRODUCTO/descuento dentro de la
        # orden (ej. "Tarjeta de regalo Descuento", en negativo). La detectamos
        # ahí, en las líneas de la orden, y la sumamos como si fuera un método más.
        todos_line_ids = [lid for o in ordenes for lid in o.get('lines', [])]
        if todos_line_ids:
            lineas = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.order.line', 'read',
                [todos_line_ids], {'fields': ['product_id', 'price_subtotal_incl']}
            )
            for l in lineas:
                nombre_prod = (l['product_id'][1] if l.get('product_id') else '') or ''
                if 'regalo' in nombre_prod.lower() or 'gift' in nombre_prod.lower():
                    monto_por_metodo['Giftcard'] = monto_por_metodo.get('Giftcard', 0.0) + abs(l.get('price_subtotal_incl', 0.0) or 0.0)
                    cantidad_por_metodo['Giftcard'] = cantidad_por_metodo.get('Giftcard', 0) + 1

        nombres_metodo = _ordenar_metodos_cuadre(nombres_metodos_odoo + list(monto_por_metodo.keys()) + [GIFTCARD_SIEMPRE_PRESENTE])

        por_metodo = [
            {"metodo": n, "monto": round(monto_por_metodo.get(n, 0.0), 2), "cantidad": cantidad_por_metodo.get(n, 0)}
            for n in nombres_metodo
        ]

        return {
            "tienda": tienda_objetivo,
            "fecha": fecha,
            "total_general": round(sum(o.get('amount_total', 0.0) or 0.0 for o in ordenes), 2),
            "cantidad_ordenes": len(ordenes),
            "por_metodo": por_metodo
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Odoo POS: {str(e)}")


# ─── MÓDULO 1.2: BOTÓN ENVIAR / CONSOLIDAR REPORTES DIARIOS ───
@app.post("/api/ventas/consolidar")
def consolidar_ventas(
    fecha: str = Form(...), tienda_nombre: str = Form(...),
    total_odoo: float = Form(...), cantidad_ordenes: int = Form(...),
    ordenes_detalle: str = Form("[]"),
    ajuste_metodos_pago: str = Form("[]"),
    conteo_fisico: str = Form("[]"),
    usuario=Depends(obtener_usuario_actual)
):
    # Un usuario normal solo puede enviar el reporte diario de SU PROPIA tienda,
    # sin importar qué mande el cliente (se ignora/sobreescribe).
    if usuario["rol"] not in ("admin", "superadmin"):
        if not usuario["tienda_nombre"]:
            raise HTTPException(status_code=403, detail="Tu usuario no tiene una tienda asignada.")
        tienda_nombre = usuario["tienda_nombre"]

    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        # Adaptación PostgreSQL: ON CONFLICT DO NOTHING reemplaza a INSERT OR IGNORE
        cursor.execute("""
            INSERT INTO tiendas (nombre) VALUES (%s)
            ON CONFLICT (nombre) DO NOTHING
        """, (tienda_nombre,))

        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_nombre,))
        tienda_id = cursor.fetchone()['id']

        # "Pagos" por método viene de Odoo en vivo (pantalla previa de confirmación,
        # sin editar) — se congela acá como snapshot exacto del momento en que se
        # generó el reporte, fuente de verdad para el Reporte de Cuadre. El Conteo
        # Físico es lo que sí ingresó la persona a mano, comparado contra "Pagos".
        try:
            json.loads(ajuste_metodos_pago)  # valida que sea JSON antes de guardarlo
        except Exception:
            ajuste_metodos_pago = "[]"
        try:
            json.loads(conteo_fisico)
        except Exception:
            conteo_fisico = "[]"

        # Adaptación PostgreSQL: UPSERT nativo con ON CONFLICT
        cursor.execute("""
            INSERT INTO consolidado_ventas_diarias (fecha, tienda_id, total_odoo, cantidad_ordenes, ajuste_metodos_pago, conteo_fisico)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (fecha, tienda_id) DO UPDATE SET
                total_odoo = EXCLUDED.total_odoo,
                cantidad_ordenes = EXCLUDED.cantidad_ordenes,
                ajuste_metodos_pago = EXCLUDED.ajuste_metodos_pago,
                conteo_fisico = EXCLUDED.conteo_fisico
        """, (fecha, tienda_id, total_odoo, cantidad_ordenes, ajuste_metodos_pago, conteo_fisico))

        # ─── Registro interno: productos y quién facturó cada venta ───
        # Este es el momento en que la tienda "termina" con las ventas del día,
        # así que es el punto natural para guardarlas de forma permanente en
        # Postgres (Odoo ya las trae, pero no queremos depender solo de Odoo).
        # No se muestra en ninguna pantalla — es la fuente que usa Cierre de
        # Caja para armar sus "pedidos" en vez de la caché del navegador.
        try:
            detalle = json.loads(ordenes_detalle) if ordenes_detalle else []
        except Exception:
            detalle = []

        for orden in detalle:
            orden_id = orden.get("orden_id")
            if not orden_id:
                continue
            cursor.execute("""
                INSERT INTO ventas_registradas (orden_id, fecha, tienda_id, total_venta, tipo_pago, facturado_por, numero_factura, comprobante_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (orden_id) DO UPDATE SET
                    total_venta = EXCLUDED.total_venta,
                    tipo_pago = EXCLUDED.tipo_pago,
                    facturado_por = EXCLUDED.facturado_por,
                    numero_factura = EXCLUDED.numero_factura,
                    comprobante_url = EXCLUDED.comprobante_url
                RETURNING id
            """, (
                orden_id, fecha, tienda_id,
                orden.get("total_venta", 0) or 0,
                orden.get("tipo_pago"),
                orden.get("usuario"),  # quién facturó — Odoo lo trae como 'cashier' de la orden POS
                orden.get("numero_factura"),
                orden.get("comprobante_url")
            ))
            venta_id = cursor.fetchone()["id"]

            # Reemplazamos el detalle de productos por si se reenvía (idempotente)
            cursor.execute("DELETE FROM venta_productos WHERE venta_id = %s", (venta_id,))
            for p in (orden.get("productos") or []):
                cursor.execute("""
                    INSERT INTO venta_productos (venta_id, nombre_producto, cantidad, subtotal)
                    VALUES (%s, %s, %s, %s)
                """, (venta_id, p.get("nombre", "Producto"), p.get("cantidad", 1) or 1, p.get("subtotal", 0) or 0))

            # Desglose de pagos (efectivo, tarjeta, etc.) — es lo que alimenta
            # el Reporte de Cuadre de Caja, que ya no puede leer esto en vivo
            # de Odoo una vez que la tienda "envió" el día.
            cursor.execute("DELETE FROM venta_pagos WHERE venta_id = %s", (venta_id,))
            for pago in (orden.get("pagos") or []):
                cursor.execute("""
                    INSERT INTO venta_pagos (venta_id, metodo, monto)
                    VALUES (%s, %s, %s)
                """, (venta_id, pago.get("metodo", "No especificado"), pago.get("monto", 0) or 0))

        conexion.commit()
        return {"status": "success", "mensaje": f"Reporte de {tienda_nombre} consolidado correctamente para el {fecha}."}
    except Exception as e:
        conexion.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ─── MÓDULO 1.3: BASE DE APERTURA DIARIA (fondo inicial de caja) ───
# Se escribe a mano en Monitoreo de Órdenes, una por tienda+fecha, y queda
# guardada en Postgres. Alimenta el campo "Base de apertura" del Reporte de
# Cuadre de Caja (antes quedaba en blanco para llenar a mano en el papel).
@app.get("/api/apertura")
def obtener_apertura(fecha: str, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    tienda_objetivo = usuario["tienda_nombre"] if usuario["rol"] not in ("admin", "superadmin") else tienda
    if not tienda_objetivo:
        return {"monto": None, "registrado_por": None, "solo_lectura": False}

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute("""
            SELECT a.monto, a.registrado_por
            FROM apertura_caja_diaria a
            JOIN tiendas t ON t.id = a.tienda_id
            WHERE a.fecha = %s AND t.nombre = %s
        """, (fecha, tienda_objetivo))
        fila = cursor.fetchone()

        # Una vez que la tienda "envió" el reporte diario de esta fecha, el día
        # queda congelado para un usuario normal (mismo criterio que Monitoreo
        # de Órdenes) — la base de apertura tampoco se puede seguir editando.
        solo_lectura = False
        if usuario["rol"] not in ("admin", "superadmin"):
            cursor.execute("""
                SELECT 1 FROM consolidado_ventas_diarias c
                JOIN tiendas t ON t.id = c.tienda_id
                WHERE c.fecha = %s AND t.nombre = %s
            """, (fecha, tienda_objetivo))
            solo_lectura = cursor.fetchone() is not None

        return {
            "monto": fila[0] if fila else None,
            "registrado_por": fila[1] if fila else None,
            "solo_lectura": solo_lectura
        }
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/apertura")
async def guardar_apertura(request: Request, usuario=Depends(obtener_usuario_actual)):
    try:
        payload = json.loads((await request.body()).decode("utf-8"))
        fecha = payload.get("fecha")
        tienda = payload.get("tienda")
        monto = float(payload.get("monto", 0) or 0)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error procesando JSON: {str(e)}")

    if not fecha:
        raise HTTPException(status_code=400, detail="Se requiere fecha.")

    # Un usuario normal solo puede registrar la apertura de SU PROPIA tienda.
    if usuario["rol"] not in ("admin", "superadmin"):
        if not usuario["tienda_nombre"]:
            raise HTTPException(status_code=403, detail="Tu usuario no tiene una tienda asignada.")
        tienda = usuario["tienda_nombre"]
    if not tienda:
        raise HTTPException(status_code=400, detail="Se requiere tienda.")

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute("""
            INSERT INTO tiendas (nombre) VALUES (%s)
            ON CONFLICT (nombre) DO NOTHING
        """, (tienda,))
        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda,))
        tienda_id = cursor.fetchone()[0]

        # Congelado para usuarios normales (no para admin), mismo criterio que
        # el resto de Monitoreo de Órdenes una vez enviado el reporte diario.
        if usuario["rol"] not in ("admin", "superadmin"):
            cursor.execute("""
                SELECT 1 FROM consolidado_ventas_diarias WHERE fecha = %s AND tienda_id = %s
            """, (fecha, tienda_id))
            if cursor.fetchone():
                raise HTTPException(status_code=403, detail="El reporte diario de esta fecha ya fue enviado, la base de apertura no se puede modificar.")

        cursor.execute("""
            INSERT INTO apertura_caja_diaria (fecha, tienda_id, monto, registrado_por)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (fecha, tienda_id) DO UPDATE SET
                monto = EXCLUDED.monto,
                registrado_por = EXCLUDED.registrado_por,
                actualizado_en = NOW()
        """, (fecha, tienda_id, monto, usuario["nombre"]))
        conexion.commit()
        return {"status": "success", "monto": monto}
    except HTTPException:
        conexion.rollback()
        raise
    except Exception as e:
        conexion.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ─── ¿Ya se envió el reporte diario de esta tienda+fecha? (cualquier usuario autenticado) ───
@app.get("/api/reporte-diario/estado")
def estado_reporte_diario(fecha: str, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    if usuario["rol"] not in ("admin", "superadmin"):
        tienda_id = usuario["tienda_id"]
    else:
        tienda_id = None
        if tienda:
            conexion = obtener_conexion()
            cursor = conexion.cursor()
            try:
                cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda,))
                fila = cursor.fetchone()
                tienda_id = fila[0] if fila else None
            finally:
                cursor.close()
                conexion.close()

    if not tienda_id:
        return {"enviado": False}

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute(
            "SELECT 1 FROM consolidado_ventas_diarias WHERE fecha = %s AND tienda_id = %s",
            (fecha, tienda_id)
        )
        return {"enviado": cursor.fetchone() is not None}
    finally:
        cursor.close()
        conexion.close()


# ─── ¿Ya existe un cierre de caja registrado para esta tienda+fecha? (cualquier
# usuario autenticado — a diferencia de /api/cierres/historial, esto NO expone
# documentos ni datos de otras tiendas, solo un booleano, así que es seguro que
# un usuario normal lo consulte para saber si su formulario debe bloquearse). ───
@app.get("/api/cierres/estado")
def estado_cierre(fecha: str, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    tienda_objetivo = usuario["tienda_nombre"] if usuario["rol"] not in ("admin", "superadmin") else tienda
    if not tienda_objetivo:
        return {"ya_registrado": False}

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute("""
            SELECT 1 FROM cierres_diarios c
            JOIN tiendas t ON t.id = c.tienda_id
            WHERE c.fecha = %s AND t.nombre = %s
        """, (fecha, tienda_objetivo))
        return {"ya_registrado": cursor.fetchone() is not None}
    finally:
        cursor.close()
        conexion.close()


# ─── MÓDULO 2: CREACIÓN DE SESIÓN DE CIERRE ───
@app.post("/api/cierres/registrar-sesion")
async def registrar_sesion_cierre(
    fecha: str = Form(...),
    tienda_nombre: str = Form(...),
    usuario: str = Form("Sistema"),
    pedidos: str = Form("[]"),
    observaciones_cajero: str = Form(""),
    archivo_resumen: UploadFile = File(None),
    archivo_lote: UploadFile = File(None),
    archivo_deposito: UploadFile = File(None),
    usuario_autenticado=Depends(obtener_usuario_actual)
):
    if not archivo_resumen and not archivo_lote and not archivo_deposito:
        raise HTTPException(status_code=400, detail="Debes cargar al menos uno de los documentos de cierre.")

    # Un usuario normal solo puede cerrar caja de SU PROPIA tienda.
    if usuario_autenticado["rol"] not in ("admin", "superadmin"):
        if not usuario_autenticado["tienda_nombre"]:
            raise HTTPException(status_code=403, detail="Tu usuario no tiene una tienda asignada.")
        tienda_nombre = usuario_autenticado["tienda_nombre"]

    from config.db_manager import RealDictCursor
    from config.drive_manager import subir_archivo_a_drive
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    
    try:
        cursor.execute("""
            INSERT INTO tiendas (nombre) VALUES (%s)
            ON CONFLICT (nombre) DO NOTHING
        """, (tienda_nombre,))

        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_nombre,))
        tienda_id = cursor.fetchone()['id']

        # ─── Pedidos desde el registro interno (fuente de verdad), no desde lo
        # que mande el navegador ───. Para esta fecha+tienda, Monitoreo de
        # Órdenes ya está vacío (se ocultó al "enviar el reporte diario"), así
        # que la caché del navegador también está vacía y no sirve. Usamos lo
        # que quedó guardado en ventas_registradas al momento del envío.
        cursor.execute("""
            SELECT id, orden_id, total_venta, tipo_pago, facturado_por, numero_factura, comprobante_url
            FROM ventas_registradas WHERE fecha = %s AND tienda_id = %s
        """, (fecha, tienda_id))
        ventas_internas = cursor.fetchall()

        if ventas_internas:
            pedidos_desde_registro = []
            for v in ventas_internas:
                cursor.execute(
                    "SELECT nombre_producto, cantidad, subtotal FROM venta_productos WHERE venta_id = %s",
                    (v['id'],)
                )
                productos = cursor.fetchall()
                pedidos_desde_registro.append({
                    "orden_id": v['orden_id'],
                    "total_venta": v['total_venta'],
                    "tipo_pago": v['tipo_pago'],
                    "comprobante_url": v['comprobante_url'],
                    "facturado_por": v['facturado_por'],
                    "numero_factura": v['numero_factura'],
                    "productos": [
                        {"nombre": p['nombre_producto'], "cantidad": p['cantidad'], "subtotal": p['subtotal']}
                        for p in productos
                    ]
                })
            pedidos = json.dumps(pedidos_desde_registro)
        # Si no hay nada registrado internamente (caso raro / de respaldo),
        # seguimos con lo que haya mandado el navegador en `pedidos`.

        # Leemos los datos de la fecha usando %s en vez de ?
        cursor.execute("""
            SELECT id, drive_resumen_caja, drive_cierre_lote, drive_deposito, completado
            FROM cierres_diarios WHERE fecha = %s AND tienda_id = %s
        """, (fecha, tienda_id))
        registro = cursor.fetchone()

        # Si el admin ya aprobó (1) o rechazó (2) este cierre, es inmutable:
        # no se puede volver a subir sobre él (mismo criterio que revisar-pedido/validar).
        if registro and registro.get('completado') in (1, 2):
            raise HTTPException(status_code=409, detail="Este cierre ya fue evaluado (aprobado/rechazado) y no se puede modificar.")

        id_resumen = registro['drive_resumen_caja'] if registro else None
        id_lote = registro['drive_cierre_lote'] if registro else None
        id_deposito = registro['drive_deposito'] if registro else None
        
        tienda_limpio = tienda_nombre.replace(" ", "-")
        
        if archivo_resumen:
            ruta_t = f"temp_resumen_{archivo_resumen.filename}"
            with open(ruta_t, "wb") as b: shutil.copyfileobj(archivo_resumen.file, b)
            id_resumen = subir_archivo_a_drive(ruta_t, f"{fecha}_{tienda_limpio}_RESUMEN.{_extension_archivo(archivo_resumen)}", archivo_resumen.content_type)
            if os.path.exists(ruta_t): os.remove(ruta_t)

        if archivo_lote:
            ruta_t = f"temp_lote_{archivo_lote.filename}"
            with open(ruta_t, "wb") as b: shutil.copyfileobj(archivo_lote.file, b)
            id_lote = subir_archivo_a_drive(ruta_t, f"{fecha}_{tienda_limpio}_LOTE.{_extension_archivo(archivo_lote)}", archivo_lote.content_type)
            if os.path.exists(ruta_t): os.remove(ruta_t)

        if archivo_deposito:
            ruta_t = f"temp_deposito_{archivo_deposito.filename}"
            with open(ruta_t, "wb") as b: shutil.copyfileobj(archivo_deposito.file, b)
            id_deposito = subir_archivo_a_drive(ruta_t, f"{fecha}_{tienda_limpio}_DEPOSITO.{_extension_archivo(archivo_deposito)}", archivo_deposito.content_type)
            if os.path.exists(ruta_t): os.remove(ruta_t)
            
        # "completado" es la decisión del admin (0=pendiente, 1=aprobado, 2=rechazado),
        # NUNCA se auto-aprueba por subir los 3 documentos. Toda sesión nueva o
        # reenviada queda pendiente hasta que un admin la valide en Historial de Cierres.
        completado = 0

        # PostgreSQL UPSERT nativo para cierres_diarios
        cursor.execute("""
            INSERT INTO cierres_diarios (fecha, tienda_id, drive_resumen_caja, drive_cierre_lote, drive_deposito, usuario_registro, completado, pedidos, observaciones_cajero)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fecha, tienda_id) DO UPDATE SET
                drive_resumen_caja = EXCLUDED.drive_resumen_caja,
                drive_cierre_lote = EXCLUDED.drive_cierre_lote,
                drive_deposito = EXCLUDED.drive_deposito,
                usuario_registro = EXCLUDED.usuario_registro,
                pedidos = EXCLUDED.pedidos,
                observaciones_cajero = EXCLUDED.observaciones_cajero
        """, (fecha, tienda_id, id_resumen, id_lote, id_deposito, usuario, completado, pedidos, observaciones_cajero or None))

        conexion.commit()
        return {
            "status": "success",
            "completado": bool(completado),
            "mensaje": "Documentos y órdenes de cierre procesados correctamente."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ─── MÓDULO 2.2: CONSULTAR HISTORIAL DE CIERRES DIARIOS (DESDE POSTGRES) ───
# ─── MÓDULO 2.2: CONSULTAR HISTORIAL DE CIERRES DIARIOS (DESDE POSTGRES) ───
@app.get("/api/cierres/historial")
def obtener_historial_cierres(admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT c.id, c.fecha, t.nombre AS tienda, c.drive_resumen_caja, c.drive_cierre_lote, c.drive_deposito, c.usuario_registro, c.completado, c.pedidos, c.observaciones_cajero
            FROM cierres_diarios c
            JOIN tiendas t ON c.tienda_id = t.id
            ORDER BY c.fecha DESC
        """)
        regresos = cursor.fetchall()
        
        historial = []
        if not regresos:
            return [] # Si está vacía la base de datos, retornamos lista vacía sin romper el frontend
            
        for r in regresos:
            lista_pedidos = []
            pedidos_origen = r.get('pedidos', '[]')
            
            if pedidos_origen:
                if isinstance(pedidos_origen, str):
                    try:
                        lista_pedidos = json.loads(pedidos_origen)
                    except Exception:
                        lista_pedidos = []
                else:
                    lista_pedidos = pedidos_origen

            # Manejo ultra seguro de la fecha de Postgres
            fecha_str = ""
            if r.get('fecha'):
                if hasattr(r['fecha'], 'strftime'):
                    fecha_str = r['fecha'].strftime("%Y-%m-%d")
                else:
                    fecha_str = str(r['fecha'])

            historial.append({
                "id": r.get('id'),
                "fecha": fecha_str,
                "tienda": r.get('tienda', 'Sin Tienda'),
                "resumen_drive": r.get('drive_resumen_caja'),
                "lote_drive": r.get('drive_cierre_lote'),
                "deposito_drive": r.get('drive_deposito'),
                "usuario": r.get('usuario_registro', 'Sistema'),
                "completado": r.get('completado', 0),
                "pedidos": lista_pedidos,
                "observaciones_cajero": r.get('observaciones_cajero')
            })
        return historial
    except Exception as e:
        # Esto nos imprimirá el error exacto en la consola si algo falla
        print(f"Error detallado en historial: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()

# ─── MÓDULO 2.2.1: MARCAR/DESMARCAR EL CHECK MANUAL DE UNA VENTA DENTRO DEL CIERRE ───
# Esto es la revisión individual que hace el auditor (imagen vs. dato de la venta)
# DENTRO del módulo de auditoría de un cierre. Es independiente de "completado"
# (que es el estado Aprobado/Rechazado/Pendiente del cierre del día completo).
@app.post("/api/cierres/revisar-pedido")
async def revisar_pedido_cierre(request: Request, admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor

    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
        cierre_id = int(payload.get('cierre_id', 0))
        orden_id = payload.get('orden_id')
        revisado = bool(payload.get('revisado', False))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error procesando JSON: {str(e)}")

    if cierre_id <= 0 or not orden_id:
        raise HTTPException(status_code=400, detail="Se requiere cierre_id y orden_id válidos.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT pedidos, completado FROM cierres_diarios WHERE id = %s", (cierre_id,))
        registro = cursor.fetchone()
        if not registro:
            raise HTTPException(status_code=404, detail="Cierre no encontrado.")
        if registro.get('completado') in (1, 2):
            raise HTTPException(status_code=409, detail="Este cierre ya fue evaluado (aprobado/rechazado) y es inmutable.")

        pedidos_origen = registro.get('pedidos') or '[]'
        pedidos = json.loads(pedidos_origen) if isinstance(pedidos_origen, str) else pedidos_origen

        encontrado = False
        for p in pedidos:
            if p.get('orden_id') == orden_id:
                p['revisado'] = revisado
                encontrado = True
                break

        if not encontrado:
            raise HTTPException(status_code=404, detail="Esa orden no pertenece a este cierre.")

        cursor.execute(
            "UPDATE cierres_diarios SET pedidos = %s WHERE id = %s",
            (json.dumps(pedidos), cierre_id)
        )
        conexion.commit()
        return {"status": "success", "pedidos": pedidos}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()

# ─── MÓDULO 2.3: APROBAR / DAR VISTO BUENO AL CIERRE ───
# ─── MÓDULO 2.3: APROBAR / DAR VISTO BUENO AL CIERRE (VERSIÓN ULTRA FLEXIBLE) ───
# ─── MÓDULO 2.3: APROBAR / DAR VISTO BUENO AL CIERRE (CON RASTREADOR) ───
# ─── MÓDULO 2.3: APROBAR / DAR VISTO BUENO AL CIERRE (SINOPSIS REAL DESDE EL FRONTEND) ───
@app.post("/api/cierres/validar")
async def validar_cierre(request: Request, admin=Depends(requerir_admin)):
    from config.db_manager import RealDictCursor
    
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")
    
    cierre_id = 0
    aprobado = True
    observaciones = ""
    
    try:
        payload = json.loads(body_str)
        cierre_id = int(payload.get('cierre_id', 0))
        aprobado = bool(payload.get('aprobado', True))
        observaciones = payload.get('observaciones', '')
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error procesando JSON: {str(e)}")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        nuevo_estado = 1 if aprobado else 2 # 1 = Aprobado (Visto Bueno), 2 = Rechazado

        if cierre_id > 0:
            # Inmutable: una vez aprobado o rechazado, no se puede re-evaluar.
            cursor.execute("SELECT completado FROM cierres_diarios WHERE id = %s", (cierre_id,))
            actual = cursor.fetchone()
            if actual and actual.get('completado') in (1, 2):
                raise HTTPException(status_code=409, detail="Este cierre ya fue evaluado (aprobado/rechazado) y es inmutable.")

            # Caso ideal: El registro ya existe y lo actualizamos por ID
            cursor.execute("""
                UPDATE cierres_diarios
                SET completado = %s, usuario_registro = %s
                WHERE id = %s
            """, (nuevo_estado, observaciones if observaciones else 'Sistema', cierre_id))
        else:
            # Caso Inicial (cierre_id: 0): Si el registro no se ha guardado formalmente, 
            # actualizamos el último cierre diario que esté pendiente de la fecha de hoy
            fecha_hoy = datetime.now().strftime("%Y-%m-%d")
            cursor.execute("""
                UPDATE cierres_diarios 
                SET completado = %s
                WHERE fecha = %s AND (completado = 0 OR completado IS NULL)
            """, (nuevo_estado, fecha_hoy))
            
            # Si no encontró ninguno hoy, aprobamos el último cierre insertado globalmente
            if cursor.rowcount == 0:
                cursor.execute("""
                    UPDATE cierres_diarios 
                    SET completado = %s 
                    WHERE id = (SELECT max(id) FROM cierres_diarios)
                """, (nuevo_estado,))

        _registrar_auditoria(cursor, admin, "aprobar_cierre" if aprobado else "rechazar_cierre", f"cierre_id={cierre_id or 'último pendiente'}")
        conexion.commit()
        return {"status": "success", "mensaje": "Estado de cierre actualizado con éxito en PostgreSQL."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ══════════════════════════════════════════════════════════════════════════
# MÓDULO 3: KPIs — ventas de hoy, meta mensual por tienda, comparativa diaria
# contra el mes anterior, y aporte de cada vendedora hacia la meta.
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/metas")
def listar_metas_mensuales(anio: int, mes: int, admin=Depends(requerir_admin)):
    """Módulo dedicado de Metas Mensuales: todas las tiendas de un vistazo,
    cada una con su meta del mes seleccionado (0 si todavía no se registró)."""
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        for nombre in TIENDAS_CONOCIDAS:
            cursor.execute("INSERT INTO tiendas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (nombre,))
        conexion.commit()

        cursor.execute("""
            SELECT t.id AS tienda_id, t.nombre AS tienda, COALESCE(m.monto_meta, 0) AS monto_meta
            FROM tiendas t
            LEFT JOIN metas_mensuales m ON m.tienda_id = t.id AND m.anio = %s AND m.mes = %s
            WHERE t.nombre = ANY(%s)
            ORDER BY t.nombre
        """, (anio, mes, TIENDAS_CONOCIDAS))
        return cursor.fetchall()
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/metas")
async def establecer_meta_mensual(request: Request, admin=Depends(requerir_admin)):
    """La meta mensual la registra el admin a mano, por tienda. El KPI mide el
    avance de la tienda y el aporte de cada vendedora contra este mismo número."""
    from config.db_manager import RealDictCursor

    body = await request.json()
    tienda_nombre = (body.get("tienda") or "").strip()
    try:
        anio = int(body.get("anio"))
        mes = int(body.get("mes"))
        monto = float(body.get("monto_meta"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="anio, mes y monto_meta son obligatorios y numéricos.")

    if not tienda_nombre or not (1 <= mes <= 12) or anio < 2000 or monto < 0:
        raise HTTPException(status_code=400, detail="Datos inválidos.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("INSERT INTO tiendas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (tienda_nombre,))
        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_nombre,))
        tienda_id = cursor.fetchone()["id"]

        cursor.execute("""
            INSERT INTO metas_mensuales (tienda_id, anio, mes, monto_meta)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tienda_id, anio, mes) DO UPDATE SET monto_meta = EXCLUDED.monto_meta
        """, (tienda_id, anio, mes, monto))
        _registrar_auditoria(cursor, admin, "editar_meta_mensual", f"{tienda_nombre} {mes}/{anio}: ${monto:.2f}")
        conexion.commit()
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conexion.close()


# ─── REGISTRO DE AUDITORÍA — quién hizo qué acción sensible y cuándo. ───
@app.get("/api/auditoria")
def obtener_auditoria(limite: int = 200, admin=Depends(requerir_superadmin)):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT id, usuario_nombre, accion, detalle, creado_en
            FROM auditoria ORDER BY creado_en DESC LIMIT %s
        """, (min(max(limite, 1), 1000),))
        filas = cursor.fetchall()
        return [
            {
                "id": f["id"],
                "usuario": f["usuario_nombre"] or "Sistema",
                "accion": f["accion"],
                "detalle": f["detalle"],
                "creado_en": f["creado_en"].strftime("%Y-%m-%d %H:%M:%S") if f["creado_en"] else None,
            }
            for f in filas
        ]
    finally:
        cursor.close()
        conexion.close()


# ─── BÚSQUEDA GLOBAL — un cuadro de texto que busca en varios módulos a la
# vez (órdenes registradas, usuarios, cierres por tienda/fecha), en vez de
# tener que saber de antemano en qué pantalla está lo que se busca. ───
@app.get("/api/buscar")
def buscar_global(q: str, usuario=Depends(obtener_usuario_actual)):
    q = (q or "").strip()
    if len(q) < 2:
        return {"resultados": []}

    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    resultados = []
    try:
        patron = f"%{q}%"

        # Órdenes registradas (por número de orden) — scope por tienda si no es admin.
        if usuario["rol"] in ("admin", "superadmin"):
            cursor.execute("""
                SELECT v.orden_id, v.fecha, v.total_venta, t.nombre AS tienda
                FROM ventas_registradas v JOIN tiendas t ON t.id = v.tienda_id
                WHERE v.orden_id ILIKE %s ORDER BY v.fecha DESC LIMIT 8
            """, (patron,))
        else:
            cursor.execute("""
                SELECT v.orden_id, v.fecha, v.total_venta, t.nombre AS tienda
                FROM ventas_registradas v JOIN tiendas t ON t.id = v.tienda_id
                WHERE v.orden_id ILIKE %s AND v.tienda_id = %s ORDER BY v.fecha DESC LIMIT 8
            """, (patron, usuario["tienda_id"]))
        for r in cursor.fetchall():
            fecha_str = r["fecha"].strftime("%Y-%m-%d") if r["fecha"] else None
            resultados.append({
                "tipo": "orden", "titulo": r["orden_id"],
                "subtitulo": f"{r['tienda']} — {fecha_str} — ${r['total_venta']:.2f}",
                "pantalla": "ventas", "fecha": fecha_str, "tienda": r["tienda"],
            })

        # Cierres (por tienda) — todos ven, pero un usuario normal solo los suyos.
        if usuario["rol"] in ("admin", "superadmin"):
            cursor.execute("""
                SELECT c.id, c.fecha, c.completado, t.nombre AS tienda
                FROM cierres_diarios c JOIN tiendas t ON t.id = c.tienda_id
                WHERE t.nombre ILIKE %s ORDER BY c.fecha DESC LIMIT 8
            """, (patron,))
            for r in cursor.fetchall():
                estado = {0: "Pendiente", 1: "Aprobado", 2: "Rechazado"}.get(r["completado"], "Pendiente")
                fecha_str = r["fecha"].strftime("%Y-%m-%d") if r["fecha"] else None
                resultados.append({
                    "tipo": "cierre", "titulo": f"Cierre — {r['tienda']}",
                    "subtitulo": f"{fecha_str} — {estado}",
                    "pantalla": "historial", "cierre_id": r["id"],
                })

        # Usuarios — solo admin.
        if usuario["rol"] in ("admin", "superadmin"):
            cursor.execute("""
                SELECT nombre, email, rol FROM usuarios
                WHERE nombre ILIKE %s OR email ILIKE %s LIMIT 8
            """, (patron, patron))
            for r in cursor.fetchall():
                resultados.append({
                    "tipo": "usuario", "titulo": r["nombre"],
                    "subtitulo": f"{r['email']} — {r['rol']}",
                    "pantalla": "usuarios",
                })

        return {"resultados": resultados}
    finally:
        cursor.close()
        conexion.close()


@app.get("/api/kpis")
def obtener_kpis(anio: int = None, mes: int = None, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    # Módulo reservado al superadministrador (y al usuario de tienda, que solo
    # ve la suya) — el rol "admin" no tiene acceso a KPIs.
    if usuario["rol"] == "admin":
        raise HTTPException(status_code=403, detail="Este módulo está reservado al superadministrador.")
    # Un usuario normal solo ve el KPI de SU tienda; el superadmin elige la
    # tienda (la misma que tenga seleccionada en el header, igual que en Monitoreo).
    tienda_objetivo = usuario["tienda_nombre"] if usuario["rol"] != "superadmin" else tienda
    if not tienda_objetivo:
        raise HTTPException(
            status_code=400,
            detail="Tu usuario no tiene una tienda asignada." if usuario["rol"] != "superadmin" else "Debes indicar una tienda."
        )

    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise HTTPException(status_code=401, detail="Fallo de autenticación con Odoo")

        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        try:
            tz_odoo = ZoneInfo(_obtener_tz_odoo(models, uid))
        except Exception:
            tz_odoo = timezone.utc

        ahora_local = datetime.now(tz_odoo)
        anio = anio or ahora_local.year
        mes = mes or ahora_local.month

        inicio_mes_actual = datetime(anio, mes, 1, tzinfo=tz_odoo)
        if mes == 1:
            anio_anterior, mes_anterior = anio - 1, 12
        else:
            anio_anterior, mes_anterior = anio, mes - 1
        inicio_mes_anterior = datetime(anio_anterior, mes_anterior, 1, tzinfo=tz_odoo)

        # Si es el mes en curso, el rango llega hasta ahora; si es un mes ya
        # cerrado, llega hasta el primer día del mes siguiente.
        es_mes_en_curso = (anio == ahora_local.year and mes == ahora_local.month)
        if es_mes_en_curso:
            fin_mes_actual = ahora_local
        else:
            anio_sig, mes_sig = (anio + 1, 1) if mes == 12 else (anio, mes + 1)
            fin_mes_actual = datetime(anio_sig, mes_sig, 1, tzinfo=tz_odoo)

        inicio_utc = inicio_mes_anterior.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        fin_utc = fin_mes_actual.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Resolvemos el punto de venta por ID (no por nombre en el dominio),
        # más directo y confiable que depender de un join implícito.
        config_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'pos.config', 'search', [[['name', '=', tienda_objetivo]]]
        )

        ordenes = []
        if config_ids:
            filtros = [
                ['config_id', 'in', config_ids],
                ['company_id', '=', TARGET_COMPANY_ID],
                ['date_order', '>=', inicio_utc],
                ['date_order', '<', fin_utc],
                ['state', 'not in', ['draft', 'cancel']],
            ]
            ordenes = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read',
                [filtros], {'fields': ['date_order', 'amount_total', 'cashier']}
            )

        # Comparativa año contra año: mismo mes, un año antes. Es un rango de
        # fechas totalmente distinto al de arriba, así que va en su propia
        # consulta — reutiliza los mismos config_ids ya resueltos.
        ventas_mismo_mes_anio_anterior = 0.0
        if config_ids:
            inicio_anio_ant = datetime(anio - 1, mes, 1, tzinfo=tz_odoo)
            anio_sig_ant, mes_sig_ant = (anio, 1) if mes == 12 else (anio - 1, mes + 1)
            fin_anio_ant = datetime(anio_sig_ant, mes_sig_ant, 1, tzinfo=tz_odoo)
            filtros_anio_ant = [
                ['config_id', 'in', config_ids],
                ['company_id', '=', TARGET_COMPANY_ID],
                ['date_order', '>=', inicio_anio_ant.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")],
                ['date_order', '<', fin_anio_ant.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")],
                ['state', 'not in', ['draft', 'cancel']],
            ]
            ordenes_anio_ant = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read',
                [filtros_anio_ant], {'fields': ['amount_total']}
            )
            ventas_mismo_mes_anio_anterior = sum(o.get('amount_total', 0.0) or 0.0 for o in ordenes_anio_ant)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Odoo POS: {str(e)}")

    # ─── Clasificar cada orden por día local: mes actual vs. mes anterior ───
    hoy_str = ahora_local.strftime("%Y-%m-%d")
    ventas_hoy = 0.0
    ventas_mes_actual = 0.0
    por_dia_actual = {}
    por_dia_anterior = {}
    por_vendedora = {}

    for o in ordenes:
        fecha_utc = datetime.strptime(o['date_order'], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        fecha_local = fecha_utc.astimezone(tz_odoo)
        monto = o.get('amount_total', 0.0) or 0.0

        if fecha_local.year == anio and fecha_local.month == mes:
            ventas_mes_actual += monto
            por_dia_actual[fecha_local.day] = por_dia_actual.get(fecha_local.day, 0.0) + monto
            cajero = o.get('cashier') or 'Sin asignar'
            por_vendedora[cajero] = por_vendedora.get(cajero, 0.0) + monto
            if fecha_local.strftime("%Y-%m-%d") == hoy_str:
                ventas_hoy += monto
        elif fecha_local.year == anio_anterior and fecha_local.month == mes_anterior:
            por_dia_anterior[fecha_local.day] = por_dia_anterior.get(fecha_local.day, 0.0) + monto

    dias_en_mes = calendar.monthrange(anio, mes)[1]
    ultimo_dia = ahora_local.day if es_mes_en_curso else dias_en_mes

    serie_diaria = [
        {
            "dia": d,
            "actual": round(por_dia_actual.get(d, 0.0), 2),
            "anterior": round(por_dia_anterior.get(d, 0.0), 2)
        }
        for d in range(1, ultimo_dia + 1)
    ]

    # ─── Meta mensual (la registra el admin a mano) ───
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("INSERT INTO tiendas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING", (tienda_objetivo,))
        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_objetivo,))
        tienda_id = cursor.fetchone()['id']
        cursor.execute(
            "SELECT monto_meta FROM metas_mensuales WHERE tienda_id = %s AND anio = %s AND mes = %s",
            (tienda_id, anio, mes)
        )
        fila_meta = cursor.fetchone()
        conexion.commit()
    finally:
        cursor.close()
        conexion.close()

    meta_mensual = fila_meta['monto_meta'] if fila_meta else 0.0
    progreso_pct = round(ventas_mes_actual / meta_mensual * 100, 1) if meta_mensual > 0 else None

    # Aporte de cada vendedora hacia la MISMA meta de la tienda (no una meta
    # individual): si la meta es $10,000 y facturó $9,000, su avance es 90%.
    vendedoras = sorted(
        [
            {
                "nombre": nombre,
                "monto": round(monto, 2),
                "pct": round(monto / meta_mensual * 100, 1) if meta_mensual > 0 else None
            }
            for nombre, monto in por_vendedora.items()
        ],
        key=lambda v: v["monto"], reverse=True
    )

    variacion_anual_pct = None
    if ventas_mismo_mes_anio_anterior > 0:
        variacion_anual_pct = round((ventas_mes_actual - ventas_mismo_mes_anio_anterior) / ventas_mismo_mes_anio_anterior * 100, 1)

    return {
        "tienda": tienda_objetivo,
        "anio": anio,
        "mes": mes,
        "ventas_hoy": round(ventas_hoy, 2),
        "ventas_mes_actual": round(ventas_mes_actual, 2),
        "meta_mensual": meta_mensual,
        "progreso_pct": progreso_pct,
        "serie_diaria": serie_diaria,
        "vendedoras": vendedoras,
        "ventas_mismo_mes_anio_anterior": round(ventas_mismo_mes_anio_anterior, 2),
        "variacion_anual_pct": variacion_anual_pct
    }


MESES_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
}

# ─── REPORTE EJECUTIVO MENSUAL (PDF) — resumen de todas las tiendas para un
# mes: venta total y comparativa vs. mes anterior y vs. mismo mes del año
# anterior, desglose de venta/meta por tienda, y estado de cierres (aprobados/
# pendientes/rechazados). Pensado para imprimir o compartir con gerencia sin
# tener que entrar al dashboard — solo admin.
@app.get("/api/reporte-ejecutivo/pdf")
def reporte_ejecutivo_pdf(anio: int = None, mes: int = None, admin=Depends(requerir_superadmin)):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from io import BytesIO

    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise HTTPException(status_code=401, detail="Fallo de autenticación con Odoo")
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        try:
            tz_odoo = ZoneInfo(_obtener_tz_odoo(models, uid))
        except Exception:
            tz_odoo = timezone.utc

        ahora_local = datetime.now(tz_odoo)
        anio = anio or ahora_local.year
        mes = mes or ahora_local.month

        def rango_utc_del_mes(a, m):
            inicio = datetime(a, m, 1, tzinfo=tz_odoo)
            a_sig, m_sig = (a + 1, 1) if m == 12 else (a, m + 1)
            fin = datetime(a_sig, m_sig, 1, tzinfo=tz_odoo)
            return inicio.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), fin.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        inicio_actual, fin_actual = rango_utc_del_mes(anio, mes)
        anio_ant_mes, mes_ant = (anio - 1, 12) if mes == 1 else (anio, mes - 1)
        inicio_mes_ant, fin_mes_ant = rango_utc_del_mes(anio_ant_mes, mes_ant)
        inicio_anio_ant, fin_anio_ant = rango_utc_del_mes(anio - 1, mes)

        def ventas_por_tienda(inicio, fin):
            filtros = [
                ['company_id', '=', TARGET_COMPANY_ID],
                ['date_order', '>=', inicio],
                ['date_order', '<', fin],
                ['state', 'not in', ['draft', 'cancel']],
            ]
            ordenes = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read',
                [filtros], {'fields': ['config_id', 'amount_total']}
            )
            por_tienda = {}
            for o in ordenes:
                nombre = o['config_id'][1] if o.get('config_id') else "Sin tienda"
                por_tienda[nombre] = por_tienda.get(nombre, 0.0) + (o.get('amount_total', 0.0) or 0.0)
            return por_tienda

        ventas_mes_actual = ventas_por_tienda(inicio_actual, fin_actual)
        ventas_mes_anterior = ventas_por_tienda(inicio_mes_ant, fin_mes_ant)
        ventas_mismo_mes_anio_anterior = ventas_por_tienda(inicio_anio_ant, fin_anio_ant)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Odoo POS: {str(e)}")

    total_actual = sum(ventas_mes_actual.values())
    total_mes_anterior = sum(ventas_mes_anterior.values())
    total_anio_anterior = sum(ventas_mismo_mes_anio_anterior.values())
    var_mensual_pct = round((total_actual - total_mes_anterior) / total_mes_anterior * 100, 1) if total_mes_anterior > 0 else None
    var_anual_pct = round((total_actual - total_anio_anterior) / total_anio_anterior * 100, 1) if total_anio_anterior > 0 else None

    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        filas_tiendas = []
        total_aprobados = total_rechazados = total_pendientes = 0
        for nombre_tienda in TIENDAS_CONOCIDAS:
            venta = ventas_mes_actual.get(nombre_tienda, 0.0)
            cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (nombre_tienda,))
            fila_t = cursor.fetchone()
            tienda_id = fila_t["id"] if fila_t else None

            meta = 0.0
            if tienda_id:
                cursor.execute(
                    "SELECT monto_meta FROM metas_mensuales WHERE tienda_id = %s AND anio = %s AND mes = %s",
                    (tienda_id, anio, mes)
                )
                fila_meta = cursor.fetchone()
                meta = fila_meta["monto_meta"] if fila_meta else 0.0

            estados_cierre = []
            if tienda_id:
                cursor.execute("""
                    SELECT completado FROM cierres_diarios
                    WHERE tienda_id = %s AND EXTRACT(YEAR FROM fecha) = %s AND EXTRACT(MONTH FROM fecha) = %s
                """, (tienda_id, anio, mes))
                estados_cierre = [f["completado"] for f in cursor.fetchall()]
            aprobados = sum(1 for e in estados_cierre if e == 1)
            rechazados = sum(1 for e in estados_cierre if e == 2)
            pendientes = sum(1 for e in estados_cierre if e in (0, None))
            total_aprobados += aprobados
            total_rechazados += rechazados
            total_pendientes += pendientes

            filas_tiendas.append({
                "tienda": nombre_tienda, "venta": venta, "meta": meta,
                "pct_meta": round(venta / meta * 100, 1) if meta > 0 else None,
                "aprobados": aprobados, "rechazados": rechazados, "pendientes": pendientes,
            })

        _registrar_auditoria(cursor, admin, "generar_reporte_ejecutivo", f"{MESES_ES[mes]} {anio}")
        conexion.commit()
    finally:
        cursor.close()
        conexion.close()

    filas_tiendas.sort(key=lambda f: f["venta"], reverse=True)

    # ─── Armado del PDF ───
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=1.8 * cm, bottomMargin=1.8 * cm, leftMargin=1.8 * cm, rightMargin=1.8 * cm
    )
    estilos = getSampleStyleSheet()
    estilo_titulo = ParagraphStyle('TituloSB', parent=estilos['Title'], textColor=colors.HexColor('#1e293b'), fontSize=20, spaceAfter=2)
    estilo_subtitulo = ParagraphStyle('SubtituloSB', parent=estilos['Normal'], textColor=colors.HexColor('#64748b'), fontSize=11, spaceAfter=18)
    estilo_seccion = ParagraphStyle('SeccionSB', parent=estilos['Heading2'], textColor=colors.HexColor('#1e293b'), fontSize=13, spaceBefore=14, spaceAfter=8)
    estilo_pie = ParagraphStyle('PieSB', parent=estilos['Normal'], textColor=colors.HexColor('#94a3b8'), fontSize=8, alignment=TA_CENTER)

    elementos = [
        Paragraph("Cultura Tejida — Reporte Ejecutivo Mensual", estilo_titulo),
        Paragraph(f"{MESES_ES[mes].capitalize()} {anio} · Generado el {datetime.now(tz_odoo).strftime('%d/%m/%Y %H:%M')} por {admin['nombre']}", estilo_subtitulo),
    ]

    def flecha_pct(pct):
        if pct is None:
            return "—"
        return f"{'▲' if pct >= 0 else '▼'} {abs(pct)}%"

    elementos.append(Paragraph("Resumen General", estilo_seccion))
    tabla_resumen = Table([
        ["Venta total del mes", "Vs. mes anterior", "Vs. mismo mes año anterior"],
        [f"${total_actual:,.2f}", flecha_pct(var_mensual_pct), flecha_pct(var_anual_pct)],
    ], colWidths=[6 * cm, 5.2 * cm, 6.2 * cm])
    tabla_resumen.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e293b')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('FONTSIZE', (0, 1), (-1, 1), 15),
        ('FONTNAME', (0, 1), (-1, 1), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('BACKGROUND', (0, 1), (-1, 1), colors.HexColor('#f1f5f9')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
    ]))
    elementos.append(tabla_resumen)

    elementos.append(Paragraph("Cierres del mes (todas las tiendas)", estilo_seccion))
    tabla_cierres = Table([
        ["Aprobados", "Pendientes/sin revisar", "Rechazados"],
        [str(total_aprobados), str(total_pendientes), str(total_rechazados)],
    ], colWidths=[5.7 * cm, 6 * cm, 5.7 * cm])
    tabla_cierres.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e2e8f0')),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('FONTSIZE', (0, 1), (-1, 1), 14),
        ('FONTNAME', (0, 1), (-1, 1), 'Helvetica-Bold'),
        ('TEXTCOLOR', (0, 1), (0, 1), colors.HexColor('#059669')),
        ('TEXTCOLOR', (1, 1), (1, 1), colors.HexColor('#d97706')),
        ('TEXTCOLOR', (2, 1), (2, 1), colors.HexColor('#dc2626')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
    ]))
    elementos.append(tabla_cierres)

    elementos.append(Paragraph("Desglose por Tienda", estilo_seccion))
    filas_tabla_tiendas = [["Tienda", "Venta", "Meta", "% Meta", "Aprob.", "Pend.", "Rech."]]
    for f in filas_tiendas:
        filas_tabla_tiendas.append([
            f["tienda"], f"${f['venta']:,.2f}",
            f"${f['meta']:,.2f}" if f['meta'] > 0 else "—",
            f"{f['pct_meta']}%" if f['pct_meta'] is not None else "—",
            str(f["aprobados"]), str(f["pendientes"]), str(f["rechazados"]),
        ])
    tabla_tiendas = Table(filas_tabla_tiendas, colWidths=[5 * cm, 2.7 * cm, 2.7 * cm, 1.8 * cm, 1.6 * cm, 1.6 * cm, 1.6 * cm], repeatRows=1)
    estilo_tabla_tiendas = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e293b')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 8.5),
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
    ]
    for i in range(1, len(filas_tabla_tiendas)):
        if i % 2 == 0:
            estilo_tabla_tiendas.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#f8fafc')))
    tabla_tiendas.setStyle(TableStyle(estilo_tabla_tiendas))
    elementos.append(tabla_tiendas)

    elementos.append(Spacer(1, 1.2 * cm))
    elementos.append(Paragraph("Generado automáticamente por Cultura Tejida — datos en vivo desde Odoo y el registro interno de cierres.", estilo_pie))

    doc.build(elementos)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    nombre_archivo = f"reporte-ejecutivo-{MESES_ES[mes]}-{anio}.pdf"
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nombre_archivo}"'}
    )


# ─── MÓDULO 4: COMISIONES — todas las tiendas, en vivo desde Odoo, para un
# rango de fechas. Comisión = 2% sobre Efectivo (Caja + Transferencia + Cheque,
# sin IVA) + 2% sobre Tarjeta (sin IVA, menos retención de tarjeta 5.18%).
# Cuenta de cliente y Giftcard NO generan comisión — Giftcard además nunca es
# un pago real (es una línea de producto/descuento), así que ya queda afuera
# de "Tarjeta"/"Efectivo" sin hacer nada especial. Se reparte en partes
# iguales entre los cajeros ('cashier') que tuvieron al menos una venta en esa
# tienda durante el periodo — solo admin.
IVA_COMISIONES = 0.15
PORCENTAJE_COMISION = 0.02
RETENCION_TARJETA_PCT = 0.0518

@app.get("/api/comisiones")
def obtener_comisiones(desde: str, hasta: str, admin=Depends(requerir_superadmin)):
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise HTTPException(status_code=401, detail="Fallo de autenticación con Odoo")

        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        try:
            tz_odoo = ZoneInfo(_obtener_tz_odoo(models, uid))
        except Exception:
            tz_odoo = timezone.utc

        inicio_local = datetime.strptime(desde, "%Y-%m-%d").replace(tzinfo=tz_odoo)
        fin_local = datetime.strptime(hasta, "%Y-%m-%d").replace(tzinfo=tz_odoo) + timedelta(days=1)
        inicio_utc = inicio_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        fin_utc = fin_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        filtros = [
            ['company_id', '=', TARGET_COMPANY_ID],
            ['date_order', '>=', inicio_utc],
            ['date_order', '<', fin_utc],
            ['state', 'not in', ['draft', 'cancel']],
        ]
        ordenes = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read',
            [filtros], {'fields': ['config_id', 'payment_ids', 'cashier']}
        )

        todos_payment_ids = [pid for o in ordenes for pid in o.get('payment_ids', [])]
        pagos_por_orden = {}
        if todos_payment_ids:
            pagos = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.payment', 'read',
                [todos_payment_ids], {'fields': ['pos_order_id', 'payment_method_id', 'amount']}
            )
            for p in pagos:
                oid = p['pos_order_id'][0] if p.get('pos_order_id') else None
                pagos_por_orden.setdefault(oid, []).append(p)

        datos_tienda = {}
        for o in ordenes:
            tienda_nombre = o['config_id'][1] if o.get('config_id') else "Cultura Tejida Local"
            d = datos_tienda.setdefault(tienda_nombre, {'efectivo': 0.0, 'tarjeta': 0.0, 'colaboradores': set()})
            cajero = o.get('cashier')
            # Odoo tiene un cajero de pruebas ("PRUEBAS PoS") usado para testear
            # el POS — no es una persona real, no debe entrar al reparto de comisión.
            if cajero and 'prueba' not in cajero.lower() and 'test' not in cajero.lower():
                d['colaboradores'].add(cajero)
            for p in pagos_por_orden.get(o['id'], []):
                metodo = _normalizar_metodo_pago(p['payment_method_id'][1] if p.get('payment_method_id') else "")
                monto = p.get('amount', 0.0) or 0.0
                if metodo in ('Caja', 'Transferencia', 'Cheque'):
                    d['efectivo'] += monto
                elif metodo == 'Tarjeta':
                    d['tarjeta'] += monto
                # 'Cuenta de cliente' y cualquier otro método: no generan comisión.

        resultado = []
        for tienda_nombre, d in datos_tienda.items():
            total_efectivo = round(d['efectivo'], 2)
            subtotal_efectivo = round(total_efectivo / (1 + IVA_COMISIONES), 2)
            comision_efectivo = round(subtotal_efectivo * PORCENTAJE_COMISION, 2)

            total_tarjeta = round(d['tarjeta'], 2)
            subtotal_tarjeta = round(total_tarjeta / (1 + IVA_COMISIONES), 2)
            retencion_tarjeta = round(total_tarjeta * RETENCION_TARJETA_PCT, 2)
            neto_tarjeta = round(subtotal_tarjeta - retencion_tarjeta, 2)
            comision_tarjeta = round(neto_tarjeta * PORCENTAJE_COMISION, 2)

            total_comision = round(comision_efectivo + comision_tarjeta, 2)
            colaboradores = sorted(d['colaboradores'])
            cantidad_colaboradores = len(colaboradores)
            comision_por_colaborador = round(total_comision / cantidad_colaboradores, 2) if cantidad_colaboradores else 0.0

            resultado.append({
                "tienda": tienda_nombre,
                "total_efectivo": total_efectivo,
                "subtotal_efectivo": subtotal_efectivo,
                "comision_efectivo": comision_efectivo,
                "total_tarjeta": total_tarjeta,
                "subtotal_tarjeta": subtotal_tarjeta,
                "retencion_tarjeta": retencion_tarjeta,
                "neto_tarjeta": neto_tarjeta,
                "comision_tarjeta": comision_tarjeta,
                "total_comision": total_comision,
                "colaboradores": colaboradores,
                "cantidad_colaboradores": cantidad_colaboradores,
                "comision_por_colaborador": comision_por_colaborador,
            })

        resultado.sort(key=lambda r: r['tienda'])
        return {"desde": desde, "hasta": hasta, "tiendas": resultado}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Odoo POS: {str(e)}")


# ─── MÓDULO 5: DASHBOARD DE INICIO — venta de hoy por tienda (en vivo desde
# Odoo) + estado del reporte diario/cierre de cada una (desde Postgres), para
# tener un vistazo general apenas se entra a la app. El admin ve todas las
# tiendas; un usuario normal solo la suya. ───
@app.get("/api/dashboard")
def obtener_dashboard(usuario=Depends(obtener_usuario_actual)):
    tiendas_a_mostrar = TIENDAS_CONOCIDAS if usuario["rol"] in ("admin", "superadmin") else (
        [usuario["tienda_nombre"]] if usuario["tienda_nombre"] else []
    )

    ventas_por_tienda = {}
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if uid:
            models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
            try:
                tz_odoo = ZoneInfo(_obtener_tz_odoo(models, uid))
            except Exception:
                tz_odoo = timezone.utc

            ahora_local = datetime.now(tz_odoo)
            hoy_str = ahora_local.strftime("%Y-%m-%d")
            inicio_local = datetime.strptime(hoy_str, "%Y-%m-%d").replace(tzinfo=tz_odoo)
            fin_local = inicio_local + timedelta(days=1)
            inicio_utc = inicio_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            fin_utc = fin_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            filtros = [
                ['company_id', '=', TARGET_COMPANY_ID],
                ['date_order', '>=', inicio_utc],
                ['date_order', '<', fin_utc],
                ['state', 'not in', ['draft', 'cancel']],
            ]
            ordenes = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read',
                [filtros], {'fields': ['config_id', 'amount_total']}
            )
            for o in ordenes:
                nombre = o['config_id'][1] if o.get('config_id') else "Cultura Tejida Local"
                d = ventas_por_tienda.setdefault(nombre, {"venta": 0.0, "cantidad": 0})
                d["venta"] += o.get('amount_total', 0.0) or 0.0
                d["cantidad"] += 1
        else:
            hoy_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        hoy_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        resultado = []
        venta_total_hoy = 0.0
        tiendas_reporte_pendiente = 0
        tiendas_con_diferencia = 0

        for nombre_tienda in tiendas_a_mostrar:
            v = ventas_por_tienda.get(nombre_tienda, {"venta": 0.0, "cantidad": 0})
            venta_total_hoy += v["venta"]

            cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (nombre_tienda,))
            fila_t = cursor.fetchone()
            tienda_id = fila_t["id"] if fila_t else None

            reporte_enviado = False
            cierre_estado = None
            hay_diferencia = False
            if tienda_id:
                cursor.execute("""
                    SELECT ajuste_metodos_pago, conteo_fisico FROM consolidado_ventas_diarias
                    WHERE fecha = %s AND tienda_id = %s
                """, (hoy_str, tienda_id))
                fila_consol = cursor.fetchone()
                reporte_enviado = fila_consol is not None
                if fila_consol and fila_consol.get("ajuste_metodos_pago") and fila_consol.get("conteo_fisico"):
                    try:
                        ajustes = {a["metodo"]: float(a.get("monto", 0) or 0) for a in json.loads(fila_consol["ajuste_metodos_pago"])}
                        conteos = {c["metodo"]: float(c.get("monto", 0) or 0) for c in json.loads(fila_consol["conteo_fisico"])}
                        for metodo, monto_contado in conteos.items():
                            if abs(monto_contado - ajustes.get(metodo, 0.0)) > 5:
                                hay_diferencia = True
                                break
                    except Exception:
                        pass

                cursor.execute("""
                    SELECT completado FROM cierres_diarios WHERE fecha = %s AND tienda_id = %s
                """, (hoy_str, tienda_id))
                fila_cierre = cursor.fetchone()
                cierre_estado = fila_cierre["completado"] if fila_cierre else None

            if reporte_enviado and cierre_estado in (None, 0):
                tiendas_reporte_pendiente += 1
            if hay_diferencia:
                tiendas_con_diferencia += 1

            resultado.append({
                "tienda": nombre_tienda,
                "venta_hoy": round(v["venta"], 2),
                "cantidad_ordenes_hoy": v["cantidad"],
                "reporte_enviado_hoy": reporte_enviado,
                "cierre_estado": cierre_estado,
                "alerta_diferencia": hay_diferencia,
            })

        return {
            "fecha": hoy_str,
            "tiendas": resultado,
            "resumen": {
                "venta_total_hoy": round(venta_total_hoy, 2),
                "tiendas_con_reporte_pendiente": tiendas_reporte_pendiente,
                "tiendas_con_diferencia": tiendas_con_diferencia,
            }
        }
    finally:
        cursor.close()
        conexion.close()


# ─── REPORTE DE CUADRE DE CAJA: suma de ventas por método de pago ───
# Los MONTOS salen del registro interno (ventas_registradas + venta_pagos),
# NO en vivo de Odoo: para cuando se llega a Cierre de Caja, esa tienda+fecha
# ya está "enviada" y Monitoreo de Órdenes ya no la trae. La LISTA de métodos
# sí se pide en vivo a Odoo (pos.config de la tienda), para que el reporte
# siempre muestre TODOS los métodos habilitados para esa tienda aunque no
# hayan tenido movimiento ese día (en $0), en vez de solo los que sí se usaron.
@app.get("/api/reporte-cuadre")
def obtener_reporte_cuadre(fecha: str, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    tienda_objetivo = usuario["tienda_nombre"] if usuario["rol"] not in ("admin", "superadmin") else tienda
    if not tienda_objetivo:
        raise HTTPException(
            status_code=400,
            detail="Tu usuario no tiene una tienda asignada." if usuario["rol"] not in ("admin", "superadmin") else "Debes indicar una tienda."
        )

    nombres_metodos_odoo = []
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if uid:
            models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
            configs = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'pos.config', 'search_read',
                [[['name', '=', tienda_objetivo]]], {'fields': ['payment_method_ids']}
            )
            if configs and configs[0]['payment_method_ids']:
                metodos = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD, 'pos.payment.method', 'read',
                    [configs[0]['payment_method_ids']], {'fields': ['name']}
                )
                nombres_metodos_odoo = [_normalizar_metodo_pago(m['name']) for m in metodos]
    except Exception:
        pass  # si Odoo no responde, seguimos solo con lo que haya guardado internamente

    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_objetivo,))
        fila_tienda = cursor.fetchone()
        tienda_id = fila_tienda["id"] if fila_tienda else None

        base_apertura = None
        if tienda_id:
            cursor.execute("""
                SELECT monto FROM apertura_caja_diaria WHERE fecha = %s AND tienda_id = %s
            """, (fecha, tienda_id))
            fila_apertura = cursor.fetchone()
            base_apertura = fila_apertura["monto"] if fila_apertura else None

        ventas = []
        if tienda_id:
            cursor.execute("""
                SELECT id, total_venta FROM ventas_registradas
                WHERE fecha = %s AND tienda_id = %s
            """, (fecha, tienda_id))
            ventas = cursor.fetchall()

        total_general = sum(v["total_venta"] or 0 for v in ventas)

        monto_por_metodo = {}
        cantidad_por_metodo = {}
        if ventas:
            ids_venta = [v["id"] for v in ventas]
            cursor.execute("""
                SELECT metodo, monto FROM venta_pagos WHERE venta_id = ANY(%s)
            """, (ids_venta,))
            # Se normaliza fila por fila (no en SQL) porque varios nombres reales
            # de Odoo (ej. "Caja Lemaler Quito", "Caja Lemaler Dorado") deben caer
            # en el mismo balde canónico ("Caja") antes de sumar.
            for r in cursor.fetchall():
                nombre = _normalizar_metodo_pago(r["metodo"])
                monto_por_metodo[nombre] = monto_por_metodo.get(nombre, 0.0) + (r["monto"] or 0)
                cantidad_por_metodo[nombre] = cantidad_por_metodo.get(nombre, 0) + 1
            monto_por_metodo = {k: round(v, 2) for k, v in monto_por_metodo.items()}

            # Giftcard no es un método de pago, es una línea de producto/descuento
            # dentro de la orden (ej. "Tarjeta de regalo Descuento", en negativo) —
            # se detecta por nombre en lo ya guardado internamente. Es un respaldo:
            # si ya existe un ajuste confirmado para "Giftcard" (más abajo), ese
            # ajuste manda igual.
            cursor.execute("""
                SELECT COUNT(*) AS cantidad, COALESCE(SUM(ABS(subtotal)), 0) AS total
                FROM venta_productos
                WHERE venta_id = ANY(%s) AND (nombre_producto ILIKE %s OR nombre_producto ILIKE %s)
            """, (ids_venta, '%regalo%', '%gift%'))
            fila_gift = cursor.fetchone()
            if fila_gift and fila_gift["cantidad"]:
                monto_por_metodo["Giftcard"] = round(fila_gift["total"] or 0, 2)
                cantidad_por_metodo["Giftcard"] = fila_gift["cantidad"]

        # Si el usuario confirmó/corrigió los montos por método en la pantalla
        # previa a "Generar e Iniciar Reporte Diario", esos valores son la fuente
        # de verdad para el Reporte de Cuadre (pisan lo calculado desde venta_pagos,
        # que puede haber quedado mal categorizado por Odoo).
        conteo_por_metodo = {}
        if tienda_id:
            cursor.execute("""
                SELECT ajuste_metodos_pago, conteo_fisico FROM consolidado_ventas_diarias
                WHERE fecha = %s AND tienda_id = %s
            """, (fecha, tienda_id))
            fila_consolidado = cursor.fetchone()
            if fila_consolidado and fila_consolidado.get("ajuste_metodos_pago"):
                try:
                    ajustes = json.loads(fila_consolidado["ajuste_metodos_pago"])
                    for a in ajustes:
                        nombre = _normalizar_metodo_pago(a.get("metodo"))
                        if nombre:
                            monto_por_metodo[nombre] = round(float(a.get("monto", 0) or 0), 2)
                except Exception:
                    pass
            # Conteo físico: lo que el usuario contó a mano en Cierre de Caja,
            # comparado contra "Pagos" (lo de arriba) para armar la Diferencia.
            if fila_consolidado and fila_consolidado.get("conteo_fisico"):
                try:
                    for c in json.loads(fila_consolidado["conteo_fisico"]):
                        nombre = _normalizar_metodo_pago(c.get("metodo"))
                        if nombre:
                            conteo_por_metodo[nombre] = float(c.get("monto", 0) or 0)
                except Exception:
                    pass

        # Orden fijo: Caja, Cheque, Transferencia, Tarjeta, Cuenta de cliente —
        # y cualquier otro método que aparezca (poco común) va al final.
        # Giftcard no es un método configurable en Odoo, así que nunca va a venir
        # en nombres_metodos_odoo ni en los pagos reales — se agrega siempre a
        # mano para que el módulo exista aunque Pagos quede en $0.
        nombres_metodo = _ordenar_metodos_cuadre(nombres_metodos_odoo + list(monto_por_metodo.keys()) + [GIFTCARD_SIEMPRE_PRESENTE])

        por_metodo = [
            {
                "metodo": nombre,
                "monto": monto_por_metodo.get(nombre, 0.0),
                "cantidad": cantidad_por_metodo.get(nombre, 0),
                "conteo_fisico": conteo_por_metodo.get(nombre),
                "diferencia": (
                    round(conteo_por_metodo[nombre] - monto_por_metodo.get(nombre, 0.0), 2)
                    if nombre in conteo_por_metodo else None
                )
            }
            for nombre in nombres_metodo
        ]

        return {
            "tienda": tienda_objetivo,
            "fecha": fecha,
            "total_general": round(total_general, 2),
            "cantidad_ordenes": len(ventas),
            "por_metodo": por_metodo,
            "base_apertura": base_apertura
        }
    finally:
        cursor.close()
        conexion.close()


# ─── MÓDULO CHAT DE CONSULTAS — Claude responde preguntas en lenguaje natural
# sobre los datos reales de la empresa (ventas, ranking de tiendas, productos,
# cierres), llamando a estas herramientas — nunca inventa cifras. Opcional:
# sin ANTHROPIC_API_KEY configurada, la app funciona igual y el chat solo
# avisa que falta configurarla (mismo criterio que SMTP para "olvidé
# contraseña"). Solo admin/superadmin. ───
def _chat_conectar_odoo():
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError("No se pudo autenticar con Odoo.")
    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
    try:
        tz_odoo = ZoneInfo(_obtener_tz_odoo(models, uid))
    except Exception:
        tz_odoo = timezone.utc
    return uid, models, tz_odoo


def _chat_rango_utc(desde, hasta, tz_odoo):
    inicio = datetime.strptime(desde, "%Y-%m-%d").replace(tzinfo=tz_odoo)
    fin = datetime.strptime(hasta, "%Y-%m-%d").replace(tzinfo=tz_odoo) + timedelta(days=1)
    return inicio.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), fin.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _chat_ventas_totales(desde, hasta, tienda=None):
    try:
        uid, models, tz_odoo = _chat_conectar_odoo()
        inicio_utc, fin_utc = _chat_rango_utc(desde, hasta, tz_odoo)
        filtros = [
            ['company_id', '=', TARGET_COMPANY_ID],
            ['date_order', '>=', inicio_utc], ['date_order', '<', fin_utc],
            ['state', 'not in', ['draft', 'cancel']],
        ]
        if tienda:
            config_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'pos.config', 'search', [[['name', 'ilike', tienda]]])
            if not config_ids:
                return {"error": f"No se encontró ninguna tienda que coincida con '{tienda}'. Tiendas válidas: {', '.join(TIENDAS_CONOCIDAS)}"}
            filtros.append(['config_id', 'in', config_ids])
        ordenes = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read', [filtros], {'fields': ['amount_total']})
        total = sum(o.get('amount_total', 0.0) or 0.0 for o in ordenes)
        return {"tienda": tienda or "todas las tiendas", "desde": desde, "hasta": hasta, "total_ventas": round(total, 2), "cantidad_ordenes": len(ordenes)}
    except Exception as e:
        return {"error": str(e)}


def _chat_ranking_tiendas(desde, hasta):
    try:
        uid, models, tz_odoo = _chat_conectar_odoo()
        inicio_utc, fin_utc = _chat_rango_utc(desde, hasta, tz_odoo)
        filtros = [
            ['company_id', '=', TARGET_COMPANY_ID],
            ['date_order', '>=', inicio_utc], ['date_order', '<', fin_utc],
            ['state', 'not in', ['draft', 'cancel']],
        ]
        ordenes = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'pos.order', 'search_read', [filtros], {'fields': ['config_id', 'amount_total']})
        por_tienda = {}
        for o in ordenes:
            nombre = o['config_id'][1] if o.get('config_id') else "Sin tienda"
            d = por_tienda.setdefault(nombre, {"total": 0.0, "cantidad": 0})
            d["total"] += o.get('amount_total', 0.0) or 0.0
            d["cantidad"] += 1
        ranking = sorted(
            [{"tienda": k, "total_ventas": round(v["total"], 2), "cantidad_ordenes": v["cantidad"]} for k, v in por_tienda.items()],
            key=lambda x: x["total_ventas"], reverse=True
        )
        return {"desde": desde, "hasta": hasta, "ranking": ranking}
    except Exception as e:
        return {"error": str(e)}


def _chat_ventas_producto(referencia_o_nombre, desde, hasta):
    try:
        uid, models, tz_odoo = _chat_conectar_odoo()
        inicio_utc, fin_utc = _chat_rango_utc(desde, hasta, tz_odoo)
        productos = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [['|', ['default_code', 'ilike', referencia_o_nombre], ['name', 'ilike', referencia_o_nombre]]],
            {'fields': ['id', 'name', 'default_code'], 'limit': 15}
        )
        if not productos:
            return {"error": f"No se encontró ningún producto que coincida con '{referencia_o_nombre}'."}
        product_ids = [p['id'] for p in productos]
        lineas = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'pos.order.line', 'search_read',
            [[
                ['product_id', 'in', product_ids],
                ['order_id.date_order', '>=', inicio_utc], ['order_id.date_order', '<', fin_utc],
                ['order_id.company_id', '=', TARGET_COMPANY_ID],
                ['order_id.state', 'not in', ['draft', 'cancel']],
            ]],
            {'fields': ['product_id', 'qty', 'price_subtotal_incl']}
        )
        por_producto = {}
        for l in lineas:
            pid = l['product_id'][0]
            d = por_producto.setdefault(pid, {"nombre": l['product_id'][1], "cantidad": 0.0, "monto": 0.0})
            d["cantidad"] += l.get('qty', 0.0) or 0.0
            d["monto"] += l.get('price_subtotal_incl', 0.0) or 0.0
        ventas = [{"producto": v["nombre"], "cantidad_vendida": v["cantidad"], "monto_total": round(v["monto"], 2)} for v in por_producto.values()]
        return {"desde": desde, "hasta": hasta, "productos_encontrados": len(productos), "ventas": ventas}
    except Exception as e:
        return {"error": str(e)}


def _chat_estado_cierres(desde, hasta, tienda=None):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        condiciones = ["c.fecha >= %s", "c.fecha <= %s"]
        parametros = [desde, hasta]
        if tienda:
            condiciones.append("t.nombre ILIKE %s")
            parametros.append(f"%{tienda}%")
        cursor.execute(f"""
            SELECT c.completado, COUNT(*) AS n
            FROM cierres_diarios c JOIN tiendas t ON t.id = c.tienda_id
            WHERE {' AND '.join(condiciones)}
            GROUP BY c.completado
        """, parametros)
        conteos = {0: 0, 1: 0, 2: 0}
        for fila in cursor.fetchall():
            conteos[fila["completado"] if fila["completado"] is not None else 0] = fila["n"]
        return {"desde": desde, "hasta": hasta, "tienda": tienda or "todas", "pendientes": conteos.get(0, 0), "aprobados": conteos.get(1, 0), "rechazados": conteos.get(2, 0)}
    except Exception as e:
        return {"error": str(e)}
    finally:
        cursor.close()
        conexion.close()


HERRAMIENTAS_CHAT = [
    {
        "name": "ventas_totales",
        "description": "Total de ventas ($ y cantidad de órdenes) en un rango de fechas, de una tienda específica o de todas las tiendas juntas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "desde": {"type": "string", "description": "Fecha de inicio, formato YYYY-MM-DD"},
                "hasta": {"type": "string", "description": "Fecha de fin (inclusive), formato YYYY-MM-DD"},
                "tienda": {"type": "string", "description": "Nombre de la tienda (opcional). Si no se especifica, es el total de todas las tiendas."},
            },
            "required": ["desde", "hasta"],
        },
    },
    {
        "name": "ranking_tiendas",
        "description": "Ranking de tiendas por ventas en un rango de fechas, de mayor a menor — usar para preguntas como 'cuál tienda vendió más'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "desde": {"type": "string", "description": "Fecha de inicio, formato YYYY-MM-DD"},
                "hasta": {"type": "string", "description": "Fecha de fin (inclusive), formato YYYY-MM-DD"},
            },
            "required": ["desde", "hasta"],
        },
    },
    {
        "name": "ventas_producto",
        "description": "Cantidad vendida y monto total de un producto específico (buscado por código de referencia o por nombre) en un rango de fechas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "referencia_o_nombre": {"type": "string", "description": "Código de referencia del producto o parte de su nombre"},
                "desde": {"type": "string", "description": "Fecha de inicio, formato YYYY-MM-DD"},
                "hasta": {"type": "string", "description": "Fecha de fin (inclusive), formato YYYY-MM-DD"},
            },
            "required": ["referencia_o_nombre", "desde", "hasta"],
        },
    },
    {
        "name": "estado_cierres",
        "description": "Cantidad de cierres de caja aprobados, pendientes y rechazados en un rango de fechas, de una tienda específica o de todas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "desde": {"type": "string", "description": "Fecha de inicio, formato YYYY-MM-DD"},
                "hasta": {"type": "string", "description": "Fecha de fin (inclusive), formato YYYY-MM-DD"},
                "tienda": {"type": "string", "description": "Nombre de la tienda (opcional)"},
            },
            "required": ["desde", "hasta"],
        },
    },
]

_HERRAMIENTAS_CHAT_FUNCIONES = {
    "ventas_totales": lambda args: _chat_ventas_totales(args.get("desde"), args.get("hasta"), args.get("tienda")),
    "ranking_tiendas": lambda args: _chat_ranking_tiendas(args.get("desde"), args.get("hasta")),
    "ventas_producto": lambda args: _chat_ventas_producto(args.get("referencia_o_nombre"), args.get("desde"), args.get("hasta")),
    "estado_cierres": lambda args: _chat_estado_cierres(args.get("desde"), args.get("hasta"), args.get("tienda")),
}


@app.post("/api/chat")
async def chat_consultas(request: Request, admin=Depends(requerir_admin)):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="El chat de consultas no está configurado todavía — falta ANTHROPIC_API_KEY en el .env del servidor."
        )

    import anthropic

    body = await request.json()
    mensaje = (body.get("mensaje") or "").strip()
    if not mensaje:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío.")
    historial = body.get("historial") or []
    if len(historial) > 40:
        historial = historial[-40:]

    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system_prompt = (
        "Sos el asistente de datos de Cultura Tejida, un panel de ventas para las tiendas Lemaler y Mariola. "
        f"Hoy es {hoy}. Respondés en español, de forma breve y concreta, citando montos en dólares con 2 "
        "decimales. Usá siempre las herramientas disponibles para consultar datos reales — nunca inventes "
        "cifras ni infieras un resultado sin haber llamado a una herramienta. Si una pregunta menciona una "
        "fecha relativa ('este mes', 'la semana pasada', 'ayer'), calculá vos mismo las fechas exactas "
        "(YYYY-MM-DD) a partir de la fecha de hoy antes de llamar a una herramienta. "
        f"Las tiendas válidas son: {', '.join(TIENDAS_CONOCIDAS)}."
    )

    client = anthropic.Anthropic(api_key=api_key)
    mensajes = list(historial) + [{"role": "user", "content": mensaje}]

    try:
        for _ in range(6):  # tope de vueltas de tool-use por seguridad
            respuesta = client.messages.create(
                model="claude-opus-5",
                max_tokens=4096,
                output_config={"effort": "medium"},
                system=system_prompt,
                tools=HERRAMIENTAS_CHAT,
                messages=mensajes,
            )

            bloque_assistant = {"role": "assistant", "content": [b.to_dict() for b in respuesta.content]}
            mensajes.append(bloque_assistant)

            if respuesta.stop_reason != "tool_use":
                texto_final = "".join(b.text for b in respuesta.content if b.type == "text")
                if not texto_final:
                    texto_final = "No pude generar una respuesta para esa consulta."
                return {"respuesta": texto_final, "historial": mensajes}

            resultados_herramientas = []
            for bloque in respuesta.content:
                if bloque.type == "tool_use":
                    funcion = _HERRAMIENTAS_CHAT_FUNCIONES.get(bloque.name)
                    resultado = funcion(bloque.input) if funcion else {"error": "Herramienta desconocida."}
                    resultados_herramientas.append({
                        "type": "tool_result", "tool_use_id": bloque.id,
                        "content": json.dumps(resultado, ensure_ascii=False),
                    })
            mensajes.append({"role": "user", "content": resultados_herramientas})

        return {
            "respuesta": "No pude terminar de procesar esa consulta — probá con una pregunta más simple o un rango de fechas más chico.",
            "historial": mensajes,
        }
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=503, detail="La clave de Anthropic configurada en el servidor no es válida.")
    except anthropic.RateLimitError:
        raise HTTPException(status_code=429, detail="Demasiadas consultas seguidas — esperá un momento y probá de nuevo.")
    except anthropic.APIStatusError as e:
        raise HTTPException(status_code=502, detail=f"Error del servicio de IA: {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(status_code=502, detail="No se pudo conectar con el servicio de IA.")


# ─── MÓDULO 6: TAREAS — se asignan en Odoo (módulo Proyecto/Tareas), a la
# tienda: cada tienda tiene su propio usuario en Odoo con el mismo nombre
# que en TIENDAS_CONOCIDAS. Cultura Tejida solo LEE de Odoo (nunca escribe de
# vuelta); el estado del lado de la tienda (pendiente/en progreso/
# completada) y la revisión del superadmin viven acá, en Postgres. ───
def _sincronizar_tareas_odoo(cursor):
    """Trae de Odoo las tareas asignadas a alguna tienda conocida y hace
    upsert en `tareas` por odoo_task_id. Los campos de workflow propios de
    Cultura Tejida (estado, comentarios, revisión) nunca se pisan acá — el
    upsert solo actualiza título/descripción/fecha límite/prioridad."""
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            return
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        usuarios_odoo = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'res.users', 'search_read',
            [[['name', 'in', TIENDAS_CONOCIDAS]]], {'fields': ['id', 'name']}
        )
        mapa_usuario_a_tienda = {u['id']: u['name'] for u in usuarios_odoo}
        if not mapa_usuario_a_tienda:
            return

        tareas_odoo = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'project.task', 'search_read',
            [[['user_ids', 'in', list(mapa_usuario_a_tienda.keys())]]],
            {'fields': ['id', 'name', 'description', 'user_ids', 'date_deadline', 'priority']}
        )
    except Exception as e:
        print(f"No se pudo sincronizar tareas desde Odoo: {e}")
        return

    for t in tareas_odoo:
        nombre_tienda = next(
            (mapa_usuario_a_tienda[uid_asignado] for uid_asignado in (t.get('user_ids') or []) if uid_asignado in mapa_usuario_a_tienda),
            None
        )
        if not nombre_tienda:
            continue

        cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (nombre_tienda,))
        fila_tienda = cursor.fetchone()
        if not fila_tienda:
            continue
        tienda_id = fila_tienda["id"] if isinstance(fila_tienda, dict) else fila_tienda[0]

        # La descripción viene en HTML desde Odoo (campo rich text) — se
        # limpia a texto plano, acá no hace falta el formato.
        descripcion_html = t.get('description') or ''
        descripcion = re.sub('<[^<]+?>', ' ', descripcion_html)
        descripcion = re.sub(r'\s+', ' ', descripcion).strip() or None

        fecha_limite = t.get('date_deadline') or None
        if fecha_limite:
            fecha_limite = fecha_limite.split(' ')[0]

        cursor.execute("""
            INSERT INTO tareas (odoo_task_id, titulo, descripcion, tienda_id, fecha_limite, prioridad)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (odoo_task_id) DO UPDATE SET
                titulo = EXCLUDED.titulo, descripcion = EXCLUDED.descripcion,
                tienda_id = EXCLUDED.tienda_id, fecha_limite = EXCLUDED.fecha_limite,
                prioridad = EXCLUDED.prioridad
        """, (t['id'], t['name'], descripcion, tienda_id, fecha_limite, t.get('priority')))


def _tarea_publica(f, evidencias=None):
    return {
        "id": f["id"],
        "titulo": f["titulo"],
        "descripcion": f["descripcion"],
        "tienda": f["tienda_nombre"],
        "fecha_limite": f["fecha_limite"].strftime("%Y-%m-%d") if f["fecha_limite"] else None,
        "prioridad": f["prioridad"],
        "estado": f["estado"],
        "comentario_usuario": f["comentario_usuario"],
        "revisado_por_admin": f["revisado_por_admin"],
        "comentario_admin": f["comentario_admin"],
        "actualizado_en": f["actualizado_en"].strftime("%Y-%m-%d %H:%M") if f["actualizado_en"] else None,
        "evidencias": evidencias or [],
    }


@app.get("/api/tareas")
def obtener_tareas(usuario=Depends(obtener_usuario_actual)):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        _sincronizar_tareas_odoo(cursor)
        conexion.commit()

        if usuario["rol"] == "usuario":
            if not usuario["tienda_id"]:
                return []
            cursor.execute("""
                SELECT t.*, td.nombre AS tienda_nombre
                FROM tareas t JOIN tiendas td ON td.id = t.tienda_id
                WHERE t.tienda_id = %s
                ORDER BY (t.estado = 'completada') ASC, t.fecha_limite ASC NULLS LAST, t.creado_en DESC
            """, (usuario["tienda_id"],))
        else:
            cursor.execute("""
                SELECT t.*, td.nombre AS tienda_nombre
                FROM tareas t LEFT JOIN tiendas td ON td.id = t.tienda_id
                ORDER BY (t.estado = 'completada') ASC, t.fecha_limite ASC NULLS LAST, t.creado_en DESC
            """)
        filas = cursor.fetchall()

        # Evidencias de todas las tareas de una — evita una consulta por
        # tarea (N+1).
        ids_tareas = [f["id"] for f in filas]
        evidencias_por_tarea = {}
        if ids_tareas:
            cursor.execute("""
                SELECT id, tarea_id, nombre_archivo, id_google_drive, tipo_archivo, subido_por, creado_en
                FROM tareas_evidencias WHERE tarea_id = ANY(%s) ORDER BY creado_en ASC
            """, (ids_tareas,))
            for e in cursor.fetchall():
                evidencias_por_tarea.setdefault(e["tarea_id"], []).append({
                    "id": e["id"],
                    "nombre_archivo": e["nombre_archivo"],
                    "url": f"https://drive.google.com/file/d/{e['id_google_drive']}/view" if e["id_google_drive"] else None,
                    "tipo_archivo": e["tipo_archivo"],
                    "subido_por": e["subido_por"],
                })

        return [_tarea_publica(f, evidencias_por_tarea.get(f["id"], [])) for f in filas]
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/tareas/{tarea_id}/evidencia")
async def subir_evidencia_tarea(
    tarea_id: int,
    archivo: UploadFile = File(...),
    usuario=Depends(obtener_usuario_actual)
):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id, tienda_id, titulo FROM tareas WHERE id = %s", (tarea_id,))
        tarea = cursor.fetchone()
        if not tarea:
            raise HTTPException(status_code=404, detail="Tarea no encontrada.")
        if usuario["rol"] == "usuario" and usuario["tienda_id"] != tarea["tienda_id"]:
            raise HTTPException(status_code=403, detail="Esta tarea no es de tu tienda.")

        from config.drive_manager import subir_archivo_a_drive
        ruta_t = f"temp_evidencia_tarea_{tarea_id}_{archivo.filename}"
        with open(ruta_t, "wb") as b:
            shutil.copyfileobj(archivo.file, b)
        drive_id = subir_archivo_a_drive(ruta_t, f"TAREA_{tarea_id}_{archivo.filename}", archivo.content_type)
        if os.path.exists(ruta_t):
            os.remove(ruta_t)

        if not drive_id:
            raise HTTPException(status_code=500, detail="No se pudo subir el archivo a Drive.")

        cursor.execute("""
            INSERT INTO tareas_evidencias (tarea_id, nombre_archivo, id_google_drive, tipo_archivo, subido_por)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (tarea_id, archivo.filename, drive_id, archivo.content_type, usuario["nombre"]))
        evidencia_id = cursor.fetchone()["id"]
        cursor.execute("UPDATE tareas SET actualizado_en = NOW() WHERE id = %s", (tarea_id,))
        _registrar_auditoria(cursor, usuario, "adjuntar_evidencia_tarea", f"{tarea['titulo']} — {archivo.filename}")
        conexion.commit()
        return {
            "status": "success",
            "evidencia": {
                "id": evidencia_id, "nombre_archivo": archivo.filename,
                "url": f"https://drive.google.com/file/d/{drive_id}/view",
                "tipo_archivo": archivo.content_type, "subido_por": usuario["nombre"],
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al subir la evidencia: {str(e)}")
    finally:
        cursor.close()
        conexion.close()


@app.get("/api/tareas/resumen")
def resumen_tareas(usuario=Depends(obtener_usuario_actual)):
    """Solo lee lo que ya está en Postgres (sin sincronizar con Odoo) — para
    que el recordatorio en el Dashboard sea instantáneo. La sincronización
    de verdad pasa al entrar a la pantalla de Tareas."""
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        if usuario["rol"] == "usuario":
            if not usuario["tienda_id"]:
                return {"pendientes": 0}
            cursor.execute(
                "SELECT COUNT(*) AS n FROM tareas WHERE tienda_id = %s AND estado != 'completada'",
                (usuario["tienda_id"],)
            )
        elif usuario["rol"] == "superadmin":
            cursor.execute("SELECT COUNT(*) AS n FROM tareas WHERE estado = 'completada' AND revisado_por_admin = FALSE")
        else:
            return {"pendientes": 0}
        return {"pendientes": cursor.fetchone()["n"]}
    finally:
        cursor.close()
        conexion.close()


@app.put("/api/tareas/{tarea_id}/estado")
async def actualizar_estado_tarea(tarea_id: int, request: Request, usuario=Depends(obtener_usuario_actual)):
    from config.db_manager import RealDictCursor
    body = await request.json()
    nuevo_estado = (body.get("estado") or "").strip()
    comentario = (body.get("comentario") or "").strip() or None
    if nuevo_estado not in ("pendiente", "en_progreso", "completada"):
        raise HTTPException(status_code=400, detail="Estado inválido.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id, tienda_id, titulo FROM tareas WHERE id = %s", (tarea_id,))
        tarea = cursor.fetchone()
        if not tarea:
            raise HTTPException(status_code=404, detail="Tarea no encontrada.")

        if usuario["rol"] == "usuario" and usuario["tienda_id"] != tarea["tienda_id"]:
            raise HTTPException(status_code=403, detail="Esta tarea no es de tu tienda.")

        cursor.execute("""
            UPDATE tareas SET estado = %s, comentario_usuario = %s, revisado_por_admin = FALSE, actualizado_en = NOW()
            WHERE id = %s
        """, (nuevo_estado, comentario, tarea_id))
        _registrar_auditoria(cursor, usuario, "actualizar_tarea", f"{tarea['titulo']} → {nuevo_estado}")
        conexion.commit()
        return {"status": "success"}
    except HTTPException:
        raise
    finally:
        cursor.close()
        conexion.close()


@app.put("/api/tareas/{tarea_id}/revisar")
async def revisar_tarea(tarea_id: int, request: Request, superadmin=Depends(requerir_superadmin)):
    from config.db_manager import RealDictCursor
    body = await request.json()
    comentario_admin = (body.get("comentario_admin") or "").strip() or None

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id, titulo FROM tareas WHERE id = %s", (tarea_id,))
        tarea = cursor.fetchone()
        if not tarea:
            raise HTTPException(status_code=404, detail="Tarea no encontrada.")

        cursor.execute("""
            UPDATE tareas SET revisado_por_admin = TRUE, comentario_admin = %s, actualizado_en = NOW()
            WHERE id = %s
        """, (comentario_admin, tarea_id))
        _registrar_auditoria(cursor, superadmin, "revisar_tarea", tarea["titulo"])
        conexion.commit()
        return {"status": "success"}
    except HTTPException:
        raise
    finally:
        cursor.close()
        conexion.close()


# ─── MÓDULO 9: CONTEO DE INVENTARIO POR CÓDIGO DE BARRAS — portado de un
# Google Apps Script que usaba Google Sheets como base (una hoja "Productos_Odoo"
# por ubicación cargada, una hoja "Config_<sesión>" por operario escaneando).
# Acá cada tienda tiene su propia ubicación activa (el script original era una
# sola global: si dos tiendas cargaban una ubicación a la vez, la segunda le
# borraba el conteo a la primera). Los escaneos son INSERTs individuales en
# Postgres — como cada operario ya no escribe en un recurso compartido, no
# hace falta ningún candado manual (CacheService) para evitar que se pisen:
# eso lo resuelve solo la base de datos. El envío del ajuste a Odoo queda
# restringido a admin/superadmin (reemplaza la clave de supervisor compartida
# del script original por el sistema de roles que ya tiene Cultura Tejida).
TAMANO_LOTE_ODOO_INVENTARIO = 500


def _inventario_en_lotes(lista, tam=TAMANO_LOTE_ODOO_INVENTARIO):
    for i in range(0, len(lista), tam):
        yield lista[i:i + tam]


def _inventario_limpiar_nombre(nombre_sucio, barcode):
    """Le saca al nombre del producto los códigos técnicos que Odoo antepone
    (ej. 'MR366-C-3 - Blusa Azul' -> 'Blusa Azul'), igual que hacía el script
    original a mano en la planilla."""
    if not nombre_sucio:
        return "Producto Sin Nombre"
    limpio = str(nombre_sucio).strip()
    limpio = re.sub(r'^[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]*\s*-\s*', '', limpio, flags=re.IGNORECASE)
    limpio = re.sub(r'^[A-Z0-9]+-[A-Z0-9]+\s*-\s*', '', limpio, flags=re.IGNORECASE)
    limpio = re.sub(r'^[A-Z0-9]+-[A-Z0-9]+\s+', '', limpio, flags=re.IGNORECASE)
    limpio = re.sub(r'^\[[^\]]+\]\s*', '', limpio)

    if barcode:
        barcode_str = str(barcode).strip()
        if barcode_str:
            if limpio.startswith(barcode_str):
                limpio = limpio[len(barcode_str):].strip()
            if limpio.endswith(barcode_str):
                limpio = limpio[:-len(barcode_str)].strip()

    partes = re.split(r'\s+-\s+', limpio)
    if len(partes) > 1 and re.search(r'[0-9]', partes[1]) and len(partes[0]) > 3:
        limpio = partes[0].strip()

    limpio = re.sub(r'^[\s\-–+]+|[\s\-–+]+$', '', limpio).strip()
    return limpio or "Producto Sin Nombre"


def _inventario_resolver_tienda_id(usuario, tienda_nombre, cursor):
    if usuario["rol"] == "usuario":
        if not usuario["tienda_id"]:
            raise HTTPException(status_code=400, detail="Tu usuario no tiene una tienda asignada.")
        return usuario["tienda_id"]
    if not tienda_nombre:
        raise HTTPException(status_code=400, detail="Debes indicar una tienda.")
    cursor.execute("SELECT id FROM tiendas WHERE nombre = %s", (tienda_nombre,))
    fila = cursor.fetchone()
    if not fila:
        raise HTTPException(status_code=400, detail=f"No se encontró la tienda '{tienda_nombre}'.")
    return fila["id"]


def _inventario_esta_activo(tienda_id, cursor):
    cursor.execute("SELECT activo FROM inventario_activaciones WHERE tienda_id = %s", (tienda_id,))
    fila = cursor.fetchone()
    return bool(fila and fila["activo"])


def _inventario_verificar_activo(tienda_id, cursor):
    """Módulo aparte: aunque el usuario tenga permiso de rol, esta tienda
    puntual tiene que estar habilitada por un superadmin (interruptor manual)
    antes de poder cargar, escanear o sumar conteo."""
    if not _inventario_esta_activo(tienda_id, cursor):
        raise HTTPException(status_code=403, detail="El módulo de Conteo de Inventario no está activado para esta tienda. Pedile a un superadmin que lo active.")


def _inventario_archivar_y_limpiar_escaneos(cursor, tienda_id):
    cursor.execute("""
        INSERT INTO inventario_escaneos_archivo (tienda_id, sesion_id, usuario_nombre, ubicacion, codigo, escaneado_en)
        SELECT tienda_id, sesion_id, usuario_nombre, ubicacion, codigo, creado_en
        FROM inventario_escaneos WHERE tienda_id = %s
    """, (tienda_id,))
    cursor.execute("DELETE FROM inventario_escaneos WHERE tienda_id = %s", (tienda_id,))


@app.get("/api/inventario/activacion")
def inventario_obtener_activacion(tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    """Cualquier usuario autenticado puede CONSULTAR si el módulo está
    habilitado para su tienda (así el frontend sabe si mostrar el conteo o
    la pantalla de bloqueo) — solo el superadmin puede CAMBIARLO (ver abajo)."""
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, tienda, cursor)
        cursor.execute("SELECT bodega_odoo FROM tiendas WHERE id = %s", (tienda_id,))
        fila = cursor.fetchone()
        return {"activo": _inventario_esta_activo(tienda_id, cursor), "bodega_odoo": fila["bodega_odoo"] if fila else None}
    finally:
        cursor.close()
        conexion.close()


@app.put("/api/inventario/activacion")
async def inventario_cambiar_activacion(request: Request, usuario=Depends(requerir_admin)):
    """Interruptor manual por tienda — sin código ni vencimiento. Admin y
    superadmin pueden prender o apagar el módulo de Conteo de Inventario
    (asignar/cambiar la bodega de Odoo sigue siendo solo de superadmin, ver
    /api/inventario/bodega más abajo)."""
    from config.db_manager import RealDictCursor
    body = await request.json()
    activo = bool(body.get("activo"))
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, body.get("tienda"), cursor)
        cursor.execute("SELECT nombre FROM tiendas WHERE id = %s", (tienda_id,))
        nombre_tienda = cursor.fetchone()["nombre"]
        cursor.execute("""
            INSERT INTO inventario_activaciones (tienda_id, activo, activado_por, actualizado_en)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (tienda_id) DO UPDATE SET activo = EXCLUDED.activo, activado_por = EXCLUDED.activado_por, actualizado_en = NOW()
        """, (tienda_id, activo, usuario["nombre"]))
        _registrar_auditoria(cursor, usuario, "inventario_cambiar_activacion", f"{nombre_tienda}: {'activado' if activo else 'desactivado'}")
        conexion.commit()
        return {"status": "success", "activo": activo}
    finally:
        cursor.close()
        conexion.close()


@app.put("/api/inventario/bodega")
async def inventario_asignar_bodega(request: Request, usuario=Depends(requerir_superadmin)):
    """Solo el superadmin asigna a cada tienda SU bodega de Odoo (código de
    barras de la ubicación, ej. 'INV-LD') — una vez asignada, esa tienda
    siempre carga esa misma bodega, nunca una escrita a mano por error."""
    from config.db_manager import RealDictCursor
    body = await request.json()
    bodega_odoo = (body.get("bodega_odoo") or "").strip().upper() or None
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, body.get("tienda"), cursor)
        cursor.execute("SELECT nombre, bodega_odoo AS anterior FROM tiendas WHERE id = %s", (tienda_id,))
        fila_previa = cursor.fetchone()
        cursor.execute("UPDATE tiendas SET bodega_odoo = %s WHERE id = %s", (bodega_odoo, tienda_id))
        _registrar_auditoria(
            cursor, usuario, "inventario_asignar_bodega",
            f"{fila_previa['nombre']}: '{fila_previa['anterior'] or '(sin asignar)'}' -> '{bodega_odoo or '(sin asignar)'}'"
        )
        conexion.commit()
        return {"status": "success", "bodega_odoo": bodega_odoo}
    finally:
        cursor.close()
        conexion.close()


@app.get("/api/inventario/mi-progreso")
def inventario_mi_progreso(tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    """Para el recordatorio del Dashboard: si esta tienda tiene el módulo
    activo y un conteo en curso sin terminar, cuánto le falta. Nunca da 403
    aunque el módulo esté apagado — simplemente responde 'activo: false' para
    que el Dashboard no muestre nada, en vez de romper la pantalla principal."""
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, tienda, cursor)
        if not _inventario_esta_activo(tienda_id, cursor):
            return {"activo": False}
        cursor.execute("SELECT ubicacion FROM inventario_ubicacion_activa WHERE tienda_id = %s", (tienda_id,))
        fila_ubi = cursor.fetchone()
        if not fila_ubi:
            return {"activo": True, "hay_sesion": False}
        cursor.execute("""
            SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE contado_real > 0) AS contados
            FROM inventario_productos WHERE tienda_id = %s
        """, (tienda_id,))
        fila = cursor.fetchone()
        total, contados = fila["total"], fila["contados"]
        return {
            "activo": True, "hay_sesion": True, "ubicacion": fila_ubi["ubicacion"],
            "total": total, "contados": contados,
            "terminado": total > 0 and contados >= total,
        }
    finally:
        cursor.close()
        conexion.close()


@app.get("/api/inventario/resumen-global")
def inventario_resumen_global(usuario=Depends(requerir_superadmin)):
    """Panorama de TODAS las tiendas para el superadmin: cuáles tienen el
    módulo activo, qué bodega tienen cargada ahora mismo y cuánto llevan
    contado — sin tener que entrar tienda por tienda."""
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT
                t.id, t.nombre, t.bodega_odoo,
                COALESCE(act.activo, FALSE) AS activo,
                u.ubicacion AS ubicacion_activa,
                u.actualizado_en,
                COUNT(p.id) AS total_productos,
                COUNT(p.id) FILTER (WHERE p.contado_real > 0) AS productos_contados
            FROM tiendas t
            LEFT JOIN inventario_activaciones act ON act.tienda_id = t.id
            LEFT JOIN inventario_ubicacion_activa u ON u.tienda_id = t.id
            LEFT JOIN inventario_productos p ON p.tienda_id = t.id
            GROUP BY t.id, t.nombre, t.bodega_odoo, act.activo, u.ubicacion, u.actualizado_en
            ORDER BY t.nombre
        """)
        filas = []
        for r in cursor.fetchall():
            total, contados = r["total_productos"], r["productos_contados"]
            filas.append({
                "tienda": r["nombre"], "bodega_odoo": r["bodega_odoo"], "activo": r["activo"],
                "ubicacion_activa": r["ubicacion_activa"],
                "actualizado_en": r["actualizado_en"].strftime("%d/%m %H:%M") if r["actualizado_en"] else None,
                "total": total, "contados": contados,
                "pct": round(contados / total * 100) if total > 0 else None,
            })
        return {"tiendas": filas}
    finally:
        cursor.close()
        conexion.close()


@app.get("/api/inventario/estado")
def inventario_estado(tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, tienda, cursor)
        _inventario_verificar_activo(tienda_id, cursor)
        cursor.execute("SELECT ubicacion, actualizado_en FROM inventario_ubicacion_activa WHERE tienda_id = %s", (tienda_id,))
        fila_ubi = cursor.fetchone()
        cursor.execute("""
            SELECT producto_ref, nombre, barcode, stock_sistema, contado_real
            FROM inventario_productos WHERE tienda_id = %s ORDER BY nombre
        """, (tienda_id,))
        productos = cursor.fetchall()
        cursor.execute("SELECT barcode, cantidad FROM inventario_productos_nuevos WHERE tienda_id = %s ORDER BY actualizado_en DESC", (tienda_id,))
        nuevos = cursor.fetchall()
        return {
            "ubicacion_activa": fila_ubi["ubicacion"] if fila_ubi else None,
            "actualizado_en": fila_ubi["actualizado_en"].strftime("%H:%M:%S") if fila_ubi else None,
            "productos": productos,
            "productos_nuevos": nuevos,
        }
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/inventario/cargar-ubicacion")
async def inventario_cargar_ubicacion(request: Request, usuario=Depends(obtener_usuario_actual)):
    """Ya NO se recibe una ubicación escrita a mano: cada tienda tiene UNA
    sola bodega de Odoo asignada (tiendas.bodega_odoo, configurada por un
    superadmin) y siempre se carga esa — así una tienda nunca puede terminar
    apuntando por error a la bodega de otra."""
    from config.db_manager import RealDictCursor
    body = await request.json()
    forzar_reinicio = bool(body.get("forzar_reinicio"))

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, body.get("tienda"), cursor)
        _inventario_verificar_activo(tienda_id, cursor)

        cursor.execute("SELECT nombre, bodega_odoo FROM tiendas WHERE id = %s", (tienda_id,))
        tienda_row = cursor.fetchone()
        bodega_odoo = (tienda_row["bodega_odoo"] or "").strip() if tienda_row else ""
        if not bodega_odoo:
            nombre_tienda = tienda_row["nombre"] if tienda_row else "Esta tienda"
            raise HTTPException(status_code=400, detail=f"'{nombre_tienda}' todavía no tiene una bodega de Odoo asignada. Pedile a un superadmin que la configure arriba, en esta misma pantalla.")

        uid, models, tz_odoo = _chat_conectar_odoo()
        ubicaciones = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
            [[['barcode', '=', bodega_odoo]]],
            {'fields': ['id', 'complete_name'], 'limit': 1}
        )
        if not ubicaciones:
            raise HTTPException(status_code=404, detail=f"La bodega asignada ('{bodega_odoo}') no se encontró en Odoo.")
        location_id = ubicaciones[0]['id']
        ubicacion = ubicaciones[0]['complete_name']

        # MULTIUSUARIO + candado contra reasignación: si ya había una sesión
        # cargada para EL MISMO location_id (no solo el mismo texto) y no se
        # pidió reinicio, cualquier operario que la vuelva a buscar se "une" a
        # ella tal cual está — nunca se reinicia sola. Si un superadmin le
        # reasignó a esta tienda otra bodega, el location_id ya no coincide y
        # se recarga sola desde Odoo, sin que nadie tenga que acordarse.
        cursor.execute("SELECT location_id, actualizado_en FROM inventario_ubicacion_activa WHERE tienda_id = %s", (tienda_id,))
        fila_activa = cursor.fetchone()
        misma_ubicacion = fila_activa and fila_activa["location_id"] == location_id
        cursor.execute("SELECT 1 FROM inventario_productos WHERE tienda_id = %s LIMIT 1", (tienda_id,))
        ya_hay_datos = cursor.fetchone() is not None

        if misma_ubicacion and ya_hay_datos and not forzar_reinicio:
            cursor.execute("""
                SELECT producto_ref, nombre, barcode, stock_sistema, contado_real
                FROM inventario_productos WHERE tienda_id = %s ORDER BY nombre
            """, (tienda_id,))
            productos = cursor.fetchall()
            cursor.execute("SELECT barcode, cantidad FROM inventario_productos_nuevos WHERE tienda_id = %s ORDER BY actualizado_en DESC", (tienda_id,))
            nuevos = cursor.fetchall()
            return {
                "status": "success", "unido": True, "ubicacion": ubicacion, "productos": productos, "productos_nuevos": nuevos,
                "actualizado_en": fila_activa["actualizado_en"].strftime("%H:%M:%S"),
            }

        # Solo quants con cantidad distinta de cero (positiva o negativa) — los
        # productos en cero no se cuentan, así lo pidió el usuario. Sin límite
        # de resultados: acá no hay paginación con tope como en Apps Script.
        stock_quants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [[['location_id', '=', location_id], ['quantity', '!=', 0]]],
            {'fields': ['product_id', 'quantity']}
        )

        # Se limpia todo lo anterior de ESTA tienda (nunca de otra) — se
        # archivan los escaneos pendientes primero, nada se pierde.
        _inventario_archivar_y_limpiar_escaneos(cursor, tienda_id)
        cursor.execute("DELETE FROM inventario_productos WHERE tienda_id = %s", (tienda_id,))
        cursor.execute("DELETE FROM inventario_productos_nuevos WHERE tienda_id = %s", (tienda_id,))
        cursor.execute("""
            INSERT INTO inventario_ubicacion_activa (tienda_id, ubicacion, location_id, actualizado_en)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (tienda_id) DO UPDATE SET ubicacion = EXCLUDED.ubicacion, location_id = EXCLUDED.location_id, actualizado_en = NOW()
            RETURNING actualizado_en
        """, (tienda_id, ubicacion, location_id))
        hora_actualizado = cursor.fetchone()["actualizado_en"].strftime("%H:%M:%S")

        if not stock_quants:
            conexion.commit()
            return {"status": "success", "unido": False, "ubicacion": ubicacion, "productos": [], "productos_nuevos": [], "actualizado_en": hora_actualizado, "mensaje": "Ubicación sin stock en sistema."}

        product_ids = list({q['product_id'][0] for q in stock_quants})
        productos_detalle = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[['id', 'in', product_ids]]],
            {'fields': ['id', 'name', 'display_name', 'barcode']}
        )

        # Referencia = el id numérico de Odoo directo — el script original
        # resolvía además un "external id" (ir.model.data) para cada producto,
        # pero acá no hace falta: nunca se exporta/importa por external id,
        # así que esa consulta extra a Odoo se elimina sin perder nada.
        mapa_productos = {}
        for p in productos_detalle:
            barcode_actual = p.get('barcode') or ''
            texto_base = p.get('name') or p.get('display_name') or "Producto Sin Nombre"
            mapa_productos[p['id']] = {
                "producto_ref": str(p['id']),
                "nombre": _inventario_limpiar_nombre(texto_base, barcode_actual),
                "barcode": barcode_actual or f"SIN_BARCODE_{p['id']}",
                "stock_sistema": 0,
                "contado_real": 0,
            }
        for q in stock_quants:
            pid = q['product_id'][0]
            if pid in mapa_productos:
                mapa_productos[pid]["stock_sistema"] += round(q.get('quantity', 0.0) or 0.0)

        filas = list(mapa_productos.values())
        for f in filas:
            cursor.execute("""
                INSERT INTO inventario_productos (tienda_id, producto_ref, nombre, barcode, stock_sistema, contado_real)
                VALUES (%s, %s, %s, %s, %s, 0)
                ON CONFLICT (tienda_id, producto_ref) DO UPDATE SET
                    nombre = EXCLUDED.nombre, barcode = EXCLUDED.barcode, stock_sistema = EXCLUDED.stock_sistema
            """, (tienda_id, f["producto_ref"], f["nombre"], f["barcode"], f["stock_sistema"]))

        conexion.commit()
        return {"status": "success", "unido": False, "ubicacion": ubicacion, "productos": filas, "productos_nuevos": [], "actualizado_en": hora_actualizado}
    except HTTPException:
        conexion.rollback()
        raise
    except Exception as e:
        conexion.rollback()
        raise HTTPException(status_code=500, detail=f"Error consultando Odoo: {str(e)}")
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/inventario/actualizar-stock-sistema")
async def inventario_actualizar_stock_sistema(request: Request, usuario=Depends(obtener_usuario_actual)):
    """Vuelve a consultar Odoo y refresca SOLO 'Stock Sistema' — el conteo
    real (columna aparte) nunca se toca, a diferencia de 'Reiniciar'."""
    from config.db_manager import RealDictCursor
    body = await request.json()
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, body.get("tienda"), cursor)
        _inventario_verificar_activo(tienda_id, cursor)
        cursor.execute("SELECT location_id FROM inventario_ubicacion_activa WHERE tienda_id = %s", (tienda_id,))
        fila_ubi = cursor.fetchone()
        if not fila_ubi or not fila_ubi["location_id"]:
            raise HTTPException(status_code=400, detail="No hay ninguna ubicación cargada para esta tienda.")

        uid, models, tz_odoo = _chat_conectar_odoo()
        stock_quants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [[['location_id', '=', fila_ubi["location_id"]], ['quantity', '!=', 0]]],
            {'fields': ['product_id', 'quantity']}
        )
        mapa_stock_fresco = {}
        for q in stock_quants or []:
            pid = q['product_id'][0]
            mapa_stock_fresco[pid] = mapa_stock_fresco.get(pid, 0) + round(q.get('quantity', 0.0) or 0.0)

        cursor.execute("SELECT id, producto_ref FROM inventario_productos WHERE tienda_id = %s", (tienda_id,))
        productos = cursor.fetchall()
        actualizados = 0
        for p in productos:
            try:
                pid = int(p["producto_ref"])
            except ValueError:
                continue
            nuevo_stock = mapa_stock_fresco.get(pid, 0)
            cursor.execute("UPDATE inventario_productos SET stock_sistema = %s WHERE id = %s AND stock_sistema != %s", (nuevo_stock, p["id"], nuevo_stock))
            if cursor.rowcount:
                actualizados += 1

        # Se marca la hora del refresco — así en pantalla se ve cuán "fresco"
        # está el Stock Sistema frente a ventas que puedan estar pasando en
        # Odoo justo mientras se cuenta físicamente.
        cursor.execute("UPDATE inventario_ubicacion_activa SET actualizado_en = NOW() WHERE tienda_id = %s RETURNING actualizado_en", (tienda_id,))
        hora_actualizado = cursor.fetchone()["actualizado_en"].strftime("%H:%M:%S")

        conexion.commit()
        cursor.execute("""
            SELECT producto_ref, nombre, barcode, stock_sistema, contado_real
            FROM inventario_productos WHERE tienda_id = %s ORDER BY nombre
        """, (tienda_id,))
        return {"status": "success", "actualizados": actualizados, "actualizado_en": hora_actualizado, "productos": cursor.fetchall()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando Odoo: {str(e)}")
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/inventario/escanear")
async def inventario_escanear(request: Request, usuario=Depends(obtener_usuario_actual)):
    from config.db_manager import RealDictCursor
    body = await request.json()
    codigos = body.get("codigos") or []
    sesion_id = (body.get("sesion_id") or "").strip()
    ubicacion = (body.get("ubicacion") or "").strip()
    if not sesion_id or not ubicacion:
        raise HTTPException(status_code=400, detail="Falta sesión o ubicación.")
    codigos_limpios = [str(c).strip() for c in codigos if str(c).strip()]
    if not codigos_limpios:
        raise HTTPException(status_code=400, detail="No se recibió ningún código para registrar.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, body.get("tienda"), cursor)
        _inventario_verificar_activo(tienda_id, cursor)
        # Candado COMPARTIDO: muchos escaneos a la vez conviven sin bloquearse
        # entre sí, pero un "Sumar" (que pide el candado exclusivo) espera a
        # que esta transacción termine antes de agrupar — así ningún escaneo
        # que entra justo en el instante del "Sumar" queda afuera del conteo.
        cursor.execute("SELECT pg_advisory_xact_lock_shared(%s)", (tienda_id,))
        for codigo in codigos_limpios:
            cursor.execute("""
                INSERT INTO inventario_escaneos (tienda_id, sesion_id, usuario_nombre, ubicacion, codigo)
                VALUES (%s, %s, %s, %s, %s)
            """, (tienda_id, sesion_id, usuario["nombre"], ubicacion, codigo))
        conexion.commit()
        return {"status": "success", "cantidad": len(codigos_limpios)}
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/inventario/sumar")
async def inventario_sumar(request: Request, usuario=Depends(obtener_usuario_actual)):
    """Agrupa TODOS los escaneos pendientes de la tienda (de cualquier
    operario) y los suma a 'Contado Real'. El candado EXCLUSIVO de transacción
    (pg_advisory_xact_lock) hace dos cosas: evita que dos 'Sumar' de la misma
    tienda cuenten el mismo escaneo dos veces, Y espera a que terminen los
    escaneos en curso (que piden el candado COMPARTIDO en /escanear) antes de
    agrupar — así un escaneo que entra justo en el instante del 'Sumar' nunca
    queda afuera del conteo. Se libera solo al terminar la transacción, sin
    necesidad del candado manual del script original."""
    from config.db_manager import RealDictCursor
    body = await request.json()
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, body.get("tienda"), cursor)
        _inventario_verificar_activo(tienda_id, cursor)
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (tienda_id,))

        cursor.execute("SELECT codigo, COUNT(*) AS cantidad FROM inventario_escaneos WHERE tienda_id = %s GROUP BY codigo", (tienda_id,))
        conteos = {f["codigo"]: f["cantidad"] for f in cursor.fetchall()}
        if not conteos:
            return {"status": "success", "sin_novedad": True, "mensaje": "No hay escaneos nuevos para sumar todavía."}
        total_escaneos = sum(conteos.values())

        cursor.execute("SELECT id, producto_ref, barcode FROM inventario_productos WHERE tienda_id = %s", (tienda_id,))
        productos = cursor.fetchall()
        codigos_usados = set()
        actualizados = 0
        for p in productos:
            cantidad = None
            if p["producto_ref"] in conteos:
                cantidad = conteos[p["producto_ref"]]
                codigos_usados.add(p["producto_ref"])
            if p["barcode"] in conteos:
                cantidad = conteos[p["barcode"]]
                codigos_usados.add(p["barcode"])
            if cantidad is not None:
                cursor.execute("UPDATE inventario_productos SET contado_real = contado_real + %s WHERE id = %s", (cantidad, p["id"]))
                actualizados += 1

        # Un código que no está en la lista de ESTA ubicación no significa que
        # el producto no exista en Odoo — puede ser un producto real que Odoo
        # tiene registrado en OTRA tienda/ubicación (por eso el quant de acá no
        # lo trajo). Antes de darlo por "no reconocido" se busca en Odoo por
        # barcode: si existe, se suma como un producto más de esta tienda (con
        # stock_sistema=0, porque acá nunca tuvo quant) — así entra al informe
        # como "De más" en vez de perderse, y sí se manda en el ajuste a Odoo.
        # Solo se guarda como "no reconocido" el código que Odoo tampoco conoce.
        codigos_nuevos = [c for c in conteos if c not in codigos_usados]
        encontrados_en_odoo = {}
        if codigos_nuevos:
            try:
                uid, models, tz_odoo = _chat_conectar_odoo()
                productos_odoo = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
                    [[['barcode', 'in', codigos_nuevos]]],
                    {'fields': ['id', 'name', 'display_name', 'barcode']}
                )
                for p in productos_odoo or []:
                    encontrados_en_odoo[p['barcode']] = p
            except Exception:
                pass  # si Odoo no responde acá, se degrada a "no reconocido" sin frenar el Sumar

        agregados_de_otra_ubicacion = 0
        no_reconocidos = 0
        for c in codigos_nuevos:
            cantidad = conteos[c]
            p = encontrados_en_odoo.get(c)
            if p:
                nombre_limpio = _inventario_limpiar_nombre(p.get('name') or p.get('display_name'), c)
                cursor.execute("""
                    INSERT INTO inventario_productos (tienda_id, producto_ref, nombre, barcode, stock_sistema, contado_real)
                    VALUES (%s, %s, %s, %s, 0, %s)
                    ON CONFLICT (tienda_id, producto_ref) DO UPDATE SET
                        contado_real = inventario_productos.contado_real + EXCLUDED.contado_real
                """, (tienda_id, str(p['id']), nombre_limpio, c, cantidad))
                agregados_de_otra_ubicacion += 1
            else:
                cursor.execute("""
                    INSERT INTO inventario_productos_nuevos (tienda_id, barcode, cantidad, actualizado_en)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (tienda_id, barcode) DO UPDATE SET
                        cantidad = inventario_productos_nuevos.cantidad + EXCLUDED.cantidad, actualizado_en = NOW()
                """, (tienda_id, c, cantidad))
                no_reconocidos += 1

        _inventario_archivar_y_limpiar_escaneos(cursor, tienda_id)
        conexion.commit()
        return {
            "status": "success", "total_escaneos": total_escaneos, "codigos_unicos": len(conteos),
            "actualizados": actualizados, "de_otra_ubicacion": agregados_de_otra_ubicacion, "no_reconocidos": no_reconocidos,
            "mensaje": (
                f"Se procesaron {total_escaneos} escaneo(s) ({len(conteos)} código(s) único(s)): {actualizados} actualizados, "
                f"{agregados_de_otra_ubicacion} de otra ubicación de Odoo, {no_reconocidos} no reconocidos."
            ),
        }
    finally:
        cursor.close()
        conexion.close()


@app.post("/api/inventario/deshacer")
async def inventario_deshacer(request: Request, usuario=Depends(obtener_usuario_actual)):
    from config.db_manager import RealDictCursor
    body = await request.json()
    sesion_id = (body.get("sesion_id") or "").strip()
    if not sesion_id:
        raise HTTPException(status_code=400, detail="Falta la sesión.")

    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, body.get("tienda"), cursor)
        _inventario_verificar_activo(tienda_id, cursor)
        cursor.execute("""
            SELECT id, codigo FROM inventario_escaneos
            WHERE tienda_id = %s AND sesion_id = %s ORDER BY creado_en DESC, id DESC LIMIT 1
        """, (tienda_id, sesion_id))
        fila = cursor.fetchone()
        if not fila:
            raise HTTPException(status_code=404, detail="No hay ningún escaneo tuyo para deshacer.")
        cursor.execute("DELETE FROM inventario_escaneos WHERE id = %s", (fila["id"],))
        conexion.commit()
        return {"status": "success", "codigo": fila["codigo"]}
    except HTTPException:
        raise
    finally:
        cursor.close()
        conexion.close()


@app.get("/api/inventario/historial-sesion")
def inventario_historial_sesion(sesion_id: str, tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, tienda, cursor)
        _inventario_verificar_activo(tienda_id, cursor)
        cursor.execute("""
            SELECT codigo, ubicacion, creado_en FROM inventario_escaneos
            WHERE tienda_id = %s AND sesion_id = %s ORDER BY creado_en DESC LIMIT 15
        """, (tienda_id, sesion_id))
        return [{"codigo": r["codigo"], "ubicacion": r["ubicacion"], "hora": r["creado_en"].strftime("%H:%M:%S")} for r in cursor.fetchall()]
    finally:
        cursor.close()
        conexion.close()


@app.get("/api/inventario/historial-dia")
def inventario_historial_dia(tienda: str = None, ubicacion: str = None, usuario=Depends(obtener_usuario_actual)):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, tienda, cursor)
        _inventario_verificar_activo(tienda_id, cursor)
        condiciones = ["tienda_id = %s", "archivado_en::date = CURRENT_DATE"]
        parametros = [tienda_id]
        if ubicacion:
            condiciones.append("ubicacion = %s")
            parametros.append(ubicacion)
        cursor.execute(f"""
            SELECT codigo, COUNT(*) AS cantidad, COUNT(DISTINCT sesion_id) AS operarios
            FROM inventario_escaneos_archivo WHERE {' AND '.join(condiciones)}
            GROUP BY codigo ORDER BY cantidad DESC
        """, parametros)
        filas = cursor.fetchall()
        return {
            "total_escaneos": sum(f["cantidad"] for f in filas),
            "codigos": [{"codigo": f["codigo"], "cantidad": f["cantidad"], "operarios": f["operarios"]} for f in filas],
        }
    finally:
        cursor.close()
        conexion.close()


@app.get("/api/inventario/operarios-activos")
def inventario_operarios_activos(tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, tienda, cursor)
        _inventario_verificar_activo(tienda_id, cursor)
        cursor.execute("SELECT COUNT(DISTINCT sesion_id) AS n FROM inventario_escaneos WHERE tienda_id = %s", (tienda_id,))
        return {"activos": cursor.fetchone()["n"]}
    finally:
        cursor.close()
        conexion.close()


@app.get("/api/inventario/informe")
def inventario_informe(tienda: str = None, usuario=Depends(obtener_usuario_actual)):
    from config.db_manager import RealDictCursor
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(usuario, tienda, cursor)
        _inventario_verificar_activo(tienda_id, cursor)
        cursor.execute("""
            SELECT producto_ref, nombre, barcode, stock_sistema, contado_real
            FROM inventario_productos WHERE tienda_id = %s ORDER BY nombre
        """, (tienda_id,))
        faltantes, de_mas, correctos = [], [], []
        for f in cursor.fetchall():
            diferencia = f["contado_real"] - f["stock_sistema"]
            item = {
                "producto_ref": f["producto_ref"], "nombre": f["nombre"], "barcode": f["barcode"],
                "stock_sistema": f["stock_sistema"], "contado_real": f["contado_real"], "diferencia": diferencia,
            }
            if diferencia < 0:
                faltantes.append(item)
            elif diferencia > 0:
                de_mas.append(item)
            elif f["contado_real"] > 0:
                correctos.append(item)

        cursor.execute("SELECT barcode, cantidad, actualizado_en FROM inventario_productos_nuevos WHERE tienda_id = %s ORDER BY actualizado_en DESC", (tienda_id,))
        nuevos = [{"barcode": r["barcode"], "cantidad": r["cantidad"], "actualizado_en": r["actualizado_en"].strftime("%Y-%m-%d %H:%M")} for r in cursor.fetchall()]

        if not faltantes and not de_mas and not nuevos and not correctos:
            return {"status": "success", "sin_datos": True, "faltantes": [], "de_mas": [], "nuevos": [], "correctos": []}
        return {"status": "success", "sin_datos": False, "faltantes": faltantes, "de_mas": de_mas, "nuevos": nuevos, "correctos": correctos}
    finally:
        cursor.close()
        conexion.close()


def _inventario_crear_ajuste_odoo(uid, models, location_id, termino, lineas):
    """Núcleo que arma el ajuste en Odoo — crea/actualiza los stock.quant y el
    stock.inventory correspondiente, en estado 'En progreso' (nunca se aplica
    solo: queda listo para que alguien lo revise y confirme en el propio Odoo).
    TODO se procesa en lotes de 500 — con miles de variantes, una sola llamada
    gigante agota el tiempo de ejecución/límite de la propia llamada a Odoo."""
    product_ids = list({l["product_id"] for l in lineas})
    quants_existentes = []
    for lote in _inventario_en_lotes(product_ids):
        resultado = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [[['product_id', 'in', lote], ['location_id', '=', location_id]]],
            {'fields': ['id', 'product_id']}
        )
        quants_existentes.extend(resultado or [])
    mapa_producto_quant = {q['product_id'][0]: q['id'] for q in quants_existentes}

    lineas_sin_quant = [l for l in lineas if l["product_id"] not in mapa_producto_quant]
    if lineas_sin_quant:
        for lote in _inventario_en_lotes(lineas_sin_quant):
            vals_crear = [{"product_id": l["product_id"], "location_id": location_id, "inventory_quantity": l["contado"]} for l in lote]
            nuevos_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'create', [vals_crear])
            lista_nuevos_ids = nuevos_ids if isinstance(nuevos_ids, list) else [nuevos_ids]
            for l, qid in zip(lote, lista_nuevos_ids):
                mapa_producto_quant[l["product_id"]] = qid

        # Al crear con inventory_quantity directo, Odoo no siempre marca
        # internamente "inventory_quantity_set" — un write adicional lo fuerza.
        grupos_nuevos = {}
        for l in lineas_sin_quant:
            grupos_nuevos.setdefault(l["contado"], []).append(mapa_producto_quant[l["product_id"]])
        for cantidad, ids in grupos_nuevos.items():
            for lote in _inventario_en_lotes(ids):
                models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'write', [lote, {"inventory_quantity": cantidad}])

    ids_sin_quant = {l["product_id"] for l in lineas_sin_quant}
    lineas_con_quant_previo = [l for l in lineas if l["product_id"] not in ids_sin_quant]
    grupos = {}
    for l in lineas_con_quant_previo:
        grupos.setdefault(l["contado"], []).append(mapa_producto_quant[l["product_id"]])
    for cantidad, ids in grupos.items():
        for lote in _inventario_en_lotes(ids):
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'write', [lote, {"inventory_quantity": cantidad}])

    todos_los_product_ids = list({l["product_id"] for l in lineas})
    fecha_texto = datetime.now().strftime("%d/%m/%Y %H:%M")
    nombre_ajuste = f"Ajuste {termino} {fecha_texto}"

    inventory_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.inventory', 'create', [{
        "name": nombre_ajuste,
        "location_ids": [[6, 0, [location_id]]],
        "product_selection": "manual",
        "state": "in_progress",
    }])
    for lote in _inventario_en_lotes(todos_los_product_ids):
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.inventory', 'write',
            [[inventory_id], {"product_ids": [[4, pid] for pid in lote]}]
        )
    return inventory_id


@app.post("/api/inventario/enviar-ajuste-odoo")
async def inventario_enviar_ajuste_odoo(request: Request, admin=Depends(requerir_admin)):
    """Envía el conteo a Odoo como un Ajuste de Inventario 'En progreso' —
    reservado a admin/superadmin (reemplaza la clave de supervisor compartida
    del script original: acá ya no hace falta, el rol del que hace clic ya
    demuestra que tiene permiso)."""
    from config.db_manager import RealDictCursor
    body = await request.json()
    conexion = obtener_conexion()
    cursor = conexion.cursor(cursor_factory=RealDictCursor)
    try:
        tienda_id = _inventario_resolver_tienda_id(admin, body.get("tienda"), cursor)
        _inventario_verificar_activo(tienda_id, cursor)
        cursor.execute("SELECT ubicacion FROM inventario_ubicacion_activa WHERE tienda_id = %s", (tienda_id,))
        fila_ubi = cursor.fetchone()
        if not fila_ubi:
            raise HTTPException(status_code=400, detail="No hay ninguna ubicación cargada para esta tienda.")
        ubicacion = fila_ubi["ubicacion"]

        # SIN filtro de contado > 0: se manda TODA la ubicación, incluidos los
        # productos que nadie escaneó (contado_real sigue en 0). Si se excluían,
        # una prenda que no se encontró físicamente nunca se corregía en Odoo
        # — se quedaba con el stock viejo para siempre en vez de quedar en 0.
        cursor.execute("SELECT producto_ref, contado_real FROM inventario_productos WHERE tienda_id = %s", (tienda_id,))
        lineas = [{"product_id": int(r["producto_ref"]), "contado": r["contado_real"]} for r in cursor.fetchall()]
        if not lineas:
            raise HTTPException(status_code=400, detail="No hay cantidades contadas para enviar.")

        uid, models, tz_odoo = _chat_conectar_odoo()
        location_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search',
            [['|', ['complete_name', 'ilike', ubicacion], ['barcode', '=', ubicacion]]],
            {'limit': 1}
        )
        if not location_ids:
            raise HTTPException(status_code=404, detail=f"No se encontró la ubicación '{ubicacion}' en Odoo.")

        inventory_id = _inventario_crear_ajuste_odoo(uid, models, location_ids[0], ubicacion, lineas)

        _registrar_auditoria(cursor, admin, "enviar_ajuste_inventario_odoo", f"{ubicacion}: {len(lineas)} producto(s), inventory_id={inventory_id}")
        conexion.commit()
        return {"status": "success", "inventory_id": inventory_id, "productos_enviados": len(lineas)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error enviando el ajuste a Odoo: {str(e)}")
    finally:
        cursor.close()
        conexion.close()



# ─── BLOQUE DE ARRANQUE NORMAL DE UVICORN ───
if __name__ == "__main__":
    import uvicorn
    # Eliminamos el bloque antiguo de creación manual porque ya lo maneja de forma impecable tu db_manager.py
    print("Iniciando el servidor de Cultura Tejida conectado a PostgreSQL 17...")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
