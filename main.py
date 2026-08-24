import os
from config.drive_manager import subir_archivo_a_drive

def procesar_y_respaldar_ventas(ruta_local_csv, nombre_tienda):
    """
    Simula el procesamiento de un reporte de ventas de una tienda
    y lo sube automáticamente a la Unidad Compartida de Google Drive.
    """
    print(f"\n🚀 [Backend] Iniciando procesamiento para: {nombre_tienda}")
    
    # 1. Validar que el archivo local exista
    if not os.path.exists(ruta_local_csv):
        print(f"❌ Error: No se encontró el archivo local en '{ruta_local_csv}'")
        return
    
    # 2. Aquí es donde en el futuro puedes meter lógica para limpiar datos,
    # calcular totales, o guardar en una base de datos local.
    print(f"📦 Leyendo datos de {ruta_local_csv}...")
    
    # 3. Definir el nombre con el que se guardará en Google Drive (ej: Ventas_TiendaA_2026.csv)
    nombre_destino_drive = f"Backup_{nombre_tienda}_2026.csv"
    
    # 4. Subir a la carpeta compartida usando nuestro manager
    print(f"☁️ Subiendo respaldo a Google Drive como '{nombre_destino_drive}'...")
    id_drive = subir_archivo_a_drive(
        ruta_archivo_local=ruta_local_csv,
        nombre_destino=nombre_destino_drive,
        mime_type="text/csv"
    )
    
    if id_drive:
        print(f"✨ [PROCESO EXITOSO] Tienda '{nombre_tienda}' respaldada con ID: {id_drive}")
    else:
        print(f"⚠️ [ALERTA] No se pudo completar el respaldo en la nube.")

if __name__ == "__main__":
    print("=== BACKEND SOCIABOSS GENERATOR ===")
    
    # --- PRUEBA REAL EN VIVO ---
    # Vamos a crear un archivo CSV de prueba local rápido
    archivo_prueba = "ventas_temporal.csv"
    with open(archivo_prueba, "w", encoding="utf-8") as f:
        f.write("id_venta,tienda,total,fecha\n1,Tienda_Centro,150.00,2026-08-24\n2,Tienda_Norte,85.50,2026-08-24")
        
    # Ejecutamos la función usando el archivo temporal que acabamos de crear
    procesar_y_respaldar_ventas(archivo_prueba, "Ventas_Tiendas_Consolidado")
    
    # Limpiamos el archivo temporal local después de subirlo
    if os.path.exists(archivo_prueba):
        os.remove(archivo_prueba)