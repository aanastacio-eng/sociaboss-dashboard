import os
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Configuración fija que ya validamos
CREDENTIALS_FILE = "config/credentials.json"
CREDENTIALS_ENV_VAR = "GOOGLE_CREDENTIALS_JSON"
GOOGLE_DRIVE_FOLDER_ID = "1ZO7qyyj1iwYWMbrn3DoiyLj92E1pYANW"

def obtener_servicio_drive():
    """Inicializa y devuelve el servicio de Google Drive API.

    En Vercel (y cualquier entorno serverless) el sistema de archivos es
    de solo lectura, así que no se puede depender de config/credentials.json.
    Por eso primero se busca la variable de entorno GOOGLE_CREDENTIALS_JSON
    (con el contenido completo del service account key). Si no existe, se
    cae al archivo local — así el flujo de desarrollo local no cambia.
    """
    scopes = ['https://www.googleapis.com/auth/drive']
    credenciales_env = os.environ.get(CREDENTIALS_ENV_VAR)

    if credenciales_env:
        try:
            info = json.loads(credenciales_env)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"La variable de entorno {CREDENTIALS_ENV_VAR} no contiene un JSON válido: {e}"
            )
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    elif os.path.exists(CREDENTIALS_FILE):
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    else:
        raise FileNotFoundError(
            f"No se encontraron credenciales de Google: falta la variable de entorno "
            f"{CREDENTIALS_ENV_VAR} y tampoco existe el archivo {CREDENTIALS_FILE}"
        )

    return build('drive', 'v3', credentials=creds)

def _obtener_o_crear_carpeta(service, nombre, carpeta_padre_id):
    """Busca una subcarpeta por nombre dentro de carpeta_padre_id; si no
    existe, la crea. Devuelve su ID. Las comillas simples del nombre se
    escapan para la query de Drive (ej. una tienda con apóstrofe en el
    nombre no rompería la búsqueda)."""
    nombre_escapado = nombre.replace("'", "\\'")
    query = (
        f"name = '{nombre_escapado}' and '{carpeta_padre_id}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    resultado = service.files().list(
        q=query, fields="files(id, name)",
        supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="allDrives"
    ).execute()
    existentes = resultado.get('files', [])
    if existentes:
        return existentes[0]['id']

    carpeta = service.files().create(
        body={'name': nombre, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [carpeta_padre_id]},
        fields='id', supportsAllDrives=True
    ).execute()
    return carpeta.get('id')


def obtener_carpeta_destino(anio, tienda, subcarpeta):
    """Devuelve el ID de la carpeta Año/Tienda/Subcarpeta (ej. "2026/Lemaler
    Dorado/Cierres") dentro de la Unidad Compartida, creando en el momento
    cualquier nivel que todavía no exista. Así Drive queda organizado en vez
    de tener todos los archivos sueltos en una sola carpeta plana."""
    service = obtener_servicio_drive()
    id_anio = _obtener_o_crear_carpeta(service, str(anio), GOOGLE_DRIVE_FOLDER_ID)
    id_tienda = _obtener_o_crear_carpeta(service, tienda, id_anio)
    return _obtener_o_crear_carpeta(service, subcarpeta, id_tienda)


def subir_archivo_a_drive(ruta_archivo_local, nombre_destino, mime_type='text/plain', carpeta_id=None):
    """
    Sube cualquier archivo local a Google Drive. Si se pasa carpeta_id (ver
    obtener_carpeta_destino), sube ahí; si no, cae en la carpeta raíz de
    siempre — así los llamados que todavía no organizan por año/tienda
    (ej. evidencia de tareas) siguen funcionando exactamente igual.
    Devuelve el ID del archivo en Google Drive si la subida fue exitosa.
    """
    if not os.path.exists(ruta_archivo_local):
        print(f"❌ Error: El archivo local {ruta_archivo_local} no existe.")
        return None

    try:
        service = obtener_servicio_drive()

        file_metadata = {
            'name': nombre_destino,
            'parents': [carpeta_id or GOOGLE_DRIVE_FOLDER_ID]
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
