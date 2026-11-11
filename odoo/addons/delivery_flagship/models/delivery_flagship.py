import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class DeliveryFlagship(models.Model):
    _inherit = 'delivery.carrier'

    delivery_type = fields.Selection(
        selection_add=[('flagship', 'FlagShip')],
        ondelete={'flagship': 'set default'}
    )

    # FlagShip-specific credentials
    flagship_api_key = fields.Char(
        string='FlagShip API Key',
        help='Your FlagShip API key'
    )
    flagship_account_id = fields.Char(
        string='FlagShip Account ID',
        help='Your FlagShip account ID'
    )
    
    flagship_allowed_courier = fields.Selection([
        ('Purolator', 'Purolator'),
        ('FedEx', 'FedEx'),
        ('UPS', 'UPS'),
        ('DHL', 'DHL')
    ], string='Courier Filter', help="If set, only rates from this specific courier will be returned by this shipping method.")

    # FlagShip integration status
    has_flagship_credentials = fields.Boolean(
        compute='_compute_has_flagship_credentials',
        string='Has FlagShip Credentials',
        help='Indicates if API credentials are configured.'
    )

    @api.depends('flagship_api_key', 'flagship_account_id')
    def _compute_has_flagship_credentials(self):
        for carrier in self:
            carrier.has_flagship_credentials = bool(
                carrier.flagship_api_key and carrier.flagship_account_id
            )

    def flagship_rate_shipment(self, order):
        """
        Calculate shipping rate from FlagShip API

        Returns dict with structure expected by Odoo:
        {'success': bool, 'price': float, 'error_message': str, 'warning_message': str}
        """
        self.ensure_one()

        if not self.has_flagship_credentials:
            return {
                'success': False,
                'price': 0.0,
                'error_message': _('FlagShip API credentials not configured'),
                'warning_message': ''
            }

        try:
            # Build the packages list dynamically from the order lines
            api_packages = []
            for line in order.order_line:
                if line.product_id.type != 'service':
                    product = line.product_id
                    
                    p_length = getattr(product, 'length', 30) or 30
                    p_width = getattr(product, 'width', 30) or 30
                    p_height = getattr(product, 'height', 30) or 30
                    p_weight = product.weight or 1.0
                    
                    for _ in range(int(line.product_qty)):
                        api_packages.append({
                            'length': p_length,
                            'width': p_width,
                            'height': p_height,
                            'weight': p_weight,
                            'description': product.name or 'Goods',
                        })
            
            if not api_packages:
                api_packages = [{
                    'length': 30, 'width': 30, 'height': 30, 
                    'weight': 1.0, 'description': 'Goods'
                }]

            warehouse = order.warehouse_id
            origin = warehouse.partner_id

            payload = {
                'from': {
                    'name': origin.name or order.company_id.name or 'Company',
                    'attn': 'Shipping Dept',
                    'address': origin.street or '123 Warehouse St',
                    'city': origin.city or 'Toronto',
                    'state': origin.state_id.code if origin.state_id else 'ON',
                    'postal_code': origin.zip or 'M1M1M1',
                    'country': origin.country_id.code if origin.country_id else 'CA',
                    'phone': origin.phone or '1234567890',
                    'is_commercial': True
                },
                'to': {
                    'name': order.partner_shipping_id.name or 'Customer',
                    'attn': order.partner_shipping_id.name or 'Customer',
                    'address': order.partner_shipping_id.street or '123 Customer St',
                    'city': order.partner_shipping_id.city or 'Toronto',
                    'state': order.partner_shipping_id.state_id.code if order.partner_shipping_id.state_id else 'ON',
                    'postal_code': order.partner_shipping_id.zip or 'A1B2C3',
                    'country': order.partner_shipping_id.country_id.code if order.partner_shipping_id.country_id else 'CA',
                    'phone': order.partner_shipping_id.phone or '1234567890',
                    'is_commercial': False
                },
                'packages': {
                    'type': 'package',
                    'units': 'metric', 
                    'content': 'goods',
                    'items': api_packages
                },
                'payment': {
                    'payer': 'F'
                }
            }

            headers = {
                'X-Smartship-Token': self.flagship_api_key,
                'Content-Type': 'application/json'
            }
            response = requests.post(
                'https://api.smartship.io/ship/rates', 
                json=payload,
                headers=headers
            )

            # SmartShip returns 200 for full success, 206 for partial success (some couriers failed)
            if response.status_code not in (200, 206):
                return {
                    'success': False,
                    'price': 0.0,
                    'error_message': f'FlagShip API error: {response.status_code} - {response.text}',
                    'warning_message': ''
                }

            response_json = response.json()
            rates_list = response_json.get('content', [])
            
            # Filter the rates by the selected courier if a filter is configured on this carrier
            if self.flagship_allowed_courier:
                rates_list = [
                    r for r in rates_list 
                    if r.get('service', {}).get('courier_name', '') == self.flagship_allowed_courier
                ]
            
            if not rates_list:
                return {
                    'success': False,
                    'price': 0.0,
                    'error_message': _('No applicable rates returned for this provider/destination.'),
                    'warning_message': ''
                }

            best_rate = min(rates_list, key=lambda r: float(r.get('price', {}).get('total', float('inf'))))

            price = float(best_rate.get('price', {}).get('total', 0.0))

            margin = self.margin or 0.0
            final_price = price * (1 + margin / 100)

            return {
                'success': True,
                'price': final_price,
                'error_message': '',
                'warning_message': f'FlagShip rate retrieved for {len(rates_list)} service options'
            }

        except Exception as e:
            return {
                'success': False,
                'price': 0.0,
                'error_message': f'Not available',
                # 'error_message': f'Error calling FlagShip API: {str(e)}',
                'warning_message': ''
            }

    def flagship_send_shipping(self, pickings):
        """
        Create shipment/label in FlagShip and return tracking number
        """
        self.ensure_one()

        if not self.has_flagship_credentials:
            raise UserError(_('FlagShip API credentials not configured'))

        try:
            tracking_number = False
            for picking in pickings:
                origin = picking.picking_type_id.warehouse_id.partner_id
                dest = picking.partner_id
                
                payload = {
                    'from': {
                        'name': origin.name or picking.company_id.name or 'Company',
                        'attn': 'Shipping Dept',
                        'address': origin.street or '123 Warehouse St',
                        'city': origin.city or 'Toronto',
                        'state': origin.state_id.code if origin.state_id else 'ON',
                        'postal_code': origin.zip or 'M1M1M1',
                        'country': origin.country_id.code if origin.country_id else 'CA',
                        'phone': origin.phone or '1234567890',
                        'is_commercial': True
                    },
                    'to': {
                        'name': dest.name or 'Customer',
                        'attn': dest.name or 'Customer',
                        'address': dest.street or '123 Customer St',
                        'city': dest.city or 'Toronto',
                        'state': dest.state_id.code if dest.state_id else 'ON',
                        'postal_code': dest.zip or 'A1B2C3',
                        'country': dest.country_id.code if dest.country_id else 'CA',
                        'phone': dest.phone or '1234567890',
                        'is_commercial': False
                    },
                    'packages': {
                        'type': 'package',
                        'units': 'metric',
                        'content': 'goods',
                        'items': [
                            {
                                'length': 30,
                                'width': 30,
                                'height': 30,
                                'weight': picking.shipping_weight or 1.0,
                                'description': 'Goods'
                            }
                        ]
                    },
                    'payment': {
                        'payer': 'F'
                    },
                    'service': {
                        # Map this dynamically in the future based on the exact rate chosen
                        'courier_name': self.flagship_allowed_courier.lower() if self.flagship_allowed_courier else 'fedex',
                        'courier_code': 'STANDARD_OVERNIGHT' 
                    }
                }

                headers = {
                    'X-Smartship-Token': self.flagship_api_key,
                    'Content-Type': 'application/json'
                }

                response = requests.post(
                    'https://api.smartship.io/ship/confirm',
                    json=payload,
                    headers = headers
                )

                if response.status_code != 200:
                    raise UserError(f'FlagShip shipment creation failed: {response.text}')

                shipment_data = response.json()
                content = shipment_data.get('content', {})
                tracking_number = content.get('tracking_number')

                tracking_url = self.tracking_url.replace('<shipmenttrackingnumber>', str(tracking_number)) if self.tracking_url and tracking_number else False
                picking.update({
                    'tracking_reference': tracking_number,
                    'carrier_tracking_url': tracking_url
                })

            return {
                'success': True,
                'tracking_number': tracking_number,
                'message': _('Shipment created successfully in FlagShip')
            }

        except Exception as e:
            raise UserError(f'Error creating shipment in FlagShip: {str(e)}')

    def _get_tracking_link(self, shipment):
        """Retrieve the tracking link for the shipment"""
        if not self.tracking_url:
            return False
        return self.tracking_url.replace('<shipmenttrackingnumber>', shipment.tracking_reference)

    def flagship_cancel_shipment(self, picking):
        """Cancel a shipment in FlagShip"""
        self.ensure_one()

        if not self.has_flagship_credentials:
            raise UserError(_('FlagShip API credentials not configured'))

        try:
            shipment_id = picking.tracking_reference
            if not shipment_id:
                raise UserError(_('No tracking reference found to cancel.'))
                
            headers = {
                'X-Smartship-Token': self.flagship_api_key
            }
            response = requests.post(
                f'https://api.smartship.io/ship/cancel/{shipment_id}',
                headers=headers
            )

            if response.status_code != 200:
                raise UserError(f'FlagShip cancellation failed: {response.text}')

            return {
                'success': True,
                'message': _('Shipment cancelled successfully in FlagShip')
            }

        except Exception as e:
            raise UserError(f'Error cancelling shipment in FlagShip: {str(e)}')