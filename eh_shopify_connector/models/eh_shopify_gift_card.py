# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Gift cards and store credit as liabilities.

A gift card sold in Shopify is money owed to the customer until it is spent, so
the gift card product books its sale to a gift card liability account instead
of revenue. When a later order is paid with a gift card, the payment is
recorded on a gift card journal whose payment account is that same liability,
so the liability falls and the order revenue is recognised then. Store credit
works the same way on its own liability account: spending it reduces the
liability, and store credit given back by a refund raises it. The accounts and
journals are created once, the first time a store needs them, and a gift card
product the merchant set up with its own income account is left alone.

Gift cards sold before Odoo followed the store are owed without ever having
been booked. Booking opening balances reads the enabled Shopify gift cards with
their transactions and books, per card, what Odoo has not recorded: the balance
today plus the spends processed since the cut over, because orders imported
since then already reduced the liability. The cut over is the earliest of the
order import start, the order history start and the day the store was added.
Cards sold through an order already in Odoo, or through an order created since
the cut over that is still to come in, are left out, since their sale credits
the liability. Large stores are read in slices that continue where they
stopped; each card is recorded once, and one entry is posted when the whole
list is read. Spends on orders that never came into Odoo, and top ups of cards
sold through orders in Odoo, are not adjusted.
"""
from odoo import _, fields, models
from odoo.exceptions import UserError

from .. import compat
from ..lib import documents, errors

LIABILITY = {
    'gift_card': ('gift_card_account_id', '2191', 'SGIFT'),
    'store_credit': ('store_credit_account_id', '2192', 'SCRED'),
}


class ShopifyStoreLiabilities(models.Model):
    _inherit = 'eh.shopify.store'

    gift_card_liability = fields.Boolean(
        'Keep gift cards and store credit as liabilities', default=True,
        help='Gift cards sold and store credit given are owed to customers until they are spent, so they are booked '
             'to liability accounts and become revenue when an order uses them.')
    gift_card_account_id = fields.Many2one('account.account', 'Gift cards issued')
    store_credit_account_id = fields.Many2one('account.account', 'Store credit issued')

    def _liability_account(self, kind):
        self.ensure_one()
        field, prefix, _code = LIABILITY[kind]
        if not self[field]:
            name = _('Shopify gift cards issued') if kind == 'gift_card' else _('Shopify store credit issued')
            self.sudo().write({field: self._payout_account(prefix, name, 'liability_current', True).id})
        return self[field]

    def _liability_journal(self, kind):
        """A payment journal whose payment account is the liability, so paying
        with a gift card or store credit reduces what is owed."""
        self.ensure_one()
        account = self._liability_account(kind)
        company = self.company_id
        _field, _prefix, code = LIABILITY[kind]
        Journal = self.env['account.journal'].sudo().with_company(company)
        journal = Journal.search([('company_id', '=', company.id), ('code', '=', code)], limit=1)
        if not journal:
            journal = Journal.create({'name': _('Shopify gift cards') if kind == 'gift_card' else _('Shopify store credit'),
                                      'code': code, 'type': 'bank', 'company_id': company.id})
        for line in journal.inbound_payment_method_line_ids | journal.outbound_payment_method_line_ids:
            if line.payment_account_id != account:
                line.payment_account_id = account
        return journal


class ShopifyGatewayLiabilities(models.Model):
    _inherit = 'eh.shopify.gateway'

    def _for(self, store, name, manual=False):
        gateway = super()._for(store, name, manual=manual)
        if gateway.tender_type in LIABILITY and not gateway.journal_id and store.gift_card_liability:
            gateway.sudo().journal_id = store._liability_journal(gateway.tender_type)
        return gateway


class ShopifyOrderLiabilities(models.Model):
    _inherit = 'eh.shopify.order'

    def _extra_product(self, kind):
        product = super()._extra_product(kind)
        store = self.store_id
        if kind == 'gift_card' and store.gift_card_liability:
            template = product.product_tmpl_id.sudo().with_company(store.company_id)
            if not template.property_account_income_id:
                template.property_account_income_id = store._liability_account('gift_card')
        return product


PAGE = 25
PAGES_PER_JOB = 20
DEBIT = 'GiftCardDebitTransaction'


def _shopify_datetime(value):
    return fields.Datetime.to_datetime(value[:19].replace('T', ' ')) if value else None


class ShopifyGiftCard(models.Model):
    _name = 'eh.shopify.gift.card'
    _description = 'Shopify Gift Card Opening Balance'
    _order = 'id desc'
    _rec_name = 'last_characters'

    store_id = fields.Many2one('eh.shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='store_id.company_id', store=True, index=True)
    shopify_gid = fields.Char('Shopify ID', required=True, readonly=True)
    last_characters = fields.Char('Code ending', readonly=True)
    order_gid = fields.Char('Shopify order', readonly=True)
    state = fields.Selection([('pending', 'Counted'), ('booked', 'Booked')], default='pending', required=True,
                             readonly=True)
    balance_now = fields.Float('Balance in Shopify', digits=(16, 2), readonly=True)
    spent_since_cutover = fields.Float('Spent since the cut over', digits=(16, 2), readonly=True)
    balance_booked = fields.Float('Balance booked', digits=(16, 2), readonly=True)
    currency_code = fields.Char('Currency', readonly=True)
    move_id = fields.Many2one('account.move', 'Journal entry', readonly=True, ondelete='set null')

    def init(self):
        self.env.cr.execute("CREATE UNIQUE INDEX IF NOT EXISTS eh_shopify_gift_card_gid_uniq "
                            "ON eh_shopify_gift_card (store_id, shopify_gid)")


class ShopifyStoreGiftCardOpening(models.Model):
    _inherit = 'eh.shopify.store'

    gift_card_opening_account_id = fields.Many2one(
        'account.account', 'Opening balance account',
        help='Balances of gift cards sold before Odoo followed the store are credited to the gift card liability and '
             'debited to this account. Current year earnings when left empty.')
    gift_card_opening_note = fields.Char('Opening gift card balances', readonly=True)
    gift_card_opening_cursor = fields.Char(readonly=True, copy=False)

    def _gift_card_cutover(self):
        self.ensure_one()
        history = getattr(self, 'history_orders_from', False)
        marks = [value for value in (self.order_import_from, fields.Datetime.to_datetime(history) if history else None,
                                     self.create_date) if value]
        return min(marks)

    def action_book_gift_card_opening(self):
        self.ensure_one()
        Job = self.env['eh.shopify.job']
        Job._require_manager()
        if not self.gift_card_liability:
            raise UserError(_('Switch on gift card and store credit liabilities first.'))
        if self.granted_scopes and not documents.scope_granted('read_gift_cards', self._granted_scope_set()):
            raise UserError(_('Add the read_gift_cards permission to the Shopify app so Odoo can read gift card '
                              'balances.'))
        Job.sudo()._enqueue_after_running(self, 'gift_cards.opening', 'gift_cards.opening',
                                          name=_('Book opening gift card balances'), priority=8)
        return self._notify(_('Gift card balances requested'), _(
            'Shopify lists the enabled gift cards with their spends. Cards sold through orders in Odoo are left out, '
            'and each card is booked once.'))

    def _job_gift_card_opening(self, job):
        store = job.store_id
        Card = self.env['eh.shopify.gift.card'].sudo()
        Order = self.env['eh.shopify.order'].sudo()
        cutover = store._gift_card_cutover()
        stamp = cutover.strftime('%Y-%m-%dT%H:%M:%SZ')
        seen = set(Card.search([('store_id', '=', store.id)]).mapped('shopify_gid'))
        client = store._client()
        document = documents.GIFT_CARDS
        counted, more, expected = 0, False, 0
        pages = client.iter_pages(store._doc_text(document), {'query': 'status:enabled'}, document.connection,
                                  page_size=PAGE, after=store.gift_card_opening_cursor or None)
        for index, (nodes, page_cursor, has_next) in enumerate(pages):
            rows = []
            for node in nodes:
                if node['id'] in seen:
                    continue
                order = node.get('order') or {}
                if order.get('id') and (
                        Order.search_count([('store_id', '=', store.id), ('shopify_gid', '=', order['id'])])
                        or (_shopify_datetime(order.get('createdAt')) or cutover) >= cutover):
                    # Its sale credits the liability when the order is, or will be, in Odoo.
                    expected += 1
                    continue
                transactions = client.complete_connection(
                    node, 'transactions', store._doc_text(documents.GIFT_CARD_TRANSACTIONS), node['id'],
                    ('node', 'transactions'), page_size=documents.GIFT_CARD_TRANSACTIONS.page_size)
                spent = sum(abs(float((item.get('amount') or {}).get('amount') or 0.0)) for item in transactions
                            if item.get('__typename') == DEBIT and (item.get('processedAt') or '') >= stamp)
                balance = node.get('balance') or {}
                now = float(balance.get('amount') or 0.0)
                owed = round(now + spent, 2)
                if owed <= 0:
                    continue
                rows.append({'store_id': store.id, 'shopify_gid': node['id'], 'state': 'pending',
                             'last_characters': node.get('lastCharacters'), 'order_gid': order.get('id') or False,
                             'balance_now': now, 'spent_since_cutover': spent, 'balance_booked': owed,
                             'currency_code': balance.get('currencyCode')})
                seen.add(node['id'])
            if rows:
                Card.create(rows)
                counted += len(rows)
            store.sudo().gift_card_opening_cursor = page_cursor if has_next else False
            if has_next and index + 1 >= PAGES_PER_JOB:
                more = True
                break
        if more:
            self.env['eh.shopify.job'].sudo()._enqueue_after_running(
                store, 'gift_cards.opening', 'gift_cards.opening', name=job.name, priority=job.priority)
            return {'counted': counted, 'expected_in_odoo': expected, 'more': True}
        return dict(store._book_gift_card_opening(expected), expected_in_odoo=expected)

    def _book_gift_card_opening(self, expected=0):
        self.ensure_one()
        store = self
        company = store.company_id
        pending = self.env['eh.shopify.gift.card'].sudo().search([('store_id', '=', store.id), ('state', '=', 'pending')])
        today = fields.Date.context_today(store)
        if not pending:
            store.sudo().write({'gift_card_opening_note': _('No new gift card balance to book on %s.') % today,
                                'gift_card_opening_cursor': False})
            return {'booked': 0}
        left_out = _(' %s cards were left out because their order is in Odoo or still to come.') % expected if expected \
            else ''
        liability = store._liability_account('gift_card')
        counterpart = store.gift_card_opening_account_id or self.env['account.account'].sudo().search(
            compat.account_company_domain(self.env, company) + [('account_type', '=', 'equity_unaffected')], limit=1)
        if not counterpart:
            raise errors.MappingError(_('Choose the opening balance account for gift cards on the store.'))
        journal = self.env['account.journal'].sudo().search([('type', '=', 'general'), ('company_id', '=', company.id)],
                                                            limit=1)
        if not journal:
            raise errors.MappingError(_('Create a miscellaneous journal to book opening gift card balances.'))
        company_currency = company.currency_id
        Currency = self.env['res.currency'].with_context(active_test=False)
        lines, totals = [], {}
        for card in pending:
            code = card.currency_code
            currency = (Currency.search([('name', '=', code)], limit=1) if code else Currency) or company_currency
            value = currency.round(card.balance_booked)
            balance = company_currency.round(currency._convert(value, company_currency, company, today)
                                             if currency != company_currency else value)
            lines.append({'name': _('Gift card ending %s') % (card.last_characters or ''), 'account_id': liability.id,
                          'currency_id': currency.id, 'amount_currency': -value, 'balance': -balance})
            total = totals.setdefault(currency, [0.0, 0.0])
            total[0] += value
            total[1] += balance
        for currency, (value, balance) in totals.items():
            lines.append({'name': _('Opening gift card balances'), 'account_id': counterpart.id,
                          'currency_id': currency.id, 'amount_currency': value, 'balance': balance})
        move = self.env['account.move'].sudo().with_company(company).create({
            'move_type': 'entry', 'journal_id': journal.id, 'date': today,
            'ref': _('Shopify gift card balances owed before Odoo followed the store'),
            'line_ids': [(0, 0, vals) for vals in lines]})
        move.action_post()
        pending.write({'state': 'booked', 'move_id': move.id})
        store.sudo().write({'gift_card_opening_cursor': False,
                            'gift_card_opening_note': _('%(count)s gift card balances booked on %(date)s in '
                                                        '%(move)s.%(left)s') % {
                                'count': len(pending), 'date': today, 'move': move.name, 'left': left_out}})
        return {'booked': len(pending), 'move': move.id}
