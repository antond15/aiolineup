---
name: AIOLineup
description: A dark, dense, status-first admin dashboard for one self-hosting owner, on a phone or a desktop.
colors:
  ground: "#0f1012"
  panel: "#17181b"
  raised: "#1f2024"
  raised-hover: "#26272c"
  border: "#2a2b30"
  border-strong: "#3a3b42"
  control-edge: "#6b6c75"
  text: "#e7e7ea"
  muted: "#9a9ba3"
  accent: "#7c8cff"
  accent-hover: "#8d9bff"
  accent-ink: "#0f1012"
  accent-selected-card: "#1d2033"
  sheet-backdrop: "rgb(0 0 0 / .6)"
  ok: "#4cc38a"
  warn: "#e5a63c"
  err: "#f0686b"
typography:
  title:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "22px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  section:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "13px"
    fontWeight: 600
    lineHeight: 1.45
    letterSpacing: "0.06em"
  health:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "15px"
    fontWeight: 550
    lineHeight: 1.45
  body:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.45
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.45
rounded:
  xs: "4px"
  sm: "8px"
  md: "10px"
  login: "14px"
  pill: "999px"
spacing:
  xs: "6px"
  sm: "10px"
  md: "16px"
  lg: "20px"
  xl: "28px"
  nav-width: "208px"
  tabbar-height: "60px"
  tap-desktop: "36px"
  tap-phone: "44px"
components:
  nav-link:
    textColor: "{colors.muted}"
    rounded: "{rounded.sm}"
    height: "38px"
    padding: "0 10px"
  nav-link-active:
    backgroundColor: "rgb(124 140 255 / .12)"
    textColor: "{colors.accent}"
  panel:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.md}"
    padding: "16px 18px"
  button:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "0 14px"
    height: "{spacing.tap-desktop}"
  button-hover:
    backgroundColor: "{colors.raised-hover}"
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    rounded: "{rounded.sm}"
    padding: "0 14px"
    height: "{spacing.tap-desktop}"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  button-danger:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.err}"
    rounded: "{rounded.sm}"
  input:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "{spacing.tap-desktop}"
  segmented:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.muted}"
    rounded: "{rounded.sm}"
  segmented-on:
    backgroundColor: "rgb(124 140 255 / .12)"
    textColor: "{colors.accent}"
  chip:
    backgroundColor: "rgb(124 140 255 / .12)"
    textColor: "{colors.text}"
    rounded: "{rounded.pill}"
    height: "30px"
    padding: "0 6px 0 10px"
  chip-source:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.muted}"
    rounded: "{rounded.pill}"
    height: "30px"
    padding: "0 6px 0 10px"
  tag:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.muted}"
    rounded: "5px"
    padding: "1px 7px"
  health-ok:
    backgroundColor: "rgb(76 195 138 / .1)"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "14px 16px"
  health-bad:
    backgroundColor: "rgb(240 104 107 / .1)"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
  card-row:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.md}"
    padding: "12px"
  card-row-selected:
    backgroundColor: "{colors.accent-selected-card}"
  lineup-health:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "14px 16px"
  lineup-open:
    textColor: "{colors.text}"
    rounded: "0px"
    padding: "8px 14px"
    height: "52px"
  lineup-row-selected:
    backgroundColor: "{colors.accent-selected-card}"
  channel-panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "14px"
    width: "360px"
  channel-sheet:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.md}"
    padding: "16px"
  sheet-backdrop:
    backgroundColor: "{colors.sheet-backdrop}"
  number-field:
    backgroundColor: "{colors.ground}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "0 4px"
    height: "{spacing.tap-desktop}"
    width: "56px"
  stream-row:
    textColor: "{colors.text}"
    padding: "8px 0"
  stream-row-off:
    textColor: "{colors.muted}"
  stream-divider:
    backgroundColor: "rgb(229 166 60 / .1)"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "8px 10px"
  candidate:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "8px 10px"
  toast:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "10px 16px"
  sign-in:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.login}"
    padding: "28px"
---

# Design System: AIOLineup

## Overview

**Creative North Star: "The Status Board"**

A control surface that answers one question before any other: are the feeds healthy, and if not, what do I fix? It is the category-standard dark dashboard of self-hosted media tools (Sonarr, Radarr, Jellyfin), played straight and executed carefully rather than reinvented. The admin visits in short sessions, on a 390 px phone or a desktop browser, so every task has to work equally well on both and nothing is desktop-only.

The system is dark only. A near-black ground carries two raised neutral layers; hairline borders do the separating; a single indigo accent marks what you can act on and where you are. Green, amber and red appear only as state, and never alone: each is paired with a drawn icon and a word. Density is moderate and list-driven. Tables on desktop become stacked cards on a phone, not a squeezed table.

**Key Characteristics:**
- Dark only (`color-scheme: dark`); there is no light theme and none is to be added.
- One indigo accent, reserved for primary actions, the active nav item, selection and focus.
- State is icon plus word plus tint, never color alone.
- System sans, tabular figures for numbers, monospace only for URLs, ids and data.
- Flat and tonal: depth comes from surface steps and hairlines; shadows exist only on floating layers.
- One breakpoint at 760 px switches the whole shell between sidebar and bottom tab bar.

## Colors

A near-black neutral stack with one cool indigo and three state colors. All neutrals are untinted-to-slightly-cool grays; nothing is pure black or white.

### Primary
- **Signal Indigo** (`#7c8cff`): primary buttons, the active nav item (text and 12% tint), selected rows and segmented options, chip tint and border, focus rings, checked checkboxes, links. Hover lifts to **Signal Indigo Lit** (`#8d9bff`). Text on a solid indigo fill is **Ground Ink** (`#0f1012`).

### Neutral
- **Ground** (`#0f1012`): page background, input fill, segmented-control track, expanded detail rows.
- **Panel** (`#17181b`): sidebar, tab bar, panels, health line at rest, guide-coverage panel, phone cards, the channel panel and sheet, sign-in card.
- **Raised** (`#1f2024`): buttons at rest, tags, logo plates, guide candidate cards, toast. Hover step is **Raised Hover** (`#26272c`).
- **Hairline** (`#2a2b30`): every panel, row and table border. **Hairline Strong** (`#3a3b42`) for hovered controls and floating layers.
- **Control Edge** (`#6b6c75`): the border of inputs and checkboxes, so a field boundary meets 3:1 against the ground.
- **Soft Text** (`#e7e7ea`): body and headings. **Muted Text** (`#9a9ba3`): labels, hints, inactive nav, secondary lines.
- **Selected Card** (`#1d2033`): the opaque fill of a selected row once it becomes a phone card, and of the open My channels row (with a 45% indigo inset border).
- **Sheet Backdrop** (`rgb(0 0 0 / .6)`): the scrim behind the modal channel sheet below 1100 px. The one translucent black in the system; it dims the page and never tints a surface.

### State
- **Healthy Green** (`#4cc38a`), **Caution Amber** (`#e5a63c`), **Fault Red** (`#f0686b`). Used for the icon and word of a state, and at 10% as the tint of a health line or banner with a 35 to 40% border of the same hue.

### Named Rules
**The One Indigo Rule.** Indigo means "you can act here" or "this is selected or current". It never decorates, and never marks state.

**The Never Alone Rule.** Green, amber and red always travel with a drawn icon and a word ("Guide", "No guide", "OK · 2 min ago"). A color-only dot is a defect. One compact exception: the Sources Status column shows the icon alone (check, alert or minus, so the shape still carries the state) and keeps its words in a **status tooltip** (focusable, `role=img` named with the full text, opens on hover, focus or tap, closes on Esc, blur or scroll; Raised fill, Hairline Strong border, 8px radius, toast shadow, 12px text with a muted second line, fixed-positioned beside the icon so a scrolling table cannot clip it).

**The Tint, Not Bar Rule.** Selection and state are shown with a full tint and a full border or a leading icon, never a one-sided stripe on a rounded box.

## Typography

**Font:** the platform system stack, `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`. It suits an operate-mode tool and is the whole voice, with no separate display face.
**Mono:** `ui-monospace, SFMono-Regular, Menlo, monospace`, only for feed URLs, `tvg-id`s, stream URLs and other data.

**Character:** plain and factual, with hierarchy from size, weight and the muted-versus-soft color step rather than from typefaces. Numbers use `font-variant-numeric: tabular-nums` so columns and counts align.

### Hierarchy
- **Title** (650, 22px, 1.2, -0.01em; 20px on phone): the page title, one per view. Sign-in title is 20px.
- **Health** (550 to 600, 15px; 14px on phone): the one health sentence on Overview.
- **Section heading** (600, 13px, muted, uppercase, 0.06em): the `h2` that groups an Overview or Settings block (for example "Feed URLs for AIOStreams"). It is a real heading with its own content below, set above 28px and below 10px.
- **Body** (400, 14px, 1.45; 15px on phone): table cells, forms, descriptions. Channel names are 550.
- **Label** (600, 12px, muted): table headers.
- **Small and hint** (400, 12px, muted): hints, notes, secondary status text, `.chid` ids.
- **Mono** (400, 12px): URLs and ids, wrapped with `overflow-wrap: anywhere` when long.
- **Tab label** (11px): bottom tab bar only.

### Named Rules
**The Phone Input Rule.** Inputs and selects are 16px on phone so the browser does not zoom on focus.

**The Real Heading Rule.** The uppercase, tracked style belongs to the `h2` section heading only. Do not reuse it as a small label above another heading or on a stat.

## Layout

Two shells split at **760 px**. At 760 px and wider: a fixed 208 px left sidebar (brand, five icon-and-label links in the order Overview, My channels, Add channels, Sources, Settings, job status pinned at the bottom) and a main column capped at 1180 px with 28/32/48 px padding. Below 760 px: the sidebar becomes a fixed 60 px bottom tab bar (plus the safe-area inset) of five equal icon-over-label tabs, the brand hides, main gets 18/16 px padding with room beneath for the bar, and the job status becomes a sticky strip shown only while a job is running.

A secondary step at 1100 px collapses the three-up stat panels to one column and the add-source form grid to two columns, and hides the lowest-priority table columns (`hide-md`). On My channels the same step turns the channel panel from a 360 px sticky side column into a modal bottom sheet, and stacks each list row's tag and state under its name.

Spacing uses the values the build actually reuses: 6, 8, 10, 12, 14, 16, 18, 20, 28. Page header to content is 20px; panel padding is 16 by 18; section headings sit 28px above and 10px below. The Overview reads top to bottom: page title, health line, optional banners, feed URLs, grouped output numbers. Settings is a single column capped at 640px.

**Touch targets.** On a phone, or on any coarse pointer, `--tap` is 44 px and every control, chip, small button and chip-remove button meets it. On a fine-pointer desktop the standard control is 36 px.

### Named Rules
**The Cards Not Squeeze Rule.** A table that does not fit 390 px becomes a stack of cards below 760 px, with labels from `data-label`. A table is never shrunk, scrolled sideways, or clipped to fit a phone.

**The One Breakpoint Rule.** The shell changes only at 760 px. New views adapt inside that split and do not add a third shell.

## Elevation & Depth

Flat and tonal. Depth is three surface steps (ground, panel, raised) separated by 1px hairlines. Nothing at rest casts a shadow. Shadows are limited to floating layers: the channel panel and sheet (`0 10px 28px rgb(0 0 0 / .5)`) and the toast (`0 8px 24px rgb(0 0 0 / .5)`), both offset, soft, and black. The sheet additionally dims the page with the Sheet Backdrop scrim.

There is no motion beyond instant state changes: the build declares no transitions or animations, hover and checked states switch directly.

### Named Rules
**The Flat At Rest Rule.** A surface that is not floating over other content has no shadow. Group with a panel and hairline instead.

## Shapes

Softly rounded, never pill-shaped except chips. Panels, health lines, banners and phone cards use 10px; buttons, inputs, segmented controls, candidate cards and the toast use 8px; the channel panel uses 10px, rounded on the top corners only as a sheet; My channels rows are square, divided by hairlines inside their panel; checkboxes 6px; tags 5px; logo plates 4px; the sign-in card 14px; chips and the active tab-bar indicator are full pills. Borders are 1px hairlines on every container. Icons are drawn line icons in one set (nav and row icons include `i-catalog` for Add channels and `i-up` for moving a stream up; moving down reuses `i-chevron`): 24px viewBox, 1.75 stroke, round caps and joins, `currentColor`, rendered at 18px (16px inside state pairs, 22px for health and tab bar, 14px inside chips).

## Components

### Navigation (sidebar and tab bar)
- **Sidebar:** 208px, Panel fill, hairline on its right edge. Links are 38px tall, 8px radius, icon at 18px with the label at 10px gap. Rest is Muted Text; hover is Raised fill with Soft Text; **active** is the 12% indigo tint, indigo text and 600 weight, with `aria-current="page"`.
- **Job status:** pinned to the sidebar foot. An 8px dot (hairline-strong when idle, indigo when busy) plus a word ("Idle" or the job name), in a `role=status` region.
- **Tab bar (phone):** five equal tabs, icon (22px) over an 11px label. Active state is the indigo icon inside a pill-shaped tint behind the icon, not a filled tab.

### Health line
The memorable moment of the system: one sentence, on Overview, that is either calm or specific. **OK:** green-tinted panel, check icon, "Feeds up to date · refreshed 2 min ago", Refresh all beside it. **Empty:** neutral panel, minus icon, plain instruction, primary "Add a source". **Failing:** red-tinted panel, alert icon and "N sources failing" header, then one row per failure with the source name, the exact error in red, a muted "Serving data from …" line, and a "Fix in Sources" button linking to that source's row. Banners (`warn`, `err`) reuse the same tint, border and leading icon for secondary notices.

### Panels and stat groups
A Panel is Panel fill, 1px hairline, 10px radius. Output numbers are grouped panels holding a heading and rows of muted term and right-aligned, tabular, 600-weight value, separated by hairlines. They are not a wall of equal tiles; group by meaning, and use at most three per row.

### Buttons
- **Default:** Raised fill, hairline border, 8px radius, 0 14px padding, icon plus label at 7px gap, 36px tall (44px on phone and coarse pointers). Hover steps to Raised Hover and Hairline Strong.
- **Primary:** solid indigo with Ground Ink text, 600 weight. One per view region; the main action only.
- **Danger:** default button with Fault Red text; red border on hover. Destructive actions confirm first.
- **Icon-only:** a square default (or danger) button at the tap size with an 18px icon, `aria-label` naming the action and its object ("Delete iptv-org CZ") and a `title` with the verb. Used for repeated row actions (Sources: refresh, edit, trash); on phone cards they keep the equal-width three-column grid.
- **Quiet and link:** no fill or border; muted or indigo text, for secondary or inline actions. **Small** is 30px tall with 12px text (44px on phone).
- **Disabled:** 50% opacity. **Focus:** a 2px indigo outline at 2px offset on every focusable element (offset -2px inside segmented controls and My channels rows).

### Inputs, checkboxes
Ground fill, 1px Control Edge border, 8px radius, 0 12px padding, same height as buttons. Search has a leading 18px muted icon. Selects draw their own 16px muted chevron 12px from the right edge (36px right padding) instead of the native arrow. Checkboxes are custom 20px squares (24px on phone cards) with a Control Edge border, filling indigo with a Ground Ink check when on. Labels sit above fields at weight 500 with a 12px muted hint beneath.

### Segmented control
A joined group on a Ground track with a hairline border and 8px radius; segments are quiet buttons divided by hairlines. The selected segment takes the indigo tint and indigo 600 text and carries `aria-pressed`. On phone it spans the full width with equal segments, inside the collapsible filter panel opened by a Filters toggle (`aria-expanded`).

### Chips
Active-filter chips: pill, 12% indigo fill, 35% indigo border, Soft Text, with a remove button (x icon, labelled "Remove filter …") inside the right edge. 30px on desktop, 44px on phone. **Tags** are small 5px-radius Raised labels in muted text for categories and languages; a `miss` tag is amber-bordered. Every category tag is sent to the playlist, so they all look alike; an admin's own list is followed by a borderless muted `edited` word. **Category chips** (channel panel, Overrides section): the **category picker** is one input-looking box (Ground fill, Control Edge border, 8px radius, 5/6px padding, 6px gap, the focus outline on the whole box when the field inside has focus) holding a `ul` of chips in the channel's order and, after them, a borderless text field (datalist of existing categories) that wraps to its own line when the chips fill the row. Category chips are 8px-radius (as inputs, not pills) with a 2px gap between label and ×; the admin's own list uses the active-filter fill and border, and an unedited channel shows its source categories as **source chips** (Raised fill, Hairline Strong border, muted text). Each chip has a remove button labelled "Remove category X"; the last chip has none. A "Use source" link button (12px, accent) sits right of the "Categories" label while the list is the admin's own. "Only the first 8 are sent." (muted 12px) shows under the box when a source list is longer. The field is disabled at 8. Nothing saves by itself: Enter or a picked suggestion adds at the end, × or Backspace in the empty field removes, "Use source" resets; all only stage until the Overrides Save. Focus moves to the chip now in that place, else the field.

### Tables that become cards
Desktop: a Panel holding a full-width table, 12px muted headers, 9px/12px cell padding, hairline row dividers, a faint white 2% row hover and indigo-tint selected rows. A row has no expandable details row; extra detail lives in the channel panel (below). Phone: header hidden, each row becomes a Panel card (10px radius, 12px padding, 10px gap) with the cell's `data-label` shown as a 12px muted caption; selected cards use Selected Card fill with a 45% indigo border. Add channels cards put checkbox, logo and name on top with every tag (country, language, categories) in one uncaptioned wrapped line under the name, then stream count and guide state on one line. A selected channel with a number carries a muted 12px tabular "Number N" line under its name. Source cards put name, a one-line ellipsized URL and the enabled toggle on top, then Priority, Status, Items and In use as a four-up grid (caption over value) with Size below, then a three-up action row. My channels is not a table; it is a numbered list (below). An empty state is a single panel with centered muted text. A page's first load shows the same panel reading "Loading…" (static, no spinner). A source no selected channel uses takes the Warn tint on its row (amber border as a card) and a warn state pair "0" in In use.

### State pairs (icon plus word)
The single status pattern: a 16px drawn icon and a word, colored together. `ok` (check), `warn` (alert triangle), `err` (alert triangle) and `none` (minus in a circle, muted). Examples: "Guide", "No guide", "OK · 2 min ago", "Never fetched", "No guide data found", "Channel missing upstream". Long error text wraps with the icon top-aligned. The title attribute carries the longer detail.

### My channels list
The My channels page answers "what is in my lineup, in what order, and which channels still need a guide?" Order: banners, guide-coverage panel, filter bar, then the list with its channel panel beside it.
- **Guide-coverage panel:** a Panel, 10px radius, 14/16 padding, with one 15px 550-weight sentence, "N of M matched", over a muted 12px meta line (programmes, size, built time, and a link to the guide count) separated by 16px gaps with tabular figures. It is neutral, not green or red; failures arrive as banners above it.
- **Filter bar:** the segmented control with a live count in each segment (All, Needs guide, Missing), beside a search field that also matches a channel number. It stays visible on phone, full width with equal segments.
- **Rows:** one Panel holds the list, sorted by number, hairline between rows, a faint white 3% hover. Each row is a two-column grid: a **number field** (Ground fill, 56px wide on desktop, 52px on phone, centered, tabular figures, `aria-label` "Number of NAME", 14px from the left edge) beside a full-width **open button** (52px minimum, 8/14 padding) laid out by grid areas as logo (40px plate), name (550) over a muted mono id line, the first category tag plus a `+N` tag (tabular figures, a title listing the rest, which screen readers get as hidden text), one state pair (120px column, right-aligned) and a muted chevron. Below 1100 px the tags and state stack under the name and the chevron hides. A channel with no logo shows a 40 by 28px Raised plate. The open row takes Selected Card fill with a 45% indigo inset border and `aria-expanded`. A channel no longer in the playlist still opens its panel, which says it is missing and holds only Channel number, Name and logo, and Remove.

### Channel panel
Opens from a row and holds one channel: its name in a 16px 650 heading with the id beneath and a close button at the tap size (36px, 44px on phone). Below the heading come hairline-separated sections, each a 10px-gap grid under a hairline with 14px top padding and a 13px 600 `h3`: **Channel number** (a 100px wide number field and its own Save, no hint), **Overrides** (Name, Logo URL and Categories as the category picker, see Chips; the inputs have no hints and their placeholders are the bare source values; one primary Save under Categories saves all three together, with a muted 12px "Unsaved changes" status beside it while anything differs from the saved values; clearing Name or Logo and saving returns to the source; the logo must be https), **Streams**, and **Guide** (a segmented switch Automatic / Chosen / Off that follows the saved choice. Automatic shows "Matched by the playlist id" with the match, or "No guide lists …" with a Similar names list of Use cards. Chosen shows the pick and a search prefilled with the channel name, 2 or more characters, 250ms debounce, Enter searches and only "Use as id" saves a typed id. Off shows nothing more. Automatic and Off save on click; Chosen only opens the search and Use saves. While a rebuild is due a muted "Applies after the rebuild" hint shows, and the row's state is a muted "Rebuilding" with a 12px spinner, which stops under reduced motion). A footer after a hairline holds a danger "Remove channel" button with a confirm. Closing the panel or opening another channel while Overrides are unsaved asks "Discard unsaved changes to NAME?"; Cancel keeps the panel and the edits. Saving toasts and keeps the panel open; removing closes it and returns focus to the list. It is built from native buttons, not an ARIA combobox.
- **Side panel (1100 px and wider):** a 360px column beside the list, sticky 20px from the top, Panel fill, Hairline Strong border, 10px radius, 14px padding, floating shadow, scrolling inside a viewport-high maximum. It takes focus when opened; Esc closes it.
- **Bottom sheet (below 1100 px):** the same panel fixed to the bottom edge, full width, top corners 10px, 85% maximum height (85dvh), 16px padding plus the safe-area inset, over the Sheet Backdrop. It is `role=dialog` with `aria-modal` and `aria-labelledby` the heading, closes on Esc, the close button or a backdrop tap, locks page scroll, and returns focus to the row. Focus that leaves it is pulled back in by a document `focusin` handler (the modal trap). Its mode is fixed when it opens; crossing 1100 px closes it.

### Stream rows
Inside the Streams section, one row per stream, separated by hairlines (the first has none), 8px vertical padding, a three-column grid: an on/off checkbox (its `aria-label` says On or Off with the stream name; no visible word), an info block, and a joined pair of icon-only buttons. The Streams hint is the muted 12px "Sent in this order." (a direct `.hint` of a panel section is styled like a field hint). The info block holds the source name (550) over a muted mono URL on one line, ellipsized, with the full URL in `title`. An off stream mutes its info. **Up** (`i-up`) and **Down** (the existing `i-chevron`) form one joined pair (shared hairline, outer 8px corners), named "Move NAME up" and "Move NAME down", disabled at the ends of the list; each keeps the tap size (44px on phone). A stream whose URL repeats an earlier one shows a `none` state pair, "Same URL as an earlier stream, ignored by AIOStreams". After the eighth distinct enabled stream a **stream divider** is drawn: a note with the Caution Amber tint at 10%, a 40% amber border, 8px radius, 8/10 padding, a 16px alert icon and 12px text saying that AIOStreams uses only the first 8 streams. On phone the row drops to two columns and the move buttons sit under the info.

### Candidate card
A guide channel offered for a mapping, used for suggestions, search results and the current choice. Raised fill, hairline border, 8px radius, 8/10 padding, a two-column grid. Left: guide channel name (550), a muted mono `id · source` line, and a muted 12px now line, `HH:MM–HH:MM Title` in the viewer's locale (or "No programmes"). Right: a standard-size **Use** button with an accessible name naming both channels ("Use X for Y"); the first suggestion's Use is primary, the rest default. The current choice has no button and sits under "Your choice" or "Current guide".

### Backup panel
Settings holds a "Backup" `h2` and a Panel (16/18 padding, 10px gap) between the settings form and Environment: one sentence, a muted 12px hint ("The file contains your source URLs, including any logins in them. Keep it private."), and two default buttons, **Export backup** (an `a.btn` download, `i-download`) and **Import backup…** (`i-upload`, opens the file picker). A chosen file is checked by the server, then a review opens under a hairline inside the panel: a 600-weight "Backup from <date>" line, the counts as `envlist` rows (muted term, tabular 600 value), the muted replace warning, a danger **Replace with backup** and a default Cancel (this review is the confirmation, so no second prompt). Focus moves to Replace; errors (a refused file, a busy worker) show as an `err` banner in the panel and a refused file shows no buttons.

### Toast
One transient message, centered at the bottom (above the tab bar on phone), Raised fill, Hairline Strong border, 8px radius, floating shadow, `role=status`, shown 2.5s (4s for errors). Copy is short and factual ("Copied", "Refresh queued").

### Sign-in
A single centered 360px Panel card with a 14px radius, 28px padding and a 14px gap, over the Ground, 14vh from the top. Title, one muted line, two labelled fields, a primary "Sign in" button, and an inline red `role=alert` error region beneath that reserves its height.

## Do's and Don'ts

### Do:
- **Do** open every view with the page title and, where status matters, a specific health or alert line before any data.
- **Do** pair every green, amber or red with a drawn icon and a word.
- **Do** keep indigo (`#7c8cff`) for primary actions, the active nav item, selection and focus only.
- **Do** write every new table so it becomes a stack of labelled cards below 760 px, with 44 px controls.
- **Do** build icons from `icons.svg` symbols at one stroke (1.75, round) and add new ones in the same style.
- **Do** put upstream text through `textContent` and wrap long URLs and ids with `overflow-wrap: anywhere`.
- **Do** give every control a visible 2px indigo focus ring and an accessible name.

### Don't:
- **Don't** add a light theme, a theme toggle, or `prefers-color-scheme` branches.
- **Don't** rely on color alone, a bare dot, or a one-sided colored stripe to signal state or selection.
- **Don't** introduce a second accent, gradients, glass or blur, or shadows on non-floating surfaces.
- **Don't** return to a flat wall of equal stat tiles, or squeeze a desktop table onto a phone.
- **Don't** add a web font, an icon font, or emoji or glyph characters as icons.
- **Don't** put a small caps or tracked label above a heading, or reuse the section-heading style as a decorative label.
- **Don't** add a third shell or a breakpoint other than 760 px (and the 1100 px column step).
