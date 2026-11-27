from odoo import http
from odoo.http import request

from odoo.addons.point_of_sale.controllers.main import PosController


class TamadooPosController(PosController):

    @http.route()
    def pos_web(self, config_id=False, from_backend=False, subpath=None, **k):
        if config_id and str(config_id).isdigit():
            config = request.env['pos.config'].browse(int(config_id)).exists()
            if config and not config.has_access('read'):
                return request.redirect('/odoo/action-point_of_sale.action_client_pos_menu')
        return super().pos_web(config_id=config_id, from_backend=from_backend, subpath=subpath, **k)
