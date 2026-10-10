# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Runtime differences between Odoo 16, 17, 18, 19 and 20, in one place."""
from odoo import release
from odoo.tools import config

MAJOR = release.version_info[0]


def sale_line_uom_field():
    return 'product_uom_id' if MAJOR >= 19 else 'product_uom'


def move_done_qty_field():
    return 'quantity' if MAJOR >= 17 else 'quantity_done'


def group_users_field():
    return 'user_ids' if MAJOR >= 19 else 'users'


def user_groups_field():
    return 'group_ids' if MAJOR >= 19 else 'groups_id'


def check_access(records, operation):
    """Model access and record domains, both checked by check_access (one
    ir.access model holds permissions and restrictions since 20)."""
    records.check_access(operation)


def server_option(name):
    try:
        return config.get(name)
    except Exception:
        return None


def in_test_mode(env):
    """Tests share one transaction, so work that normally runs in its own
    cursor runs inline instead."""
    return bool(env.context.get('eh_shopify_inline')) or bool(server_option('test_enable'))


def base_url(env):
    return (env['ir.config_parameter'].sudo().get_str('web.base.url') or '').rstrip('/')


def record_url(model, res_id):
    return '/web#model=%s&id=%s&view_type=form' % (model, res_id)


# ---------------------------------------------------------------------------
# Products, taxes, sales, invoices and payments (verified against the Odoo 16,
# 17, 18 and 19 sources; field presence is tested rather than version numbers
# wherever a field decides the behaviour).
# ---------------------------------------------------------------------------
from odoo import Command  # noqa: E402
from odoo.exceptions import UserError  # noqa: E402
from odoo.tools import float_compare  # noqa: E402


def product_type_vals(env, kind):
    """kind: 'storable' (stock tracked goods), 'consu' or 'service'."""
    template_fields = env['product.template']._fields
    if 'detailed_type' in template_fields:  # 16, 17
        if kind == 'storable':
            values = [value for value, _label in template_fields['detailed_type'].selection]
            return {'detailed_type': 'product' if 'product' in values else 'consu'}
        return {'detailed_type': kind}
    vals = {'type': 'service' if kind == 'service' else 'consu'}  # 18, 19
    if 'is_storable' in template_fields:
        vals['is_storable'] = kind == 'storable'
    return vals


def product_create_vals(env, name, kind, company=None, list_price=0.0):
    vals = {'name': name, 'sale_ok': True, 'purchase_ok': kind != 'service', 'list_price': list_price,
            'taxes_id': [Command.set([])]}
    vals.update(product_type_vals(env, kind))
    if 'invoice_policy' in env['product.template']._fields:
        vals['invoice_policy'] = 'order'
    if company:
        vals['company_id'] = company.id
    return vals


def sale_line_names(env):
    line_fields = env['sale.order.line']._fields
    uom_field = 'product_uom_id' if 'product_uom_id' in line_fields else 'product_uom'
    tax_field = 'tax_ids' if 'tax_ids' in line_fields else 'tax_id'
    return uom_field, tax_field


def sale_line_vals(env, product, quantity, price_unit, taxes, discount=0.0, name=None, sequence=None,
                   is_delivery=False):
    """A line whose price, discount and taxes the ORM keeps as given."""
    line_fields = env['sale.order.line']._fields
    uom_field, tax_field = sale_line_names(env)
    vals = {
        'product_id': product.id,
        'product_uom_qty': quantity,
        uom_field: product.uom_id.id,
        'price_unit': price_unit,
        'discount': discount or 0.0,
        tax_field: [Command.set(taxes.ids)],
    }
    if 'technical_price_unit' in line_fields:  # 18, 19 remember a manual price
        vals['technical_price_unit'] = price_unit
    if name:
        vals['name'] = name
    if sequence is not None:
        vals['sequence'] = sequence
    if is_delivery and 'is_delivery' in line_fields:
        vals['is_delivery'] = True
    return vals


def ensure_currency(env, code):
    Currency = env['res.currency'].sudo().with_context(active_test=False)
    currency = Currency.search([('name', '=', (code or '').upper())], limit=1)
    if currency and not currency.active:
        currency.active = True
    return currency.sudo(False)


def pricelist_for_currency(env, currency, company):
    """sale.order currency comes from its pricelist on 16 to 19."""
    Pricelist = env['product.pricelist'].with_context(active_test=False)
    candidates = Pricelist.search([('currency_id', '=', currency.id), ('company_id', 'in', [company.id, False])])
    if candidates:
        pricelist = candidates.sorted(lambda p: (not p.active, not p.company_id, p.id))[0]
        if not pricelist.active:
            pricelist.active = True
        return pricelist
    return env['product.pricelist'].with_company(company).create({
        'name': 'Shopify %s' % currency.name, 'currency_id': currency.id, 'company_id': company.id})


def company_lookup_ids(company):
    if 'parent_ids' in company._fields:  # 17+ branches see parent records
        return (company.parent_ids | company).ids
    return company.ids


def tax_price_include_vals(env, include):
    if 'price_include_override' in env['account.tax']._fields:  # 18, 19
        return {'price_include_override': 'tax_included' if include else 'tax_excluded'}
    return {'price_include': bool(include)}


def ensure_tax_group(env, company, country):
    Group = env['account.tax.group'].sudo()
    if 'company_id' not in Group._fields:  # 16: shared groups
        groups = Group.search([('country_id', 'in', [country.id, False])], order='sequence, id')
        group = groups.filtered(lambda g: g.country_id == country)[:1] or groups[:1]
        return group or Group.create({'name': 'Taxes', 'country_id': country.id})
    base = [('company_id', 'in', company_lookup_ids(company))]
    group = Group.search(base + [('country_id', '=', country.id)], limit=1) or \
        Group.search(base + [('country_id', '=', False)], limit=1)
    return group or Group.create({'name': 'Taxes', 'company_id': company.id, 'country_id': country.id})


def find_sale_tax(env, company, rate_percent, include, title=None, exclude=None):
    """An Odoo sales tax of this rate and tax basis. ``exclude`` keeps a tax
    already used by another line of the same order from being used twice."""
    Tax = env['account.tax'].with_context(active_test=False)
    candidates = Tax.search([('company_id', 'in', company_lookup_ids(company)), ('type_tax_use', '=', 'sale'),
                             ('amount_type', '=', 'percent')])
    matches = candidates.filtered(lambda t: float_compare(t.amount, rate_percent, precision_digits=4) == 0
                                  and bool(t.price_include) == bool(include))
    active = matches.filtered('active')
    if exclude:
        active -= exclude
    named = active.filtered(lambda t: title and t.name and title.lower() in t.name.lower())
    return (named or active)[:1]


def create_sale_tax(env, company, name, rate_percent, include):
    country = company.account_fiscal_country_id or company.country_id
    if not country:
        return None
    vals = {'name': name, 'amount': rate_percent, 'amount_type': 'percent', 'type_tax_use': 'sale',
            'company_id': company.id}
    vals.update(tax_price_include_vals(env, include))
    if 'country_id' in env['account.tax']._fields:
        vals['country_id'] = country.id
    vals['tax_group_id'] = ensure_tax_group(env, company, country).id
    Tax = env['account.tax']
    existing = Tax.with_context(active_test=False).search([('name', '=', name), ('company_id', '=', company.id),
                                                          ('type_tax_use', '=', 'sale')], limit=1)
    if existing:
        vals['name'] = '%s (%s)' % (name, 'incl.' if include else 'excl.')
    return Tax.create(vals)


def so_confirm(orders, keep_date_order=True):
    todo = orders.filtered(lambda o: o.state in ('draft', 'sent'))
    if todo:
        todo.with_context(eh_keep_date_order=keep_date_order).action_confirm()
    return todo


def so_cancel(orders):
    orders = orders.filtered(lambda o: o.state != 'cancel')
    if not orders:
        return True
    locked = orders.filtered(lambda o: o.locked) if 'locked' in orders._fields else \
        orders.filtered(lambda o: o.state == 'done')
    if locked:
        locked.action_unlock()
    return orders.with_context(disable_cancel_warning=True, eh_shopify_local_cancel=True).action_cancel()


def set_sale_line_quantity(line, quantity):
    """Change an ordered quantity without Odoo repricing the line from the
    pricelist: 16 and 17 recompute price and discount when the quantity changes,
    and 18 and 19 do unless the manual price is remembered."""
    vals = {'product_uom_qty': quantity, 'price_unit': line.price_unit, 'discount': line.discount}
    if 'technical_price_unit' in line._fields:
        vals['technical_price_unit'] = line.price_unit
    line.write(vals)


def unlock_orders(orders):
    """Unlock locked sales orders and return them, so they can be locked again.
    16 locks with the done state, 17 to 19 with the locked flag."""
    if 'locked' in orders._fields:
        locked = orders.filtered(lambda order: order.locked)
    else:
        locked = orders.filtered(lambda order: order.state == 'done')
    if locked:
        locked.action_unlock()
    return locked


def lock_orders(orders):
    if not orders:
        return
    if 'locked' in orders._fields:
        orders.action_lock()
    else:
        orders.action_done()


def create_invoices(orders, invoice_date=None):
    """Invoices for ``orders``, posted. ``invoice_date`` dates them on the day
    the order was placed, unless that day is closed, when Odoo dates them today."""
    if invoice_date:
        company = orders.company_id[:1] or orders.env.company
        locks = [getattr(company, name, False) for name in ('fiscalyear_lock_date', 'period_lock_date',
                                                            'hard_lock_date', 'tax_lock_date')]
        if any(lock and lock >= invoice_date for lock in locks):
            invoice_date = None
    context = {'raise_if_nothing_to_invoice': False}
    if invoice_date:
        context['default_invoice_date'] = invoice_date
    moves = orders.with_context(**context)._create_invoices(grouped=True, final=True)
    moves = moves.sudo(False)
    drafts = moves.filtered(lambda m: m.state == 'draft')
    if drafts:
        # Marked as the connector's own posting, so the hook that catches invoices posted by hand stays quiet.
        drafts.with_context(eh_shopify_posting=True).action_post()
    return moves


def payment_is_confirmed(payment):
    """A posted payment: 20 calls it paid until its bank line is matched, then reconciled."""
    return payment.state in ('paid', 'reconciled')


def register_invoice_payment(invoices, journal, amount=None, date=None, memo=None, method_line=None):
    env = invoices.env
    ctx = dict(env.context, active_model='account.move', active_ids=invoices.ids, dont_redirect_to_payments=True)
    vals = {'journal_id': journal.id, 'group_payment': True}
    if date:
        vals['payment_date'] = date
    if amount is not None:
        vals['amount'] = amount
    if memo:
        vals['communication'] = memo
    direction = 'outbound' if all(move.move_type in ('out_refund', 'in_invoice') for move in invoices) else 'inbound'
    available = journal._get_available_payment_method_lines(direction)
    if method_line and method_line.payment_type != direction:
        method_line = available.filtered(lambda line: line.code == method_line.code)[:1] or available[:1]
    method_line = method_line or available[:1]
    if method_line:
        vals['payment_method_line_id'] = method_line.id
    if not method_line.payment_account_id:
        # Money taken in Shopify is in transit until its payout reaches the bank, so the payment has to
        # carry a journal entry on an outstanding account for the payout to settle it later. Odoo only
        # falls back to the chart outstanding account on its own when full accounting is not installed;
        # with full accounting a method line that has no outstanding account of its own produces a
        # payment with no journal entry at all, which no payout and no bank line can ever match. Ask for
        # the same fallback in both editions, and only when the method line names no account itself, so
        # an outstanding account a merchant configured on the gateway still wins.
        ctx['force_payment_move'] = True
    wizard = env['account.payment.register'].with_context(ctx).create(vals)
    return wizard._create_payments()


# ---------------------------------------------------------------------------
# Stock: done quantities, validation without wizards, picking to sale order
# ---------------------------------------------------------------------------
def move_done_qty(move):
    """Quantity a move shipped, in the move unit of measure."""
    if 'quantity_done' in move._fields:  # 16
        return move.quantity_done
    if move.state == 'done':
        return move.quantity
    return move.quantity if getattr(move, 'picked', False) else 0.0


def set_move_quantity(move, quantity):
    if 'quantity_done' in move._fields:  # 16
        move.quantity_done = quantity
        return
    move.quantity = quantity  # 17 to 19: quantity first, then picked
    move.picked = quantity > 0


def validate_picking(picking, quantities=None, backorder=True):
    """Validate a picking with no wizard. ``quantities`` maps move ids to the
    quantity shipped; None ships everything ordered. Moves left out ship nothing."""
    picking.ensure_one()
    if picking.state == 'draft':
        picking.with_context(skip_zero_demand_check=True).action_confirm()
    if picking.state in ('confirmed', 'waiting'):
        picking.action_assign()
    for move in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
        quantity = move.product_uom_qty if quantities is None else quantities.get(move.id, 0.0)
        set_move_quantity(move, quantity)
    ctx = {'skip_backorder': True, 'skip_sms': True, 'skip_immediate': True, 'skip_expired': True,
           'skip_zero_demand_check': True}
    if not backorder:
        ctx['picking_ids_not_to_backorder'] = picking.ids
    return picking.with_context(**ctx).button_validate()


def picking_sale_order(picking):
    order = picking.sale_id if 'sale_id' in picking._fields else picking.env['sale.order']
    return order or picking.move_ids.sale_line_id.order_id[:1]


def carrier_fields_available(env):
    picking_fields = env['stock.picking']._fields
    return 'carrier_id' in picking_fields and 'carrier_tracking_ref' in picking_fields


def product_free_qty(product, locations):
    """Free quantity (on hand minus reserved) of ``product`` in ``locations``
    and their sub locations. Kits are computed from components when the
    manufacturing app is installed, with no dependency on it here."""
    if not locations:
        return 0.0
    return product.with_context(location=locations.ids).free_qty


def kit_components(env, product, company_id=None):
    """Component quantities in one unit of a kit, or None when it is not a kit.
    Kits exist only with the manufacturing app."""
    if 'mrp.bom' not in env:
        return None
    found = env['mrp.bom'].sudo()._bom_find(product, company_id=company_id, bom_type='phantom')
    bom = found.get(product) if hasattr(found, 'get') else found
    if not bom:
        return None
    _boms, lines = bom.explode(product, 1.0 / (bom.product_qty or 1.0))
    components = {}
    for bom_line, data in lines:
        quantity = bom_line.product_uom_id._compute_quantity(data['qty'], bom_line.product_id.uom_id)
        if quantity > 1e-9:
            components[bom_line.product_id] = components.get(bom_line.product_id, 0.0) + quantity
    return components or None


def is_storable(product):
    if 'is_storable' in product._fields:  # 18, 19
        return bool(product.is_storable)
    return product.type == 'product'  # 16, 17


def move_line_reserved_field(env):
    """Reserved quantity of an open stock.move.line in the product unit."""
    if 'reserved_qty' in env['stock.move.line']._fields:  # 16
        return 'reserved_qty'
    return 'quantity_product_uom'  # 17 to 19


def create_return_picking(picking, quantities):
    """Return picking for ``picking`` with ``quantities`` ({original move id:
    quantity in the product unit}), confirmed and reserved.

    20 has no return wizard: stock.picking._create_return copies every move of
    the picking with no demand. The demand is set here in the unit of each
    return move, and moves left out return nothing, so they are removed."""
    return_picking = picking._create_return()
    empty = return_picking.move_ids.browse()
    for move in return_picking.move_ids:
        origin = move.origin_returned_move_id
        quantity = quantities.get(origin.id, 0.0)
        if quantity > 0 and move.uom_id and move.uom_id != origin.product_id.uom_id:
            quantity = origin.product_id.uom_id._compute_quantity(quantity, move.uom_id, rounding_method='HALF-UP')
        if move.uom_id.is_zero(quantity) or quantity < 0:
            empty |= move
        else:
            move.product_uom_qty = quantity
    empty.unlink()
    if not return_picking.move_ids:
        return_picking.unlink()
        raise UserError(picking.env._('Please specify at least one non-zero quantity.'))
    # No move is left without demand, so confirming never stops on the zero demand warning.
    return_picking.with_context(skip_zero_demand_check=True).action_confirm()
    return_picking.action_assign()
    return return_picking


# ---------------------------------------------------------------------------
# Accounting: accounts per company, payment outstanding lines, bank deposits
# ---------------------------------------------------------------------------
def account_company_domain(env, company):
    if 'company_ids' in env['account.account']._fields:  # 18, 19 share accounts between companies
        return [('company_ids', 'in', company.ids)]
    return [('company_id', '=', company.id)]


def account_create_vals(env, company, vals):
    vals = dict(vals)
    if 'company_ids' in env['account.account']._fields:
        vals['company_ids'] = [(6, 0, company.ids)]
    else:
        vals['company_id'] = company.id
    return vals


def payment_outstanding_lines(payment):
    """Open lines of a posted payment on its outstanding account: the lines a
    bank line or a payout settles. Community creates them on 16 to 19."""
    Line = payment.env['account.move.line']
    if not payment or not payment.move_id or payment.move_id.state != 'posted':
        return Line
    liquidity, _counterpart, _writeoff = payment._seek_for_lines()
    return liquidity.filtered(lambda line: line.account_id.reconcile and not line.reconciled
                              and line.account_id != payment.journal_id.default_account_id)


def match_bank_deposit(st_line, transit_line):
    """Settle a new bank statement line against an open transit line of the same
    amount: its suspense line moves to the transit account and both reconcile."""
    _liquidity, suspense, other = st_line.with_context(skip_account_move_synchronization=True)._seek_for_lines()
    if len(suspense) != 1 or other or transit_line.reconciled:
        return False
    company_currency = st_line.company_id.currency_id
    currency = transit_line.currency_id or company_currency
    if currency != company_currency:
        # Rates move between payout and deposit: compare in the payout currency and let the
        # reconciliation book the exchange difference.
        if suspense.currency_id != currency or not currency.is_zero(
                suspense.amount_currency + transit_line.amount_residual_currency):
            return False
    elif not company_currency.is_zero(suspense.balance + transit_line.amount_residual):
        return False
    suspense.account_id = transit_line.account_id
    (suspense + transit_line).reconcile()
    return True


def payment_term_for_days(env, company, days, create=True):
    """A payment term due ``days`` after the invoice date: an existing one with a
    single line doing exactly that, or a new "Net N". 16 expresses it as a
    balance line with days, 17 to 19 as 100 percent with a days after delay."""
    Term = env['account.payment.term'].sudo()
    modern = 'nb_days' in env['account.payment.term.line']._fields
    for term in Term.search(['|', ('company_id', '=', False), ('company_id', '=', company.id)]):
        line = term.line_ids
        if len(line) != 1:
            continue
        if modern:
            if line.value == 'percent' and abs(line.value_amount - 100.0) < 0.001 and line.delay_type == 'days_after' \
                    and line.nb_days == days:
                return term
        elif line.value == 'balance' and line.days == days and not line.months and not line.end_month:
            return term
    if not create:
        return Term.browse()
    if modern:
        line_vals = {'value': 'percent', 'value_amount': 100.0, 'delay_type': 'days_after', 'nb_days': days}
    else:
        line_vals = {'value': 'balance', 'days': days}
    return Term.create({'name': 'Net %s' % days if days else 'Due on receipt', 'company_id': company.id,
                        'line_ids': [(0, 0, line_vals)]})
