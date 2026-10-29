import * as Chrome from "@point_of_sale/../tests/pos/tours/utils/chrome_util";
import * as Dialog from "@point_of_sale/../tests/generic_helpers/dialog_util";
import * as Order from "@point_of_sale/../tests/generic_helpers/order_widget_util";
import * as ProductScreen from "@point_of_sale/../tests/pos/tours/utils/product_screen_util";
import { negate } from "@point_of_sale/../tests/generic_helpers/utils";
import { registry } from "@web/core/registry";

function badgeIs(productName, qty) {
    return {
        content: `stock badge of '${productName}' shows ${qty}`,
        trigger: `article.product:has(.product-name:contains("${productName}")) .o_stock_guard_badge:contains("${qty}")`,
    };
}

registry.category("web_tour.tours").add("PosStockGuardBlockTour", {
    steps: () =>
        [
            Chrome.startPoS(),
            Dialog.confirm("Open Register"),
            badgeIs("Guard Desk", "2"),
            badgeIs("Guard Empty", "0"),
            {
                content: "services have no stock badge",
                trigger: negate(
                    `article.product:has(.product-name:contains("Guard Fee")) .o_stock_guard_badge`
                ),
            },
            ProductScreen.clickDisplayedProduct("Guard Desk"),
            ProductScreen.selectedOrderlineHas("Guard Desk", "1"),
            badgeIs("Guard Desk", "1"),
            ProductScreen.clickDisplayedProduct("Guard Desk"),
            ProductScreen.selectedOrderlineHas("Guard Desk", "2"),
            badgeIs("Guard Desk", "0"),
            ProductScreen.clickDisplayedProduct("Guard Desk"),
            Dialog.is({ title: "Out of stock" }),
            Dialog.confirm(),
            ProductScreen.selectedOrderlineHas("Guard Desk", "2"),
            ProductScreen.clickNumpad("5"),
            Dialog.is({ title: "Out of stock" }),
            Dialog.confirm(),
            ProductScreen.selectedOrderlineHas("Guard Desk", "2"),
            ProductScreen.clickDisplayedProduct("Guard Empty"),
            Dialog.is({ title: "Out of stock" }),
            Dialog.confirm(),
            Order.doesNotHaveLine({ productName: "Guard Empty" }),
            ProductScreen.clickDisplayedProduct("Guard Fee"),
            ProductScreen.selectedOrderlineHas("Guard Fee", "1"),
        ].flat(),
});

registry.category("web_tour.tours").add("PosStockGuardConfirmTour", {
    steps: () =>
        [
            Chrome.startPoS(),
            Dialog.confirm("Open Register"),
            badgeIs("Guard Empty", "0"),
            ProductScreen.clickDisplayedProduct("Guard Empty"),
            Dialog.is({ title: "Not enough stock" }),
            Dialog.confirm("Add anyway"),
            ProductScreen.selectedOrderlineHas("Guard Empty", "1"),
            badgeIs("Guard Empty", "-1"),
        ].flat(),
});
