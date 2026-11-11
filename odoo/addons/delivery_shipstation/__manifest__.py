{
    'name': 'ShipStation Carrier Integration',
    'version': '20.0.1.0.0',
    'category': 'Inventory',
    'summary': 'Integrate ShipStation shipping API with Odoo',
    'description': 'Allows calculating and using shipping rates from 3rd party carriers.',
    'author': 'Kokemoo',
    'depends': ['delivery', 'sale_stock', 'stock'],
    'data': [
        'views/delivery_shipstation_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}