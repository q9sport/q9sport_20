from odoo import api, models

class IrModel(models.Model):
    _inherit = "ir.model"

    @api.model
    def _get_dashboard_model_domain(self, domain=None):
        domain = list(domain or [])

        if "dashboard_model" not in self.env.context:
            return domain

        search_models = self.with_context(
            dashboard_inner_model=True
        ).search(domain)

        exclude_model_list = []

        for model_id in search_models:
            model = self.env[model_id.model]

            if (
                isinstance(model, models.AbstractModel)
                and not isinstance(model, models.Model)
            ):
                exclude_model_list.append(model_id.id)

        if exclude_model_list:
            domain.append(("id", "not in", exclude_model_list))

        return domain

    @api.model
    def name_search(self, name="", operator="ilike", limit=100):
        domain = []

        if name:
            domain = [("name", operator, name)]

        domain = self._get_dashboard_model_domain(domain)

        records = self.search(
            domain,
            limit=limit,
        )

        return records.name_get()

    @api.model
    def search_fetch(
        self,
        domain,
        field_names,
        offset=0,
        limit=None,
        order=None,
    ):
        domain = self._get_dashboard_model_domain(domain)

        return super(IrModel, self).search_fetch(
            domain=domain,
            field_names=field_names,
            offset=offset,
            limit=limit,
            order=order,
        )

# from odoo import models, api
# class IrModel(models.Model):
#     _inherit = "ir.model"

#     @api.model
#     def name_search(self, name="", args=None, operator="ilike", limit=100):
#         args = args or []
#         context = dict(self.env.context)
#         if "dashboard_inner_model" in context:
#             return super(IrModel, self).name_search(
#                 name=name, args=args, operator=operator, limit=limit
#             )
#         if "dashboard_model" in context:
#             search_models = self.with_context(**{"dashboard_inner_model": True}).search(
#                 args
#             )
#             exclude_model_list = []
#             for model_id in search_models:
#                 if isinstance(
#                     self.env[model_id.model], models.AbstractModel
#                 ) and not isinstance(self.env[model_id.model], models.Model):
#                     exclude_model_list.append(model_id.id)
#             args.append(("id", "not in", exclude_model_list))
#         return super(IrModel, self).name_search(
#             name=name, args=args, operator=operator, limit=limit
#         )

#     @api.model
#     def search_fetch(self, domain, field_names, offset=0, limit=None, order=None):
#         domain = domain or []
#         context = dict(self.env.context)
#         if "dashboard_inner_model" in context:
#             return super(IrModel, self).search_fetch(
#                 domain=domain,
#                 field_names=field_names,
#                 offset=offset,
#                 limit=limit,
#                 order=order,
#             )
#         if "dashboard_model" in context:
#             search_models = self.with_context(**{"dashboard_inner_model": True}).search(
#                 domain
#             )
#             exclude_model_list = []
#             for model_id in search_models:
#                 if isinstance(
#                     self.env[model_id.model], models.AbstractModel
#                 ) and not isinstance(self.env[model_id.model], models.Model):
#                     exclude_model_list.append(model_id.id)
#             domain.append(("id", "not in", exclude_model_list))
#         return super(IrModel, self).search_fetch(
#             domain=domain,
#             field_names=field_names,
#             offset=offset,
#             limit=limit,
#             order=order,
#         )
