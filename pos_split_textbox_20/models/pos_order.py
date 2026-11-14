# -*- coding: utf-8 -*-
from odoo import fields, models


class PosOrder(models.Model):
    _inherit = "pos.order"

    customer_zone_no = fields.Char(related="partner_id.zone_no", string="Zone No", readonly=True, store=True)
    customer_street_no = fields.Char(related="partner_id.street_no", string="Street No", readonly=True, store=True)
    customer_building_no = fields.Char(related="partner_id.building_no", string="Building No", readonly=True, store=True)
    customer_gps_latitude = fields.Char(related="partner_id.gps_latitude", string="GPS Latitude", readonly=True, store=True)
    customer_gps_longitude = fields.Char(related="partner_id.gps_longitude", string="GPS Longitude", readonly=True, store=True)

    customer_location_description_ar = fields.Char(related="partner_id.location_description_ar", string="Geographic Location (Arabic)", readonly=True, store=True)
