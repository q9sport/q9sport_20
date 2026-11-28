import logging
import os
import json
import subprocess

from odoo import models, release, fields

_logger = logging.getLogger(__name__)


class ImportApps(models.TransientModel):
    _name = "import.apps"

    def import_all_apps_from_json(self):
        json_dir = "/home/eg/test/inkerp_apps"
        repo_url = "https://github.com/dc-139/inkerp_apps.git"
        branch_name = "11.0"

        # Clone or pull the repo from the specified branch
        if not os.path.exists(json_dir):
            try:
                subprocess.run(["git", "clone", "--depth", "1", "-b", branch_name, repo_url, json_dir], check=True)
                _logger.info("Cloned repository branch '%s' into %s", branch_name, json_dir)
            except subprocess.CalledProcessError as e:
                raise Warning(f"Failed to clone repository branch '{branch_name}': {e}")
        else:
            try:
                subprocess.run(["git", "-C", json_dir, "checkout", branch_name], check=True)
                subprocess.run(["git", "-C", json_dir, "pull", "origin", branch_name], check=True)
                _logger.info("Pulled latest changes from branch '%s'", branch_name)
            except subprocess.CalledProcessError as e:
                pass
                # raise Warning(f"Failed to pull latest changes from branch '{branch_name}': {e}")

        # Load JSON files
        json_files = [f for f in os.listdir(json_dir) if f.endswith(".json")]
        if not json_files:
            raise Warning(f"No JSON files found in {json_dir}")

        required_headers = {"name", "technical_name"}

        for json_file in json_files:
            file_path = os.path.join(json_dir, json_file)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                _logger.warning("Failed to read %s: %s", json_file, str(e))
                continue

            if not isinstance(data, list):
                _logger.warning("Skipping %s: JSON must contain a list of records", json_file)
                continue

            for record in data:
                release_version = ''
                if '+e' in release.version:
                    release_version = release.version.split('+')[0]
                if release.version in record.get('odoo_version') or release_version in record.get('odoo_version'):
                    if not required_headers.issubset(record):
                        _logger.info("Skipping record in %s due to missing required fields: %s", json_file, record)
                        continue

                    name = record.get("name")
                    technical_name = record.get("technical_name")
                    image_data = record.get("image")  # Should be base64 string or None

                    apps_id = self.env["app.data.inkerp"].search([("technical_name", "=", technical_name)], limit=1)
                    app_cat_id = self.env['ir.module.category'].search(
                        [('parent_id', '=', False), ('name', '=ilike', record.get("category"))], limit=1)
                    special_category_id = self.env['special.category'].search(
                        [('name', '=ilike', record.get("special_category"))], limit=1)
                    if record.get("special_category") and not special_category_id:
                        special_category_id = self.env['special.category'].create({'name':record.get("special_category")})
                    module_id = self.env['ir.module.module'].search([('name', '=', technical_name)], limit=1)

                    offer_price = record.get("offer_price")
                    actual_price = record.get("actual_price")

                    app_data = {
                        "name": name,
                        "technical_name": technical_name,
                        "category_id": app_cat_id.id,
                        "is_offer": record.get("is_offer"),
                        "offer_price": f"€ {offer_price:.2f}" if offer_price is not None else False,
                        "actual_price": f"€ {actual_price:.2f}" if actual_price is not None else False,
                        "is_free": record.get("is_free"),
                        "description": record.get("description"),
                        "summary": record.get("summary"),
                        "offer_end": fields.Date.from_string(record.get("offer_date")) if record.get(
                            "offer_date") else False,
                    }
                    if module_id:
                        app_data.update({'app_status':'installable' if module_id.state == 'uninstalled' else 'installed'})
                    if special_category_id:
                        app_data.update({'special_category_id': special_category_id.id})

                    if image_data:
                        app_data["icon"] = image_data

                    if apps_id:
                        apps_id.write(app_data)
                    else:
                        self.env["app.data.inkerp"].create(app_data)

        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }
