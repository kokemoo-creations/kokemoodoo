# models/sale_order.py
from odoo import models, fields


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    shipstation_rate_id = fields.Char(
        string='ShipStation Rate ID',
        copy=False,
        help='Rate ID selected at checkout; used to buy the label so the '
             'customer is charged the same price.',
    )
    shipstation_shipment_id = fields.Char(
        string='ShipStation Shipment ID',
        copy=False,
    )