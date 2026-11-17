/** @odoo-module **/
import { proxy } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

const CUSTOMER_FIELDS = [
    "name",
    "phone",
    "street",
    "zone_no",
    "street_no",
    "building_no",
    "gps_latitude",
    "gps_longitude",
    "birthdate_date",
    "location_description_ar",
];

function partnerCustomerField(partner, field) {
    if (!partner) return "";
    if (field === "phone") return partner.mobile || partner.phone || "";
    return partner[field] || "";
}

function normalizeQatarPhone(value) {
    const arabicDigits = "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹";
    const westernDigits = "01234567890123456789";
    const converted = String(value || "").replace(/[٠-٩۰-۹]/g, (digit) => {
        const index = arabicDigits.indexOf(digit);
        return index >= 0 ? westernDigits[index] : digit;
    });
    const digits = converted.replace(/\D/g, "");
    return digits.length > 8 ? digits.slice(-8) : digits;
}

const REQUIRED_FIELDS = [
    "name",
    "phone",
    "zone_no",
    "street_no",
    "building_no",
];

function formatArabicAddress(address) {
    if (!address || typeof address !== "object") return "";

    const parts = [];
    if (address.area) {
        parts.push(`المنطقة: ${address.area}`);
    }
    if (address.road) {
        parts.push(`الشارع: ${address.road}`);
    }
    if (address.city) {
        parts.push(`المدينة: ${address.city}`);
    }

    if (!parts.length && address.display_name) {
        return String(address.display_name).slice(0, 500);
    }
    return parts.join("، ").slice(0, 500);
}

function applyServerCustomerToPos(partnerModel, data) {
    if (!partnerModel || !data?.id) {
        throw new Error(_t("The customer was found but could not be loaded into the POS."));
    }

    const values = {
        name: data.name || "",
        phone: data.phone || data.mobile || "",
        street: data.street || "",
        zone_no: data.zone_no || "",
        street_no: data.street_no || "",
        building_no: data.building_no || "",
        gps_latitude: data.gps_latitude || "",
        gps_longitude: data.gps_longitude || "",
        birthdate_date: data.birthdate_date || "",
        location_description_ar: data.location_description_ar || "",
    };

    let partner = partnerModel.get(data.id);
    if (!partner && typeof partnerModel.create === "function") {
        partner = partnerModel.create({ id: data.id, ...values });
    }
    if (!partner) {
        throw new Error(_t("The customer was found but could not be loaded into the POS. Reload POS and try again."));
    }

    for (const [field, value] of Object.entries(values)) {
        partner[field] = value;
    }
    return partner;
}

patch(PaymentScreen.prototype, {
    setup(...args) {
        super.setup(...args);
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.dialog = useService("dialog");

        this.qatarCustomer = proxy({
            partnerId: false,
            name: "",
            phone: "",
            street: "",
            zone_no: "",
            street_no: "",
            building_no: "",
            gps_latitude: "",
            gps_longitude: "",
            birthdate_date: "",
            location_description_ar: "",
            saving: false,
            lookingUp: false,
            savedSignature: "",
            originalSignature: "",
            resolvingAddress: false,
            googleMapsUrl: "",
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
                : String(partnerCustomerField(partner, field) || "").trim();
        }
        return values;
    },

    getQatarCustomerValue(field) {
        return this.getQatarCustomerValues()[field] || "";
    },

    getQatarMapSearchUrl() {
        const values = this.getQatarCustomerValues();

        if (values.gps_latitude && values.gps_longitude) {
            if (
                this.qatarCustomer.partnerId ===
                    (this.currentOrder?.getPartner?.()?.id || false)
                && this.qatarCustomer.googleMapsUrl
            ) {
                return this.qatarCustomer.googleMapsUrl;
            }

            return `https://www.google.com/maps?q=${encodeURIComponent(
                values.gps_latitude + "," + values.gps_longitude
            )}`;
        }
        return "#";
    },

    getQatarCustomerSignature(
        values = this.getQatarCustomerValues(),
        partnerId = this.currentOrder?.getPartner?.()?.id || false
    ) {
        return JSON.stringify({ partner_id: partnerId, values });
    },

    hasQatarCustomerData() {
        const values = this.getQatarCustomerValues();
        return CUSTOMER_FIELDS.some((field) =>
            String(values[field] || "").trim()
        );
    },

    isQatarCustomerDirty() {
        const values = this.getQatarCustomerValues();
        const hasData = CUSTOMER_FIELDS.some((field) =>
            String(values[field] || "").trim()
        );

        if (!hasData) return false;

        const partner = this.currentOrder?.getPartner?.();
        if (
            partner
            && this.qatarCustomer.partnerId !== partner.id
            && !this.qatarCustomer.originalSignature
        ) {
            return false;
        }

        if (!this.qatarCustomer.originalSignature) return true;
        return this.getQatarCustomerSignature()
            !== this.qatarCustomer.originalSignature;
    },

    confirmQatarCustomerSave(onConfirm, onCancel = () => {}) {
        this.dialog.add(ConfirmationDialog, {
            title: _t("Save customer changes?"),
            body: _t("Customer details have changed. Do you want to save these changes to the customer record?"),
            confirmLabel: _t("Save changes"),
            cancelLabel: _t("Don't save"),
            confirm: async () => {
                try {
                    await onConfirm();
                } catch (error) {
                    this.notification.add(
                        error?.data?.message
                            || error?.message
                            || _t("Could not complete the customer action."),
                        { type: "danger" }
                    );
                }
            },
            cancel: async () => {
                await onCancel();
            },
        });
    },

    async resolveQatarAddress() {
        const values = this.getQatarCustomerValues();

        if (!values.zone_no || !values.street_no || !values.building_no) {
            this.notification.add(
                _t("Enter Zone, Street and Building numbers first."),
                { type: "warning" }
            );
            return false;
        }

        this.qatarCustomer.resolvingAddress = true;

        try {
            // Step 1: Get the verified building coordinates from QNAS.
            const result = await this.orm.call(
                "res.partner",
                "pos_qatar_geocode_address",
                [values.zone_no, values.street_no, values.building_no]
            );

            if (
                !result
                || result.verified !== true
                || result.latitude === false
                || result.longitude === false
                || result.latitude === undefined
                || result.longitude === undefined
            ) {
                this.qatarCustomer.gps_latitude = "";
                this.qatarCustomer.gps_longitude = "";
                this.qatarCustomer.googleMapsUrl = "";
                this.qatarCustomer.location_description_ar =
                    result?.place_name || _t("غير معرف");
                this.qatarCustomer.savedSignature = "";

                this.notification.add(
                    _t("العنوان غير معرف: راجع أرقام المنطقة والشارع والمبنى."),
                    { type: "info" }
                );
                return false;
            }

            // Save the QNAS coordinates immediately. Arabic reverse geocoding
            // is an additional step and must not invalidate a valid QNAS pin.
            this.qatarCustomer.gps_latitude = String(result.latitude);
            this.qatarCustomer.gps_longitude = String(result.longitude);
            this.qatarCustomer.googleMapsUrl =
                result.google_maps_url
                || `https://www.google.com/maps?q=${result.latitude},${result.longitude}`;

            // Step 2: Use structured Arabic address returned by the server.
            let address = result.address || {};
            let arabicAddress = formatArabicAddress(address);

            // Compatibility fallback: call the reverse-geocode method
            // separately if the QNAS response did not include address data.
            if (!arabicAddress) {
                try {
                    const reverseResult = await this.orm.call(
                        "res.partner",
                        "pos_qatar_reverse_geocode",
                        [result.latitude, result.longitude]
                    );

                    address = reverseResult || {};
                    arabicAddress =
                        reverseResult?.formatted_address
                        || formatArabicAddress(reverseResult);
                } catch (reverseError) {
                    // QNAS coordinates remain valid even if this service fails.
                    console.warn(
                        "Qatar Arabic reverse geocoding failed:",
                        reverseError
                    );
                }
            }

            if (arabicAddress) {
                // Put the readable Arabic address in the existing street field.
                // This avoids requiring additional database fields or XML fields.
                this.qatarCustomer.street = arabicAddress;
                this.qatarCustomer.location_description_ar = arabicAddress;
            } else {
                this.qatarCustomer.location_description_ar =
                    result.place_name
                    || `المنطقة ${values.zone_no}، الشارع ${values.street_no}، المبنى ${values.building_no}`;
            }

            this.qatarCustomer.savedSignature = "";

            if (arabicAddress) {
                this.notification.add(
                    _t("تم تحديد الموقع وجلب المنطقة والشارع والمدينة بالعربي."),
                    { type: "success" }
                );
            } else {
                this.notification.add(
                    _t("تم تحديد إحداثيات المبنى من QNAS، لكن تعذر جلب تفاصيل العنوان بالعربي."),
                    { type: "warning" }
                );
            }

            return true;
        } catch (error) {
            this.notification.add(
                error?.data?.message
                    || error?.message
                    || _t("Could not resolve this Qatar address."),
                { type: "danger" }
            );
            return false;
        } finally {
            this.qatarCustomer.resolvingAddress = false;
        }
    },

    onQatarCustomerInput(field, event) {
        const partner = this.currentOrder?.getPartner?.();
        const partnerId = partner?.id || false;

        if (this.qatarCustomer.partnerId !== partnerId) {
            for (const key of CUSTOMER_FIELDS) {
                this.qatarCustomer[key] = String(
                    partnerCustomerField(partner, key) || ""
                );
            }

            this.qatarCustomer.partnerId = partnerId;
            this.qatarCustomer.savedSignature = "";
            this.qatarCustomer.originalSignature = partner
                ? this.getQatarCustomerSignature(
                    Object.fromEntries(
                        CUSTOMER_FIELDS.map((key) => [
                            key,
                            String(partnerCustomerField(partner, key) || "").trim(),
                        ])
                    ),
                    partnerId
                )
                : "";
        }

        const newValue = String(event.target.value || "");

        if (field === "phone" && partner) {
            const oldPhone = normalizeQatarPhone(
                partner.mobile || partner.phone || ""
            );
            const newPhone = normalizeQatarPhone(newValue);

            if (newPhone && newPhone !== oldPhone) {
                this.currentOrder.setPartner(false);
                this.qatarCustomer.partnerId = false;
                this.qatarCustomer.originalSignature = "";
                this.qatarCustomer.googleMapsUrl = "";

                for (const key of CUSTOMER_FIELDS) {
                    if (key !== "phone") {
                        this.qatarCustomer[key] = "";
                    }
                }
            }
        }

        this.qatarCustomer[field] = newValue;
        this.qatarCustomer.savedSignature = "";

        if (["zone_no", "street_no", "building_no"].includes(field)) {
            this.qatarCustomer.gps_latitude = "";
            this.qatarCustomer.gps_longitude = "";
            this.qatarCustomer.googleMapsUrl = "";
            this.qatarCustomer.location_description_ar = "";
        }

        if (field === "phone") {
            if (this.qatarPhoneLookupTimer) {
                clearTimeout(this.qatarPhoneLookupTimer);
            }

            const digits = normalizeQatarPhone(newValue);
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
        const phone = String(
            this.qatarCustomer.phone
            || this.getQatarCustomerValue("phone")
            || ""
        ).trim();

        const digits = normalizeQatarPhone(phone);
        if (digits.length < 8 || this.qatarCustomer.lookingUp) return;

        const currentPartner = this.currentOrder?.getPartner?.();
        const currentDigits = normalizeQatarPhone(
            currentPartner?.mobile || currentPartner?.phone || ""
        );

        if (currentPartner && currentDigits === digits) {
            this.qatarCustomer.partnerId = currentPartner.id;
            for (const field of CUSTOMER_FIELDS) {
                this.qatarCustomer[field] = String(
                    partnerCustomerField(currentPartner, field) || ""
                );
            }
            return;
        }

        this.qatarCustomer.lookingUp = true;

        try {
            const result = await this.orm.call(
                "res.partner",
                "pos_find_qatar_customer_by_phone",
                [phone]
            );

            if (normalizeQatarPhone(this.qatarCustomer.phone) !== digits) {
                return;
            }

            if (!result) {
                this.notification.add(
                    _t("No customer found for this phone. Enter the remaining details to create a customer."),
                    { type: "info" }
                );
                return;
            }

            const partnerModel = this.pos?.models?.["res.partner"];
            const partner = applyServerCustomerToPos(partnerModel, result);
            this.currentOrder.setPartner(partner);
            this.qatarCustomer.partnerId = result.id;

            for (const field of CUSTOMER_FIELDS) {
                this.qatarCustomer[field] = result[field] || "";
            }

            this.qatarCustomer.savedSignature = "";
            this.qatarCustomer.originalSignature =
                this.getQatarCustomerSignature(
                    Object.fromEntries(
                        CUSTOMER_FIELDS.map((field) => [
                            field,
                            String(result[field] || "").trim(),
                        ])
                    ),
                    result.id
                );

            this.qatarCustomer.googleMapsUrl =
                result.gps_latitude && result.gps_longitude
                    ? `https://www.google.com/maps?q=${result.gps_latitude},${result.gps_longitude}`
                    : "";

            this.notification.add(
                _t("Customer data loaded automatically."),
                { type: "success" }
            );
        } catch (error) {
            this.notification.add(
                error?.data?.message
                    || error?.message
                    || _t("Could not search by phone number."),
                { type: "danger" }
            );
        } finally {
            this.qatarCustomer.lookingUp = false;

            const latestDigits = normalizeQatarPhone(
                this.qatarCustomer.phone || ""
            );

            if (latestDigits.length >= 8 && latestDigits !== digits) {
                Promise.resolve().then(() =>
                    this.lookupQatarCustomerByPhone()
                );
            }
        }
    },

    async saveQatarCustomer(showMessage = true) {
        const values = this.getQatarCustomerValues();
        const missing = REQUIRED_FIELDS.filter((field) => !values[field]);

        if (missing.length) {
            this.notification.add(
                _t("Required: customer name, phone, Zone No, Street No and Building No. GPS is optional."),
                { type: "warning" }
            );
            return false;
        }

        const currentPartner = this.currentOrder.getPartner();
        const signature = JSON.stringify({
            partner_id: currentPartner?.id || false,
            values,
        });

        if (
            this.qatarCustomer.savedSignature === signature
            && currentPartner
        ) {
            return true;
        }

        this.qatarCustomer.saving = true;

        try {
            const result = await this.orm.call(
                "res.partner",
                "pos_create_or_update_qatar_customer",
                [{
                    ...values,
                    partner_id: currentPartner?.id
                        || this.qatarCustomer.partnerId
                        || false,
                }]
            );

            const partnerModel = this.pos?.models?.["res.partner"];
            const partner = applyServerCustomerToPos(partnerModel, result);
            this.currentOrder.setPartner(partner);
            this.qatarCustomer.partnerId = result.id;

            for (const field of CUSTOMER_FIELDS) {
                this.qatarCustomer[field] = result[field] || "";
            }

            this.qatarCustomer.savedSignature = JSON.stringify({
                partner_id: result.id,
                values: this.getQatarCustomerValues(),
            });
            this.qatarCustomer.originalSignature =
                this.qatarCustomer.savedSignature;

            this.qatarCustomer.googleMapsUrl =
                result.gps_latitude && result.gps_longitude
                    ? `https://www.google.com/maps?q=${result.gps_latitude},${result.gps_longitude}`
                    : "";

            if (showMessage) {
                this.notification.add(
                    result.created
                        ? _t("تم إنشاء العميل")
                        : _t("تم تحديث بيانات العميل"),
                    { type: "success" }
                );
            }

            return true;
        } catch (error) {
            this.notification.add(
                error?.data?.message
                    || error?.message
                    || _t("Could not save customer."),
                { type: "danger" }
            );
            return false;
        } finally {
            this.qatarCustomer.saving = false;
        }
    },

    async onClickValidate(...args) {
        const validateOrder = async () => {
            const remaining = this.currentOrder.remainingDue || 0;

            if (
                !this.pos.currency.isZero(remaining)
                && remaining > 0
            ) {
                this.notification.add(
                    _t("Remaining amount must be fully paid before validating the order."),
                    { type: "warning" }
                );
                return;
            }

            return await super.onClickValidate(...args);
        };

        const saveAndValidate = async () => {
            const saved = await this.saveQatarCustomer(true);
            if (saved) return await validateOrder();
        };

        if (this.isQatarCustomerDirty()) {
            this.confirmQatarCustomerSave(
                saveAndValidate,
                validateOrder
            );
            return;
        }

        if (
            !this.hasQatarCustomerData()
            || this.currentOrder?.getPartner?.()
        ) {
            return await validateOrder();
        }

        const saved = await this.saveQatarCustomer(true);
        if (saved) return await validateOrder();
    },

    async onClickSaveQatarCustomer() {
        const save = () => this.saveQatarCustomer(true);

        if (this.isQatarCustomerDirty()) {
            this.confirmQatarCustomerSave(save);
            return;
        }

        return await save();
    },
});
