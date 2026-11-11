{
    'name': 'FlagShip Carrier Integration',
    'version': '20.0.1.0.0',
    'category': 'Inventory',
    'summary': 'Integrate FlagShip shipping API with Odoo',
    'description': 'Allows calculating and using shipping rates from 3rd party carriers.',
    'author': 'Your Name',
    'depends': ['delivery'],
    'data': [
        'views/delivery_flagship_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}