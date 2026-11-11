import logging

import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


SHIPSTATION_API = 'https://api.shipstation.com/v2'


class DeliveryShipstation(models.Model):
    _inherit = 'delivery.carrier'

    delivery_type = fields.Selection(
        selection_add=[('shipstation', 'ShipStation')],
        ondelete={'shipstation': 'set default'},
    )

    # ---------------- Credentials & configuration ----------------
    shipstation_api_key = fields.Char(
        string='ShipStation API Key',
        help='Your ShipStation V2 API key.',
    )
    shipstation_carrier_ids = fields.Char(
        string='ShipStation Carrier IDs',
        help='Comma-separated list of ShipStation carrier IDs (e.g. se-123890,se-123891). '
             'Used as rate_options.carrier_ids when shopping rates.',
    )
    shipstation_carrier_filter = fields.Char(
        string='Preferred Carrier Code',
        help="Optional. If set, only rates from this carrier code (e.g. 'fedex', 'ups') "
             "are considered when picking the cheapest rate.",
    )
    has_shipstation_credentials = fields.Boolean(
        compute='_compute_has_shipstation_credentials',
        string='Has ShipStation Credentials',
    )

    @api.depends('shipstation_api_key', 'shipstation_carrier_ids')
    def _compute_has_shipstation_credentials(self):
        for carrier in self:
            carrier.has_shipstation_credentials = bool(
                carrier.shipstation_api_key and carrier.shipstation_carrier_ids
            )

    # ---------------- Helpers ----------------
    def _shipstation_headers(self):
        self.ensure_one()
        return {
            'api-key': self.shipstation_api_key,
            'Content-Type': 'application/json',
        }

    def _shipstation_carrier_id_list(self):
        self.ensure_one()
        return [
            cid.strip()
            for cid in (self.shipstation_carrier_ids or '').split(',')
            if cid.strip()
        ]

    def _shipstation_build_address(self, partner, residential):
        return {
            'name': partner.name or 'Contact',
            'phone': partner.phone or '0000000000',
            'company_name': partner.commercial_company_name or '',
            'address_line1': partner.street or '',
            'address_line2': partner.street2 or '',
            'city_locality': partner.city or '',
            'state_province': partner.state_id.code if partner.state_id else '',
            'postal_code': partner.zip or '',
            'country_code': partner.country_id.code if partner.country_id else 'US',
            'address_residential_indicator': 'yes' if residential else 'no',
        }

    def _shipstation_package_from_quant_package(self, package):
        """Build a ShipStation package dict from a stock.quant.package record."""
        # Odoo's stock.package_level stores packaging; when real dimensions
        # aren't set we fall back to sensible defaults.
        length = package.packaging_id.length if package.packaging_id else 0.0
        width = package.packaging_id.width if package.packaging_id else 0.0
        height = package.packaging_id.height if package.packaging_id else 0.0
        # weight in kg; use package's own weight if available, else sum of contents
        weight_kg = 0.0
        for quant in package.quant_ids:
            weight_kg += (quant.product_id.weight or 0.0) * quant.quantity
        return {
            'package_code': 'package',
            'weight': {'value': max(weight_kg, 0.1), 'unit': 'kilogram'},
            'dimensions': {
                'unit': 'centimeter',
                'length': length,
                'width': width,
                'height': height,
            },
            'content_description': package.name or 'Goods',
        }

    def _shipstation_packages_from_picking(self, picking):
        """Return list of package dicts from the picking's real packages."""
        packages = []
        seen = set()
        for move_line in picking.move_line_ids:
            package = move_line.result_package_id
            if package and package.id not in seen:
                seen.add(package.id)
                packages.append(self._shipstation_package_from_quant_package(package))
        if not packages:
            _logger.warning(
                'Picking %s has no result_package_id; using a single default package.',
                picking.name,
            )
            packages = [{
                'package_code': 'package',
                'weight': {
                    'value': max(picking.shipping_weight or 1.0, 0.1),
                    'unit': 'kilogram',
                },
            }]
        return packages

    def _shipstation_packages_from_order(self, order):
        """Return list of package dicts from sale order lines (kg → kilogram)."""
        packages = []
        for line in order.order_line:
            if line.product_id.type == 'service':
                continue
            product = line.product_id
            for _ in range(int(line.product_qty)):
                packages.append({
                    'package_code': 'package',
                    'weight': {
                        'value': max(product.weight or 1.0, 0.1),
                        'unit': 'kilogram',
                    },
                    'dimensions': {
                        'unit': 'centimeter',
                        'length': getattr(product, 'length', 0.0) or 0.0,
                        'width': getattr(product, 'width', 0.0) or 0.0,
                        'height': getattr(product, 'height', 0.0) or 0.0,
                    },
                    'content_description': product.name or 'Goods',
                })
        if not packages:
            packages = [{
                'package_code': 'package',
                'weight': {'value': 1.0, 'unit': 'kilogram'},
            }]
        return packages

    def _shipstation_build_shipment(self, from_partner, to_partner, packages,
                                    to_is_residential):
        return {
            'validate_address': 'no_validation',
            'ship_from': self._shipstation_build_address(from_partner, False),
            'ship_to': self._shipstation_build_address(to_partner, to_is_residential),
            'packages': packages,
        }

    @staticmethod
    def _shipstation_total_amount(rate):
        total = 0.0
        for key in ('shipping_amount', 'insurance_amount',
                    'confirmation_amount', 'other_amount'):
            amt = rate.get(key) or {}
            total += float(amt.get('amount', 0.0) or 0.0)
        return total

    # ---------------- Rate shopping (checkout) ----------------
    def shipstation_rate_shipment(self, order):
        """
        Shop rates via POST /v2/rates.

        Persists the winning rate_id on the order so send_shipping can buy
        exactly the rate the customer saw.
        """
        self.ensure_one()

        if not self.has_shipstation_credentials:
            return {
                'success': False,
                'price': 0.0,
                'error_message': _('ShipStation API credentials not configured'),
                'warning_message': '',
            }

        try:
            origin = order.warehouse_id.partner_id
            dest = order.partner_shipping_id

            payload = {
                'rate_options': {
                    'carrier_ids': self._shipstation_carrier_id_list(),
                },
                'shipment': self._shipstation_build_shipment(
                    origin, dest,
                    self._shipstation_packages_from_order(order),
                    to_is_residential=True,
                ),
            }

            response = requests.post(
                f'{SHIPSTATION_API}/rates',
                json=payload,
                headers=self._shipstation_headers(),
                timeout=30,
            )

            if response.status_code not in (200, 201):
                return {
                    'success': False,
                    'price': 0.0,
                    'error_message': (
                        f'ShipStation API error: {response.status_code} - '
                        f'{response.text[:500]}'
                    ),
                    'warning_message': '',
                }

            data = response.json()
            rates = (data.get('rate_response') or {}).get('rates', []) or []

            if self.shipstation_carrier_filter:
                wanted = self.shipstation_carrier_filter.strip().lower()
                rates = [
                    r for r in rates
                    if (r.get('carrier_code') or '').lower() == wanted
                ]

            if not rates:
                return {
                    'success': False,
                    'price': 0.0,
                    'error_message': _(
                        'No applicable rates returned for this provider/destination.'
                    ),
                    'warning_message': '',
                }

            best_rate = min(rates, key=self._shipstation_total_amount)
            price = self._shipstation_total_amount(best_rate)

            margin = self.margin or 0.0
            final_price = price * (1 + margin / 100)

            # Persist so send_shipping can buy the exact same rate.
            order.sudo().write({
                'shipstation_rate_id': best_rate.get('rate_id'),
                'shipstation_shipment_id': data.get('shipment_id'),
            })

            return {
                'success': True,
                'price': final_price,
                'error_message': '',
                'warning_message': (
                    f'ShipStation rate retrieved for {len(rates)} service option(s)'
                ),
            }

        except Exception as exc:
            _logger.exception('ShipStation rate lookup failed: %s', exc)
            return {
                'success': False,
                'price': 0.0,
                'error_message': 'Not available',
                'warning_message': '',
            }

    # ---------------- Label creation ----------------
    def shipstation_send_shipping(self, pickings):
        """
        Buy a label using the rate_id previously stored on the related
        sale order. Falls back to a fresh rate lookup if none is found.
        """
        self.ensure_one()

        if not self.has_shipstation_credentials:
            raise UserError(_('ShipStation API credentials not configured'))

        tracking_number = False
        try:
            for picking in pickings:
                sale_order = picking.sale_id
                rate_id = sale_order.shipstation_rate_id if sale_order else False

                if not rate_id:
                    rate_id = self._shipstation_pick_rate_for_picking(picking)
                    if not rate_id:
                        raise UserError(_(
                            'No ShipStation rate available for picking %s. '
                            'Re-shop rates or configure the carrier.'
                        ) % picking.name)

                origin = picking.picking_type_id.warehouse_id.partner_id
                dest = picking.partner_id

                shipment = self._shipstation_build_shipment(
                    origin, dest,
                    self._shipstation_packages_from_picking(picking),
                    to_is_residential=True,
                )

                payload = {
                    'shipment': shipment,
                    'rate_id': rate_id,
                    'label_format': 'pdf',
                    'label_layout': '4x6',
                }

                response = requests.post(
                    f'{SHIPSTATION_API}/labels',
                    json=payload,
                    headers=self._shipstation_headers(),
                    timeout=60,
                )

                if response.status_code not in (200, 201):
                    raise UserError(
                        f'ShipStation label creation failed: {response.text[:500]}'
                    )

                label_data = response.json()
                tracking_number = label_data.get('tracking_number')
                shipment_id = label_data.get('shipment_id')

                tracking_url = False
                if self.tracking_url and tracking_number:
                    tracking_url = self.tracking_url.replace(
                        '<shipmenttrackingnumber>', str(tracking_number)
                    )

                picking.update({
                    'tracking_reference': tracking_number,
                    'carrier_tracking_url': tracking_url,
                    'shipstation_shipment_id': shipment_id,
                })

            return {
                'success': True,
                'tracking_number': tracking_number,
                'message': _('Label created successfully in ShipStation'),
            }

        except UserError:
            raise
        except Exception as e:
            raise UserError(f'Error creating label in ShipStation: {str(e)}')

    def _shipstation_pick_rate_for_picking(self, picking):
        """Fallback: shop a fresh rate for this picking if none was stored."""
        shipment = self._shipstation_build_shipment(
            picking.picking_type_id.warehouse_id.partner_id,
            picking.partner_id,
            self._shipstation_packages_from_picking(picking),
            to_is_residential=True,
        )
        payload = {
            'rate_options': {'carrier_ids': self._shipstation_carrier_id_list()},
            'shipment': shipment,
        }
        response = requests.post(
            f'{SHIPSTATION_API}/rates',
            json=payload,
            headers=self._shipstation_headers(),
            timeout=30,
        )
        if response.status_code not in (200, 201):
            return False
        rates = (response.json().get('rate_response') or {}).get('rates', [])
        if not rates:
            return False
        return min(rates, key=self._shipstation_total_amount).get('rate_id')

    def _get_tracking_link(self, shipment):
        if not self.tracking_url or not shipment.tracking_reference:
            return False
        return self.tracking_url.replace(
            '<shipmenttrackingnumber>', shipment.tracking_reference
        )

    # ---------------- Cancellation ----------------
    def shipstation_cancel_shipment(self, picking):
        """Cancel the ShipStation shipment using the stored shipment_id."""
        self.ensure_one()

        if not self.has_shipstation_credentials:
            raise UserError(_('ShipStation API credentials not configured'))

        shipment_id = picking.shipstation_shipment_id
        if not shipment_id:
            raise UserError(_(
                'No ShipStation shipment ID stored on picking %s. '
                'Cancel from the ShipStation UI instead.'
            ) % picking.name)

        try:
            response = requests.post(
                f'{SHIPSTATION_API}/shipments/{shipment_id}/cancel',
                headers=self._shipstation_headers(),
                timeout=30,
            )
            if response.status_code not in (200, 204):
                raise UserError(
                    f'ShipStation cancellation failed: {response.text[:500]}'
                )
            return {
                'success': True,
                'message': _('Shipment cancelled successfully in ShipStation'),
            }
        except UserError:
            raise
        except Exception as e:
            raise UserError(f'Error cancelling shipment in ShipStation: {str(e)}')