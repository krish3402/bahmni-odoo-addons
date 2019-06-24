# -*- coding: utf-8 -*-
from odoo import fields, models, api
from odoo.exceptions import UserError, ValidationError
import logging
_logger = logging.getLogger(__name__)

class AccountInvoice(models.Model):
    _inherit = 'account.invoice'

#     # overridden this method to deduct discounted amount from total of invoice
    @api.one
    @api.depends('invoice_line_ids.price_subtotal', 'tax_line_ids.amount', 
                 'currency_id', 'company_id', 'date_invoice', 'type', 'discount')
    def _compute_amount(self):
        round_curr = self.currency_id.round
        self.amount_untaxed = sum(line.price_subtotal for line in self.invoice_line_ids)
        self.amount_tax = sum(round_curr(line.amount) for line in self.tax_line_ids)
        amount_total = self.amount_untaxed + self.amount_tax - self.discount
        self.round_off_amount = self.env['rounding.off'].round_off_value_to_nearest(amount_total)
        self.amount_total = self.amount_untaxed + self.amount_tax - self.discount + self.round_off_amount
        amount_total_company_signed = self.amount_total
        amount_untaxed_signed = self.amount_untaxed
        if self.currency_id and self.company_id and self.currency_id != self.company_id.currency_id:
            currency_id = self.currency_id.with_context(date=self.date_invoice)
            amount_total_company_signed = currency_id.compute(self.amount_total, self.company_id.currency_id)
            amount_untaxed_signed = currency_id.compute(self.amount_untaxed, self.company_id.currency_id)
        sign = self.type in ['in_refund', 'out_refund'] and -1 or 1
        self.amount_total_company_signed = amount_total_company_signed * sign
        self.amount_total_signed = self.amount_total * sign
        self.amount_untaxed_signed = amount_untaxed_signed * sign

    discount_type = fields.Selection([('none', 'No Discount'),
                                      ('fixed', 'Fixed'),
                                      ('percentage', 'Percentage')],
                                     string="Discount Method",
                                     default='none')
    discount = fields.Monetary(string="Discount")
    discount_percentage = fields.Float(string="Discount Percentage")
    disc_acc_id = fields.Many2one('account.account',
                                  string="Discount Account Head")
    round_off_amount = fields.Monetary(string="Round Off Amount",
                                       compute=_compute_amount)

    @api.onchange('invoice_line_ids')
    def onchange_invoice_lines(self):
        amount_total = self.amount_untaxed + self.amount_tax
        if self.discount_type == 'fixed':
            self.discount_percentage = (self.discount / amount_total) * 100
        elif self.discount_type == 'percentage':
            self.discount = amount_total * self.discount_percentage / 100

    @api.onchange('discount', 'discount_percentage', 'discount_type')
    def onchange_discount(self):
        amount_total = self.amount_untaxed + self.amount_tax
        if self.discount:
            self.discount_percentage = (self.discount / amount_total) * 100
        if self.discount_percentage:
            self.discount = amount_total * self.discount_percentage / 100
            
    @api.model
    def create(self, vals):
        rec = super(AccountInvoice,self).create(vals)
        if rec.origin and self.env.ref('bahmni_account.validate_picking_basedon_invoice').value == '1':
            sale_order = self.env['sale.order'].search([('name','=',rec.origin)])
            if any(sale_order) and len(sale_order.picking_ids):
                for picking in sale_order.picking_ids:
                    if picking.state in ('confirmed','partially_available'):
                        products_not_available = ""
                        for move in picking.move_lines.filtered(lambda m:m.state != 'assigned'):
                            products_not_available += '<li>' + move.product_id.name + '</li>'
                        message = ("<b>Auto validation Failed</b> <br/> <b>Reason:</b>There is no enough stock for below products%s")%(products_not_available)
                        picking.message_post(body=message)
                    found_issue = False
                    for pack in picking.pack_operation_product_ids:
                        if pack.product_id.tracking != 'none':
                            lot_ids = self._find_batch(pack.product_id,pack.product_qty,pack.location_id,picking)
                            _logger.info("\n\n***** lot_ids result:%s\n*****",lot_ids)
                            if lot_ids:
                                #First need to Find the related move_id of this operation
                                operation_link_obj = self.env['stock.move.operation.link'].search([('operation_id','=',pack.id)],limit=1)
                                move_obj = operation_link_obj.move_id
                                #Now we have to update entry to the related table which holds the lot, stock_move and operation entrys
                                pack_operation_lot = self.env['stock.pack.operation.lot'].search([('operation_id','=',pack.id)],limit=1)
                                for lot in lot_ids:
                                    pack_operation_lot.write({
                                        'lot_name': lot.name,
                                        'qty': pack.product_qty,
                                        'operation_id': pack.id,
                                        'move_id': move_obj.id,
                                        'lot_id': lot.id,
                                        'cost_price': lot.cost_price,
                                        'sale_price': lot.sale_price,
                                        'mrp': lot.mrp
                                        })
                                pack.qty_done = pack.product_qty
                            else:
                                found_issue = True
                        else:
                            pack.qty_done = pack.product_qty
                    if not found_issue:
                        picking.do_new_transfer()#Validate
        return rec
        
    @api.multi
    def _find_batch(self, product, qty, location, picking):
        _logger.info("\n\n***** Product :%s, Quantity :%s Location :%s\n*****",product,qty,location)
        lot_objs = self.env['stock.production.lot'].search([('product_id','=',product.id),('life_date','>=',str(fields.datetime.now()))])
        _logger.info('\n *** Searched Lot Objects:%s \n',lot_objs)
        if any(lot_objs):
            #Sort losts based on the expiry date FEFO(First Expiry First Out)
            lot_objs = list(lot_objs)
            sorted_lot_list = sorted(lot_objs, key=lambda l: l.life_date)
            _logger.info('\n *** Sorted based on FEFO :%s \n',sorted_lot_list)
            done_qty = qty
            res_lot_ids = []
            lot_ids_for_query = tuple([lot.id for lot in sorted_lot_list])
            self._cr.execute("SELECT SUM(qty) FROM stock_quant WHERE lot_id IN %s and location_id=%s",(lot_ids_for_query,location.id,))
            qry_rslt = self._cr.fetchall()
            available_qty = qry_rslt[0] and qry_rslt[0][0] or 0
            if available_qty >= qty:
                for lot_obj in sorted_lot_list:
                    quants = lot_obj.quant_ids.filtered(lambda q: q.location_id == location)
                    for quant in quants:
                        if done_qty >= 0:
                            res_lot_ids.append(lot_obj)
                            done_qty = done_qty - quant.qty
                return res_lot_ids
            else:
                message = ("<b>Auto validation Failed</b> <br/> <b>Reason:</b> There are not enough stock available for <a href=# data-oe-model=product.product data-oe-id=%d>%s</a> product on <a href=# data-oe-model=stock.location data-oe-id=%d>%s</a> Location") % (product.id,product.name,location.id,location.name)
                picking.message_post(body=message)
        else:
            message = ("<b>Auto validation Failed</b> <br/> <b>Reason:</b> There are no Batches/Serial no's available for <a href=# data-oe-model=product.product data-oe-id=%d>%s</a> product") % (product.id,product.name)
            picking.message_post(body=message)
            return False
