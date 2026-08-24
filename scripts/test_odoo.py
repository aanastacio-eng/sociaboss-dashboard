import xmlrpc.client

# ==========================================
# CONFIGURA AQUÍ TUS CREDENCIALES DE ODOO
# ==========================================
ODOO_URL = "https://culturatejida.odoo.com/"  # Asegúrate de incluir el https://
ODOO_DB = "workcosta-18-0-22926443"              # Nombre de la base de datos de Odoo
ODOO_USER = "jgonzalez@culturatejida.com"          # Tu correo de acceso a Odoo
ODOO_PASSWORD = "9404c28862464129392b3ee6ecbd4d9872e6d97e"   # Se recomienda usar un API Key generado en tu perfil de Odoo

TARGET_COMPANY_ID = 2 

def test_odoo_connection():
    print("Iniciando conexión con Odoo...")
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        
        if uid:
            print(f"¡Conexión exitosa! Tu UID de usuario en Odoo es: {uid}")
            models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
            
            # CONSULTAR VENTAS FILTRANDO DIRECTAMENTE POR EL ID DE LA EMPRESA
            print(f"Consultando las últimas 5 ventas exclusivas de la empresa ID: {TARGET_COMPANY_ID}...")
            sales = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'sale.order', 'search_read',
                [[['company_id', '=', TARGET_COMPANY_ID]]],  # FILTRO DIRECTO POR ID
                {
                    'fields': ['id', 'name', 'partner_id', 'amount_total', 'state', 'company_id'], 
                    'limit': 5
                }
            )
            
            print("\n--- VENTAS FILTRADAS ENCONTRADAS ---")
            if not sales:
                print(f"No se encontraron órdenes de venta para la empresa con ID {TARGET_COMPANY_ID}.")
            for sale in sales:
                cliente = sale['partner_id'][1] if sale['partner_id'] else "Sin Cliente"
                empresa_nombre = sale['company_id'][1] if sale['company_id'] else "Desconocida"
                print(f"- Pedido: {sale['name']} | Cliente: {cliente} | Total: {sale['amount_total']} | Empresa: {empresa_nombre} (ID Odoo: {sale['id']})")
        else:
            print("❌ Error de autenticación.")
            
    except Exception as e:
        print(f"❌ Ocurrió un error en la conexión: {e}")

if __name__ == "__main__":
    test_odoo_connection()