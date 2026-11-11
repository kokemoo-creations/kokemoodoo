# models/stock_picking.py
from odoo import models, fields


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    shipstation_shipment_id = fields.Char(
        string='ShipStation Shipment ID',
        copy=False,
        help='Needed for cancellation via ShipStation API.',
    )