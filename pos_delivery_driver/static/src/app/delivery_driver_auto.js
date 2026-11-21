/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { PosStore } from "@point_of_sale/app/services/pos_store";
import { PosOrderline } from "@point_of_sale/app/models/pos_order_line";

// ============================================================
// DELIVERY PRODUCT CONFIGURATION
// ============================================================

const DELIVERY_PRODUCT_ID = 38;
const DELIVERY_PRODUCT_BARCODE = "10002";

const processingOrders = new WeakSet();

let notificationService = null;

// ============================================================
// NOTIFICATIONS - NATIVE ODOO 20
// ============================================================

function showDeliveryNotification(type, title, message) {
    if (!notificationService?.add) {
        return;
    }

    notificationService.add(message, {
        title,
        type,
        sticky: false,
    });
}

// ============================================================
// PRODUCT IDENTIFICATION
// ============================================================

function matchesDeliveryProduct(product) {
    if (!product) {
        return false;
    }

    if (typeof product === "number" || typeof product === "string") {
        return Number(product) === DELIVERY_PRODUCT_ID;
    }

    return (
        Number(product.id || 0) === DELIVERY_PRODUCT_ID ||
        String(product.barcode || "").trim() === DELIVERY_PRODUCT_BARCODE
    );
}

function isDeliveryProduct(values) {
    if (!values) {
        return false;
    }

    const candidates = [
        values,
        values.product_id,
        values.product_tmpl_id,
        values.product_id?.product_tmpl_id,
    ];

    for (const product of candidates) {
        if (matchesDeliveryProduct(product)) {
            return true;
        }

        const variants = product?.product_variant_ids || [];

        if (
            Array.isArray(variants) &&
            variants.some(matchesDeliveryProduct)
        ) {
            return true;
        }
    }

    return false;
}

// ============================================================
// EXISTING DELIVERY LINE
// ============================================================

function getDeliveryLine(order) {
    if (!order || !Array.isArray(order.lines)) {
        return false;
    }

    return (
        order.lines.find((line) =>
            isDeliveryProduct({
                product_id: line.product_id,
            })
        ) || false
    );
}

// ============================================================
// POS STORE PATCH
// ============================================================

patch(PosStore.prototype, {

    async addLineToCurrentOrder(vals, opts = {}, configure = true) {

        // Keep native Odoo notification service available
        notificationService = this.notification;

        // Normal products: original Odoo behavior
        if (!isDeliveryProduct(vals)) {
            return await super.addLineToCurrentOrder(
                vals,
                opts,
                configure
            );
        }

        const order = this.getOrder();

        // Always force delivery quantity to one
        const deliveryValues = {
            ...vals,
            qty: 1,
        };

        if (!order) {
            const result = await super.addLineToCurrentOrder(
                deliveryValues,
                opts,
                configure
            );

            if (result) {
                showDeliveryNotification(
                    "success",
                    "🚚 Added Successfully",
               "Delivery service has been successfully added to the order."
                );
            }

            return result;
        }

        // Prevent simultaneous additions
        if (processingOrders.has(order)) {
            showDeliveryNotification(
                "warning",
                "⏳ Please Wait",
"Delivery service is being processed for this order."
            );

            return false;
        }

        processingOrders.add(order);

        try {

            // Prevent duplicate delivery service
            if (getDeliveryLine(order)) {

                showDeliveryNotification(
                    "warning",
                    "🚚 Delivery Service Already Added",
"Delivery service can only be added once per order."
                );

                return false;
            }

            // Add delivery once, with quantity = 1
            const result = await super.addLineToCurrentOrder(
                deliveryValues,
                opts,
                configure
            );

            if (result) {
                showDeliveryNotification(
                    "success",
                   "✓ Delivery Service Added",
"Delivery service has been successfully added. The quantity is fixed and cannot be increased."
                );
            }

            return result;

        } catch (error) {

            console.error(
                "POS Delivery Driver Error:",
                error
            );

            throw error;

        } finally {
            processingOrders.delete(order);
        }
    },
});

// ============================================================
// POS ORDER LINE - LOCK DELIVERY QUANTITY
// ============================================================

patch(PosOrderline.prototype, {

    setQuantity(quantity, keepPrice) {

        if (isDeliveryProduct(this.product_id)) {

            const requestedQuantity = Number(quantity);

            // Allow deleting the delivery service
            if (requestedQuantity === 0) {
                return super.setQuantity(quantity, keepPrice);
            }

            // Allow only one unit (also allow -1 for return orders)
            if (
                !Number.isFinite(requestedQuantity) ||
                Math.abs(requestedQuantity) !== 1
            ) {

                showDeliveryNotification(
                    "warning",
                    "🔒 Quantity Cannot Be Modified",
                    "Delivery service can only be added once. Its quantity cannot be increased or decreased."
                );

                return false;
            }
        }

        return super.setQuantity(quantity, keepPrice);
    },
});


// ============================================================
// POS ORDER LINE - LOCK DELIVERY QUANTITY
// ============================================================

// patch(PosOrderline.prototype, {

//     setQuantity(quantity, keepPrice) {

//         if (isDeliveryProduct(this.product_id)) {

//             const requestedQuantity = Number(quantity);

//             // Allow only one unit (also allow -1 for return orders)
//             if (
//                 !Number.isFinite(requestedQuantity) ||
//                 Math.abs(requestedQuantity) !== 1
//             ) {

//                 showDeliveryNotification(
//                     "warning",
//                   "🔒 Quantity Cannot Be Modified",
// "Delivery service can only be added once. Its quantity cannot be increased or decreased."
//                 );

//                 return false;
//             }
//         }

//         return super.setQuantity(
//             quantity,
//             keepPrice
//         );
//     },
// });

// /** @odoo-module **/
// import { patch } from "@web/core/utils/patch";
// import { PosStore } from "@point_of_sale/app/services/pos_store";
// import { makeAwaitable } from "@point_of_sale/app/utils/make_awaitable_dialog";
// import { DeliveryDriverPopup } from "@pos_delivery_driver/app/delivery_driver_popup";

// // ============================================================
// // DELIVERY PRODUCT SETTINGS
// // ============================================================

// const DELIVERY_PRODUCT_ID = 38;
// const DELIVERY_PRODUCT_BARCODE = "10002";

// const processingOrders = new WeakSet();


// // ============================================================
// // CHECK PRODUCT ID / BARCODE
// // ============================================================

// function matchesDeliveryProduct(product) {
//     if (!product) {
//         return false;
//     }

//     return (
//         Number(product.id || 0) === DELIVERY_PRODUCT_ID ||
//         String(product.barcode || "").trim() === DELIVERY_PRODUCT_BARCODE
//     );
// }


// // ============================================================
// // IDENTIFY DELIVERY PRODUCT
// // ============================================================

// function isDeliveryProduct(vals) {
//     if (!vals) {
//         return false;
//     }

//     const product = vals.product_id || null;

//     const template =
//         vals.product_tmpl_id ||
//         product?.product_tmpl_id ||
//         null;

//     if (matchesDeliveryProduct(product)) {
//         return true;
//     }

//     if (matchesDeliveryProduct(template)) {
//         return true;
//     }

//     const variants = template?.product_variant_ids || [];

//     return (
//         Array.isArray(variants) &&
//         variants.some((variant) => matchesDeliveryProduct(variant))
//     );
// }


// // ============================================================
// // FIND EXISTING DELIVERY LINE
// // ============================================================

// function getDeliveryLine(order) {
//     if (!order || !Array.isArray(order.lines)) {
//         return false;
//     }

//     return order.lines.find((line) => {
//         return (
//             matchesDeliveryProduct(line.product_id) ||
//             matchesDeliveryProduct(line.product_id?.product_tmpl_id)
//         );
//     }) || false;
// }


// // ============================================================
// // GET ORDER NAME
// // ============================================================

// function getOrderName(order) {
//     if (!order) {
//         return "New Order";
//     }

//     if (order.tracking_number) {
//         return String(order.tracking_number);
//     }

//     if (order.name && order.name !== "/") {
//         return String(order.name);
//     }

//     if (order.pos_reference) {
//         return String(order.pos_reference);
//     }

//     if (order.uuid) {
//         return String(order.uuid).substring(0, 8);
//     }

//     return "New Order";
// }


// // ============================================================
// // SHOW DUPLICATE REMINDER
// // ============================================================

// async function showAlreadyAddedReminder(store, order) {
//     return await makeAwaitable(
//         store.dialog,
//         DeliveryDriverPopup,
//         {
//             orderName: getOrderName(order),
//             alreadyAdded: true,
//             isCashOnDelivery: Boolean(
//                 order.is_cash_on_delivery
//             ),
//             employees: [],
//             selectedDriverId: false,
//         }
//     );
// }


// // ============================================================
// // INCREASE DELIVERY QUANTITY ONLY
// // ============================================================

// async function increaseDeliveryQuantity(line) {
//     if (!line) {
//         return false;
//     }

//     const currentQty = Number(line.get_quantity());

//     // تغيير الكمية فقط مع الاحتفاظ بالسعر والخصم والضرائب.
//     line.set_quantity(currentQty + 1);

//     return true;
// }


// // ============================================================
// // POS STORE PATCH
// // ============================================================

// patch(PosStore.prototype, {

//     async addLineToCurrentOrder(vals, opts = {}, configure = true) {

//         // المنتجات العادية: سلوك Odoo الأصلي.
//         if (!isDeliveryProduct(vals)) {
//             return await super.addLineToCurrentOrder(
//                 vals,
//                 opts,
//                 configure
//             );
//         }

//         const order = this.getOrder();

//         if (!order) {
//             return await super.addLineToCurrentOrder(
//                 vals,
//                 opts,
//                 configure
//             );
//         }

//         // منع التداخل أثناء معالجة نفس الطلب.
//         if (processingOrders.has(order)) {
//             return false;
//         }

//         processingOrders.add(order);

//         try {

//             // ====================================================
//             // CHECK EXISTING DELIVERY LINE
//             // ====================================================

//             const existingLine = getDeliveryLine(order);

//             if (existingLine) {

//                 // لا نضيف سطراً جديداً.
//                 // لا نفتح نافذة التأكيد مرة أخرى.
//                 // نزيد الكمية فقط.

//                 await increaseDeliveryQuantity(existingLine);

//                 return existingLine;
//             }


//             // ====================================================
//             // FIRST TIME: SHOW DELIVERY POPUP
//             // ====================================================

//             let employees = [];

//             try {
//                 employees = await this.orm.call(
//                     "pos.order",
//                     "get_active_delivery_employees",
//                     [],
//                     {}
//                 );
//             } catch (error) {
//                 console.warn(
//                     "Could not load delivery drivers:",
//                     error
//                 );
//             }

//             const payload = await makeAwaitable(
//                 this.dialog,
//                 DeliveryDriverPopup,
//                 {
//                     orderName: getOrderName(order),
//                     alreadyAdded: false,
//                     isCashOnDelivery:
//                         Boolean(order.is_cash_on_delivery),
//                     employees: employees,
//                     selectedDriverId:
//                         order.delivery_employee_id || false,
//                 }
//             );


//             // ====================================================
//             // CANCEL
//             // ====================================================

//             if (!payload || !payload.confirmed) {
//                 return false;
//             }


//             // ====================================================
//             // CHECK AGAIN
//             // ====================================================

//             const lineAfterPopup = getDeliveryLine(order);

//             if (lineAfterPopup) {
//                 await increaseDeliveryQuantity(lineAfterPopup);
//                 return lineAfterPopup;
//             }


//             // ====================================================
//             // ADD DELIVERY PRODUCT
//             // ====================================================

//             const result = await super.addLineToCurrentOrder(
//                 vals,
//                 opts,
//                 configure
//             );


//             // ====================================================
//             // SAVE PAYMENT OPTION
//             // ====================================================

//             if (result) {

//                 if ("is_cash_on_delivery" in order) {
//                     order.is_cash_on_delivery =
//                         Boolean(payload.isCashOnDelivery);
//                 }

//                 if (typeof order._markDirty === "function") {
//                     order._markDirty();
//                 } else if (typeof order.setDirty === "function") {
//                     order.setDirty();
//                 }

//                 if (typeof this.save_to_db === "function") {
//                     await this.save_to_db();
//                 }
//             }

//             return result;

//         } catch (error) {

//             console.error(
//                 "POS Delivery Service Error:",
//                 error
//             );

//             throw error;

//         } finally {

//             processingOrders.delete(order);

//         }
//     },

// });


// /** @odoo-module **/
// import { patch } from "@web/core/utils/patch";

// import { PosStore } from "@point_of_sale/app/services/pos_store";

// import { makeAwaitable } from
//     "@point_of_sale/app/utils/make_awaitable_dialog";

// import { DeliveryDriverPopup } from
//     "@pos_delivery_driver/app/delivery_driver_popup";


// const DELIVERY_PRODUCT_ID = 38;
// const DELIVERY_PRODUCT_BARCODE = "10002";

// const processingOrders = new WeakSet();


// function getProduct(vals) {
//     const product = vals?.product_id;

//     if (product && typeof product === "object") {
//         return product;
//     }

//     return false;
// }


// function isDeliveryProduct(vals) {
//     const product = getProduct(vals);

//     if (!product) {
//         return false;
//     }

//     const productId = Number(product.id || 0);

//     const barcode = String(
//         product.barcode || ""
//     ).trim();

//     return (
//         productId === DELIVERY_PRODUCT_ID ||
//         barcode === DELIVERY_PRODUCT_BARCODE
//     );
// }


// function isDeliveryAlreadyInOrder(order) {
//     const lines = order?.lines || [];

//     return lines.some((line) => {
//         const product = line.product_id;

//         if (!product) {
//             return false;
//         }

//         return (
//             Number(product.id || 0) === DELIVERY_PRODUCT_ID ||
//             String(product.barcode || "").trim() === DELIVERY_PRODUCT_BARCODE
//         );
//     });
// }


// function getOrderLabel(order) {
//     if (!order) {
//         return "New Order";
//     }

//     if (order.tracking_number) {
//         return String(order.tracking_number);
//     }

//     if (order.name && order.name !== "/") {
//         return String(order.name);
//     }

//     if (order.pos_reference) {
//         return String(order.pos_reference);
//     }

//     if (order.uuid) {
//         return String(order.uuid).substring(0, 8);
//     }

//     return "New Order";
// }


// patch(PosStore.prototype, {

//     async addLineToCurrentOrder(vals, opts = {}, configure = true) {

//         // Normal products are not affected.
//         if (!isDeliveryProduct(vals)) {
//             return await super.addLineToCurrentOrder(...arguments);
//         }

//         const order = this.getOrder();

//         if (!order) {
//             return await super.addLineToCurrentOrder(...arguments);
//         }

//         // Prevent duplicate popup for the same order.
//         if (processingOrders.has(order)) {
//             return false;
//         }

//         processingOrders.add(order);

//         try {

//             // Prevent adding delivery product more than once.
//             if (isDeliveryAlreadyInOrder(order)) {

//                 await makeAwaitable(
//                     this.dialog,
//                     DeliveryDriverPopup,
//                     {
//                         orderName: getOrderLabel(order),
//                         alreadyAdded: true,
//                         isCashOnDelivery:
//                             order.is_cash_on_delivery !== false,
//                         employees: [],
//                         selectedDriverId:
//                             order.delivery_employee_id || false,
//                     }
//                 );

//                 return false;
//             }

//             // Load active drivers.
//             let employees = [];

//             try {
//                 employees = await this.orm.call(
//                     "pos.order",
//                     "get_active_delivery_employees",
//                     [],
//                     {}
//                 );
//             } catch (error) {
//                 console.warn(
//                     "Could not load delivery drivers:",
//                     error
//                 );
//             }

//             // Open delivery confirmation popup.
//             const payload = await makeAwaitable(
//                 this.dialog,
//                 DeliveryDriverPopup,
//                 {
//                     orderName: getOrderLabel(order),
//                     alreadyAdded: false,
//                     isCashOnDelivery:
//                         order.is_cash_on_delivery !== false,
//                     employees: employees,
//                     selectedDriverId:
//                         order.delivery_employee_id || false,
//                 }
//             );

//             if (!payload || !payload.confirmed) {
//                 return false;
//             }

//             // Check again before adding.
//             if (isDeliveryAlreadyInOrder(order)) {
//                 return false;
//             }

//             // Add product using Odoo native behavior.
//             // No price or quantity modifications.
//             const result = await super.addLineToCurrentOrder(
//                 ...arguments
//             );

//             if (result) {

//                 order.is_cash_on_delivery =
//                     Boolean(payload.isCashOnDelivery);

//                 order.delivery_employee_id =
//                     payload.driverId || false;

//                 order.delivery_status =
//                     payload.driverId ? "assigned" : "pending";

//                 if (typeof order._markDirty === "function") {
//                     order._markDirty();
//                 } else if (typeof order.setDirty === "function") {
//                     order.setDirty();
//                 }

//                 if (typeof this.save_to_db === "function") {
//                     await this.save_to_db();
//                 }
//             }

//             return result;

//         } catch (error) {

//             console.error(
//                 "POS Delivery Service Error:",
//                 error
//             );

//             throw error;

//         } finally {

//             processingOrders.delete(order);

//         }
//     },

// });
