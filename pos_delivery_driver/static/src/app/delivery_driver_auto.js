/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";

import { PosStore } from "@point_of_sale/app/services/pos_store";

import { makeAwaitable } from "@point_of_sale/app/utils/make_awaitable_dialog";

import { DeliveryDriverPopup } from "@pos_delivery_driver/app/delivery_driver_popup";


// ============================================================
// DELIVERY PRODUCT SETTINGS
// ============================================================

const DELIVERY_PRODUCT_ID = 38;
const DELIVERY_PRODUCT_BARCODE = "10002";


// منع تكرار معالجة نفس الطلب أثناء فتح النافذة.
const processingOrders = new WeakSet();


// ============================================================
// CHECK PRODUCT ID / BARCODE
// ============================================================

function matchesDeliveryProduct(product) {

    if (!product) {
        return false;
    }

    const productId = Number(product.id || 0);

    const barcode = String(product.barcode || "").trim();

    return (
        productId === DELIVERY_PRODUCT_ID ||
        barcode === DELIVERY_PRODUCT_BARCODE
    );
}


// ============================================================
// IDENTIFY DELIVERY PRODUCT
// يدعم المنتج نفسه أو قالب المنتج ومتغيراته.
// ============================================================

function isDeliveryProduct(vals) {

    if (!vals) {
        return false;
    }

    const product = vals.product_id || null;

    const template =
        vals.product_tmpl_id ||
        product?.product_tmpl_id ||
        null;

    // فحص المنتج مباشرة.
    if (matchesDeliveryProduct(product)) {
        return true;
    }

    // فحص قالب المنتج.
    if (matchesDeliveryProduct(template)) {
        return true;
    }

    // فحص متغيرات قالب المنتج.
    const variants = template?.product_variant_ids || [];

    if (
        Array.isArray(variants) &&
        variants.some((variant) => matchesDeliveryProduct(variant))
    ) {
        return true;
    }

    return false;
}


// ============================================================
// CHECK EXISTING DELIVERY LINE
// Odoo 20: order.lines
// ============================================================

function isDeliveryAlreadyInOrder(order) {

    if (!order || !Array.isArray(order.lines)) {
        return false;
    }

    return order.lines.some((line) => {

        const product = line.product_id;

        if (matchesDeliveryProduct(product)) {
            return true;
        }

        // احتياطاً إذا كان التعرف على المنتج من خلال قالب المنتج.
        if (matchesDeliveryProduct(product?.product_tmpl_id)) {
            return true;
        }

        return false;
    });
}


// ============================================================
// GET ORDER NUMBER
// ============================================================

function getOrderName(order) {

    if (!order) {
        return "طلب جديد";
    }

    if (order.tracking_number) {
        return String(order.tracking_number);
    }

    if (order.name && order.name !== "/") {
        return String(order.name);
    }

    if (order.pos_reference) {
        return String(order.pos_reference);
    }

    if (order.uuid) {
        return String(order.uuid).substring(0, 8);
    }

    return "طلب جديد";
}


// ============================================================
// SHOW DUPLICATE REMINDER
// ============================================================

async function showAlreadyAddedReminder(store, order) {

    return await makeAwaitable(
        store.dialog,
        DeliveryDriverPopup,
        {
            orderName: getOrderName(order),
            alreadyAdded: true,
            isCashOnDelivery: Boolean(
                order.is_cash_on_delivery
            ),
        }
    );
}


// ============================================================
// POS STORE PATCH
// اعتراض جميع طرق إضافة المنتج:
// - اختيار المنتج من شاشة POS.
// - مسح الباركود.
// - الإضافة من المسارات التي تستخدم addLineToCurrentOrder.
// ============================================================

patch(PosStore.prototype, {

    async addLineToCurrentOrder(vals, opts = {}, configure = true) {

        // المنتجات العادية تعمل بالطريقة الأصلية دون أي تغيير.
        if (!isDeliveryProduct(vals)) {

            return await super.addLineToCurrentOrder(
                vals,
                opts,
                configure
            );
        }

        const order = this.getOrder();

        // إذا لم يوجد طلب، نترك Odoo ينفذ الإضافة بالطريقة الأصلية.
        if (!order) {

            return await super.addLineToCurrentOrder(
                vals,
                opts,
                configure
            );
        }

        // منع معالجة نفس الطلب أكثر من مرة أثناء فتح النافذة.
        if (processingOrders.has(order)) {
            return false;
        }

        processingOrders.add(order);

        try {

            // ====================================================
            // CHECK DUPLICATE
            // ====================================================

            if (isDeliveryAlreadyInOrder(order)) {

                await showAlreadyAddedReminder(this, order);

                // لا نضيف المنتج مرة ثانية.
                return false;
            }


            // ====================================================
            // SHOW DELIVERY REMINDER
            // ====================================================

            const payload = await makeAwaitable(
                this.dialog,
                DeliveryDriverPopup,
                {
                    orderName: getOrderName(order),
                    alreadyAdded: false,
                    isCashOnDelivery: Boolean(
                        order.is_cash_on_delivery
                    ),
                }
            );


            // ====================================================
            // CANCEL
            // ====================================================

            if (!payload || !payload.confirmed) {
                return false;
            }


            // ====================================================
            // CHECK AGAIN BEFORE ADDING
            // ====================================================

            if (isDeliveryAlreadyInOrder(order)) {

                await showAlreadyAddedReminder(this, order);

                return false;
            }


            // ====================================================
            // ADD DELIVERY PRODUCT
            // ====================================================

            // لا نحدد سعر المنتج.
            // لا نغير الكمية.
            // لا نعين مندوب توصيل.
            // نترك Odoo ينفذ الإضافة والسعر بالطريقة الأصلية.

            const result = await super.addLineToCurrentOrder(
                vals,
                opts,
                configure
            );


            // ====================================================
            // CASH ON DELIVERY
            // ====================================================

            // الاحتفاظ بخيار الدفع عند الاستلام إذا كان الحقل
            // موجوداً في موديل الطلب المخصص لديك.

            if (
                result &&
                "is_cash_on_delivery" in order
            ) {

                order.is_cash_on_delivery = Boolean(
                    payload.isCashOnDelivery
                );

                if (typeof order._markDirty === "function") {
                    order._markDirty();
                }
            }


            return result;

        } catch (error) {

            console.error(
                "POS Delivery Service Error:",
                error
            );

            throw error;

        } finally {

            processingOrders.delete(order);

        }
    },

});
