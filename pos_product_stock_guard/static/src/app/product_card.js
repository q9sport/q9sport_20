import { ProductCard } from "@point_of_sale/app/components/product_card/product_card";
import { patch } from "@web/core/utils/patch";

patch(ProductCard.prototype, {
    /** Available quantity for the badge, or null when there is nothing to show. */
    get stockGuardQty() {
        const pos = this.env.services.pos;
        if (!pos?.config?.stock_guard_show || this.props.isComboPopup || !this.props.product) {
            return null;
        }
        return pos.stockGuardCardQty(this.props.product);
    },

    get stockGuardQtyText() {
        return this.env.services.pos.formatProductQty(this.stockGuardQty ?? 0, false);
    },
    // Product Barcode
    get stockGuardBarcode() {
        return this.props?.product?.barcode || "";
    },
   
});
