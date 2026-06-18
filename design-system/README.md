# AI Companion — Design System

**Premium dark / glass.** Frosted translucent panels over an ambient indigo→violet→cyan
backdrop, an accent gradient (`#8b8cff → #c062ea`) reserved for brand and primary actions,
soft glow on interactive accents, and a refined light theme as the alternate.

This folder is the local component library that mirrors the live app (`ui.html`). Each
file is a standalone preview that renders as a card in the claude.ai Design System pane
(via the first-line `<!-- @dsCard group="…" -->` marker). Keep it in sync one component at
a time with `/design-sync` — never wholesale-replace.

## Cards

| File | Group | What it shows |
|------|-------|----------------|
| `foundations/tokens.html` | Foundations | Color, type scale, radius, glow |
| `components/buttons.html` | Components | Primary / secondary / ghost / danger / icon, chips |
| `components/cards-stats.html` | Components | Stat cards + quick-command grid |
| `components/inputs-toggles.html` | Components | Text/select/search inputs, gradient toggles |
| `components/nav-rail.html` | Navigation | 64 px frosted app rail + status dot |
| `components/chat.html` | Components | Chat dock: user gradient bubbles + bot thread |
| `components/player.html` | Components | Track list + now-playing bar |
| `components/badges-menu.html` | Components | Automation job badges + context menu |

## Tokens (canonical — dark)

```
--bg            #06070c
--surface       rgba(30,33,48,.50)   glass, backdrop-filter: saturate(150%) blur(18px)
--border        rgba(255,255,255,.08)
--text          #eceefb   --text-2 #a8b0c9   --muted #6f7796
--accent        #8b8cff
--accent-grad   linear-gradient(135deg, #8b8cff, #c062ea)
--glow          0 10px 34px rgba(139,140,255,.42)
--good #4ade80  --warn #fbbf24  --danger #fb7185
--radius 14px
```

The same tokens live in `ui.html` under `:root` (light) and `[data-theme="dark"]` (this set).
