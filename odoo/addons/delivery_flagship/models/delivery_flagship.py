import requests
from odoo import models, fields, api, _
from odoo.exceptions import UserError

class DeliveryFlagship(models.Model):
    _inherit = 'delivery.carrier'

    # FlagShip-specific credentials
    flagship_api_key = fields.Char(string='FlagShip API Key', help='Your FlagShip API key')
    flagship_account_id = fields.Char(string='FlagShip Account ID', help='Your FlagShip account ID')
    
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
            # Calculate total weight and dimensions for the order
            total_weight = sum(
                line.product_id.weight * line.product_qty
                for line in order.order_line
            )
            
            # Get warehouse (shipping) address
            warehouse = self.env['stock.warehouse'].search(
                [('code', '=', order.company_id.default_warehouse.code)],
                limit=1
            )
            
            # Prepare API request payload
            # Adjust this based on FlagShip's actual API documentation
            payload = {
                'apiKey': self.flagship_api_key,
                'account': self.flagship_account_id,
                'from': {
                    'city': warehouse.city,
                    'state': warehouse.state_id.code if warehouse.state_id else '',
                    'zip': warehouse.zip,
                    'country': warehouse.country_id.code if warehouse.country_id else '',
                },
                'to': {
                    'city': order.partner_shipping_id.city,
                    'state': order.partner_shipping_id.state_id.code 
                        if order.partner_shipping_id.state_id else '',
                    'zip': order.partner_shipping_id.zip,
                    'country': order.partner_shipping_id.country_id.code 
                        if order.partner_shipping_id.country_id else '',
                },
                'packages': [
                    {
                        'length': order.partner_shipping_id.product_length or 30,  # default cm
                        'width': order.partner_shipping_id.product_width or 30,   # default cm
                        'height': order.partner_shipping_id.product_height or 30, # default cm
                        'weight': total_weight,
                        'service': 'standard',  # or 'express', 'overnight', etc.
                    }
                ]
            }

            # Make API call
            response = requests.post(
                'https://api.shippingapi.com/v1/rates',  # Replace with FlagShip endpoint
                json=payload,
                headers={'Content-Type': 'application/json'}
            )

            if response.status_code != 200:
                return {
                    'success': False,
                    'price': 0.0,
                    'error_message': f'FlagShip API error: {response.status_code} - {response.text}',
                    'warning_message': ''
                }

            # Parse response and calculate price
            rates_data = response.json()
            
            # Choose the best rate (lowest price for standard service)
            best_rate = min(
                rates_data.get('rates', []),
                key=lambda r: r.get('price', float('inf'))
            )
            
            price = float(best_rate.get('price', 0.0))
            
            # Apply margin (same as Odoo's built-in logic)
            margin = self.margin or 0.0
            final_price = price * (1 + margin / 100)

            return {
                'success': True,
                'price': final_price,
                'error_message': '',
                'warning_message': f'FlagShip rate retrieved for {len(rates_data.get("rates", []))} service options'
            }

        except Exception as e:
            return {
                'success': False,
                'price': 0.0,
                'error_message': f'Error calling FlagShip API: {str(e)}',
                'warning_message': ''
            }

    def flagship_send_shipping(self, move_ids):
        """
        Create shipment/label in FlagShip and return tracking number
        """
        self.ensure_one()
        
        if not self.has_flagship_credentials:
            raise UserError(_('FlagShip API credentials not configured'))

        try:
            # Get the delivery order associated with this carrier
            delivery_order = self.env['delivery.order'].search(
                [('carrier_id', '=', self.id)],
                order_by='id desc',
                limit=1
            )
            
            if not delivery_order:
                raise UserError(_('No delivery order found for this carrier'))

            # Prepare shipment API call
            payload = {
                'apiKey': self.flagship_api_key,
                'account': self.flagship_account_id,
                'shipment': {
                    'from': {
                        'city': delivery_order.warehouse.city,
                        'zip': delivery_order.warehouse.zip,
                        'country': delivery_order.warehouse.country_id.code,
                    },
                    'to': {
                        'city': delivery_order.partner.city,
                        'zip': delivery_order.partner.zip,
                        'country': delivery_order.partner.country_id.code,
                    },
                    'weight': delivery_order.weight,
                    'service': 'standard',
                }
            }

            # Call FlagShip to create shipment
            response = requests.post(
                'https://api.shippingapi.com/v1/create-shipment',
                json=payload
            )

            if response.status_code != 200:
                raise UserError(f'FlagShip shipment creation failed: {response.text}')

            # Extract tracking number from response
            shipment_data = response.json()
            tracking_number = shipment_data.get('tracking_number') or shipment_data.get('tracking_id')
            
            # Update the delivery order with tracking info
            delivery_order.update({
                'tracking_reference': tracking_number,
                'carrier_tracking_url': self.tracking_url.replace('<shipmenttrackingnumber>', tracking_number)
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

    def flagship_cancel_shipment(self, shipment_id):
        """Cancel a shipment in FlagShip"""
        self.ensure_one()
        
        if not self.has_flagship_credentials:
            raise UserError(_('FlagShip API credentials not configured'))

        try:
            response = requests.post(
                f'https://api.shippingapi.com/v1/cancel-shipment/{shipment_id}',
                json={'apiKey': self.flagship_api_key}
            )

            if response.status_code != 200:
                raise UserError(f'FlagShip cancellation failed: {response.text}')

            return {
                'success': True,
                'message': _('Shipment cancelled successfully in FlagShip')
            }

        except Exception as e:
            raise UserError(f'Error cancelling shipment in FlagShip: {str(e)}')