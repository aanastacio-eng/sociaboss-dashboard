r"""
Respaldo diario de la base de datos.

Genera un dump de Postgres, lo sube a la carpeta de Google Drive ya usada
para comprobantes/documentos (así queda una copia fuera de la PC-servidor,
no solo local), y borra del disco local los respaldos más viejos que
RETENCION_DIAS_LOCAL para no llenar el disco — la copia en Drive queda con
su propia retención, más larga, controlada aparte en Drive si hace falta.

Pensado para correr una vez al día vía el Programador de Tareas de Windows:
    schtasks /create /tn "SociaBoss - Backup DB" /tr "\"C:\SociaBoss\venv\Scripts\python.exe\" \"C:\SociaBoss\scripts\backup_db.py\"" /sc daily /st 03:00
"""
import sys
# Igual que main.py: sin esto, un print() con emoji revienta si corre sin
# consola UTF-8 (ej. disparado por el Programador de Tareas de Windows).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os
import subprocess
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

RETENCION_DIAS_LOCAL = 14
CARPETA_BACKUPS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")

# El instalador de PostgreSQL en Windows no siempre deja pg_dump en el PATH
# del sistema — buscamos la ruta típica si el comando simple no está.
def _ruta_pg_dump():
    for candidato in ("pg_dump", r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe", r"C:\Program Files\PostgreSQL\16\bin\pg_dump.exe"):
        try:
            subprocess.run([candidato, "--version"], capture_output=True, check=True)
            return candidato
        except Exception:
            continue
    raise RuntimeError("No se encontró pg_dump. Agregá su carpeta al PATH o ajustá _ruta_pg_dump() en este script.")


def hacer_backup():
    os.makedirs(CARPETA_BACKUPS, exist_ok=True)

    host = os.getenv("POSTGRES_HOST", "localhost")
    db = os.getenv("POSTGRES_DB", "sociaboss")
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "")
    port = os.getenv("POSTGRES_PORT", "5433")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    nombre_archivo = f"sociaboss_{timestamp}.sql"
    ruta_local = os.path.join(CARPETA_BACKUPS, nombre_archivo)

    print(f"📦 Generando respaldo de '{db}'...")
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    pg_dump = _ruta_pg_dump()
    resultado = subprocess.run(
        [pg_dump, "-h", host, "-p", port, "-U", user, "-d", db, "-f", ruta_local, "--no-owner", "--no-privileges"],
        env=env, capture_output=True, text=True
    )
    if resultado.returncode != 0:
        print(f"❌ pg_dump falló: {resultado.stderr}")
        sys.exit(1)

    tamano_kb = os.path.getsize(ruta_local) / 1024
    print(f"✅ Respaldo local: {ruta_local} ({tamano_kb:.0f} KB)")

    # Subida a Drive — no es crítico si falla (el respaldo local ya existe),
    # así que solo se avisa, no se corta la ejecución.
    try:
        from config.drive_manager import subir_archivo_a_drive
        drive_id = subir_archivo_a_drive(ruta_local, f"BACKUP_DB_{nombre_archivo}", "application/sql")
        if drive_id:
            print(f"☁️  Subido a Drive: {drive_id}")
        else:
            print("⚠️  No se pudo subir a Drive (revisá config/credentials.json).")
    except Exception as e:
        print(f"⚠️  No se pudo subir a Drive: {e}")

    # Limpieza de respaldos locales viejos.
    limite = datetime.now() - timedelta(days=RETENCION_DIAS_LOCAL)
    borrados = 0
    for archivo in os.listdir(CARPETA_BACKUPS):
        ruta = os.path.join(CARPETA_BACKUPS, archivo)
        if archivo.startswith("sociaboss_") and archivo.endswith(".sql"):
            if datetime.fromtimestamp(os.path.getmtime(ruta)) < limite:
                os.remove(ruta)
                borrados += 1
    if borrados:
        print(f"🧹 Se borraron {borrados} respaldo(s) local(es) de más de {RETENCION_DIAS_LOCAL} días.")

    print("Listo.")


if __name__ == "__main__":
    hacer_backup()
