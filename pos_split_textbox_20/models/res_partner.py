# -*- coding: utf-8 -*-
import json
import re
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    qnas_enabled = fields.Boolean(
        string="Enable QNAS Integration",
        config_parameter="pos_split_textbox_20.qnas_enabled",
        default=False,
    )
    qnas_domain = fields.Char(
        string="QNAS Domain",
        config_parameter="pos_split_textbox_20.qnas_domain",
        default="q9sport-q9sport-20.odoo.com",
    )
    qnas_token = fields.Char(
        string="QNAS API Token",
        config_parameter="pos_split_textbox_20.qnas_token",
    )


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
        # Only request fields that actually exist in this database.
        for field_name in (
            "mobile",
            "phone_sanitized",
            "street",
            "zone_no",
            "street_no",
            "building_no",
            "gps_latitude",
            "gps_longitude",
            "birthdate_date",
            "location_description_ar",
        ):
            if field_name in self._fields and field_name not in result:
                result.append(field_name)
        return result

    @api.model
    def _pos_qatar_normalize_phone(self, value):
        """Normalize Qatar phone values, including Arabic-Indic numerals."""
        value = str(value or "").translate(str.maketrans({
            "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
            "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
            "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
            "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
        }))
        digits = re.sub(r"\D", "", value)
        if len(digits) > 8:
            return digits[-8:]
        return digits

    @api.model
    def _pos_qatar_partner_by_phone(self, phone):
        digits = self._pos_qatar_normalize_phone(phone)
        if len(digits) < 8:
            return self.browse()

        Partner = self.sudo().with_context(active_test=False)
        phone_fields = [
            field_name for field_name in ("phone", "mobile")
            if field_name in Partner._fields
        ]
        if not phone_fields:
            return self.browse()

        suffix = digits[-4:]
        search_domain = ["|"] * (len(phone_fields) - 1)
        search_domain += [
            (field_name, "ilike", suffix) for field_name in phone_fields
        ]
        candidates = Partner.search(
            search_domain,
            order="active desc, id asc",
            limit=10000,
        )

        def matches_local(partner):
            for field_name in phone_fields:
                candidate = self._pos_qatar_normalize_phone(
                    partner[field_name] or ""
                )
                if len(candidate) >= 8 and candidate[-8:] == digits[-8:]:
                    return True
            return False

        matches = candidates.filtered(matches_local)
        active_matches = matches.filtered("active")
        if active_matches:
            matches = active_matches

        if len(matches) > 1:
            exact = matches.filtered(lambda partner: any(
                self._pos_qatar_normalize_phone(partner[field_name] or "") == digits
                or self._pos_qatar_normalize_phone(
                    partner[field_name] or ""
                )[-8:] == digits[-8:]
                for field_name in phone_fields
            ))
            if len(exact) == 1:
                matches = exact
            else:
                raise ValidationError(_(
                    "This phone number belongs to more than one customer. "
                    "Please merge or correct the duplicate customer records first."
                ))
        return matches[:1]

    @api.model
    def _pos_qatar_customer_payload(self, partner):
        return {
            "id": partner.id,
            "name": partner.name or "",
            "phone": (
                partner["mobile"]
                if "mobile" in partner._fields else False
            ) or partner.phone or "",
            "street": partner.street or "",
            "mobile": (
                partner["mobile"]
                if "mobile" in partner._fields else ""
            ) or "",
            "zone_no": partner.zone_no or "",
            "street_no": partner.street_no or "",
            "building_no": partner.building_no or "",
            "gps_latitude": partner.gps_latitude or "",
            "gps_longitude": partner.gps_longitude or "",
            "birthdate_date": (
                fields.Date.to_string(partner.birthdate_date)
                if partner.birthdate_date else ""
            ),
            "location_description_ar": (
                partner.location_description_ar or ""
            ),
        }

    @api.model
    def _pos_qatar_get_system_parameter(self, key):
        """Read a system parameter across Odoo builds."""
        ICP = self.env["ir.config_parameter"].sudo()
        getter = getattr(ICP, "get_param", None)
        if callable(getter):
            try:
                return getter(key, default=False)
            except TypeError:
                return getter(key)

        record = ICP.search([("key", "=", key)], limit=1)
        return record.value if record else False

    @api.model
    def _pos_qatar_qnas_configuration(self):
        """Read and normalize QNAS configuration saved from Odoo Settings."""
        enabled = self._pos_qatar_get_system_parameter(
            "pos_split_textbox_20.qnas_enabled"
        )
        token = self._pos_qatar_get_system_parameter(
            "pos_split_textbox_20.qnas_token"
        )
        domain = self._pos_qatar_get_system_parameter(
            "pos_split_textbox_20.qnas_domain"
        ) or "q9sport-q9sport-20.odoo.com"

        domain = str(domain or "").strip()
        domain = re.sub(r"^https?://", "", domain, flags=re.IGNORECASE)
        domain = domain.strip().rstrip("/")

        enabled = str(enabled or "").strip().lower() in (
            "1", "true", "yes", "on"
        )
        return enabled, str(token or "").strip(), domain

    @api.model
    def pos_qatar_geocode_address(self, zone_no, street_no, building_no):
        """Resolve the Qatar building coordinates using QNAS."""
        if not self.env.user.has_group("point_of_sale.group_pos_user"):
            raise AccessError(_("You are not allowed to resolve POS addresses."))

        zone_no = str(zone_no or "").strip()
        street_no = str(street_no or "").strip()
        building_no = str(building_no or "").strip()

        if not all((zone_no, street_no, building_no)):
            raise ValidationError(_(
                "Enter Zone, Street and Building numbers first."
            ))

        def is_zero(value):
            normalized = re.sub(r"\D", "", value)
            return bool(normalized) and int(normalized) == 0

        if any(is_zero(value) for value in (
            zone_no, street_no, building_no
        )):
            return {
                "latitude": False,
                "longitude": False,
                "google_maps_url": False,
                "waze_url": False,
                "place_name": _("غير معرف"),
                "verified": False,
                "not_found": True,
            }

        enabled, token, domain = self._pos_qatar_qnas_configuration()

        if not enabled:
            raise UserError(_(
                "QNAS integration is disabled. Enable it from "
                "Settings > Qatar POS > QNAS Geographic Location."
            ))

        if not token or not domain:
            raise UserError(_(
                "QNAS is not configured. Open Settings > Qatar POS and "
                "enter the QNAS Domain and API Token."
            ))

        url = "https://qnas.qa/get_location/%s/%s/%s" % (
            quote(zone_no, safe=""),
            quote(street_no, safe=""),
            quote(building_no, safe=""),
        )

        request = Request(
            url,
            headers={
                "X-Token": token,
                "X-Domain": domain,
                "Accept": "application/json",
                "User-Agent": "OdooPOSQatarCustomer/20.0",
            },
        )

        try:
            with urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise UserError(_(
                "QNAS address service failed: %s"
            ) % str(exc))

        if not isinstance(data, dict):
            raise UserError(_("QNAS returned an invalid response."))

        if (
            str(data.get("status", "")).lower() == "error"
            or data.get("success") is False
        ):
            return {
                "latitude": False,
                "longitude": False,
                "google_maps_url": False,
                "waze_url": False,
                "place_name": _("غير معرف"),
                "verified": False,
                "not_found": True,
            }

        payloads = [data]
        for key in (
            "data", "result", "location", "building", "coordinates"
        ):
            value = data.get(key)
            if isinstance(value, dict):
                payloads.insert(0, value)

        lat = lon = None
        for payload in payloads:
            lat = payload.get(
                "latitude",
                payload.get("lat", payload.get("y"))
            )
            lon = payload.get(
                "longitude",
                payload.get("lng", payload.get("lon", payload.get("x")))
            )
            if lat is not None and lon is not None:
                break

        if lat is None or lon is None:
            return {
                "latitude": False,
                "longitude": False,
                "google_maps_url": False,
                "waze_url": False,
                "place_name": _("غير معرف"),
                "verified": False,
                "not_found": True,
            }

        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            raise UserError(_("QNAS returned invalid GPS coordinates."))

        if not (24.4 <= lat <= 26.3 and 50.6 <= lon <= 52.1):
            raise UserError(_(
                "QNAS returned coordinates outside Qatar. "
                "The address was not saved as verified."
            ))

        place_name = "، ".join([
            _("المنطقة %s") % zone_no,
            _("الشارع %s") % street_no,
            _("المبنى %s") % building_no,
        ])

        for payload in payloads:
            candidate_name = (
                payload.get("formatted_address")
                or payload.get("address_name")
                or payload.get("name")
                or payload.get("location_name")
            )
            if candidate_name:
                place_name = candidate_name
                break

        return {
            "latitude": lat,
            "longitude": lon,
            "google_maps_url": (
                "https://www.google.com/maps?q=%s,%s" % (lat, lon)
            ),
            "waze_url": (
                "https://www.waze.com/ul?ll=%s,%s&navigate=yes" % (lat, lon)
            ),
            "place_name": place_name,
            "verified": True,
            "not_found": False,
        }

    @api.model
    def _pos_qatar_reverse_geocode(self, latitude, longitude):
        """Return a short Arabic place description; fail safely if offline."""
        try:
            lat = float(latitude)
            lon = float(longitude)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return False

            query = urlencode({
                "lat": lat,
                "lon": lon,
                "format": "jsonv2",
                "addressdetails": 1,
                "zoom": 18,
                "accept-language": "ar",
            })

            request = Request(
                "https://nominatim.openstreetmap.org/reverse?" + query,
                headers={
                    "User-Agent": (
                        "OdooPOSQatarCustomer/20.0 (reverse geocoding)"
                    )
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
            return False

    @api.model
    def pos_qatar_reverse_geocode(self, latitude, longitude):
        if not self.env.user.has_group("point_of_sale.group_pos_user"):
            raise AccessError(_(
                "You are not allowed to resolve POS locations."
            ))
        return self._pos_qatar_reverse_geocode(latitude, longitude)

    @api.model
    def pos_find_qatar_customer_by_phone(self, phone):
        if not self.env.user.has_group("point_of_sale.group_pos_user"):
            raise AccessError(_(
                "You are not allowed to search POS customers."
            ))
        partner = self._pos_qatar_partner_by_phone(phone)
        return self._pos_qatar_customer_payload(partner) if partner else False

    @api.model
    def pos_create_or_update_qatar_customer(self, values):
        """Create/update a POS customer by phone first, then exact name."""
        if not self.env.user.has_group("point_of_sale.group_pos_user"):
            raise AccessError(_(
                "You are not allowed to create POS customers."
            ))

        values = values or {}
        name = (values.get("name") or "").strip()
        phone = (values.get("phone") or "").strip()
        address = (
            values.get("street") or values.get("address") or ""
        ).strip()
        zone_no = (values.get("zone_no") or "").strip()
        street_no = (values.get("street_no") or "").strip()
        building_no = (values.get("building_no") or "").strip()
        latitude = (values.get("gps_latitude") or "").strip()
        longitude = (values.get("gps_longitude") or "").strip()
        birthdate_date = (values.get("birthdate_date") or "").strip()
        location_description_ar = (
            values.get("location_description_ar") or ""
        ).strip()

        required = {
            "name": name,
            "phone": phone,
            "zone_no": zone_no,
            "street_no": street_no,
            "building_no": building_no,
        }
        if any(not value for value in required.values()):
            raise ValidationError(_(
                "Please complete customer name, phone, Zone No, "
                "Street No and Building No."
            ))

        if len(self._pos_qatar_normalize_phone(phone)) < 8:
            raise ValidationError(_(
                "Enter a valid Qatar phone number with at least 8 digits."
            ))

        if any(
            len(value) > 30
            for value in (zone_no, street_no, building_no)
        ):
            raise ValidationError(_(
                "Zone, Street and Building numbers must not exceed 30 characters."
            ))

        lat = lon = False
        if latitude or longitude:
            if not latitude or not longitude:
                raise ValidationError(_(
                    "Enter both GPS latitude and longitude, or leave both empty."
                ))
            try:
                lat = float(latitude)
                lon = float(longitude)
            except (TypeError, ValueError):
                raise ValidationError(_(
                    "GPS coordinates must be valid numbers."
                ))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValidationError(_(
                    "GPS coordinates are outside the valid range."
                ))

        if birthdate_date:
            try:
                birthdate_date = fields.Date.to_string(
                    fields.Date.from_string(birthdate_date)
                )
            except (TypeError, ValueError):
                raise ValidationError(_(
                    "Customer date of birth must be a valid date."
                ))

        def is_zero_address_number(value):
            digits = re.sub(r"\D", "", str(value or ""))
            return bool(digits) and int(digits) == 0

        if any(is_zero_address_number(value) for value in (
            zone_no, street_no, building_no
        )):
            location_description_ar = _("غير معرف")
            latitude = longitude = ""
            lat = lon = False
        elif (
            not location_description_ar
            and lat is not False
            and lon is not False
        ):
            location_description_ar = (
                self._pos_qatar_reverse_geocode(lat, lon) or ""
            )

        company = self.env.company
        Partner = self.sudo().with_company(company).with_context(
            active_test=False
        )

        partner = (
            Partner.browse(int(values.get("partner_id"))).exists()
            if values.get("partner_id")
            else Partner.browse()
        )

        if partner and partner.company_id and partner.company_id != company:
            raise AccessError(_(
                "This customer belongs to another company."
            ))

        if not partner:
            partner = self._pos_qatar_partner_by_phone(
                phone
            ).with_company(company)

        country = self.env["res.country"].sudo().search(
            [("code", "=", "QA")],
            limit=1,
        )
        if not country:
            raise UserError(_(
                "Qatar country is not configured in the database."
            ))

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
            "street": address or (
                "Zone %s, Street %s, Building %s"
                % (zone_no, street_no, building_no)
            ),
            "street2": (
                "المنطقة %s، الشارع %s، المبنى %s"
                % (zone_no, street_no, building_no)
            ),
            "company_id": False,
            "active": True,
        }

        if "mobile" in Partner._fields:
            partner_values["mobile"] = phone

        if lat is not False and lon is not False:
            partner_values.update({
                "partner_latitude": lat,
                "partner_longitude": lon,
            })

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
            "message": (
                _("Customer created successfully")
                if created
                else _("Customer details updated successfully")
            ),
        }
