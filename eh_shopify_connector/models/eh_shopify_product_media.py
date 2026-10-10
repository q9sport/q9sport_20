# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Product images between Odoo and Shopify.

Images are compared by a SHA-256 of the bytes Odoo stores, which can differ
from the downloaded bytes when Odoo resizes a large image, so an unchanged
image is never sent or downloaded again. Images found during a bulk import are
copied by their own jobs, so an import chunk never waits on downloads. Uploaded files are named after that hash, and
Shopify's files are searched for the name before uploading, so a retry after an
unanswered request reuses the file instead of creating a copy. Images are only
downloaded from Shopify's own CDN over HTTPS, with a size cap and without
following redirects, never from an arbitrary address.
"""
import hashlib
from urllib.parse import urlparse

from odoo import _, fields, models
from odoo.tools import BinaryBytes

from ..lib import documents, errors
from .eh_shopify_product import DEFER_IMAGES, FROM_SHOPIFY

try:
    from odoo.tools.mimetypes import guess_mimetype
except ImportError:  # pragma: no cover
    guess_mimetype = None

ALLOWED_HOSTS = ('cdn.shopify.com',)
EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp'}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _mimetype(content):
    return (guess_mimetype(content) if guess_mimetype else '') or ''


class ShopifyProductMedia(models.Model):
    _inherit = 'eh.shopify.product'

    image_hash = fields.Char(readonly=True, copy=False)
    media_gid = fields.Char('Shopify image', readonly=True, copy=False)

    # ------------------------------------------------------------------ push
    def _push_image(self, client, template):
        """Send the template image to Shopify when it changed. Returns a note or None."""
        self.ensure_one()
        content = template.image_1920.content if template.image_1920 else b''
        if not content:
            return None
        digest = hashlib.sha256(content).hexdigest()
        if digest == self.image_hash:
            return None
        mimetype = _mimetype(content)
        extension = EXTENSIONS.get(mimetype)
        if not extension:
            return _('The product image is not a PNG, JPEG, GIF or WebP file, so it was not sent.')
        store = self.store_id
        filename = 'odoo-%s.%s' % (digest[:40], extension)
        try:
            data = client.execute(store._doc_text(documents.FILES_BY_NAME),
                                  {'query': 'filename:%s' % filename}).data or {}
            nodes = (data.get('files') or {}).get('nodes') or []
            file_gid = nodes[0]['id'] if nodes else self._upload_image(client, filename, mimetype, content, template)
            client.mutate(store._doc_text(documents.FILE_UPDATE),
                          {'files': [{'id': file_gid, 'referencesToAdd': [self.shopify_gid]}]},
                          documents.FILE_UPDATE.payload_key)
        except errors.ScopeError:
            return _('Shopify did not allow sending images. Add the files permissions to the app.')
        except errors.UserErrors as error:
            return _('Shopify refused the product image: %s') % error.message
        self.write({'image_hash': digest, 'media_gid': file_gid})
        return None

    def _upload_image(self, client, filename, mimetype, content, template):
        store = self.store_id
        staged = client.mutate(store._doc_text(documents.STAGED_UPLOADS), {'input': [{
            'resource': 'IMAGE', 'filename': filename, 'mimeType': mimetype, 'httpMethod': 'POST',
            'fileSize': str(len(content))}]}, documents.STAGED_UPLOADS.payload_key)
        target = (staged.get('stagedTargets') or [{}])[0]
        if not target.get('url'):
            raise errors.TransientError(_('Shopify did not return an upload address for the image.'), retry_after=60)
        parameters = [(parameter['name'], parameter['value']) for parameter in target.get('parameters') or []]
        status = store._transport().upload_multipart(target['url'], parameters, filename, content, mimetype)
        if status not in (200, 201, 204):
            raise errors.TransientError(_('The image upload was refused with HTTP %s.') % status, retry_after=60)
        created = client.mutate(store._doc_text(documents.FILE_CREATE), {'files': [{
            'originalSource': target['resourceUrl'], 'contentType': 'IMAGE', 'filename': filename,
            'alt': template.name, 'duplicateResolutionMode': 'RAISE_ERROR'}]}, documents.FILE_CREATE.payload_key)
        file_info = (created.get('files') or [{}])[0]
        if not file_info.get('id'):
            raise errors.TransientError(_('Shopify did not return the uploaded image.'), retry_after=60)
        if file_info.get('fileStatus') != 'READY':
            data = client.execute(store._doc_text(documents.FILE_STATUS), {'id': file_info['id']}).data or {}
            status = (data.get('node') or {}).get('fileStatus')
            if status == 'FAILED':
                raise errors.MappingError(_('Shopify could not process the product image.'))
            if status != 'READY':
                raise errors.TransientError(_('Shopify is still processing the product image.'), retry_after=30)
        return file_info['id']

    # ------------------------------------------------------------------ pull
    def _pull_image(self, product, template, force=False):
        """Copy the Shopify featured image onto the template. Returns a note or None."""
        self.ensure_one()
        media = product.get('featuredMedia') or {}
        url = (media.get('image') or {}).get('url')
        if not url or not template or (template.image_1920 and not force):
            return None
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS:
            return None
        if media.get('id') and media.get('id') == self.media_gid and template.image_1920:
            return None
        if self.env.context.get(DEFER_IMAGES):
            self.env['eh.shopify.job'].sudo()._enqueue(
                self.store_id, 'product.image', name=_('Copy the Shopify image of %s') % (self.title or self.shopify_gid),
                payload={'binding_id': self.id, 'direction': 'pull', 'template_id': template.id, 'media': media,
                         'force': bool(force)},
                dedup_key='product.image.pull:%s' % self.id, resource_key=self.shopify_gid, priority=9)
            return None
        try:
            content = self.store_id._transport().get_bytes(url, max_bytes=MAX_IMAGE_BYTES)
        except errors.ShopifyError as error:
            return _('The Shopify image could not be downloaded: %s') % error.message
        if _mimetype(content) not in EXTENSIONS:
            return _('The Shopify image is not a PNG, JPEG, GIF or WebP file, so it was not copied.')
        target = template.sudo()
        if hashlib.sha256(content).hexdigest() != self.image_hash or not target.image_1920:
            target.with_context(**{FROM_SHOPIFY: self.store_id.id}).write({'image_1920': BinaryBytes(content)})
        stored = target.image_1920.content if target.image_1920 else content
        self.write({'image_hash': hashlib.sha256(stored).hexdigest(), 'media_gid': media.get('id')})
        return None

    def _job_product_image(self, job):
        """Send the Odoo image of a published product, or copy a Shopify image
        found during a bulk import, in a job of its own."""
        payload = job.payload or {}
        binding = self.browse(payload.get('binding_id')).exists()
        if not binding or binding.state == 'removed':
            return {'skipped': 'the product is not linked'}
        if payload.get('direction') == 'push':
            template = binding.product_tmpl_id
            if not template:
                return {'skipped': 'the Shopify product has no Odoo product'}
            note = binding._push_image(binding.store_id._client(), template)
        else:
            template = self.env['product.template'].browse(payload.get('template_id')).exists()
            if not template:
                return {'skipped': 'the Odoo product was removed'}
            note = binding._pull_image({'featuredMedia': payload.get('media') or {}}, template,
                                       force=payload.get('force'))
        if note:
            binding.reason = note
            return {'note': note}
        return {'image': binding.media_gid}
