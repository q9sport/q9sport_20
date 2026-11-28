from odoo import fields, models


class SpecialCategories(models.Model):
    _name = 'special.category'
    _description = "Special Categories"

    name = fields.Char(string="Name")
