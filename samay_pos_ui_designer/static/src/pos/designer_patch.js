/** @odoo-module **/
import { patch } from '@web/core/utils/patch';
import { ProductScreen } from '@point_of_sale/app/screens/product_screen/product_screen';
import { PaymentScreen } from '@point_of_sale/app/screens/payment_screen/payment_screen';
import { ControlButtons } from '@point_of_sale/app/screens/product_screen/control_buttons/control_buttons';
import { CategorySelector } from '@point_of_sale/app/components/category_selector/category_selector';
import { Chrome } from '@point_of_sale/app/pos_app';
import { normalizeDesign, themeStyle, buildLayout } from '@samay_pos_ui_designer/common/designer';

const snapshots = new WeakMap();
function design(pos) {
    if (!snapshots.has(pos)) snapshots.set(pos, normalizeDesign(pos.config.pui_payload));
    return snapshots.get(pos);
}
function screenExtension(screen) {
    return {
        get puiDesign() { return design(this.pos); },
        get puiEnabled() { return this.puiDesign.enabled; },
        get puiTheme() { return this.puiEnabled ? themeStyle(this.puiDesign, screen) : ''; },
        get puiLayout() { return buildLayout(this.puiDesign, screen); },
        get puiStyle() { return this.puiEnabled ? this.puiTheme + this.puiLayout.style : ''; },
        puiRegion(name) { return this.puiEnabled ? this.puiLayout.regionStyle(name) : ''; },
    };
}
patch(Chrome.prototype, {
    get puiEnabled() { return design(this.pos).enabled; },
});
patch(ProductScreen.prototype, screenExtension('sales'));
patch(PaymentScreen.prototype, screenExtension('payment'));
patch(ControlButtons.prototype, {
    get puiDesign() { return design(this.pos); },
    get puiEnabled() { return this.puiDesign.enabled; },
    get puiTheme() { return this.puiEnabled ? themeStyle(this.puiDesign) : ''; },
    puiActionVisible(code) { return !this.puiEnabled || this.puiDesign.actions[code]?.visible !== false; },
    puiActionStyle(code) {
        const position = this.puiDesign.action_order?.indexOf(code);
        return this.puiEnabled ? `order:${position >= 0 ? position : this.puiDesign.actions[code]?.sequence ?? 0};` : '';
    },
});
patch(CategorySelector.prototype, {
    showCategoryImg(category) {
        const d = design(this.pos);
        return !(d.enabled && d.text_only) && super.showCategoryImg(category);
    },
});
