# -*- coding: utf-8 -*-
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ResPartner(models.Model):
    _inherit = "res.partner"

    # Qatar address: keep only the three official delivery numbers.
    zone_no = fields.Char(string="Zone No")
    street_no = fields.Char(string="Street No")
    building_no = fields.Char(string="Building No")
    gps_latitude = fields.Char(string="GPS Latitude", copy=False)
    gps_longitude = fields.Char(string="GPS Longitude", copy=False)
    birthdate_date = fields.Date(string="Date of Birth")
    location_description_ar = fields.Char(
        string="Geographic Location (Arabic)", copy=False
    )

    @api.model
    def _load_pos_data_fields(self, config):
        result = super()._load_pos_data_fields(config)
        for field_name in (
            "zone_no",
            "street_no",
            "building_no",
            "gps_latitude",
            "gps_longitude",
            "birthdate_date",
            "location_description_ar",
        ):
            if field_name not in result:
                result.append(field_name)
        return result

    @api.model
    def _pos_qatar_normalize_phone(self, value):
        return re.sub(r"\D", "", value or "")

    @api.model
    def _pos_qatar_partner_by_phone(self, phone):
        digits = self._pos_qatar_normalize_phone(phone)
        if not digits:
            return self.browse()

        tail = digits[-8:] if len(digits) >= 8 else digits
        search_tail = tail[-4:] if len(tail) >= 4 else tail
        Partner = self.sudo().with_context(active_test=False)
        # IMPORTANT: this database does not have res.partner.mobile. Search phone only.
        candidates = Partner.search(
            [
                "&",
                ("company_id", "in", [False, self.env.company.id]),
                ("phone", "ilike", search_tail),
            ],
            order="active desc, id asc",
            limit=300,
        )
        matches = candidates.filtered(
            lambda partner: bool(
                partner.phone
                and (
                    self._pos_qatar_normalize_phone(partner.phone) == digits
                    or (
                        len(digits) >= 8
                        and self._pos_qatar_normalize_phone(partner.phone).endswith(tail)
                    )
                )
            )
        )
        if len(matches) > 1:
            exact = matches.filtered(
                lambda partner: bool(
                    partner.phone
                    and self._pos_qatar_normalize_phone(partner.phone) == digits
                )
            )
            matches = exact or matches
        if len(matches) > 1:
            raise ValidationError(
                _("More than one customer has this phone number. Please use a unique phone number.")
            )
        return matches[:1]

    @api.model
    def _pos_qatar_customer_payload(self, partner):
        return {
            "id": partner.id,
            "name": partner.name or "",
            "phone": partner.phone or "",
            "zone_no": partner.zone_no or "",
            "street_no": partner.street_no or "",
            "building_no": partner.building_no or "",
            "gps_latitude": partner.gps_latitude or "",
            "gps_longitude": partner.gps_longitude or "",
            "birthdate_date": fields.Date.to_string(partner.birthdate_date)
            if partner.birthdate_date
            else "",
            "location_description_ar": partner.location_description_ar or "",
        }

    @api.model
    def _pos_qatar_reverse_geocode(self, latitude, longitude):
        """Return a short Arabic place description from GPS; fail safely if offline."""
        try:
            lat = float(latitude)
            lon = float(longitude)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return False
            query = urlencode(
                {
                    "lat": lat,
                    "lon": lon,
                    "format": "jsonv2",
                    "addressdetails": 1,
                    "zoom": 18,
                    "accept-language": "ar",
                }
            )
            request = Request(
                "https://nominatim.openstreetmap.org/reverse?" + query,
                headers={
                    "User-Agent": "OdooPOSQatarCustomer/20.0 (reverse geocoding)"
                },
            )
            with urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            address = data.get("address") or {}
            area = (
                address.get("neighbourhood")
                or address.get("suburb")
                or address.get("quarter")
                or address.get("city_district")
                or address.get("district")
            )
            city = (
                address.get("city")
                or address.get("town")
                or address.get("village")
                or address.get("municipality")
            )
            road = (
                address.get("road")
                or address.get("pedestrian")
                or address.get("residential")
                or address.get("footway")
            )
            parts = []
            if area:
                parts.append("الحي/المنطقة: " + area)
            if road:
                parts.append("الشارع: " + road)
            if city:
                parts.append("المدينة: " + city)
            if not parts:
                display_name = data.get("display_name")
                if display_name:
                    parts.append(display_name)
            return "، ".join(parts)[:500] if parts else False
        except Exception:
            # GPS coordinates are still saved and shown if the geocoder is unavailable.
            return False

    @api.model
    def pos_qatar_reverse_geocode(self, latitude, longitude):
        if not self.env.user.has_group("point_of_sale.group_pos_user"):
            raise AccessError(_("You are not allowed to resolve POS locations."))
        return self._pos_qatar_reverse_geocode(latitude, longitude)

    @api.model
    def pos_find_qatar_customer_by_phone(self, phone):
        if not self.env.user.has_group("point_of_sale.group_pos_user"):
            raise AccessError(_("You are not allowed to search POS customers."))
        partner = self._pos_qatar_partner_by_phone(phone)
        return self._pos_qatar_customer_payload(partner) if partner else False

    @api.model
    def pos_create_or_update_qatar_customer(self, values):
        """Create/update a POS customer by phone first, then exact name."""
        if not self.env.user.has_group("point_of_sale.group_pos_user"):
            raise AccessError(_("You are not allowed to create POS customers."))

        values = values or {}
        name = (values.get("name") or "").strip()
        phone = (values.get("phone") or "").strip()
        zone_no = (values.get("zone_no") or "").strip()
        street_no = (values.get("street_no") or "").strip()
        building_no = (values.get("building_no") or "").strip()
        latitude = (values.get("gps_latitude") or "").strip()
        longitude = (values.get("gps_longitude") or "").strip()
        birthdate_date = (values.get("birthdate_date") or "").strip()
        location_description_ar = (values.get("location_description_ar") or "").strip()

        required = {
            "name": name,
            "phone": phone,
            "zone_no": zone_no,
            "street_no": street_no,
            "building_no": building_no,
            "gps_latitude": latitude,
            "gps_longitude": longitude,
        }
        if any(not value for value in required.values()):
            raise ValidationError(
                _("Please complete customer name, phone, Zone No, Street No, Building No and GPS location.")
            )

        try:
            lat = float(latitude)
            lon = float(longitude)
        except (TypeError, ValueError):
            raise ValidationError(_("GPS coordinates must be valid numbers."))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValidationError(_("GPS coordinates are outside the valid range."))

        if birthdate_date:
            try:
                birthdate_date = fields.Date.to_string(fields.Date.from_string(birthdate_date))
            except (TypeError, ValueError):
                raise ValidationError(_("Customer date of birth must be a valid date."))

        if not location_description_ar:
            location_description_ar = self._pos_qatar_reverse_geocode(lat, lon) or ""

        company = self.env.company
        Partner = self.sudo().with_company(company).with_context(active_test=False)
        partner = Partner.browse(int(values.get("partner_id"))).exists() if values.get("partner_id") else Partner.browse()
        if partner and partner.company_id and partner.company_id != company:
            raise AccessError(_("This customer belongs to another company."))

        if not partner:
            partner = self._pos_qatar_partner_by_phone(phone).with_company(company)
        if not partner:
            partner = Partner.search(
                [
                    ("name", "=ilike", name),
                    ("company_id", "in", [False, company.id]),
                ],
                order="active desc, id asc",
                limit=1,
            )

        country = self.env["res.country"].sudo().search([("code", "=", "QA")], limit=1)
        if not country:
            raise UserError(_("Qatar country is not configured in the database."))

        receivable = self.env["account.account"].sudo().search(
            [
                ("code", "=", "100201"),
                ("account_type", "=", "asset_receivable"),
                ("company_ids", "in", company.id),
            ],
            limit=1,
        )
        if not receivable:
            raise UserError(
                _("Receivable account 100201 was not found for company %s.") % company.display_name
            )
        account_currency = receivable.currency_id or company.currency_id
        if account_currency.name != "QAR":
            raise UserError(
                _("Account 100201 must use QAR. Its current effective currency is %s.")
                % account_currency.name
            )

        partner_values = {
            "name": name,
            "phone": phone,
            "customer_rank": 1,
            "type": "contact",
            "country_id": country.id,
            "zone_no": zone_no,
            "street_no": street_no,
            "building_no": building_no,
            "gps_latitude": latitude,
            "gps_longitude": longitude,
            "birthdate_date": birthdate_date or False,
            "location_description_ar": location_description_ar or False,
            # Keep the Arabic reverse-geocoded place name in Odoo's standard
            # address field, and retain the three Qatar delivery numbers clearly.
            "street": location_description_ar or (
                "المنطقة: %s، الشارع: %s، المبنى: %s"
                % (zone_no, street_no, building_no)
            ),
            "street2": "المنطقة رقم %s، شارع رقم %s، مبنى رقم %s"
            % (zone_no, street_no, building_no),
            "property_account_receivable_id": receivable.id,
            "company_id": False,
            "active": True,
        }
        if partner:
            partner.write(partner_values)
            created = False
        else:
            partner = Partner.create(partner_values)
            created = True

        return {
            **self._pos_qatar_customer_payload(partner),
            "country_id": partner.country_id.id or False,
            "created": created,
            "message": _("Customer created successfully") if created else _("Customer details updated successfully"),
        }
