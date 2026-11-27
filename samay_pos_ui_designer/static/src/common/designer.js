/** @odoo-module **/

// Pure helpers shared by the live preview, POS and standalone layout tests.
export const DEFAULTS = Object.freeze({
    enabled: false, sales_keypad_position: 'right', sales_actions_position: 'top',
    sales_first_panel: 'keypad', cart_side: 'left', cart_width: 36,
    action_direction: 'horizontal', density: 'comfortable',
    payment_keypad_position: 'left', payment_methods_position: 'top',
    payment_first_panel: 'methods', payment_summary_position: 'right',
    payment_inherit: true, sticky_payment: true,
    payment_primary_color: '#2563EB', payment_accent_color: '#0891B2',
    preset: 'ocean', appearance: 'light', primary_color: '#2563EB', accent_color: '#0891B2',
    background_color: '#EEF2F7', surface_color: '#FFFFFF', text_color: '#172033',
    success_color: '#047857', warning_color: '#B45309', danger_color: '#B91C1C',
    button_height: 48, keypad_height: 48, font_size: 16, product_font_size: 16,
    product_min_width: 145, spacing: 10, keypad_spacing: 6, radius: 12, shadow: 1,
    text_only: true,
});
const RANGES = {
    cart_width: [25, 55], button_height: [44, 80], keypad_height: [44, 76],
    font_size: [14, 22], product_font_size: [14, 24], product_min_width: [110, 240],
    spacing: [4, 20], keypad_spacing: [2, 12], radius: [0, 24], shadow: [0, 3],
};
const POSITIONS = ['left', 'right', 'top', 'bottom'];
const SELECTIONS = {
    sales_keypad_position: POSITIONS, sales_actions_position: POSITIONS,
    payment_keypad_position: POSITIONS, payment_methods_position: POSITIONS,
    payment_summary_position: POSITIONS, cart_side: ['left', 'right'],
    sales_first_panel: ['keypad', 'actions'], payment_first_panel: ['keypad', 'methods'],
    action_direction: ['horizontal', 'vertical'], density: ['compact', 'comfortable', 'touch'],
    appearance: ['light', 'dark'], preset: ['ocean', 'emerald', 'sunset', 'purple', 'neutral'],
};
export const ACTION_CODES = ['internal_note', 'preset', 'customer_note', 'pricelist', 'tax', 'info'];

function luminance(hex) {
    const c = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
        .map(v => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    return c[0] * .2126 + c[1] * .7152 + c[2] * .0722;
}
export function contrast(a, b) {
    const x = luminance(a), y = luminance(b);
    return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
}
export function foreground(bg) {
    return contrast(bg, '#FFFFFF') >= contrast(bg, '#000000') ? '#FFFFFF' : '#000000';
}
export function normalizeDesign(raw = {}) {
    if (typeof raw === 'string') {
        try { raw = JSON.parse(raw); } catch { raw = {}; }
    }
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) raw = {};
    const d = { ...DEFAULTS };
    for (const [name, fallback] of Object.entries(DEFAULTS)) {
        const value = raw[name];
        if (RANGES[name]) {
            const [lo, hi] = RANGES[name];
            d[name] = typeof value === 'number' && Number.isFinite(value) ? Math.max(lo, Math.min(hi, Math.round(value))) : fallback;
        } else if (SELECTIONS[name]) {
            d[name] = SELECTIONS[name].includes(value) ? value : fallback;
        } else if (name.endsWith('_color')) {
            d[name] = /^#[0-9a-f]{6}$/i.test(value) ? value : fallback;
        } else if (typeof fallback === 'boolean') {
            d[name] = typeof value === 'boolean' ? value : fallback;
        }
    }
    // A stale/invalid imported color setting must not make the register unreadable.
    if ([d.background_color, d.surface_color].some(bg => contrast(d.text_color, bg) < 4.5)) {
        d.background_color = d.appearance === 'dark' ? '#111827' : '#EEF2F7';
        d.surface_color = d.appearance === 'dark' ? '#1F2937' : '#FFFFFF';
        d.text_color = d.appearance === 'dark' ? '#F3F4F6' : '#172033';
    }
    d.actions = Object.fromEntries(ACTION_CODES.map((code, index) => {
        const item = raw.actions?.[code];
        return [code, {
            visible: typeof item?.visible === 'boolean' ? item.visible : true,
            sequence: Number.isInteger(item?.sequence) ? Math.max(0, Math.min(999, item.sequence)) : index * 10,
        }];
    }));
    const rawActionOrder = Array.isArray(raw.puid_action_order) ? raw.puid_action_order : [];
    d.action_order = [...new Set(rawActionOrder.filter((code) => ACTION_CODES.includes(code)))];
    d.action_order.push(...ACTION_CODES.filter((code) => !d.action_order.includes(code)));
    return d;
}

export function themeStyle(raw, screen = 'sales') {
    const d = normalizeDesign(raw);
    const primary = screen === 'payment' && !d.payment_inherit ? d.payment_primary_color : d.primary_color;
    const accent = screen === 'payment' && !d.payment_inherit ? d.payment_accent_color : d.accent_color;
    const densityHeight = { compact: 44, comfortable: 48, touch: 60 }[d.density];
    const vars = {
        primary, accent, bg: d.background_color, surface: d.surface_color, text: d.text_color,
        'on-primary': foreground(primary), success: d.success_color, warning: d.warning_color,
        danger: d.danger_color, 'on-success': foreground(d.success_color),
        'on-warning': foreground(d.warning_color), 'on-danger': foreground(d.danger_color),
        'button-height': `${Math.max(d.button_height, densityHeight)}px`,
        'key-height': `${Math.max(d.keypad_height, densityHeight)}px`,
        font: `${d.font_size}px`, 'product-font': `${d.product_font_size}px`,
        'product-width': `${d.product_min_width}px`, gap: `${d.spacing}px`,
        'key-gap': `${d.keypad_spacing}px`, radius: `${d.radius}px`,
        shadow: `0 ${d.shadow * 2}px ${d.shadow * 8}px rgba(0,0,0,${d.shadow ? .12 : 0})`,
    };
    return Object.entries(vars).map(([key, value]) => `--pui-${key}:${value}`).join(';') + ';';
}

/** All side panels surround the central cart/catalog or payment detail area.
 * Top/bottom panels span the full register; panels sharing a side stack.
 * Region coordinates never overlap. Checkout always gets its own last row.
 */
export function buildLayout(raw, screen = 'sales') {
    const d = normalizeDesign(raw);
    const zones = { left: [], right: [], top: [], bottom: [] };
    let center;
    let panels;
    if (screen === 'sales') {
        center = d.cart_side === 'left' ? ['order', 'products'] : ['products', 'order'];
        panels = [['keypad', d.sales_keypad_position], ['actions', d.sales_actions_position]];
        panels.sort(([a], [b]) => (a === d.sales_first_panel ? -1 : b === d.sales_first_panel ? 1 : 0));
    } else {
        center = d.payment_summary_position === 'left' ? ['summary', 'options'] : ['options', 'summary'];
        panels = [['keypad', d.payment_keypad_position], ['methods', d.payment_methods_position]];
        panels.sort(([a], [b]) => (a === d.payment_first_panel ? -1 : b === d.payment_first_panel ? 1 : 0));
        if (['top', 'bottom'].includes(d.payment_summary_position)) {
            center = ['options'];
            panels.push(['summary', d.payment_summary_position]);
        }
    }
    for (const [name, side] of panels) zones[side].push(name);
    const hasLeft = zones.left.length > 0, hasRight = zones.right.length > 0;
    const colCount = center.length + Number(hasLeft) + Number(hasRight);
    const mainRows = Math.max(zones.left.length, zones.right.length, 1);
    const mainStart = zones.top.length + 1, mainEnd = mainStart + mainRows;
    const regions = {};
    const set = (name, r1, c1, r2, c2, rank) => {
        regions[name] = { area: `${r1} / ${c1} / ${r2} / ${c2}`, rank };
    };
    zones.top.forEach((name, i) => set(name, i + 1, 1, i + 2, colCount + 1, i));
    center.forEach((name, i) => set(name, mainStart, Number(hasLeft) + i + 1, mainEnd, Number(hasLeft) + i + 2, 20 + i));
    for (const [side, col] of [['left', 1], ['right', colCount]]) {
        zones[side].forEach((name, i) => set(name, mainStart + (zones[side].length > 1 ? i : 0), col,
            zones[side].length > 1 ? mainStart + i + 1 : mainEnd, col + 1, (side === 'left' ? 10 : 30) + i));
    }
    zones.bottom.forEach((name, i) => set(name, mainEnd + i, 1, mainEnd + i + 1, colCount + 1, 40 + i));
    const lastRow = mainEnd + zones.bottom.length;
    set('checkout', lastRow, 1, lastRow + 1, colCount + 1, 50);
    const columns = [
        ...(hasLeft ? ['minmax(210px, 260px)'] : []),
        ...center.map(name => screen === 'sales' ? `minmax(${name === 'order' ? 220 : 240}px, ${name === 'order' ? d.cart_width : 100 - d.cart_width}fr)` : 'minmax(220px, 1fr)'),
        ...(hasRight ? ['minmax(210px, 260px)'] : []),
    ];
    const rows = [...zones.top.map(() => 'max-content'), ...Array(mainRows).fill('minmax(170px, 1fr)'), ...zones.bottom.map(() => 'max-content'), 'max-content'];
    return {
        style: `--pui-columns:${columns.join(' ')};--pui-rows:${rows.join(' ')};`,
        regions,
        regionStyle(name) {
            const r = regions[name];
            return r ? `--pui-area:${r.area};--pui-rank:${r.rank};` : '';
        },
    };
}
