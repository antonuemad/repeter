---
name: Repeater
description: "The Magic Lap publishing workspace, derived from the approved Sentinel Eye family."
colors:
  "primary": "#2563eb"
  "primary-hover": "#1d4ed8"
  "primary-bg": "#eff4fe"
  "primary-text": "#ffffff"
  "bg": "#f4f6fb"
  "surface": "#ffffff"
  "surface-alt": "#f8fafc"
  "text": "#0f172a"
  "muted": "#475569"
  "subtle": "#607087"
  "border": "#e5e9f2"
  "border-strong": "#cbd3e1"
  "danger": "#b91c1c"
  "danger-bg": "#fef2f2"
  "danger-text": "#ffffff"
  "success": "#15803d"
  "success-bg": "#f0fdf4"
  "warning": "#92400e"
  "warning-bg": "#fffbeb"
  "info": "#0369a1"
  "info-bg": "#f0f9ff"
  "pf-tiktok": "#be185d"
  "pf-instagram": "#a21caf"
typography:
  headline:
    fontFamily: "Inter, \"IBM Plex Sans Arabic\", system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "-.025em"
  title:
    fontFamily: "Inter, \"IBM Plex Sans Arabic\", system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 600
    lineHeight: 1.5
  compact-title:
    fontFamily: "Inter, \"IBM Plex Sans Arabic\", system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 600
    lineHeight: 1.65
  body:
    fontFamily: "Inter, \"IBM Plex Sans Arabic\", system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.65
  label:
    fontFamily: "Inter, \"IBM Plex Sans Arabic\", system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 500
    lineHeight: 1.65
  small:
    fontFamily: "Inter, \"IBM Plex Sans Arabic\", system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.65
  caption:
    fontFamily: "Inter, \"IBM Plex Sans Arabic\", system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 400
    lineHeight: 1.65
  code:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
  metric:
    fontFamily: "Inter, \"IBM Plex Sans Arabic\", system-ui, sans-serif"
    fontSize: "30px"
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: "-.03em"
rounded:
  "badge": "6px"
  "compact": "8px"
  "control": "10px"
  "surface": "14px"
  "round": "50%"
spacing:
  "4": "4px"
  "8": "8px"
  "10": "10px"
  "12": "12px"
  "16": "16px"
  "18": "18px"
  "20": "20px"
  "24": "24px"
  "32": "32px"
  "48": "48px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-text}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  button-secondary-hover:
    backgroundColor: "{colors.surface-alt}"
  button-danger:
    backgroundColor: "{colors.danger}"
    textColor: "{colors.danger-text}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
  nav-item:
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
  nav-item-active:
    backgroundColor: "{colors.primary-bg}"
    textColor: "{colors.primary}"
  settings-nav:
    textColor: "{colors.primary}"
    rounded: "{rounded.control}"
    padding: "9px 14px"
  settings-nav-active:
    backgroundColor: "{colors.primary-bg}"
    textColor: "{colors.primary}"
  badge-neutral:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.text}"
    rounded: "{rounded.badge}"
    padding: "2px 8px"
  badge-success:
    backgroundColor: "{colors.success-bg}"
    textColor: "{colors.success}"
    rounded: "{rounded.badge}"
    padding: "2px 8px"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.surface}"
    padding: "24px"
  stat:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.surface}"
    padding: "20px"
---

<!-- Recorded from `repeater/web/static/app.css`, `repeater/web/templates/base.html`, the shared macros, and implemented route templates. The frontmatter records the default light theme. The sidecar preserves dark-theme CSS, interaction details, and renderable component specimens. PRODUCT.md owns product truth; route strategy lives in the surface brief. -->

# Design System: Repeater

## Overview

**Creative North Star: "The Magic Lap Publishing Workspace"**

Repeater belongs to the user-approved Sentinel Eye product family: a white navigation rail, a pale slate canvas, crisp blue actions, and compact bilingual typography. The visual character is calm, precise, and practical. Thin borders and a faint ambient shadow keep each task region legible without making the chrome the center of attention.

The system gives Arabic and English equal weight. Direction follows the document language, the navigation rail follows logical start, and content names can resolve their own text direction. Repeater retains its own name, publishing content, and truthful workspace states within the shared Magic Lap identity.

**Key Characteristics:**

- Pale canvas, white surfaces, blue actions, and semantic state colors.
- Compact hierarchy with quiet borders and gently rounded controls.
- Arabic and English typography with direction-aware layout.
- Native forms and disclosures with progressive enhancement.

## Colors

The palette is restrained: one blue action family over slate neutrals, with semantic colors reserved for operational meaning. Frontmatter names correspond to the CSS custom-property names; the stylesheet remains the implementation source of truth.

### Primary

- **Magic Lap Blue** (`primary`): main actions, links, selected navigation, and supporting line icons.
- **Action Blue** (`primary-hover`): primary-button hover; also the light-theme Facebook identifier.
- **Blue Wash** (`primary-bg`): selected navigation, setup progress, and workspace initials.
- **On Action** (`primary-text`): text and icons on the filled primary action.

### Neutral

- **Pale Slate Canvas** (`bg`): the application ground and quiet neutral badges.
- **White Surface** (`surface`): navigation, utility bar, cards, and form controls.
- **Quiet Surface** (`surface-alt`): subtle hover, table headers, and secondary fills.
- **Ink Slate** (`text`): headings and working content.
- **Reading Slate** (`muted`): supporting copy, inactive navigation, metadata, and labels.
- **Quiet Slate** (`subtle`): tertiary brand and workspace information and input placeholders.
- **Soft Divider** (`border`): surface edges and row separators.
- **Control Edge** (`border-strong`): fields, secondary buttons, and stronger attention boundaries.

Semantic pairs complete the system: **Danger** (`danger`, `danger-bg`, `danger-text`) for errors and destructive actions; **Success** (`success`, `success-bg`) for ready, published, and active states; **Warning** (`warning`, `warning-bg`) for duplicate and warning states; **Information** (`info`, `info-bg`) for queued and processing states. TikTok and Instagram have their own identifiers (`pf-tiktok`, `pf-instagram`); YouTube, Facebook, and Google Drive use the corresponding existing danger, action-blue, and success hues. Platform color is an identifier, not publication status.

The light theme is the default. An explicit utility action selects the alternate dark theme, which changes semantic custom properties while preserving every component's role. The complete dark override is recorded in the sidecar rather than approximated as a second visual identity.

**The State Has Words Rule.** Every operational color appears with a readable status label or explanatory text; color never carries the state alone.

## Typography

**Body Font:** Inter, followed by IBM Plex Sans Arabic, then system-ui and sans-serif. Both families are self-hosted in `repeater/web/static/fonts`; keep their OFL license files with the assets. Latin text resolves to Inter and Arabic glyphs resolve to IBM Plex Sans Arabic. The supplied Arabic faces cover regular, medium, semibold, and bold weights.

**Character:** compact and clear, with semibold headings, medium navigation and actions, and regular body copy. This is an observed hierarchy rather than a mathematical modular scale.

### Hierarchy

- **Headline** (`headline`): page headings, with restrained negative tracking.
- **Title** (`title`): section and card headings.
- **Compact title** (`compact-title`): smaller subheadings and empty-state titles.
- **Body** (`body`): working copy and form content.
- **Label** (`label`): primary navigation. Field labels inherit the body size with medium weight.
- **Small** (`small`): supporting copy and compact actions. Status and platform badges use a smaller annotation size (11px), with medium weight (500).
- **Caption** (`caption`): brand, workspace, utility-account, and footer metadata; also narrow-screen metric labels. Badges use this size with medium weight.
- **Code** (`code`): native code and preformatted diagnostics; it inherits the surrounding size. Content-caption previews explicitly retain the body face.
- **Metric** (`metric`): summary values, with tabular numerals. On narrow screens the value size reduces (26px).

Empty-state explanations have a readable measure (48ch). Legal pages increase line-height (1.85) inside a narrower reading container. Larger login-story type belongs to that surface and is not a general dashboard heading.

**The Bilingual Type Rule.** Keep the same hierarchy in both languages, preserve the Arabic font assets, and let mixed-direction names and diagnostics resolve their own direction.

## Layout

The authenticated desktop shell uses a fixed rail at logical start (240px) and a sticky utility bar with a minimum height (56px). The content body offsets by the rail width. The main container is fluid with a maximum width (1344px), inline gutters (24px), and main block padding (32px above, 48px below). The footer follows the content through a flex column.

The spacing vocabulary is compact. Small gaps organize icons and actions; larger gaps separate task groups. Standard cards use the recorded card padding and a block-end separation (20px). Two-column task grids have a gap (20px); summary cards use a gap (16px). Rows use thin dividers and comfortable block padding rather than separate cards for every item.

At the task-grid breakpoint (700px), common content grids become two columns. Below it, gutters reduce (16px), main block padding reduces (24px above, 32px below), ordinary cards reduce their padding (20px), fields stack, and heading actions share the available width. Summary metrics remain two columns. The login story is hidden so the credential form stays foremost.

At the rail breakpoint (1000px), the fixed rail and workspace footer appear, summary metrics become four columns, and the account email appears in the utility bar. Below it, the rail becomes a document-flow header with a native navigation disclosure and a two-column navigation grid. JavaScript starts the disclosure closed on these screens; native disclosure remains usable when JavaScript is absent.

Account-connection tiles expand from one column to two at the task-grid breakpoint, then four at the wide breakpoint (1400px). Data tables scroll inside their own wrappers. The public shell has its own utility-bar minimum height (72px; 64px below 700px); it does not reserve rail space.

Access-management screens reuse the same shell and cards. Invitation, password setup, recovery, and issued-link forms use a centered card capped at an inline size (620px), with block margins (32px; 16px at 600px and below). Compact invitation, customer-creation, and recovery forms embedded in operational cards cap their inline size (580px). Longer help copy uses a readable measure (72ch). Access lists wrap their content and actions; at 600px and below each row's action form takes the full width.

**The Logical Start Rule.** Use logical inline properties and start-aligned content so the rail and reading order follow Arabic RTL and English LTR without separate screen compositions.

## Elevation & Depth

Depth is a hybrid of borders, tonal layering, and one faint ambient shadow. White surfaces sit above the pale canvas; thin edges define cards and rows. Cards and metrics share the shadow token (`--shadow`); navigation and utility chrome rely on borders instead of extra elevation.

The light ambient shadow is `0 1px 3px rgb(15 23 42 / 6%)`; the dark counterpart is `0 1px 3px rgb(0 0 0 / 12%)`. The sidecar records both values. The focus indicator is an interaction boundary, not decorative depth: a solid primary outline (3px) with an offset (3px).

**The Quiet Surface Rule.** Extend the existing thin borders, tonal fills, and ambient shadow when adding task regions; keep surface depth subordinate to the content.

## Shapes

Surfaces have gently rounded corners (`surface`), while fields, actions, navigation items, fieldsets, and notices share the smaller control corner (`control`). Badges use the tighter badge corner (`badge`). Compact previews and tabs use the compact corner (`compact`). Avatars and numbered setup markers are circular (`round`). Ordinary surface and control boundaries remain thin (1px).

Icons are inline SVG line drawings with rounded caps and joins and a shared stroke (1.7). Standard sizes follow the existing small interface range (16–24px); empty states use a larger icon (32px). Decorative icons are hidden from assistive technology; icon-only actions retain accessible names.

## Components

### Buttons

Filled primary actions are confident and compact. They use the primary and on-action roles, medium weight (500), the control corner, and a minimum hit height (44px). Hover uses action blue. Secondary actions use the surface fill, ink text, and stronger control edge; hover uses quiet surface. Destructive actions use the danger pair. Disabled buttons reduce opacity (.55) and use the unavailable cursor.

Compact actions use smaller type (12px) and padding (8px 12px), with a desktop minimum height (36px); below 700px they return to the touch height (44px). All keyboard-focusable actions use the shared visible outline. The implementation has no animated button transitions or lift effect.

### Inputs / Fields

Text fields, native search and date fields, selects, and textareas use the surface fill, stronger control edge, control corner, full available width, and a minimum height (44px). Labels remain visible above fields with a small gap (7px). Placeholders use quiet slate. Focus uses the shared outline. Textareas resize vertically; checkbox and radio controls retain their native behavior with the primary accent. File upload uses a dashed edge on a quiet surface.

Checkbox labels place the native control beside its text, including within stacked forms. Groups wrap as space narrows; longer option names wrap within the text area. Selectable list rows can reuse blue wash to reinforce the checked state while keeping the native checkbox visible.

Validation messages use explanatory alert content. The current shared styles do not define a separate invalid-field border token; do not infer one from the error badge.

Access forms retain these native field styles. Email addresses, access codes, and copied links read LTR inside either document direction; workspace names resolve their own direction. Password guidance sits directly below the first password field, and legal acceptance uses the shared native checkbox row. Issued private links appear in a labeled, readonly multiline field with a copy action and a status message; the field remains manually selectable when clipboard enhancement is unavailable.

### Navigation

Navigation items have medium text (13px), a generous hit height (44px), a line icon, and a rounded rectangular selection. Default text is muted; hover uses a quiet surface and ink text; the current page uses blue wash, primary text, and a small trailing dot. Set `aria-current="page"` on the selected destination.

The mobile disclosure is native `details` and `summary`. The enhancement adapts it at the rail breakpoint; Escape closes it and returns focus to the summary. Its reveal animation lasts (160ms) with easing (`cubic-bezier(.16,1,.3,1)`) only when reduced motion is not requested. Keep navigation and form submissions usable without JavaScript.

Workspace settings and members share a wrapping text subnavigation below the page title. It uses the control corner, compact padding, and blue-wash current-page treatment with semibold text. Native links carry `aria-current="page"`; this local navigation does not add a second rail.

### Cards / Containers

Cards use the surface corner, soft edge, ambient shadow, and standard padding. Inside a card, separate repeating records with dividers rather than inventing another level of elevation. Metric cards share that grammar with smaller padding, a muted label, a primary line icon, and a tabular value.

Member, customer, and invitation records use the same flat-list grammar: wrapping rows with moderate gaps (16px), block padding (16px), and a soft divider removed from the last row. Identity and explanatory metadata stay together; row actions wrap alongside them. Customer-owner invitation forms use native disclosures inside these records. Each recovery request occupies a separate task card.

### Status and platform labels

Status badges are compact rounded labels with medium annotation type (11px), tight padding (2px 8px), and a neutral border. Semantic variants replace that border with a transparent edge and use the matching text and tint pair. Ready, published, active, and the stored `info` badge variant use success; scheduled, uploading, processing, and importing use information; failed, needs-attention, error, and reconnect use danger; warning and duplicate use warning. Skipped, canceled, archived, and disabled remain muted.

Platform labels identify a connected service with its platform hue over the canvas fill. Preserve translated text labels alongside color.

### Notices and setup guidance

Banners and flashes use the control corner and compact padding (12px 16px). A paused-workspace banner has a warning tint, clock icon, explanation, and a settings link. Errors use alert semantics; ordinary flashes use status semantics.

Numbered setup rows are flat, separated by a soft divider, and combine a circular marker, task link, explanatory line, and text status. Completion changes the marker to the success pair. On mobile, the status moves below the explanation so the task label keeps its width.

### Utility actions and theme

Language, theme, and logout are explicit utility actions. The theme control submits a CSRF-protected server form and returns to the current path. Its cosmetic cookie is HttpOnly, SameSite=Lax, and Secure in production. Preserve its accessible action name and the server-rendered theme; an operating-system preference does not override the default light theme.

## Do's and Don'ts

### Do:

- **Do** inherit the existing semantic custom properties so new components work in both themes.
- **Do** keep the logical-start rail and the same information hierarchy in Arabic and English.
- **Do** pair status color with translated text and preserve accessible names for icon actions.
- **Do** reuse surface, control, badge, and icon grammar before adding a new component variant.
- **Do** preserve native links, forms, disclosures, and visible keyboard focus.

### Don't:

- **Don't** restore the replaced purple horizontal navigation; the approved family uses the blue action system and logical-start rail.
- **Don't** replace the self-hosted Inter and IBM Plex Sans Arabic pairing when extending this product family.
- **Don't** use a platform identifier color as the only indication of publication status.
- **Don't** give new cards stronger shadows or arbitrary corner shapes that compete with the existing task hierarchy.
- **Don't** require JavaScript to navigate, submit a form, or choose the theme.
