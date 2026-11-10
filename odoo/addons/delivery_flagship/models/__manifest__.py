{
    'name': 'FlagShip Carrier Integration',
    'version': '19.0.1.0.0',
    'category': 'Inventory',
    'summary': 'Integrate FlagShip shipping API with Odoo',
    'depends': ['delivery'],
    'data': [
        'security/ir.model.access.csv',
        'views/delivery_flagship_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}