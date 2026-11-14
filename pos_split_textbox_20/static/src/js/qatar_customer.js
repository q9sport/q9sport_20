/** @odoo-module **/

import { proxy } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";

const CUSTOMER_FIELDS = [
    "name",
    "phone",
    "zone_no",
    "street_no",
    "building_no",
    "gps_latitude",
    "gps_longitude",
    "birthdate_date",
    "location_description_ar",
];

const REQUIRED_FIELDS = [
    "name",
    "phone",
    "zone_no",
    "street_no",
    "building_no",
    "gps_latitude",
    "gps_longitude",
];

patch(PaymentScreen.prototype, {
    setup(...args) {
        super.setup(...args);
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.qatarCustomer = proxy({
            partnerId: false,
            name: "",
            phone: "",
            zone_no: "",
            street_no: "",
            building_no: "",
            gps_latitude: "",
            gps_longitude: "",
            birthdate_date: "",
            location_description_ar: "",
            saving: false,
            locating: false,
            lookingUp: false,
            savedSignature: "",
        });
        this.qatarPhoneLookupTimer = null;
    },

    getNumpadButtons() {
        const buttons = super.getNumpadButtons();
        return buttons.map((button) =>
            ["-", "+/-", "±"].includes(button.value)
                ? { ...button, text: "±", class: `${button.class || ""} o_qatar_sign_button` }
                : button
        );
    },

    getQatarCustomerValues() {
        const partner = this.currentOrder?.getPartner?.();
        const partnerId = partner?.id || false;
        const values = {};
        for (const field of CUSTOMER_FIELDS) {
            values[field] = this.qatarCustomer.partnerId === partnerId
                ? String(this.qatarCustomer[field] || "").trim()
                : String(partner?.[field] || "").trim();
        }
        return values;
    },

    getQatarCustomerValue(field) {
        return this.getQatarCustomerValues()[field] || "";
    },

    onQatarCustomerInput(field, event) {
        const partner = this.currentOrder?.getPartner?.();
        const partnerId = partner?.id || false;
        if (this.qatarCustomer.partnerId !== partnerId) {
            for (const key of CUSTOMER_FIELDS) {
                this.qatarCustomer[key] = String(partner?.[key] || "");
            }
            this.qatarCustomer.partnerId = partnerId;
            this.qatarCustomer.savedSignature = "";
        }

        const newValue = String(event.target.value || "");
        if (field === "phone" && partner) {
            const oldPhone = String(partner.phone || "").replace(/\D/g, "");
            const newPhone = newValue.replace(/\D/g, "");
            if (newPhone && newPhone !== oldPhone) {
                this.currentOrder.setPartner(false);
                this.qatarCustomer.partnerId = false;
                for (const key of CUSTOMER_FIELDS) {
                    if (key !== "phone") this.qatarCustomer[key] = "";
                }
            }
        }

        this.qatarCustomer[field] = newValue;
        this.qatarCustomer.savedSignature = "";

        if (field === "phone") {
            if (this.qatarPhoneLookupTimer) clearTimeout(this.qatarPhoneLookupTimer);
            const digits = newValue.replace(/\D/g, "");
            if (digits.length >= 8) {
                this.qatarPhoneLookupTimer = setTimeout(() => {
                    this.lookupQatarCustomerByPhone();
                }, 500);
            }
        }
    },

    onQatarPhoneKeydown(event) {
        if (event.key === "Enter") {
            event.preventDefault();
            this.lookupQatarCustomerByPhone();
        }
    },

    async lookupQatarCustomerByPhone() {
        const phone = String(this.qatarCustomer.phone || this.getQatarCustomerValue("phone") || "").trim();
        const digits = phone.replace(/\D/g, "");
        if (digits.length < 8 || this.qatarCustomer.lookingUp) return;

        const currentPartner = this.currentOrder?.getPartner?.();
        const currentDigits = String(currentPartner?.phone || "").replace(/\D/g, "");
        if (currentPartner && currentDigits === digits) return;

        this.qatarCustomer.lookingUp = true;
        try {
            const result = await this.orm.call(
                "res.partner",
                "pos_find_qatar_customer_by_phone",
                [phone]
            );
            // Ignore a delayed response if the cashier has already typed another number.
            if (String(this.qatarCustomer.phone || "").replace(/\D/g, "") !== digits) return;
            if (!result) {
                this.notification.add(
                    _t("No customer found for this phone. Enter the remaining details to create a customer."),
                    { type: "info" }
                );
                return;
            }

            const partnerModel = this.pos?.models?.["res.partner"];
            let partner = partnerModel?.get(result.id);
            const partnerValues = {
                id: result.id,
                name: result.name,
                display_name: result.name,
                phone: result.phone || "",
                zone_no: result.zone_no || "",
                street_no: result.street_no || "",
                building_no: result.building_no || "",
                gps_latitude: result.gps_latitude || "",
                gps_longitude: result.gps_longitude || "",
                birthdate_date: result.birthdate_date || "",
                location_description_ar: result.location_description_ar || "",
            };
            if (!partner && partnerModel?.create) {
                // Add the server-side customer to the local POS data model so setPartner()
                // receives a real POS record, even when the customer was not preloaded.
                partner = partnerModel.create(partnerValues);
            }
            if (!partner) {
                throw new Error(_t("The customer was found but could not be loaded into the POS. Reload the POS and try again."));
            }
            Object.assign(partner, partnerValues);
            this.currentOrder.setPartner(partner);
            this.qatarCustomer.partnerId = result.id;
            for (const field of CUSTOMER_FIELDS) {
                this.qatarCustomer[field] = result[field] || "";
            }
            this.qatarCustomer.savedSignature = "";
            this.notification.add(_t("Customer data loaded automatically."), { type: "success" });
        } catch (error) {
            this.notification.add(
                error?.data?.message || error?.message || _t("Could not search by phone number."),
                { type: "danger" }
            );
        } finally {
            this.qatarCustomer.lookingUp = false;
        }
    },

    async captureQatarLocation() {
        if (!navigator.geolocation) {
            this.notification.add(_t("Geolocation is not supported by this device."), { type: "warning" });
            return;
        }
        this.qatarCustomer.locating = true;
        navigator.geolocation.getCurrentPosition(
            async (position) => {
                const partner = this.currentOrder?.getPartner?.();
                const partnerId = partner?.id || false;
                if (this.qatarCustomer.partnerId !== partnerId) {
                    for (const field of CUSTOMER_FIELDS) {
                        this.qatarCustomer[field] = String(partner?.[field] || "");
                    }
                    this.qatarCustomer.partnerId = partnerId;
                }
                const latitude = String(position.coords.latitude);
                const longitude = String(position.coords.longitude);
                this.qatarCustomer.gps_latitude = latitude;
                this.qatarCustomer.gps_longitude = longitude;
                this.qatarCustomer.location_description_ar = "";
                this.qatarCustomer.savedSignature = "";
                try {
                    const placeName = await this.orm.call(
                        "res.partner",
                        "pos_qatar_reverse_geocode",
                        [latitude, longitude]
                    );
                    this.qatarCustomer.location_description_ar = placeName || _t("تم تحديد الموقع بالإحداثيات GPS");
                } catch {
                    this.qatarCustomer.location_description_ar = _t("تم تحديد الموقع بالإحداثيات GPS");
                } finally {
                    this.qatarCustomer.locating = false;
                }
                this.notification.add(_t("Geographic location captured."), { type: "success" });
            },
            (error) => {
                this.qatarCustomer.locating = false;
                const message = error.code === 1
                    ? _t("Location permission was denied. Allow location access in the browser.")
                    : _t("Could not get the location. Check GPS and try again.");
                this.notification.add(message, { type: "warning" });
            },
            { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 }
        );
    },

    async saveQatarCustomer(showMessage = true) {
        const values = this.getQatarCustomerValues();
        const missing = REQUIRED_FIELDS.filter((field) => !values[field]);
        if (missing.length) {
            this.notification.add(
                _t("Required: customer name, phone, Zone No, Street No, Building No and GPS location."),
                { type: "warning" }
            );
            return false;
        }

        const currentPartner = this.currentOrder.getPartner();
        const signature = JSON.stringify({ partner_id: currentPartner?.id || false, values });
        if (this.qatarCustomer.savedSignature === signature && currentPartner) return true;

        this.qatarCustomer.saving = true;
        try {
            const result = await this.orm.call(
                "res.partner",
                "pos_create_or_update_qatar_customer",
                [{ ...values, partner_id: currentPartner?.id || false }]
            );
            const partnerModel = this.pos?.models?.["res.partner"];
            let partner = partnerModel?.get(result.id);
            const partnerValues = {
                id: result.id,
                name: result.name,
                display_name: result.name,
                phone: result.phone || "",
                zone_no: result.zone_no || "",
                street_no: result.street_no || "",
                building_no: result.building_no || "",
                gps_latitude: result.gps_latitude || "",
                gps_longitude: result.gps_longitude || "",
                birthdate_date: result.birthdate_date || "",
                location_description_ar: result.location_description_ar || "",
            };
            if (!partner && partnerModel?.create) {
                partner = partnerModel.create(partnerValues);
            }
            if (!partner) {
                throw new Error(_t("Customer saved, but POS could not refresh the customer record. Reload POS and select the customer."));
            }
            Object.assign(partner, partnerValues);
            this.currentOrder.setPartner(partner);
            this.qatarCustomer.partnerId = result.id;
            for (const field of CUSTOMER_FIELDS) {
                this.qatarCustomer[field] = result[field] || "";
            }
            this.qatarCustomer.savedSignature = JSON.stringify({ partner_id: result.id, values: this.getQatarCustomerValues() });
            if (showMessage) {
                this.notification.add(
                    result.created ? _t("تم إنشاء العميل") : _t("تم تحديث بيانات العميل"),
                    { type: "success" }
                );
            }
            return true;
        } catch (error) {
            this.notification.add(
                error?.data?.message || error?.message || _t("Could not save customer."),
                { type: "danger" }
            );
            return false;
        } finally {
            this.qatarCustomer.saving = false;
        }
    },

    async onClickValidate(...args) {
        const saved = await this.saveQatarCustomer(true);
        if (!saved) return;
        const remaining = this.currentOrder.remainingDue || 0;
        if (!this.pos.currency.isZero(remaining) && remaining > 0) {
            this.notification.add(
                _t("Remaining amount must be fully paid before validating the order."),
                { type: "warning" }
            );
            return;
        }
        return await super.onClickValidate(...args);
    },
});
