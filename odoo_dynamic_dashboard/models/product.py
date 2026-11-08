from odoo import api, fields, models

class ProductSeason(models.Model):
    _name = 'product.season'
    _description = 'Season Label'
    _order = 'name'

    name = fields.Char(string='Label', required=True, translate=True)
    color = fields.Integer(string='Color')

    
class ProductTemplate(models.Model):
    _inherit = 'product.template'

    jersey_version = fields.Char(string="Jersey Version", tracking=True, copy=True)
    size = fields.Char(string="Size", tracking=True, copy=True)
    kit_type = fields.Char(string="Kit Type", tracking=True, copy=True)
    section = fields.Char(string="Section", tracking=True, copy=True)
    team = fields.Char(string="Team", tracking=True, copy=True)
    season = fields.Many2many ( comodel_name='product.season' , string='Season' , tracking=True ,)
    
    @api.onchange(
        'jersey_version',
        'size',
        'kit_type',
        'section',
        'team',
    )
    def action_get_attributes_fromvariables(self):

        attribute_mapping = {
            'jersey_version': 'Jersey Version',
            'size': 'Size',
            'kit_type': 'Kit Type',
            'section': 'Section',
            'team': 'Team',
        }

        Attribute = self.env['product.attribute']
        AttributeValue = self.env['product.attribute.value']

        for product in self:
            for field_name, attribute_name in attribute_mapping.items():

                value = (product[field_name] or '').strip()
                if not value:
                    continue

                # Find or create attribute
                attribute = Attribute.search([
                    ('name', '=', attribute_name)
                ], limit=1)

                if not attribute:
                    attribute = Attribute.create({
                        'name': attribute_name,
                        'create_variant': 'always',
                    })

                # Find or create attribute value
                attribute_value = AttributeValue.search([
                    ('attribute_id', '=', attribute.id),
                    ('name', '=', value),
                ], limit=1)

                if not attribute_value:
                    attribute_value = AttributeValue.create({
                        'attribute_id': attribute.id,
                        'name': value,
                    })

                # Find existing line
                line = product.attribute_line_ids.filtered(
                    lambda l: l.attribute_id.id == attribute.id
                )[:1]

                if line:
                    if attribute_value not in line.value_ids:
                        line.value_ids = [(4, attribute_value.id)]
                else:
                    # Create line immediately in the form
                    product.attribute_line_ids += self.env[
                        'product.template.attribute.line'
                    ].new({
                        'attribute_id': attribute.id,
                        'value_ids': [(6, 0, [attribute_value.id])],
                    })
