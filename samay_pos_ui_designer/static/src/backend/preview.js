/** @odoo-module **/
import { Component, proxy, useProps } from '@odoo/owl';
import { registry } from '@web/core/registry';
import { standardWidgetProps } from '@web/views/widgets/standard_widget_props';
import { standardFieldProps } from '@web/views/fields/standard_field_props';
import { normalizeDesign, themeStyle, buildLayout, ACTION_CODES } from '@samay_pos_ui_designer/common/designer';

export class PUIColor extends Component {
    static template = 'pos_ui_designer.Color';
    props = useProps(standardFieldProps);
    get value() { return this.props.record.data[this.props.name] || '#000000'; }
    get safeColor() { return /^#[0-9a-f]{6}$/i.test(this.value) ? this.value : '#000000'; }
    onChange(ev) { return this.props.record.update({ [this.props.name]: ev.target.value }); }
}
registry.category('fields').add('pui_color', { component: PUIColor, supportedTypes: ['char'] });

export class PUIDesignerPreview extends Component {
    static template = 'pos_ui_designer.Preview';
    props = useProps(standardWidgetProps);
    setup() {
        this.state = proxy({ screen: 'sales', device: 'desktop' });
    }
    setScreen(screen) { this.state.screen = screen; }
    setDevice(device) { this.state.device = device; }
    get design() {
        const record = this.props.record.data;
        const actions = Object.fromEntries((record.action_ids?.records || []).map(row => [row.data.code, { visible: row.data.visible, sequence: row.data.sequence }]));
        return normalizeDesign({ ...record, enabled: true, actions });
    }
    get rootStyle() { return themeStyle(this.design, this.state.screen) + buildLayout(this.design, this.state.screen).style; }
    region(name) { return buildLayout(this.design, this.state.screen).regionStyle(name); }
    get keys() { return ['1', '2', '3', 'Qty', '4', '5', '6', '%', '7', '8', '9', 'Price', '+/−', '0', '.', '←']; }
    get products() {
        return [
            { name: 'Nitro Cold Brew', price: '4.50' }, { name: 'Artisan Croissant', price: '3.50' },
            { name: 'Matcha Latte', price: '6.00' }, { name: 'Espresso Romano', price: '3.20' },
            { name: 'Almond Cookie', price: '2.80' }, { name: 'Avocado Toast', price: '8.50' },
        ];
    }
    get actionLabels() { return { internal_note: 'Note', preset: 'Preset', customer_note: 'Customer Note', pricelist: 'Pricelist', tax: 'Tax', info: 'Info' }; }
    get actions() { return ACTION_CODES.filter(code => this.design.actions[code].visible).sort((a, b) => this.design.actions[a].sequence - this.design.actions[b].sequence); }
    get swatches() { return [this.design.primary_color, this.design.accent_color, this.design.surface_color]; }
    get dirty() { return this.props.record.dirty; }
}
registry.category('view_widgets').add('pos_ui_designer_preview', { component: PUIDesignerPreview });
