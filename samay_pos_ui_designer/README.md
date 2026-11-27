# POS UI Designer — Odoo 19

Backend-managed sales/payment layouts, reusable company-specific profiles, and CSS themes. No decorative image assets, external fonts, or third-party frontend packages are shipped.

**Release status: Odoo 19 test build.** It has passed static checks, isolated Odoo database installation/model tests, and the live backend profile/preview screen. Test a safe POS register before enabling it for a production checkout. See `VALIDATION.md`.

## Install

1. Copy the `pos_ui_designer` folder into your Odoo 19 custom addons directory.
2. Confirm that directory is in your server's `addons_path`, then restart the development server.
3. Update Apps List in developer mode. Remove the default Apps filter and search for **POS UI Designer**. Install it.
4. Alternatively, from your actual Odoo checkout, using your development configuration and disposable database:

```bash
python odoo-bin -c /path/to/dev.conf -d disposable_test_db -i pos_ui_designer --stop-after-init
```

The addon depends only on `point_of_sale`. It does not contain the Odoo server or dependencies. It is intended for Odoo 19 and requires a hosting environment that supports custom server addons.

## Configure

1. As a POS manager, open **Point of Sale → Configuration → POS UI Designer**.
2. Create a profile for the same company as the target POS.
3. Choose Sales Screen positions, sizes, density, and supported optional-action visibility/sequence.
4. Configure Payment Screen positions and, optionally, independent primary/accent colors.
5. Choose a preset and Light/Dark appearance, then refine the colors. Changing a preset replaces primary/accent; changing appearance replaces background/surface/text. Color fields support a picker and hexadecimal input.
6. Check the Preview tab, then save.
7. Assign the profile in **POS Settings → PoS Interface → UI Designer**, or the POS configuration form's **UI Designer** tab. Enable customization and save.
8. Apply during a safe POS load/reopening after completing the current sale and ensuring pending orders have synced. No bus notification, automatic reload, polling, or active-order replacement is implemented.

Profiles start with text-only product/category cards. Existing POS image display settings are preserved in the database. Customized screens suppress those image elements before loading them when text-only mode is enabled. Payment method decorative image elements are omitted while customization is enabled. Functional QR codes/barcodes, native dialogs, printer receipt content, and customer displays are not removed.

## Layout rules

- **Sales:** the central area contains cart and product catalog. Cart Side swaps their order. Cart Share is the relative central width, excluding outside docks.
- **Keypad / Actions Left or Right:** flank the central area.
- **Top or Bottom:** span the entire screen area. Keypads use a wider row arrangement at desktop widths.
- **Same position:** panels stack in the selected sequence; neither overlaps the other.
- **Payment:** keypad and payment methods use the same docking rules. Summary Left/Right selects its side within the central payment area next to customer/invoice options. Summary Top/Bottom spans the full width.
- **Checkout:** always has its own final row. Back/Validate and normal payment conditions remain in the upstream components.
- **At 1100px or narrower:** regions stack into one column, keeping their configured zone order and checkout last. Product and cart regions retain scroll access. The customized sales screen exposes both regions instead of the native mobile pane switcher.
- **Preview:** desktop 1280px, tablet 820px, mobile 390px. A narrow device preview is scrollable inside the backend. Preview content is illustrative sample data; it uses the same region resolver and CSS variables as the POS, not live transactional components.

Supported optional actions: internal note, preset, customer note, pricelist, fiscal position/tax, and product information. Missing entries fall back to visible. Core visibility/access conditions still apply. Customer selection, More, order cancellation, Back and Payment/Validate cannot be hidden by an optional-action policy. Unknown buttons supplied by other addons are left intact.

## Profiles, security and restoration

- Each POS opts in separately. Existing configurations default to disabled.
- A profile is shared: changing it affects every assigned POS on their next load. Duplicate Profile creates independent settings and optional-action rows without assignments.
- POS managers manage profiles. Cashiers receive only whitelisted visual settings in the normal POS bootstrap; they receive no new profile-edit permissions.
- Profiles/actions have allowed-company rules; profile assignment uses company checks.
- Reset Design restores visual defaults without removing profile assignments.
- Disable UI Designer and load the POS safely to restore standard layout rules. Missing/archived profiles fall back to standard mode.
- On removal, extended fields/views/assets are removed through normal module uninstall; verify this on the disposable database before deploying. Do not uninstall modules or discard offline orders on a live register as a test.

## Validation and performance

Backend validation accepts only six-digit hexadecimal colors, bounded numeric values, and readable text/surface/background contrast (minimum 4.5:1). Primary/status foreground colors are derived for contrast. The frontend sanitizes imported/stale settings and uses safe defaults.

Settings are exported once at bootstrap. The frontend keeps a per-POS snapshot for that application lifetime. No per-keypress RPC calls or remote design assets are introduced. Text-only mode avoids product/category `<img>` creation; it does not use CSS hiding to disguise downloaded photos. Native POS image caching/preloading by other extensions must still be checked in your target environment.

Two scoped `!important` overrides counter upstream Bootstrap `fs-3` / `rounded-0` keypad utilities. No core files are edited. XML extension wrappers preserve existing event handlers and avoid duplicate keypad/actionpad instances. Because wrappers change ancestry, custom extensions with strict DOM selectors need regression testing even when customization is disabled.

## Developer checks

Run from the addon folder:

```bash
python -m compileall -q .
node tools/test_layout.mjs
python tools/check_source.py --odoo-source /path/to/odoo19
```

The source checker requires `lxml` and reads the actual source tree. It checks sequential XPath matching and selected structural invariants, not actual Odoo inheritance or browser rendering.

Run model tests with the real Odoo 19 environment and a disposable database:

```bash
python odoo-bin -c /path/to/dev.conf -d disposable_test_db -u pos_ui_designer --test-enable --test-tags /pos_ui_designer --stop-after-init
```

Adjust these paths/commands to your checkout and environment. Tests cover profile constraints, duplication/reset, permissions, company isolation, POS isolation, snapshot export, and fallback behavior.

## Scope and compatibility

No sale/payment/accounting/stock business methods are overridden. This indicates intended scope, not evidence that their workflows have passed runtime regression tests. Payment methods, terminal errors/cancellation, refunds, offline behavior, receipt navigation, restaurant mode, Enterprise modules, and third-party POS extensions require target-environment validation.

This version is a settings-based designer, not a drag-and-drop page builder. Payment sizing/density and typography are shared with Sales; payment accents and panel positions can differ. Previewed sample action buttons may be unavailable for a real order.

License: LGPL-3.0-or-later. See `LICENSE`.
