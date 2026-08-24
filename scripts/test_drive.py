import os
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Configuración de rutas e IDs
CREDENTIALS_FILE = "config/credentials.json"
GOOGLE_DRIVE_FOLDER_ID = "1ZO7qyyj1iwYWMbrn3DoiyLj92E1pYANW"
def test_drive_upload():
    print("Iniciando conexión con Google Drive API...")
    
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"❌ Error: No se encontró el archivo '{CREDENTIALS_FILE}'.")
        return
        
    try:
        scopes = ['https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        service = build('drive', 'v3', credentials=creds)
        
        temp_filename = "prueba_dashboard.txt"
        with open(temp_filename, "w") as f:
            f.write("Prueba desde el Backend de Sociaboss.")
            
        print("Intentando subir el archivo...")
        file_metadata = {
            'name': 'Prueba_Subida_Dashboard.txt',
            'parents': [GOOGLE_DRIVE_FOLDER_ID]
        }
        
        media = MediaFileUpload(temp_filename, mimetype='text/plain', resumable=True)
        
        # Agregamos 'supportsAllDrives=True' para que permita subir a carpetas compartidas
        file = service.files().create(
            body=file_metadata, 
            media_body=media, 
            fields='id',
            supportsAllDrives=True  
        ).execute()
        
        print(f"¡SUBIDA EXITOSA! ID del archivo en Drive: {file.get('id')}")
        
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
            
    except Exception as e:
        print(f"❌ Error durante la subida: {e}")

if __name__ == "__main__":
    test_drive_upload()