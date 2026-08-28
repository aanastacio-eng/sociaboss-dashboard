"""
Crea (o resetea la contraseña de) un usuario superadmin en la base de datos.

Uso:
    python scripts/crear_admin.py "Nombre Apellido" correo@ejemplo.com "contraseña"

Usa la misma conexión que el resto de la app (config/db_manager.py), así que
respeta DATABASE_URL / POSTGRES_* del .env local o del entorno donde corra
(por ejemplo, exportando las variables de Neon antes de correrlo).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt
from config.db_manager import obtener_conexion


def crear_o_actualizar_admin(nombre, email, password):
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    email = email.strip().lower()

    conexion = obtener_conexion()
    cursor = conexion.cursor()
    try:
        cursor.execute("SELECT id FROM usuarios WHERE email = %s", (email,))
        existente = cursor.fetchone()

        if existente:
            cursor.execute(
                "UPDATE usuarios SET nombre = %s, password_hash = %s, rol = 'superadmin', activo = TRUE, "
                "intentos_fallidos = 0, bloqueado_hasta = NULL WHERE id = %s",
                (nombre, password_hash, existente[0]),
            )
            conexion.commit()
            print(f"Usuario existente actualizado a superadmin: {email} (id={existente[0]})")
        else:
            cursor.execute(
                "INSERT INTO usuarios (nombre, email, password_hash, rol, activo) "
                "VALUES (%s, %s, %s, 'superadmin', TRUE) RETURNING id",
                (nombre, email, password_hash),
            )
            nuevo_id = cursor.fetchone()[0]
            conexion.commit()
            print(f"Superadmin creado: {email} (id={nuevo_id})")
    finally:
        cursor.close()
        conexion.close()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print('Uso: python scripts/crear_admin.py "Nombre Apellido" correo@ejemplo.com "contraseña"')
        sys.exit(1)

    _, nombre, email, password = sys.argv
    crear_o_actualizar_admin(nombre, email, password)
