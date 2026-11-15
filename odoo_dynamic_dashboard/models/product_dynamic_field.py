# -*- coding: utf-8 -*-
import re
from lxml import etree

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def action_open_dynamic_char_field_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Add Product Field'),
            'res_model': 'product.dynamic.field.wizard',
            'view_mode': 'form',
            'view_id': self.env.ref(
                'odoo_dynamic_dashboard.view_product_dynamic_field_wizard_form'
            ).id,
            'target': 'new',
            'context': {
                'active_model': 'product.template',
                'active_id': self.id,
                'default_field_name': '',
            },
        }


class ProductDynamicFieldWizard(models.TransientModel):
    _name = 'product.dynamic.field.wizard'
    _description = 'Create Dynamic Product Char Field'

    field_name = fields.Char(
        string='Technical Field Name',
        required=True,
    )

    field_label = fields.Char(
        string='Field Label',
        required=True,
    )

    # Normalize technical name and add x_ automatically
    def _normalize_field_name(self, value):
        name = (value or '').strip().lower()

        # Replace spaces and hyphens with underscores
        name = re.sub(r'[\s\-]+', '_', name)

        # Remove invalid characters
        name = re.sub(r'[^a-z0-9_]', '', name)

        # Add x_ automatically
        if name and not name.startswith('x_'):
            name = 'x_' + name

        return name

    @api.onchange('field_name')
    def _onchange_field_name(self):
        if self.field_name:
            self.field_name = self._normalize_field_name(
                self.field_name
            )

    @api.constrains('field_name')
    def _check_field_name(self):
        for wizard in self:
            name = self._normalize_field_name(
                wizard.field_name
            )

            if not re.match(r'^x_[a-z][a-z0-9_]*$', name):
                raise ValidationError(_(
                    'Technical name must start with x_ '
                    'and contain lowercase letters, numbers '
                    'and underscores.'
                ))

    def action_create_field(self):
        self.ensure_one()

        # Normalize name on server side
        field_name = self._normalize_field_name(
            self.field_name
        )

        field_label = (self.field_label or '').strip()

        if not field_name or not field_label:
            raise ValidationError(_(
                'Please enter the technical name and field label.'
            ))

        if not re.match(r'^x_[a-z][a-z0-9_]*$', field_name):
            raise ValidationError(_('Invalid technical field name.'))

        IrModel = self.env['ir.model'].sudo()
        IrFields = self.env['ir.model.fields'].sudo()
        IrView = self.env['ir.ui.view'].sudo()

        # Get Product Template model
        model = IrModel._get('product.template')

        if not model:
            raise ValidationError(_(
                'Product Template model was not found.'
            ))

        # Check duplicate technical name
        existing = IrFields.search([
            ('model_id', '=', model.id),
            ('name', '=', field_name),
        ], limit=1)

        if existing:
            raise ValidationError(_(
                'This field already exists: %s'
            ) % field_name)

        # 1. Create the Char field
        IrFields.create({
            'name': field_name,
            'field_description': field_label,
            'model_id': model.id,
            'ttype': 'char',
            'state': 'manual',
        })

        # 2. Get original Product Template form
        parent_view = self.env.ref(
            'product.product_template_only_form_view'
        )

        # 3. Use ONE dedicated dynamic inherited view
        # This prevents XPath ordering problems between fields.
        dynamic_view = IrView.search([
            ('name', '=', 'product.template.dynamic.char.fields'),
            ('model', '=', 'product.template'),
            ('inherit_id', '=', parent_view.id),
        ], limit=1)

        if dynamic_view:
            root = etree.fromstring(
                dynamic_view.arch_db.encode('utf-8')
            )

            xpath_node = root.find('.//xpath')

            if xpath_node is None:
                raise ValidationError(_(
                    'Dynamic product fields view is invalid.'
                ))

        else:
            root = etree.Element('data')

            xpath_node = etree.SubElement(
                root,
                'xpath',
                expr="//field[@name='season']",
                position="after",
            )

        # Prevent adding the same field twice
        existing_nodes = xpath_node.findall('field')

        if not any(
            node.get('name') == field_name
            for node in existing_nodes
        ):
            etree.SubElement(
                xpath_node,
                'field',
                name=field_name,
            )

        # 4. Save/update the inherited view
        xml_content = etree.tostring(
            root,
            encoding='unicode',
        )

        if dynamic_view:
            dynamic_view.write({
                'arch': xml_content,
                'active': True,
            })
        else:
            IrView.create({
                'name': 'product.template.dynamic.char.fields',
                'model': 'product.template',
                'type': 'form',
                'inherit_id': parent_view.id,
                'arch': xml_content,
                'priority': 90,
                'active': True,
            })

        # 5. Reopen the SAME product record with the updated form
        product_id = self.env.context.get('active_id')

        if product_id:
            return {
                'type': 'ir.actions.act_window',
                'name': _('Product'),
                'res_model': 'product.template',
                'res_id': product_id,
                'view_mode': 'form',
                'views': [
                    (parent_view.id, 'form'),
                ],
                'target': 'current',
            }

        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }
