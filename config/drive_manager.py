import os
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Configuración fija que ya validamos
CREDENTIALS_FILE = "config/credentials.json"
GOOGLE_DRIVE_FOLDER_ID = "1ZO7qyyj1iwYWMbrn3DoiyLj92E1pYANW"

def obtener_servicio_drive():
    """Inicializa y devuelve el servicio de Google Drive API."""
    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(f"No se encontró el archivo de credenciales en {CREDENTIALS_FILE}")
        
    scopes = ['https://www.googleapis.com/auth/drive']
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    return build('drive', 'v3', credentials=creds)

def subir_archivo_a_drive(ruta_archivo_local, nombre_destino, mime_type='text/plain'):
    """
    Sube cualquier archivo local a la carpeta configurada en la Unidad Compartida.
    Devuelve el ID del archivo en Google Drive si la subida fue exitosa.
    """
    if not os.path.exists(ruta_archivo_local):
        print(f"❌ Error: El archivo local {ruta_archivo_local} no existe.")
        return None

    try:
        service = obtener_servicio_drive()
        
        file_metadata = {
            'name': nombre_destino,
            'parents': [GOOGLE_DRIVE_FOLDER_ID]
        }
        
        media = MediaFileUpload(ruta_archivo_local, mimetype=mime_type, resumable=True)
        
        # Subida compatible con la unidad compartida de Workspace
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id',
            supportsAllDrives=True
        ).execute()
        
        print(f"✅ Archivo '{nombre_destino}' subido con éxito. ID: {file.get('id')}")
        return file.get('id')
        
    except Exception as e:
        print(f"❌ Error al subir a Google Drive: {e}")
        return None